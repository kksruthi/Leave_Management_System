"""Module 3 — the Accrual Engine.

Turns the policy number from Module 2 into dated rows in `leave_ledger`.

    monthly  (EL)        -> entitlement / 12, written every run
    annual_lump (CL, SL) -> full entitlement, written once per cycle
    none     (Unpaid)    -> never accrues

The dynamism is NOT in this module. There is deliberately no anniversary
check, no bracket-crossing logic, no special-casing anywhere below. Every run
calls `resolve_policy(..., as_of_date=run_date)` fresh, and simply writes down
whatever it says. When Priya crosses from the 0-1yr bracket into 1-3yr, the
policy engine returns 18 instead of 15 and this code — unchanged, unaware —
starts writing 1.5 instead of 1.25.

Both jobs are pure batch functions. They take a session and a run date, write
rows, and return a report. No scheduler coupling, no HTTP.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Employee, EmployeeException, LeaveLedger, OrgPolicy
from app.policy_engine import ResolvedPolicy, resolve_policy, years_between
from app.proration import accrual_cycle, eligible_days, prorate_entitlement, round_days

log = logging.getLogger("leave_engine.accrual")

__all__ = [
    "run_monthly_accrual",
    "run_annual_grant",
    "AccrualResult",
    "LedgerEntry",
    "SkippedEntry",
    "REASON_MONTHLY",
    "REASON_ANNUAL",
    "REASON_TRUE_UP",
    "REASON_CARRYOVER_IN",
    "REASON_CARRYOVER_LAPSE",
    "run_year_end_carryover",
    "ACCRUAL_DP",
]

# Ledger amounts are stored to three decimals. Two would be easier to read but
# 10/12 -> 0.83 loses 0.04 days a year, every year, for every US employee.
# Three decimals plus the year-end true-up below makes twelve runs sum to the
# annual entitlement exactly.
ACCRUAL_DP = Decimal("0.001")

REASON_MONTHLY = "monthly accrual"
REASON_ANNUAL = "annual grant"
REASON_TRUE_UP = "annual true-up"


def _round(amount: Decimal, rounding_dp: int | None = None) -> Decimal:
    """Round to the POLICY's decimal places, falling back to the default.

    Rounding used to be a constant in this module. It is now carried on each
    `org_policies` row (`rounding_dp`), so a region whose payroll insists on
    2dp says so in data rather than in a code change. See app/proration.py
    for the shared implementation.
    """
    if rounding_dp is None:
        return Decimal(amount).quantize(ACCRUAL_DP, rounding=ROUND_HALF_UP)
    return round_days(amount, rounding_dp)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LedgerEntry:
    """One row this run wrote."""

    employee_id: int
    employee_name: str
    leave_type_id: str
    amount: Decimal
    reason: str
    effective_date: dt.date
    policy_snapshot_id: int | None
    ledger_id: int | None = None


@dataclass(frozen=True)
class SkippedEntry:
    """One (employee, leave type) pair this run did NOT write, and why."""

    employee_id: int
    employee_name: str
    leave_type_id: str
    reason: str


@dataclass
class AccrualResult:
    """What a batch run did. Returned rather than printed, so callers decide."""

    run_date: dt.date
    job: str
    written: list[LedgerEntry] = field(default_factory=list)
    skipped: list[SkippedEntry] = field(default_factory=list)

    @property
    def total_days(self) -> Decimal:
        return sum((e.amount for e in self.written), Decimal("0"))

    @property
    def true_ups(self) -> list[LedgerEntry]:
        return [e for e in self.written if e.reason == REASON_TRUE_UP]

    def summary(self) -> str:
        return (
            f"{self.job} for {self.run_date}: {len(self.written)} entries "
            f"(+{self.total_days} days), {len(self.skipped)} skipped"
        )

    def __str__(self) -> str:  # pragma: no cover
        return self.summary()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _coerce_date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


def active_employees(session: Session, run_date: dt.date) -> list[Employee]:
    """Everyone eligible to accrue on this run.

    Eligibility is **any day of service in the run month**, not service on the
    run date itself. Two reasons that distinction matters:

      * A joiner who starts on the 15th would otherwise miss that month
        entirely, because the job runs on the 1st. They should earn a
        pro-rated share of it instead — see `_monthly_amount`.
      * A leaver who exits on the 10th should still earn their final part
        month, for the same reason at the other end.

    Ineligible means: not started yet (a pre-boarded hire must not bank leave
    before their first day), or already gone before the month began (a leaver
    must not accrue forever — which is exactly what happened before the
    employee table grew an exit date).

    Note `status` is deliberately NOT filtered here. It is a *current* flag,
    while accrual is *point-in-time*: a back-dated re-run of last March must
    still pay someone who has since resigned. The `exit_date` is what bounds
    service, and Module 1's CHECK guarantees a terminated employee has one.
    """
    month_start, month_end = _month_bounds(run_date)
    return [
        employee
        for employee in session.scalars(
            select(Employee).where(Employee.join_date <= month_end).order_by(Employee.id)
        )
        if eligible_days(employee, month_start, month_end) > 0
    ]


def candidate_leave_types(session: Session, employee: Employee) -> list[str]:
    """Leave types worth resolving for this employee.

    Union of the types configured for their region and any type they hold a
    personal exception for — an exception may cover a type the region does not
    otherwise offer. The accrual METHOD is not filtered here; that comes from
    the resolved policy, so this module never second-guesses Module 2.
    """
    regional = session.scalars(
        select(OrgPolicy.leave_type_id).where(OrgPolicy.region == employee.region).distinct()
    ).all()
    personal = session.scalars(
        select(EmployeeException.leave_type_id)
        .where(EmployeeException.employee_id == employee.id)
        .distinct()
    ).all()
    return sorted(set(regional) | set(personal))


def _already_written(
    session: Session, employee_id: int, leave_type_id: str, effective_date: dt.date, reason: str
) -> bool:
    """Idempotency guard.

    Re-running a job for a date that already has an entry must not credit the
    balance twice. A double-click on the demo endpoint, or a re-run after a
    partial failure, is safe.
    """
    existing = session.scalar(
        select(LeaveLedger.id).where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.effective_date == effective_date,
            LeaveLedger.reason == reason,
        )
    )
    return existing is not None


def _annual_already_granted(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    cycle_start: dt.date,
    cycle_end: dt.date,
) -> bool:
    """Has this leave year's lump already been granted, on any date?

    The annual entitlement is a once-per-leave-year event. Keying the guard on
    the run date would make it once-per-run-date, which is a different and
    much weaker promise.
    """
    return session.scalar(
        select(LeaveLedger.id).where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.reason == REASON_ANNUAL,
            LeaveLedger.effective_date >= cycle_start,
            LeaveLedger.effective_date <= cycle_end,
        ).limit(1)
    ) is not None


def _write(
    session: Session,
    employee: Employee,
    policy: ResolvedPolicy,
    amount: Decimal,
    reason: str,
    effective_date: dt.date,
    result: AccrualResult,
    expires_on: dt.date | None = None,
) -> None:
    if amount == 0:
        result.skipped.append(SkippedEntry(
            employee.id, employee.name, policy.leave_type_id,
            "resolved entitlement is zero — nothing to accrue",
        ))
        return

    if _already_written(session, employee.id, policy.leave_type_id, effective_date, reason):
        log.info("skip %s/%s on %s: already written (%s)",
                 employee.name, policy.leave_type_id, effective_date, reason)
        result.skipped.append(SkippedEntry(
            employee.id, employee.name, policy.leave_type_id,
            f"already has a '{reason}' entry for {effective_date}",
        ))
        return

    row = LeaveLedger(
        employee_id=employee.id,
        leave_type_id=policy.leave_type_id,
        amount=amount,
        reason=reason,
        effective_date=effective_date,
        policy_snapshot_id=policy.policy_snapshot_id,
        expires_on=expires_on,
    )
    session.add(row)
    session.flush()

    result.written.append(LedgerEntry(
        employee_id=employee.id,
        employee_name=employee.name,
        leave_type_id=policy.leave_type_id,
        amount=amount,
        reason=reason,
        effective_date=effective_date,
        policy_snapshot_id=policy.policy_snapshot_id,
        ledger_id=row.id,
    ))


def _resolve_or_skip(
    session: Session, employee: Employee, leave_type_id: str,
    run_date: dt.date, result: AccrualResult,
) -> ResolvedPolicy | None:
    policy = resolve_policy(session, employee, leave_type_id, run_date)
    if policy is None:
        log.warning(
            "skip %s (%s) / %s on %s: no policy resolved",
            employee.name, employee.region, leave_type_id, run_date,
        )
        result.skipped.append(SkippedEntry(
            employee.id, employee.name, leave_type_id,
            f"resolve_policy returned no match for region {employee.region!r} on {run_date}",
        ))
    return policy


# ---------------------------------------------------------------------------
# Anniversary-cycle helpers (used only by the true-up)
# ---------------------------------------------------------------------------
def _month_bounds(run_date: dt.date) -> tuple[dt.date, dt.date]:
    """First and last day of the calendar month containing `run_date`."""
    first = run_date.replace(day=1)
    last = calendar.monthrange(run_date.year, run_date.month)[1]
    return first, run_date.replace(day=last)


def _monthly_amount(
    employee: Employee, policy: ResolvedPolicy, run_date: dt.date
) -> Decimal:
    """One month's accrual, pro-rated across a partial first or last month.

    A full month is simply entitlement / 12. A month the employee only partly
    worked — because they joined on the 15th, or left on the 20th — earns the
    same share of that month's accrual as the days they were actually in
    service. Someone who joins on the 15th of a 30-day month earns half.

    Controlled by the policy's `proration_method`: `none` disables this and
    pays the full month regardless.
    """
    monthly = policy.monthly_accrual
    if policy.proration_method == "none":
        return monthly

    month_start, month_end = _month_bounds(run_date)
    month_days = (month_end - month_start).days + 1
    served = eligible_days(employee, month_start, month_end)

    if served >= month_days or served == 0:
        return monthly
    return _round(monthly * Decimal(served) / Decimal(month_days), policy.rounding_dp)


def _anniversary(join_date: dt.date, year_offset: int) -> dt.date:
    try:
        return join_date.replace(year=join_date.year + year_offset)
    except ValueError:  # 29 Feb into a non-leap year
        return join_date.replace(year=join_date.year + year_offset, day=28)


def cycle_bounds(join_date: dt.date, run_date: dt.date) -> tuple[dt.date, dt.date]:
    """The accrual year containing `run_date`, as [start, end_exclusive).

    Cycles run anniversary to anniversary, matching how tenure brackets move,
    so an employee's entitlement is constant across a whole cycle.
    """
    n = years_between(join_date, run_date)
    return _anniversary(join_date, n), _anniversary(join_date, n + 1)


def _is_final_month_of_cycle(employee: Employee, run_date: dt.date) -> bool:
    """Is this the last accrual run of the employee's current cycle?

    Normally the month before the next anniversary. For a leaver it is the
    month they exit, which may be much earlier — their final accrual still
    needs truing up against their pro-rated entitlement.
    """
    _, cycle_end = cycle_bounds(employee.join_date, run_date)
    last_day = cycle_end - dt.timedelta(days=1)
    if employee.exit_date is not None:
        last_day = min(last_day, employee.exit_date)
    return (run_date.year, run_date.month) == (last_day.year, last_day.month)


def _eligible_month_count(employee: Employee, cycle_start: dt.date, cycle_end: dt.date) -> int:
    """How many monthly runs this employee should have in the cycle.

    12 for a full year; fewer for a joiner or leaver. This is what the
    completeness check counts against, so a partial cycle can still be trued
    up — while a cycle the SYSTEM simply hasn't finished running is not.
    """
    months = 0
    year, month = cycle_start.year, cycle_start.month
    while (year, month) <= (cycle_end.year, cycle_end.month):
        first = dt.date(year, month, 1)
        last = dt.date(year, month, calendar.monthrange(year, month)[1])
        if eligible_days(employee, first, last) > 0:
            months += 1
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _maybe_true_up(
    session: Session, employee: Employee, policy: ResolvedPolicy,
    run_date: dt.date, result: AccrualResult,
) -> None:
    """Close the rounding gap on the last accrual of a complete cycle.

    12 x 0.833 = 9.996, not 10. Rather than let that drift compound year over
    year, the final run of each anniversary cycle writes a correction so the
    cycle sums to the entitlement exactly.

    The target is the employee's PRO-RATED entitlement for the cycle, so a
    joiner or leaver is trued up against what they actually earned, not
    against a full year they were never entitled to.

    Only fires once every ELIGIBLE month of the cycle has an entry. A cycle
    the system simply hasn't finished running is left alone, because that gap
    is genuine under-accrual rather than rounding, and topping it up silently
    would be wrong. Counting eligible months rather than a flat 12 is what
    lets a partial cycle still be trued up correctly.
    """
    if not _is_final_month_of_cycle(employee, run_date):
        return

    cycle_start, cycle_end = cycle_bounds(employee.join_date, run_date)
    cycle_last_day = cycle_end - dt.timedelta(days=1)

    monthly_rows = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == employee.id,
            LeaveLedger.leave_type_id == policy.leave_type_id,
            LeaveLedger.reason == REASON_MONTHLY,
            LeaveLedger.effective_date >= cycle_start,
            LeaveLedger.effective_date < cycle_end,
        )
    ).all()

    expected = _eligible_month_count(employee, cycle_start, cycle_last_day)
    if expected == 0 or len(monthly_rows) != expected:
        log.info(
            "no true-up for %s/%s cycle %s: %d of %d eligible monthly entries present",
            employee.name, policy.leave_type_id, cycle_start,
            len(monthly_rows), expected,
        )
        return

    proration = prorate_entitlement(
        session, employee, policy.leave_type_id, cycle_start, cycle_last_day
    )
    accrued = sum((Decimal(r.amount) for r in monthly_rows), Decimal("0"))
    gap = _round(proration.prorated_days - accrued, policy.rounding_dp)
    if gap == 0:
        return

    _write(session, employee, policy, gap, REASON_TRUE_UP, run_date, result)


# ---------------------------------------------------------------------------
# 1. Monthly accrual job
# ---------------------------------------------------------------------------
def run_monthly_accrual(
    session: Session,
    run_date: dt.date | str,
    *,
    commit: bool = True,
    true_up: bool = True,
) -> AccrualResult:
    """Write one month of accrual for every employee with a `monthly` policy.

    Args:
        session: open SQLAlchemy session.
        run_date: the effective date for this batch (date or ISO string).
        commit: commit at the end. Set False to inspect inside a transaction.
        true_up: write the rounding correction on the final month of a
            complete anniversary cycle.

    Returns:
        An `AccrualResult` listing every row written and every pair skipped.
        A missing policy skips that pair and logs a warning — it never aborts
        the batch, because one bad region should not stop payroll for everyone.
    """
    run_date = _coerce_date(run_date)
    result = AccrualResult(run_date=run_date, job="monthly accrual")

    for employee in active_employees(session, run_date):
        for leave_type_id in candidate_leave_types(session, employee):
            policy = _resolve_or_skip(session, employee, leave_type_id, run_date, result)
            if policy is None:
                continue
            if policy.accrual_method != "monthly":
                continue  # annual_lump / none are not this job's business

            amount = _monthly_amount(employee, policy, run_date)
            _write(session, employee, policy, amount, REASON_MONTHLY, run_date, result)

            if true_up:
                _maybe_true_up(session, employee, policy, run_date, result)

    if commit:
        session.commit()
    log.info(result.summary())
    return result


# ---------------------------------------------------------------------------
# 2. Annual lump job
# ---------------------------------------------------------------------------
def run_annual_grant(
    session: Session,
    run_date: dt.date | str,
    *,
    commit: bool = True,
) -> AccrualResult:
    """Grant the full annual entitlement for every `annual_lump` leave type.

    No pro-ration for mid-cycle joiners — that is explicitly STRETCH in the
    build phases, and is not implemented here. A joiner mid-cycle currently
    receives the full lump.
    """
    run_date = _coerce_date(run_date)
    result = AccrualResult(run_date=run_date, job="annual grant")

    for employee in active_employees(session, run_date):
        for leave_type_id in candidate_leave_types(session, employee):
            policy = _resolve_or_skip(session, employee, leave_type_id, run_date, result)
            if policy is None:
                continue
            if policy.accrual_method != "annual_lump":
                continue

            # Pro-rated across the cycle the grant covers, so a mid-year joiner
            # or leaver receives their share rather than the whole year's lump.
            cycle_start, cycle_end = accrual_cycle(employee, policy, run_date)
            proration = prorate_entitlement(
                session, employee, leave_type_id, cycle_start, cycle_end,
            )
            amount = proration.prorated_days

            if proration.is_partial:
                log.info(
                    "pro-rated %s for %s: %s of %s days eligible -> %s (was %s)",
                    leave_type_id, employee.name, proration.eligible_days,
                    proration.cycle_days, amount, proration.full_year_entitlement,
                )

            # Idempotency for the annual lump is per CYCLE, not per date.
            # `_write`'s guard keys on the effective date, which is right for
            # a monthly accrual (one entry per month) but wrong here: running
            # the job on 1 April and again on 18 August is the same leave
            # year, and granting twice is how a demo database ends up showing
            # 30 days of sick leave against a 10-day entitlement.
            if _annual_already_granted(
                session, employee.id, leave_type_id, cycle_start, cycle_end
            ):
                result.skipped.append(SkippedEntry(
                    employee.id, employee.name, leave_type_id,
                    f"already granted for the cycle {cycle_start}–{cycle_end}",
                ))
                continue

            # Date the grant at the START OF THE LEAVE YEAR IT COVERS, not at
            # the date the job happened to run. An annual lump *is* that
            # year's entitlement, so it must fall inside that year's window —
            # otherwise a job run on 1 January credits an India employee
            # (leave year 1 Apr – 31 Mar) outside the year the days belong to,
            # and CL and SL read as zero even though they were granted.
            # Clamped to the join date so a mid-year joiner is never credited
            # before their first day.
            granted_on = max(cycle_start, employee.join_date)
            _write(session, employee, policy, amount, REASON_ANNUAL, granted_on, result)

    if commit:
        session.commit()
    log.info(result.summary())
    return result


# ---------------------------------------------------------------------------
# 3. Year-end carry-over (completes findings 9 and 14)
# ---------------------------------------------------------------------------
REASON_CARRYOVER_IN = "carry-over brought forward"
REASON_CARRYOVER_OUT = "carry-over moved to next year"
REASON_CARRYOVER_LAPSE = "carry-over forfeited"


#: Organisation-wide ceiling on carry-over, regardless of what an individual
#: policy row allows. A policy may be stricter (a 5-day cap still caps at 5);
#: it may not be more generous.
CARRYOVER_CEILING_DAYS = Decimal("18")

#: Carried days expire this many months after the new leave year begins.
CARRYOVER_EXPIRY_MONTHS = 3


def _carryover_expiry_date(policy: ResolvedPolicy, new_year_start: dt.date) -> dt.date:
    """Three months after the new leave year starts — the day before, inclusive.

    Carried over on 2026-04-01, expires 2026-06-30. The policy's own
    `carryover_expiry` ("MM-DD") is no longer consulted: the rule is now a
    fixed window measured from the start of the new year, so it lands
    correctly whether that year begins in April (India) or January (Texas)
    without every policy row having to state it.
    """
    month = new_year_start.month - 1 + CARRYOVER_EXPIRY_MONTHS
    year = new_year_start.year + month // 12
    month = month % 12 + 1
    day = min(new_year_start.day, calendar.monthrange(year, month)[1])
    # The window is [start, start + 3 months), so the last usable day is the
    # day before the quarter mark.
    return dt.date(year, month, day) - dt.timedelta(days=1)


def _carryover_year_end(
    employee: Employee, policy: ResolvedPolicy, run_date: dt.date
) -> dt.date:
    """The date this leave type's carry-over is assessed on.

    Accrual for a monthly type is anniversary-aligned — each employee earns
    from their own join date, so `leave_year_end` is deliberately NULL on
    those policy rows. Carry-over is a different question: it is an
    ORGANISATIONAL year-end event ("what is left on 31 March"), and the
    example the rule was written against — 15 days carried on 1 April 2026,
    expiring 30 June — only makes sense on the organisation's calendar.

    So when a policy states no leave year of its own, the region's is used.
    """
    if getattr(policy, "leave_year_end", None):
        return accrual_cycle(employee, policy, run_date)[1]

    from app.seed import REGION_LEAVE_YEAR_END

    stated = REGION_LEAVE_YEAR_END.get(employee.region)
    if not stated:
        return accrual_cycle(employee, policy, run_date)[1]

    month, day = (int(part) for part in stated.split("-"))
    end_this_year = dt.date(run_date.year, month, min(day, calendar.monthrange(run_date.year, month)[1]))
    if run_date <= end_this_year:
        return end_this_year
    nxt = run_date.year + 1
    return dt.date(nxt, month, min(day, calendar.monthrange(nxt, month)[1]))


def run_year_end_carryover(
    session: Session,
    run_date: dt.date | str,
    *,
    commit: bool = True,
) -> AccrualResult:
    """Close a leave year: carry forward what policy allows, forfeit the rest.

    `carryover_max_days` and `carryover_expiry` were policy settings nothing
    ever acted on — the dashboard could enforce an expiry date, but no row
    ever carried one. This is the job that produces them.

    For each employee and leave type whose leave year ends on `run_date`:

      * balance up to `carryover_max_days` moves into the new year as a
        `carryover` row stamped with the expiry the policy sets;
      * anything above the cap is written off explicitly, so the loss appears
        in the history instead of as a silent gap;
      * everything is appended — nothing is edited, per the append-only rule.

    Run on the last day of the leave year. Idempotent like the other jobs.
    """
    run_date = _coerce_date(run_date)
    result = AccrualResult(run_date=run_date, job="year-end carry-over")

    for employee in active_employees(session, run_date):
        for leave_type_id in candidate_leave_types(session, employee):
            policy = _resolve_or_skip(session, employee, leave_type_id, run_date, result)
            if policy is None or policy.accrual_method == "none":
                continue

            cycle_end = _carryover_year_end(employee, policy, run_date)
            if run_date != cycle_end:
                continue  # not this leave type's year end

            balance = Decimal(session.scalar(
                select(func.coalesce(func.sum(LeaveLedger.amount), 0)).where(
                    LeaveLedger.employee_id == employee.id,
                    LeaveLedger.leave_type_id == leave_type_id,
                    LeaveLedger.effective_date <= run_date,
                    or_(LeaveLedger.expires_on.is_(None),
                        LeaveLedger.expires_on >= run_date),
                )
            ))
            if balance <= 0:
                continue

            # The policy's own cap still applies, but never above the
            # organisation-wide 18-day ceiling.
            cap = min(Decimal(policy.carryover_max_days or 0), CARRYOVER_CEILING_DAYS)
            carried = _round(min(balance, cap), policy.rounding_dp)
            forfeited = _round(balance - carried, policy.rounding_dp)
            new_year_start = cycle_end + dt.timedelta(days=1)

            # These two rows CLOSE the outgoing year: they zero out what was
            # not carried. They expire with that year, so they can never leak
            # into a later window — which matters when an employee transfers
            # between regions whose leave years start on different dates, and
            # a 31 March closing entry would otherwise sit inside a Texas
            # January-to-December window whose opening credits it never saw.
            if forfeited > 0:
                _write(session, employee, policy, -forfeited,
                       REASON_CARRYOVER_LAPSE, run_date, result, expires_on=run_date)

            if carried > 0:
                _write(session, employee, policy, -carried,
                       REASON_CARRYOVER_OUT, run_date, result, expires_on=run_date)
                if _already_written(session, employee.id, leave_type_id,
                                    new_year_start, REASON_CARRYOVER_IN):
                    continue
                row = LeaveLedger(
                    employee_id=employee.id,
                    leave_type_id=leave_type_id,
                    amount=carried,
                    reason=REASON_CARRYOVER_IN,
                    effective_date=new_year_start,
                    policy_snapshot_id=policy.policy_snapshot_id,
                    bucket="carryover",
                    expires_on=_carryover_expiry_date(policy, new_year_start),
                )
                session.add(row)
                session.flush()
                result.written.append(LedgerEntry(
                    employee_id=employee.id, employee_name=employee.name,
                    leave_type_id=leave_type_id, amount=carried,
                    reason=REASON_CARRYOVER_IN, effective_date=new_year_start,
                    policy_snapshot_id=policy.policy_snapshot_id, ledger_id=row.id,
                ))

    if commit:
        session.commit()
    log.info(result.summary())
    return result
