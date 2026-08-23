"""Module 4 — Live Balance & Dashboard.

Answers "what does the employee actually see". Two public functions:

    get_live_balance(session, employee_id, leave_type_id, as_of_date)
    get_dashboard(session, employee, as_of_date)

**This module never writes.** Module 3 owns every `leave_ledger` insert; this
one only reads.

## The no-cache rule

`get_live_balance()` is a `SUM` over the ledger, computed on every single call.
There is deliberately:

  * no balance column on `employee`,
  * no cache, memo, or `lru_cache` anywhere in this file,
  * no snapshot table and no scheduled rollup job.

That is a correctness requirement, not a performance preference. The ledger is
the single source of truth; the moment a balance is stored somewhere else,
there are two truths and they will diverge. If this ever becomes slow, the
answer is the index Module 1 already ships
(`ix_leave_ledger_balance` on employee_id, leave_type_id, effective_date) —
not a cached number.

Entitlements are re-resolved through Module 2 on every call for the same
reason, so a dashboard can never show a stale bracket.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.accrual import REASON_ANNUAL, REASON_MONTHLY, candidate_leave_types, cycle_bounds
from app.authorization import require_view_employee
from app.models import ApprovalStep, Employee, LeaveLedger, LeaveRequest
from app.policy_engine import ResolvedPolicy, resolve_policy, years_between
from app.util import format_days

__all__ = [
    "get_live_balance",
    "get_dashboard",
    "get_balances",
    "get_balance_buckets",
    "get_days_taken",
    "get_expiring_soon",
    "get_lapsed",
    "EmployeeNotFoundError",
    "Dashboard",
    "LeaveTypeView",
    "LedgerLine",
    "NextAccrual",
    "PendingRequest",
]


def _coerce_date(value: dt.date | str | None) -> dt.date:
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


# ===========================================================================
# 1. Live balance
# ===========================================================================
class EmployeeNotFoundError(LookupError):
    """No employee with that id. Distinct from a zero balance."""


def leave_year_start(employee: Employee, as_of_date: dt.date) -> dt.date:
    """First day of the leave year `as_of_date` falls in, for this region.

    India runs 1 Apr – 31 Mar, Texas 1 Jan – 31 Dec.
    """
    from app.seed import REGION_LEAVE_YEAR_END

    stated = REGION_LEAVE_YEAR_END.get(employee.region) or "12-31"
    month, day = (int(part) for part in stated.split("-"))
    end_this_year = dt.date(as_of_date.year, month, day)
    end = end_this_year if as_of_date <= end_this_year else dt.date(
        as_of_date.year + 1, month, day
    )
    return dt.date(end.year - 1, month, day) + dt.timedelta(days=1)


def _in_scope(employee: Employee, as_of_date: dt.date):
    """The rows that count toward a *spendable* balance today.

    This is the fix for "Fatima has 50 days of Earned Leave against a 30-day
    entitlement". The ledger is append-only and goes back years, so summing
    all of it answers "how much has this person ever been credited", not "how
    much can they take". Those are different numbers and only the second one
    belongs on a dashboard.

    Two kinds of row count:

      * **current** — credits and deductions dated inside THIS leave year.
        Last year's accrual is history; the only part of it that survives is
        whatever the year-end job actually carried forward.
      * **carryover** — the days the year-end job moved into this year, still
        inside their expiry window. These are dated at the start of the new
        leave year, so they pass the same window anyway; the expiry filter is
        what removes them once they lapse.

    Anything older is deliberately excluded. If it should have survived, the
    carry-over job's job was to say so — and it did, with a cap and an expiry.
    """
    start = leave_year_start(employee, as_of_date)
    return and_(
        LeaveLedger.effective_date >= start,
        LeaveLedger.effective_date <= as_of_date,
    )


def _expiry_filter(as_of_date: dt.date):
    """Carry-over credits stop counting once they expire (finding 9).

    `expires_on` NULL means the credit never expires, which is every row
    written before carry-over existed and every current-year accrual.
    """
    return or_(LeaveLedger.expires_on.is_(None), LeaveLedger.expires_on >= as_of_date)


def get_live_balance(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
    *,
    viewer: Employee | None = None,
    include_expired: bool = False,
    strict: bool = True,
) -> Decimal:
    """Sum the ledger. Computed fresh every call — never cached.

    Entries dated after `as_of_date` are excluded, so this doubles as a
    point-in-time balance ("what did I have on 15 Sep?") and not just a
    current one. Carry-over credits whose `expires_on` has passed are
    excluded too, so a stored carry-over rule is actually *enforced* rather
    than merely recorded.

    Args:
        viewer: who is asking. `None` means a trusted internal caller
            (an accrual job); anyone else is authorization-checked.
        include_expired: count lapsed carry-over anyway — for reconciliation
            and "you lost 3 days on 31 March" messages, not for spending.
        strict: raise `EmployeeNotFoundError` for an unknown id rather than
            returning 0. An employee who does not exist and an employee with
            no leave are very different facts, and quietly conflating them
            hides typos and broken joins (finding 13).
    """
    as_of_date = _coerce_date(as_of_date)

    employee = session.get(Employee, employee_id)
    if employee is None:
        if strict:
            raise EmployeeNotFoundError(
                f"No employee with id {employee_id}. A missing employee is not a "
                "zero balance — pass strict=False if you genuinely want 0 here."
            )
        return Decimal("0")

    if viewer is not None:
        require_view_employee(session, viewer, employee)

    conditions = [
        LeaveLedger.employee_id == employee_id,
        LeaveLedger.leave_type_id == leave_type_id,
        _in_scope(employee, as_of_date),
    ]
    if not include_expired:
        conditions.append(_expiry_filter(as_of_date))

    total = session.scalar(
        select(func.coalesce(func.sum(LeaveLedger.amount), 0)).where(*conditions)
    )
    return Decimal(total)


def get_days_taken(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
) -> Decimal:
    """Days already spent on approved leave this leave year.

    "12 days left" only means something next to "8 days taken". This counts
    settlement deductions only — accrual, carry-over movements and forfeitures
    are not leave anybody took.
    """
    as_of_date = _coerce_date(as_of_date)
    employee = session.get(Employee, employee_id)
    if employee is None:
        return Decimal("0")
    total = session.scalar(
        select(func.coalesce(func.sum(LeaveLedger.amount), 0)).where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.reason.like("leave taken%"),
            _in_scope(employee, as_of_date),
        )
    )
    return -Decimal(total)


def get_balance_buckets(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
) -> dict[str, Decimal]:
    """Split the balance into current-year and carry-over (finding 14).

    "12 days, of which 3 are carried over and expire on 31 March" is a
    different — and far more useful — statement than "12 days".
    """
    as_of_date = _coerce_date(as_of_date)
    employee = session.get(Employee, employee_id)
    if employee is None:
        return {"current": Decimal("0"), "carryover": Decimal("0")}
    rows = session.execute(
        select(LeaveLedger.bucket, func.coalesce(func.sum(LeaveLedger.amount), 0))
        .where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            _in_scope(employee, as_of_date),
            _expiry_filter(as_of_date),
        )
        .group_by(LeaveLedger.bucket)
    ).all()
    buckets = {"current": Decimal("0"), "carryover": Decimal("0")}
    for bucket, total in rows:
        buckets[bucket] = Decimal(total)
    return buckets


def get_expiring_soon(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
    within_days: int = 90,
) -> list[tuple[dt.date, Decimal]]:
    """Carry-over credits about to lapse, so the dashboard can warn."""
    as_of_date = _coerce_date(as_of_date)
    horizon = as_of_date + dt.timedelta(days=within_days)
    rows = session.execute(
        select(LeaveLedger.expires_on, func.sum(LeaveLedger.amount))
        .where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.effective_date <= as_of_date,
            LeaveLedger.expires_on.is_not(None),
            LeaveLedger.expires_on >= as_of_date,
            LeaveLedger.expires_on <= horizon,
        )
        .group_by(LeaveLedger.expires_on)
        .order_by(LeaveLedger.expires_on)
    ).all()
    return [(when, Decimal(amount)) for when, amount in rows if Decimal(amount) > 0]


def get_lapsed(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
) -> Decimal:
    """Carry-over days already lost to expiry — for the history view."""
    as_of_date = _coerce_date(as_of_date)
    total = session.scalar(
        select(func.coalesce(func.sum(LeaveLedger.amount), 0)).where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.effective_date <= as_of_date,
            LeaveLedger.expires_on.is_not(None),
            LeaveLedger.expires_on < as_of_date,
        )
    )
    return Decimal(total)


def get_balances(
    session: Session,
    employee_id: int,
    as_of_date: dt.date | str | None = None,
) -> dict[str, Decimal]:
    """Every leave type's balance in one query — for callers that only need numbers."""
    as_of_date = _coerce_date(as_of_date)
    rows = session.execute(
        select(LeaveLedger.leave_type_id, func.sum(LeaveLedger.amount))
        .where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.effective_date <= as_of_date,
            _expiry_filter(as_of_date),
        )
        .group_by(LeaveLedger.leave_type_id)
    ).all()
    return {leave_type: Decimal(total) for leave_type, total in rows}


