"""Ledger settlement — where an approval finally costs somebody days.

This closes the gap the review named: `record_approval_decision()` changed
`status` to `approved` and stopped. The dashboard computed "after approval" as
`balance - paid`, which is a *projection*, not a fact. Nothing ever wrote it.
So the system could say a request was approved while the balance stayed
exactly where it was.

## What settlement writes

One ledger row **per source leave type**, not one combined row. A 6-day EL
request covered by 3 days of EL and 3 of CL produces two deductions, because
those are two different balances and a single `-6 EL` row would be a lie that
also over-draws EL.

The per-type breakdown comes from the classification stored on the request
(`Classification.sources`) — which is why Module 5 records the draws rather
than only a paid/unpaid total.

## Which split is used, and why

**The split stored at SUBMISSION**, not a fresh one computed now.

That is deliberate. The employee saw a paid/unpaid split when they submitted;
the approver saw the same numbers on the card they signed off. If balances
moved in between, re-deriving the split at approval time would deduct
something neither of them agreed to. `reclassify_request()` exists to show an
approver that the picture has drifted — but the decision they make is on the
figures in front of them.

## Reversal

An approved request that is later cancelled is reversed by APPENDING an
opposite entry, never by deleting the original. The ledger is append-only at
the database level, and "the deduction happened, then it was reversed" is a
truer history than "the deduction never happened".
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Employee, LeaveLedger, LeaveRequest
from app.outbox import TOPIC_LEAVE_DEDUCTED, emit
from app.policy_engine import resolve_policy

log = logging.getLogger("leave_engine.settlement")

__all__ = [
    "settle_approved_request",
    "reverse_settlement",
    "is_settled",
    "settlement_rows",
    "notify_payroll",
    "PayrollNotice",
    "REASON_LEAVE_TAKEN",
    "REASON_LEAVE_REVERSED",
]

REASON_LEAVE_TAKEN = "leave taken"
REASON_LEAVE_REVERSED = "leave reversed"


def _reason(request_id: int, kind: str = REASON_LEAVE_TAKEN) -> str:
    """Ledger reason text that also identifies the request.

    The audit screen finds a request's ledger movement by matching on this,
    so the format is load-bearing rather than decorative.
    """
    return f"{kind} (request {request_id})"


def settlement_rows(session: Session, request_id: int) -> list[LeaveLedger]:
    return list(session.scalars(
        select(LeaveLedger)
        # The closing paren makes this exact: "(request 5)" never matches
        # "(request 55)". The trailing % allows a reversal to append its note.
        .where(LeaveLedger.reason.like(f"%(request {request_id})%"))
        .order_by(LeaveLedger.id)
    ))


def is_settled(session: Session, request_id: int) -> bool:
    return any(
        row.reason.startswith(REASON_LEAVE_TAKEN)
        for row in settlement_rows(session, request_id)
    )


# ---------------------------------------------------------------------------
# Deduction
# ---------------------------------------------------------------------------
def settle_approved_request(
    session: Session,
    request: LeaveRequest | int,
    *,
    effective_date: dt.date | None = None,
    commit: bool = False,
) -> list[LeaveLedger]:
    """Deduct an approved request from the ledger. Idempotent.

    Called by the approval engine the moment the final tier approves, inside
    that same transaction — so status and balance move together or not at all.

    Returns the rows written (empty if it was already settled, or if the whole
    request was unpaid and there is nothing to deduct).
    """
    request_obj = (
        session.get(LeaveRequest, request) if isinstance(request, int) else request
    )
    if request_obj is None:
        raise ValueError(f"No leave request {request!r}.")
    if request_obj.status != "approved":
        raise ValueError(
            f"Request {request_obj.id} is {request_obj.status!r}; only an approved "
            "request can be settled."
        )
    if is_settled(session, request_obj.id):
        log.info("request %s already settled — skipping", request_obj.id)
        return []

    effective_date = effective_date or request_obj.start_date
    employee = session.get(Employee, request_obj.employee_id)
    sources = _sources_for(session, request_obj)

    written: list[LeaveLedger] = []
    for leave_type_id, days in sources:
        if days <= 0:
            continue
        policy = resolve_policy(session, employee, leave_type_id, request_obj.start_date)
        for bucket, part, expires_on in _split_across_buckets(
            session, request_obj.employee_id, leave_type_id, days, effective_date
        ):
            row = LeaveLedger(
                employee_id=request_obj.employee_id,
                leave_type_id=leave_type_id,
                amount=-part,                   # signed: a deduction
                reason=_reason(request_obj.id),
                effective_date=effective_date,
                policy_snapshot_id=policy.policy_snapshot_id if policy else None,
                bucket=bucket,
                expires_on=expires_on,
            )
            session.add(row)
            written.append(row)

    session.flush()

    if written:
        emit(
            session, TOPIC_LEAVE_DEDUCTED, "leave_request", request_obj.id,
            {
                "request_id": request_obj.id,
                "employee_id": request_obj.employee_id,
                "effective_date": effective_date,
                "paid_days": request_obj.paid_days,
                "unpaid_days": request_obj.unpaid_days,
                "deductions": [
                    {"leave_type_id": r.leave_type_id, "days": -Decimal(r.amount)}
                    for r in written
                ],
            },
        )
        log.info(
            "settled request %s: %s",
            request_obj.id,
            ", ".join(f"{r.amount} {r.leave_type_id}" for r in written),
        )

    if commit:
        session.commit()
    return written


def _split_across_buckets(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    days: Decimal,
    on_date: dt.date,
) -> list[tuple[str, Decimal, dt.date | None]]:
    """Spend the expiring carry-over first, then this year's accrual.

    Carried days expire three months into the new leave year; this year's
    accrual does not. Taking from `current` first would let carry-over lapse
    while a perfectly good balance sat unused — which is the employee losing
    days to nothing but the order the code happened to deduct in.

    So the deduction is split: as much as the live carry-over bucket can
    cover, then the remainder from `current`. Both parts carry the same
    reason, so `settlement_rows()` still finds the whole movement, and the
    carry-over part keeps the original expiry date so the ledger stays
    honest about which pot it came out of.

    Returns `[(bucket, days, expires_on), ...]`, most-perishable first.
    """
    carry_rows = list(session.execute(
        select(LeaveLedger.expires_on, func.coalesce(func.sum(LeaveLedger.amount), 0))
        .where(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.bucket == "carryover",
            LeaveLedger.effective_date <= on_date,
            or_(LeaveLedger.expires_on.is_(None), LeaveLedger.expires_on >= on_date),
        )
        .group_by(LeaveLedger.expires_on)
        .order_by(LeaveLedger.expires_on.nulls_last())
    ))

    out: list[tuple[str, Decimal, dt.date | None]] = []
    remaining = days
    for expires_on, total in carry_rows:
        available = Decimal(total)
        if available <= 0 or remaining <= 0:
            continue
        take = min(available, remaining)
        out.append(("carryover", take, expires_on))
        remaining -= take

    if remaining > 0:
        out.append(("current", remaining, None))
    return out


def _sources_for(session: Session, request: LeaveRequest) -> list[tuple[str, Decimal]]:
    """Which balances to draw from, and how much of each.

    Prefers the per-type breakdown recorded at submission. Falls back to
    "all paid days come from the requested type" only when a request predates
    the breakdown being stored — a single-source request, which is the common
    case, gives the same answer either way.
    """
    paid = Decimal(request.paid_days or 0)
    if paid <= 0:
        return []

    breakdown = _stored_breakdown(request)
    if breakdown:
        total = sum(days for _, days in breakdown)
        if total == paid:
            return breakdown
        log.warning(
            "request %s: stored breakdown sums to %s but paid_days is %s — "
            "falling back to a single-type deduction",
            request.id, total, paid,
        )
    return [(request.leave_type_id, paid)]


def _stored_breakdown(request: LeaveRequest) -> list[tuple[str, Decimal]]:
    """Read the per-source split out of the request's stored classification."""
    raw = getattr(request, "classification_sources", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return [
            (item["leave_type"], Decimal(str(item["days"])))
            for item in parsed
            if Decimal(str(item["days"])) > 0
        ]
    except (ValueError, KeyError, TypeError):
        log.warning("request %s has an unreadable stored breakdown", request.id)
        return []


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------
def reverse_settlement(
    session: Session,
    request: LeaveRequest | int,
    *,
    reason: str | None = None,
    commit: bool = False,
) -> list[LeaveLedger]:
    """Give back the days an approved request took. Idempotent.

    Appends mirror-image entries; the originals stay untouched, because the
    ledger is append-only and the fact that a deduction *happened* is part of
    the history.
    """
    request_obj = (
        session.get(LeaveRequest, request) if isinstance(request, int) else request
    )
    if request_obj is None:
        raise ValueError(f"No leave request {request!r}.")

    rows = settlement_rows(session, request_obj.id)
    taken = [r for r in rows if r.reason.startswith(REASON_LEAVE_TAKEN)]
    already_reversed = [r for r in rows if r.reason.startswith(REASON_LEAVE_REVERSED)]
    if not taken or already_reversed:
        return []

    written = []
    for original in taken:
        row = LeaveLedger(
            employee_id=original.employee_id,
            leave_type_id=original.leave_type_id,
            amount=-Decimal(original.amount),   # the mirror image
            reason=_reason(request_obj.id, REASON_LEAVE_REVERSED)
                   + (f" — {reason}" if reason else ""),
            # Same effective date as the original, NOT today. A spendable
            # balance is scoped to its leave year, so dating the mirror to
            # today would return the days into a different year from the one
            # they were taken out of — they would either vanish or reappear
            # somewhere they were never earned. `created_at` still records
            # when the reversal actually happened.
            effective_date=original.effective_date,
            policy_snapshot_id=original.policy_snapshot_id,
            # Same bucket AND same expiry: days taken out of an expiring
            # carry-over pot must go back into that pot, still expiring.
            # Returning them as non-expiring `current` days would quietly
            # extend a deadline the policy set.
            bucket=original.bucket,
            expires_on=original.expires_on,
        )
        session.add(row)
        written.append(row)

    session.flush()
    log.info("reversed settlement for request %s (%d rows)", request_obj.id, len(written))
    if commit:
        session.commit()
    return written


# ---------------------------------------------------------------------------
# Payroll hand-off
# ---------------------------------------------------------------------------
class PayrollNotice:
    """What payroll is told when leave is approved.

    A stub with a real shape. The interface is the point: a genuine
    integration replaces `notify_payroll` without touching anything upstream,
    and everything it needs is already on this object.
    """

    def __init__(self, request: LeaveRequest, employee: Employee):
        self.employee_id = employee.id
        self.employee_name = employee.name
        self.employee_email = employee.email
        self.region = employee.region
        self.request_id = request.id
        self.leave_type_id = request.leave_type_id
        self.start_date = request.start_date
        self.end_date = request.end_date
        # The pay cycle this lands in — the month the leave starts.
        self.pay_period = request.start_date.strftime("%Y-%m")
        self.paid_days = Decimal(request.paid_days or 0)
        self.unpaid_days = Decimal(request.unpaid_days or 0)

    def to_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "employee_email": self.employee_email,
            "region": self.region,
            "request_id": self.request_id,
            "leave_type_id": self.leave_type_id,
            "pay_period": self.pay_period,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "paid_days": str(self.paid_days),
            "unpaid_days": str(self.unpaid_days),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PayrollNotice {self.employee_name} {self.pay_period} "
            f"paid={self.paid_days} unpaid={self.unpaid_days}>"
        )


def notify_payroll(session: Session, request: LeaveRequest) -> PayrollNotice:
    """Hand the pay impact to payroll.

    Logs today. A real integration posts `notice.to_dict()` to the payroll
    system — or, better, consumes the `leave_ledger.deducted` outbox event, so
    delivery survives a crash. The outbox row is already written by
    `settle_approved_request`.
    """
    employee = session.get(Employee, request.employee_id)
    notice = PayrollNotice(request, employee)
    log.info(
        "PAYROLL %s: %s %s paid=%s unpaid=%s",
        notice.pay_period, notice.employee_name, notice.leave_type_id,
        notice.paid_days, notice.unpaid_days,
    )
    return notice