# ===========================================================================
# Payload types
# ===========================================================================
@dataclass(frozen=True)
class LedgerLine:
    """One history row, with the running total as of that entry."""

    effective_date: dt.date
    amount: Decimal
    reason: str
    running_total: Decimal
    policy_snapshot_id: int | None
    ledger_id: int


@dataclass(frozen=True)
class NextAccrual:
    """What the employee will next receive, and when.

    For monthly types this is resolved with `as_of_date` set to the accrual
    date itself, so a tenure-bracket crossing shows up BEFORE it happens —
    "next accrual: +1.5 days" appears on the March dashboard, a month before
    the first 1.5-day entry is written.
    """

    date: dt.date
    amount: Decimal
    entitlement_days_per_year: Decimal
    note: str | None = None


@dataclass(frozen=True)
class PendingRequest:
    """A leave request still working through its approval chain."""

    request_id: int
    leave_type_id: str
    start_date: dt.date
    end_date: dt.date
    duration_days: Decimal
    status: str
    current_tier: int | None
    current_role: str | None
    routing_reason: str | None
    chain: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class LeaveTypeView:
    """Everything the dashboard shows for one leave type."""

    leave_type_id: str
    balance: Decimal
    # The same balance, split by origin (finding 14).
    current_year_balance: Decimal
    carryover_balance: Decimal
    # Carry-over about to lapse, and what has already lapsed (finding 9).
    expiring_soon: list[tuple[dt.date, Decimal]]
    lapsed_days: Decimal
    # Days already committed to pending requests (finding 18).
    pending_days: Decimal
    # Approved leave whose deduction is dated in the future, so it has not hit
    # the balance yet. Committed all the same — the days are spent.
    scheduled_days: Decimal
    available_days: Decimal
    # Days actually taken this leave year — the other half of "how many left".
    taken_days: Decimal
    entitlement_days_per_year: Decimal | None
    is_paid: bool | None
    accrual_method: str | None
    carryover_max_days: Decimal | None
    carryover_expiry: str | None
    max_consecutive_days: int | None
    min_notice_days: int | None
    policy_source: str | None  # "org_policy" | "exception" | None
    policy_found: bool
    next_accrual: NextAccrual | None
    history: list[LedgerLine]
    note: str | None = None


@dataclass(frozen=True)
class Dashboard:
    """The whole payload, for every leave type, in one call."""

    employee_id: int
    name: str
    email: str
    region: str
    join_date: dt.date
    tenure_years: int
    as_of_date: dt.date
    leave_types: list[LeaveTypeView]
    pending_requests: list[PendingRequest]

    def leave_type(self, leave_type_id: str) -> LeaveTypeView | None:
        return next((lt for lt in self.leave_types if lt.leave_type_id == leave_type_id), None)

    def to_dict(self, *, decimals_as_str: bool = True) -> dict:
        """JSON-ready payload.

        Decimals serialise as strings by default — `"1.250"` survives a round
        trip, `1.25` as a float does not. Pass `decimals_as_str=False` if the
        consumer would rather have numbers.
        """

        def convert(value):
            if isinstance(value, Decimal):
                return str(value) if decimals_as_str else float(value)
            if isinstance(value, dt.date):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            return value

        return convert(asdict(self))


# ===========================================================================
# Assembly helpers
# ===========================================================================
def _first_of_next_month(from_date: dt.date) -> dt.date:
    year, month = from_date.year, from_date.month + 1
    if month > 12:
        year, month = year + 1, 1
    return dt.date(year, month, 1)


def _history(
    session: Session, employee_id: int, leave_type_id: str, as_of_date: dt.date
) -> list[LedgerLine]:
    rows = session.scalars(
        select(LeaveLedger)
        .where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.effective_date <= as_of_date,
        )
        .order_by(LeaveLedger.effective_date, LeaveLedger.id)
    ).all()

    lines, running = [], Decimal("0")
    for row in rows:
        running += Decimal(row.amount)
        lines.append(LedgerLine(
            effective_date=row.effective_date,
            amount=Decimal(row.amount),
            reason=row.reason,
            running_total=running,
            policy_snapshot_id=row.policy_snapshot_id,
            ledger_id=row.id,
        ))
    return lines


def _next_accrual(
    session: Session, employee: Employee, leave_type_id: str,
    policy: ResolvedPolicy, as_of_date: dt.date,
) -> NextAccrual | None:
    if policy.accrual_method == "monthly":
        next_date = _first_of_next_month(as_of_date)
        # Re-resolve AT the future date, so a bracket crossing is visible in
        # advance. Same mechanism Module 3's job uses — no duplicated logic.
        future = resolve_policy(session, employee, leave_type_id, next_date)
        if future is None:
            return None
        note = None
        if future.entitlement_days_per_year != policy.entitlement_days_per_year:
            direction = (
                "rises" if future.entitlement_days_per_year > policy.entitlement_days_per_year
                else "changes"
            )
            note = (
                f"Tenure bracket changes on {next_date}: entitlement {direction} from "
                f"{format_days(policy.entitlement_days_per_year)} to "
                f"{format_days(future.entitlement_days_per_year)} days/year."
            )
        return NextAccrual(
            date=next_date,
            amount=future.monthly_accrual,
            entitlement_days_per_year=future.entitlement_days_per_year,
            note=note,
        )

    if policy.accrual_method == "annual_lump":
        # Next grant lands at the start of the next anniversary cycle.
        _, cycle_end = cycle_bounds(employee.join_date, as_of_date)
        future = resolve_policy(session, employee, leave_type_id, cycle_end)
        if future is None:
            return None
        return NextAccrual(
            date=cycle_end,
            amount=future.entitlement_days_per_year,
            entitlement_days_per_year=future.entitlement_days_per_year,
            note="Granted as a lump sum at the start of each leave year.",
        )

    return None  # accrual_method "none" — Unpaid never accrues


def _scheduled_days(
    session: Session, employee_id: int, as_of_date: dt.date
) -> dict[str, Decimal]:
    """Approved leave whose deduction is dated in the future.

    Settlement dates a deduction at the request's START DATE, because that is
    when the leave is actually consumed. A balance is summed to `as_of_date`,
    so an approved November request does not touch an August balance — which
    is right for the ledger and wrong for the screen: the moment HR approved
    it, `pending_days` dropped to zero and the available figure sprang back
    up, telling the employee they had days they have already spent.

    So approved-but-not-yet-effective leave is committed in exactly the same
    way a pending request is. It leaves this bucket and enters the balance on
    its start date, and the total never moves.
    """
    rows = session.execute(
        select(LeaveRequest.leave_type_id, func.coalesce(func.sum(LeaveRequest.paid_days), 0))
        .where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status == "approved",
            LeaveRequest.start_date > as_of_date,
        )
        .group_by(LeaveRequest.leave_type_id)
    ).all()
    return {leave_type: Decimal(total) for leave_type, total in rows}


def _pending_requests(
    session: Session, employee_id: int, as_of_date: dt.date
) -> list[PendingRequest]:
    """Read hook for Modules 5/6.

    Returns [] until `leave_request` has rows — the field exists in the payload
    from day one so the UI never has to change shape later. No request or
    approval LOGIC lives here; this only reads what those modules write.
    """
    # `as_of_date` bounds this list exactly as it bounds the balance
    # (finding 12). A dashboard rendered "as of 15 Sep" must not show a
    # request that was only submitted in November — otherwise the balance and
    # the commitments against it are being read at two different moments.
    requests = session.scalars(
        select(LeaveRequest)
        .where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status == "pending",
            func.date(LeaveRequest.submitted_at) <= as_of_date,
        )
        .order_by(LeaveRequest.start_date)
    ).all()
    if not requests:
        return []

    out = []
    for req in requests:
        steps = session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.request_id == req.id)
            .order_by(ApprovalStep.tier)
        ).all()
        active = next((s for s in steps if s.status == "active"), None)
        out.append(PendingRequest(
            request_id=req.id,
            leave_type_id=req.leave_type_id,
            start_date=req.start_date,
            end_date=req.end_date,
            duration_days=Decimal(req.duration_days),
            status=req.status,
            current_tier=active.tier if active else None,
            current_role=active.role if active else None,
            routing_reason=active.routing_reason if active else None,
            chain=[
                {
                    "tier": s.tier,
                    "role": s.role,
                    "status": s.status,
                    "routing_reason": s.routing_reason,
                }
                for s in steps
            ],
        ))
    return out


# ===========================================================================
# 2. Dashboard assembly
# ===========================================================================
def get_dashboard(
    session: Session,
    employee: Employee | int,
    as_of_date: dt.date | str | None = None,
    *,
    viewer: Employee | None = None,
    strict: bool = True,
) -> Dashboard | None:
    """Assemble the full dashboard payload for every leave type, in one call.

    Args:
        session: open SQLAlchemy session (read-only use).
        employee: an `Employee` instance or an employee id.
        as_of_date: date or ISO string. Defaults to today.

    Returns:
        A `Dashboard`, or None if the employee id is unknown.

    Every entitlement comes from a live `resolve_policy()` call at
    `as_of_date`, and every balance from a live `SUM`. Nothing on this payload
    is stored or cached, so calling it twice a second apart across a ledger
    write returns two different, correct answers.
    """
    as_of_date = _coerce_date(as_of_date)

    if isinstance(employee, int):
        employee_obj = session.get(Employee, employee)
        if employee_obj is None:
            if strict:
                raise EmployeeNotFoundError(
                    f"No employee with id {employee}. Pass strict=False to get None."
                )
            return None
    else:
        employee_obj = employee

    # Finding 10: enforced here, not left to whatever calls this.
    if viewer is not None:
        require_view_employee(session, viewer, employee_obj)

    pending = _pending_requests(session, employee_obj.id, as_of_date)
    pending_by_type: dict[str, Decimal] = {}
    for request in pending:
        pending_by_type[request.leave_type_id] = (
            pending_by_type.get(request.leave_type_id, Decimal("0")) + request.duration_days
        )

    scheduled_by_type = _scheduled_days(session, employee_obj.id, as_of_date)

    views: list[LeaveTypeView] = []
    for leave_type_id in candidate_leave_types(session, employee_obj):
        balance = get_live_balance(session, employee_obj.id, leave_type_id, as_of_date)
        buckets = get_balance_buckets(session, employee_obj.id, leave_type_id, as_of_date)
        expiring = get_expiring_soon(session, employee_obj.id, leave_type_id, as_of_date)
        lapsed = get_lapsed(session, employee_obj.id, leave_type_id, as_of_date)
        awaiting = pending_by_type.get(leave_type_id, Decimal("0"))
        scheduled = scheduled_by_type.get(leave_type_id, Decimal("0"))
        committed = awaiting + scheduled
        history = _history(session, employee_obj.id, leave_type_id, as_of_date)
        taken = get_days_taken(session, employee_obj.id, leave_type_id, as_of_date)
        policy = resolve_policy(session, employee_obj, leave_type_id, as_of_date)

        if policy is None:
            # No policy resolves, but the employee may still hold a balance
            # from a period when one did. Showing the balance with an explicit
            # note beats hiding days the employee has actually earned.
            views.append(LeaveTypeView(
                leave_type_id=leave_type_id,
                balance=balance,
                current_year_balance=buckets["current"],
                carryover_balance=buckets["carryover"],
                expiring_soon=expiring,
                lapsed_days=lapsed,
                pending_days=awaiting,
                scheduled_days=scheduled,
                available_days=balance - committed,
                taken_days=taken,
                entitlement_days_per_year=None,
                is_paid=None,
                accrual_method=None,
                carryover_max_days=None,
                carryover_expiry=None,
                max_consecutive_days=None,
                min_notice_days=None,
                policy_source=None,
                policy_found=False,
                next_accrual=None,
                history=history,
                note=(
                    f"No active policy for {leave_type_id} in {employee_obj.region} "
                    f"on {as_of_date}. Contact HR."
                ),
            ))
            continue

        views.append(LeaveTypeView(
            leave_type_id=leave_type_id,
            balance=balance,
            current_year_balance=buckets["current"],
            carryover_balance=buckets["carryover"],
            expiring_soon=expiring,
            lapsed_days=lapsed,
            pending_days=awaiting,
            scheduled_days=scheduled,
            available_days=balance - committed,
            taken_days=taken,
            entitlement_days_per_year=policy.entitlement_days_per_year,
            is_paid=policy.is_paid,
            accrual_method=policy.accrual_method,
            carryover_max_days=policy.carryover_max_days,
            carryover_expiry=policy.carryover_expiry,
            max_consecutive_days=policy.max_consecutive_days,
            min_notice_days=policy.min_notice_days,
            policy_source=policy.source,
            policy_found=True,
            next_accrual=_next_accrual(
                session, employee_obj, leave_type_id, policy, as_of_date
            ),
            history=history,
            note=policy.explain(),
        ))

    return Dashboard(
        employee_id=employee_obj.id,
        name=employee_obj.name,
        email=employee_obj.email,
        region=employee_obj.region,
        join_date=employee_obj.join_date,
        tenure_years=years_between(employee_obj.join_date, as_of_date),
        as_of_date=as_of_date,
        leave_types=views,
        pending_requests=pending,
    )


# The reason vocabulary a history view will want to label or filter on.
HISTORY_REASONS = (REASON_MONTHLY, REASON_ANNUAL)
