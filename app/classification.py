"""Module 5 — Request & Classification.

Records the request, then decides how many days are paid.

**This module writes exactly one kind of row: `leave_request`.** It never
touches `leave_ledger` — the split is only *computed* here; deduction happens
after the approval chain completes (Module 6/7).

Review findings closed in this file:

  17  Public holidays are excluded, per region, via the `holidays` package.
  18  Days already committed to other pending requests are reserved, so two
      requests cannot spend the same balance.
  19  The split is stored on the request, and re-checked at approval time.
  20  Overlapping requests are refused (and a DB constraint backs it up).
  21  A request spanning a policy change is split across both policies.
  22  Backdated leave is rejected unless the policy allows it or the caller
      passes an explicit override.
  23  Minimum notice is enforced, not merely warned about.
  24  Maximum consecutive days is enforced, not merely warned about.
  25  A missing policy rejects the request rather than silently making it unpaid.
  26  The substitution order is read from `substitution_rules`, not hardcoded.
  27  Half days are supported at either end of a request.
  28  Notice is measured from `submitted_at`, not the current system clock.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.dashboard import get_live_balance
from app.holidays import (
    HALF_DAY,
    UnknownRegionError,
    holidays_in_range,
    working_days_between,
)
from app.models import Employee, LeaveRequest, SubstitutionRule
from app.policy_engine import ResolvedPolicy, resolve_policy
from app.util import format_days

log = logging.getLogger("leave_engine.classification")

__all__ = [
    "working_days_between",
    "business_days_between",
    "submit_leave_request",
    "classify_paid_unpaid",
    "reclassify_request",
    "substitution_order",
    "committed_days",
    "available_balance",
    "find_overlapping_request",
    "policy_spans",
    "Classification",
    "Draw",
    "PolicySpan",
    "SubmissionResult",
    "RequestValidationError",
    "PolicyMissingError",
    "OverlappingRequestError",
    "UNPAID",
    "DEFAULT_SUBSTITUTION_CHAIN",
]

UNPAID = "Unpaid"

# Fallback used only when `substitution_rules` has no rows for a leave type.
# The table is the source of truth (finding 26); this keeps a fresh database
# working before it is seeded rather than silently classifying everything
# unpaid.
DEFAULT_SUBSTITUTION_CHAIN = ("CL", "EL", UNPAID)


class RequestValidationError(ValueError):
    """The request cannot be recorded as submitted."""


class PolicyMissingError(RequestValidationError):
    """No policy resolves for this leave type — the request is invalid.

    Finding 25: this used to classify as unpaid. Silently converting an
    unrecognised request into loss of pay is worse than refusing it, because
    the employee only discovers it on their payslip.
    """


class OverlappingRequestError(RequestValidationError):
    """The employee already has an open request covering these dates."""


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------
def business_days_between(start_date: dt.date, end_date: dt.date, region: str = "India-TamilNadu"):
    """Backwards-compatible alias. Prefer `working_days_between`.

    The old signature took no region, because it only knew about weekends.
    Holidays are regional, so the region is now required — this shim keeps
    older call sites working with an explicit default.
    """
    return working_days_between(start_date, end_date, region)


# ---------------------------------------------------------------------------
# Substitution order — data, not code (finding 26)
# ---------------------------------------------------------------------------
def substitution_order(
    session: Session, leave_type_id: str, region: str | None = None
) -> list[str]:
    """Ordered leave types to draw from for a request of this type.

    Read from `substitution_rules`, preferring a region-specific chain over
    the default one (`region IS NULL`). Texas has no Casual Leave, so its
    chain genuinely differs from Tamil Nadu's — that is data, not a branch.

    Unpaid never substitutes. The chain always starts with the requested type
    itself, because the first draw comes from its own balance.
    """
    if leave_type_id == UNPAID:
        return [UNPAID]

    rows = session.scalars(
        select(SubstitutionRule)
        .where(
            SubstitutionRule.leave_type_id == leave_type_id,
            SubstitutionRule.is_active.is_(True),
            or_(SubstitutionRule.region == region, SubstitutionRule.region.is_(None)),
        )
        .order_by(SubstitutionRule.position)
    ).all()

    if rows:
        regional = [r for r in rows if r.region == region]
        chosen = regional or [r for r in rows if r.region is None]
        order: list[str] = []
        for rule in sorted(chosen, key=lambda r: r.position):
            if rule.fallback_leave_type_id not in order:
                order.append(rule.fallback_leave_type_id)
        if order and order[0] != leave_type_id:
            order.insert(0, leave_type_id)
        if UNPAID not in order:
            order.append(UNPAID)
        return order

    # Unseeded database: fall back to the documented default.
    order = [leave_type_id]
    for fallback in DEFAULT_SUBSTITUTION_CHAIN:
        if fallback not in order:
            order.append(fallback)
    return order


# ---------------------------------------------------------------------------
# Commitments — pending requests reserve balance (finding 18)
# ---------------------------------------------------------------------------
def committed_days(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    *,
    exclude_request_id: int | None = None,
) -> Decimal:
    """Days already promised to other pending requests.

    Without this, an employee with 5 days can submit three 5-day requests and
    every one of them classifies as fully paid. They cannot all be — the
    balance gets spent once. Pending requests therefore reserve their days
    until they are approved (when they become a real deduction) or rejected
    (when the reservation is released).
    """
    stmt = select(LeaveRequest).where(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.leave_type_id == leave_type_id,
        LeaveRequest.status == "pending",
    )
    if exclude_request_id is not None:
        stmt = stmt.where(LeaveRequest.id != exclude_request_id)

    total = Decimal("0")
    for request in session.scalars(stmt):
        # The paid portion is what actually draws on this balance. Before a
        # request is classified, assume the worst and reserve the whole thing.
        total += (
            Decimal(request.paid_days) if request.paid_days is not None
            else Decimal(request.duration_days)
        )
    return total


def available_balance(
    session: Session,
    employee_id: int,
    leave_type_id: str,
    as_of_date: dt.date | None = None,
    *,
    exclude_request_id: int | None = None,
) -> Decimal:
    """Live balance minus days committed to pending requests."""
    balance = get_live_balance(session, employee_id, leave_type_id, as_of_date)
    reserved = committed_days(
        session, employee_id, leave_type_id, exclude_request_id=exclude_request_id
    )
    return balance - reserved


# ---------------------------------------------------------------------------
# Overlap (finding 20)
# ---------------------------------------------------------------------------
def find_overlapping_request(
    session: Session,
    employee_id: int,
    start: dt.date,
    end: dt.date,
    *,
    exclude_request_id: int | None = None,
) -> LeaveRequest | None:
    """An open request covering any of these dates.

    Only `pending` and `approved` requests hold their dates; a rejected or
    cancelled one releases them. A DB exclusion constraint enforces the same
    rule, so a concurrent submission cannot slip past this check — this exists
    to produce a readable error rather than a constraint violation.
    """
    stmt = select(LeaveRequest).where(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.status.in_(("pending", "approved")),
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    )
    if exclude_request_id is not None:
        stmt = stmt.where(LeaveRequest.id != exclude_request_id)
    return session.scalars(stmt.order_by(LeaveRequest.start_date).limit(1)).first()


# ---------------------------------------------------------------------------
# Policy spans (finding 21)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PolicySpan:
    """A stretch of a request over which one policy applies."""

    start: dt.date
    end: dt.date
    days: Decimal
    policy: ResolvedPolicy | None

    @property
    def is_paid(self) -> bool:
        return bool(self.policy and self.policy.is_paid)


def policy_spans(
    session: Session,
    employee: Employee,
    leave_type_id: str,
    start: dt.date,
    end: dt.date,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
) -> list[PolicySpan]:
    """Split a request wherever the resolved policy changes mid-request.

    A request running 28 March to 4 April crosses an Indian leave-year
    boundary, and may cross a tenure bracket or a policy edit too. Charging
    the whole thing at whichever policy happened to apply on day one is wrong
    when the paid/unpaid status or the rules differ across the span.

    Most requests produce exactly one span, and the caller's arithmetic
    collapses to the simple case.
    """
    boundaries: set[dt.date] = {start}
    cursor = start
    previous_id = None
    while cursor <= end:
        policy = resolve_policy(session, employee, leave_type_id, cursor)
        marker = (policy.policy_snapshot_id, policy.is_paid) if policy else None
        if previous_id is not None and marker != previous_id:
            boundaries.add(cursor)
        previous_id = marker
        cursor += dt.timedelta(days=1)

    ordered = sorted(boundaries)
    spans: list[PolicySpan] = []
    for index, span_start in enumerate(ordered):
        span_end = ordered[index + 1] - dt.timedelta(days=1) if index + 1 < len(ordered) else end
        days = working_days_between(
            span_start, span_end, employee.region, session,
            start_half_day=start_half_day and span_start == start,
            end_half_day=end_half_day and span_end == end,
        )
        if days == 0:
            continue
        spans.append(PolicySpan(
            start=span_start,
            end=span_end,
            days=days,
            policy=resolve_policy(session, employee, leave_type_id, span_start),
        ))
    return spans


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Draw:
    """One leg of the substitution walk: how many days came from where."""

    leave_type_id: str
    days: Decimal
    balance_before: Decimal | None  # None for Unpaid, which needs no balance
    is_substitution: bool  # False for the originally requested type


@dataclass(frozen=True)
class Classification:
    """The paid/unpaid split for a request. Computed, never written."""

    leave_type_id: str
    requested_days: Decimal
    paid_days: Decimal
    unpaid_days: Decimal
    draws: list[Draw] = field(default_factory=list)
    reason: str = ""
    spans: list[PolicySpan] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Sum conservation is the invariant this whole module rests on.
        total = self.paid_days + self.unpaid_days
        if total != self.requested_days:
            raise AssertionError(
                f"classification does not conserve days: {self.paid_days} paid + "
                f"{self.unpaid_days} unpaid != {self.requested_days} requested"
            )

    @property
    def used_substitution(self) -> bool:
        return any(d.is_substitution and d.days > 0 for d in self.draws)

    @property
    def is_fully_paid(self) -> bool:
        return self.unpaid_days == 0

    @property
    def sources(self) -> list[dict]:
        """Per-type breakdown Module 7 deducts from — one entry per source.

        The Module 7 brief flags this as the contract that has to line up:
        a total is not enough to write the right ledger rows when
        substitution drew from more than one type.
        """
        return [
            {"leave_type": d.leave_type_id, "days": d.days}
            for d in self.draws
            if d.days > 0 and d.leave_type_id != UNPAID
        ]

    def explain(self) -> str:
        if not self.draws:
            return self.reason
        parts = [f"{format_days(d.days)} from {d.leave_type_id}"
                 for d in self.draws if d.days > 0]
        return f"{self.reason} ({', '.join(parts)})" if parts else self.reason

    def to_dict(self) -> dict:
        return {
            "leave_type_id": self.leave_type_id,
            "requested_days": str(self.requested_days),
            "paid_days": str(self.paid_days),
            "unpaid_days": str(self.unpaid_days),
            "used_substitution": self.used_substitution,
            "reason": self.reason,
            "sources": [{"leave_type": s["leave_type"], "days": str(s["days"])}
                        for s in self.sources],
            "draws": [
                {
                    "leave_type_id": d.leave_type_id,
                    "days": str(d.days),
                    "balance_before": None if d.balance_before is None else str(d.balance_before),
                    "is_substitution": d.is_substitution,
                }
                for d in self.draws
            ],
            "spans": [
                {"start": s.start.isoformat(), "end": s.end.isoformat(),
                 "days": str(s.days), "is_paid": s.is_paid}
                for s in self.spans
            ],
        }


@dataclass(frozen=True)
class SubmissionResult:
    """What `submit_leave_request` produced."""

    request: LeaveRequest
    classification: Classification
    warnings: list[str] = field(default_factory=list)
    holidays_excluded: list[tuple[dt.date, str]] = field(default_factory=list)

    @property
    def request_id(self) -> int:
        return self.request.id


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_paid_unpaid(
    session: Session,
    employee: Employee | int,
    leave_type_id: str,
    requested_days: Decimal | int | float | str,
    as_of_date: dt.date | str | None = None,
    *,
    exclude_request_id: int | None = None,
    respect_pending: bool = True,
    strict_policy: bool = True,
    already_drawn: dict[str, Decimal] | None = None,
) -> Classification:
    """Split a request into paid and unpaid days.

    `already_drawn` lets a caller run several classifications against ONE
    balance: pass the per-type totals consumed so far and each type's
    available balance is reduced by them. Without it, splitting a request
    across policy periods double-spends — two 4-day spans against a 5-day
    balance would each independently see 5 days free and report 8 paid.

      1. No policy -> raise (finding 25), unless `strict_policy=False`.
      2. Unpaid policy -> every day unpaid, no substitution.
      3. Otherwise walk `substitution_order()`, drawing from each type's
         AVAILABLE balance (live balance minus pending commitments).
      4. Whatever the chain cannot cover falls to Unpaid.

    Nothing is written to the ledger.
    """
    requested = Decimal(str(requested_days))
    if requested <= 0:
        raise RequestValidationError(f"requested_days must be positive, got {requested}.")

    employee_obj = session.get(Employee, employee) if isinstance(employee, int) else employee
    if employee_obj is None:
        raise RequestValidationError(f"No employee with id {employee!r}.")

    policy = resolve_policy(session, employee_obj, leave_type_id, as_of_date)
    if policy is None:
        if strict_policy:
            raise PolicyMissingError(
                f"No active policy for leave type {leave_type_id!r} in "
                f"{employee_obj.region} on {as_of_date or dt.date.today()}. The request "
                "is invalid and has been rejected rather than silently treated as "
                "unpaid — an employee should not discover a policy gap on their payslip."
            )
        return Classification(
            leave_type_id=leave_type_id, requested_days=requested,
            paid_days=Decimal("0"), unpaid_days=requested,
            draws=[Draw(UNPAID, requested, None, is_substitution=True)],
            reason=f"No active policy for {leave_type_id}; treated as unpaid.",
        )

    if not policy.is_paid:
        return Classification(
            leave_type_id=leave_type_id, requested_days=requested,
            paid_days=Decimal("0"), unpaid_days=requested,
            draws=[Draw(leave_type_id, requested, None, is_substitution=False)],
            reason=f"{leave_type_id} is an unpaid leave type; no balance is drawn.",
        )

    remaining = requested
    paid = Decimal("0")
    draws: list[Draw] = []

    for index, fallback_type in enumerate(
        substitution_order(session, leave_type_id, employee_obj.region)
    ):
        if remaining == 0:
            break
        is_substitution = index > 0

        if fallback_type == UNPAID:
            draws.append(Draw(UNPAID, remaining, None, is_substitution=True))
            break

        fallback_policy = (
            policy if fallback_type == leave_type_id
            else resolve_policy(session, employee_obj, fallback_type, as_of_date)
        )
        if fallback_policy is None or not fallback_policy.is_paid:
            continue

        if respect_pending:
            usable = available_balance(
                session, employee_obj.id, fallback_type, as_of_date,
                exclude_request_id=exclude_request_id,
            )
        else:
            usable = get_live_balance(session, employee_obj.id, fallback_type, as_of_date)

        # Subtract what earlier spans of this same request already committed.
        if already_drawn:
            usable -= already_drawn.get(fallback_type, Decimal("0"))

        # Negative-balance behaviour is now explicit policy (finding 11).
        # By default a balance cannot go below zero: the shortfall falls to
        # unpaid rather than borrowing against days not yet accrued. A policy
        # that sets `allow_negative_balance` lets the REQUESTED type overdraw
        # — never a substitute, since silently overdrawing a type the employee
        # did not ask for would be a surprise on their next payslip.
        if fallback_policy.allow_negative_balance and not is_substitution:
            draw = remaining
        else:
            draw = min(max(usable, Decimal("0")), remaining)

        draws.append(Draw(fallback_type, draw, usable, is_substitution))
        paid += draw
        remaining -= draw

    unpaid = remaining

    if unpaid == 0 and not any(d.is_substitution and d.days > 0 for d in draws):
        reason = f"Balance covers the request in full from {leave_type_id}."
    elif unpaid == 0:
        reason = f"{leave_type_id} balance was short; covered from the substitution order."
    elif paid == 0:
        reason = "No paid balance available in the substitution order; fully unpaid."
    else:
        reason = f"Partially covered; {format_days(unpaid)} day(s) fall to unpaid leave."

    return Classification(
        leave_type_id=leave_type_id, requested_days=requested,
        paid_days=paid, unpaid_days=unpaid, draws=draws, reason=reason,
    )


def reclassify_request(
    session: Session, request: LeaveRequest, as_of_date: dt.date | None = None
) -> tuple[Classification, bool]:
    """Re-run classification for a request, and report whether it moved.

    Finding 19: balances shift between submission and final approval. The
    stored split is what the employee and the approvers saw, so Module 7
    deducts against THAT — but an approver should be told when the picture
    has changed, which is what the boolean is for.

    The request's own reservation is excluded, otherwise it would be counted
    against itself.
    """
    employee = session.get(Employee, request.employee_id)
    fresh = classify_paid_unpaid(
        session, employee, request.leave_type_id, request.duration_days,
        as_of_date or request.start_date,
        exclude_request_id=request.id,
        strict_policy=False,
    )
    stored_paid = Decimal(request.paid_days) if request.paid_days is not None else None
    changed = stored_paid is not None and stored_paid != fresh.paid_days
    return fresh, changed


# ---------------------------------------------------------------------------
# Enforcement (findings 22, 23, 24)
# ---------------------------------------------------------------------------
def _enforce_limits(
    policy: ResolvedPolicy,
    start: dt.date,
    duration: Decimal,
    submitted_on: dt.date,
    leave_type_id: str,
    override_reason: str | None,
) -> list[str]:
    """Apply the policy's limits. Returns warnings; raises when blocking.

    `enforcement` is per policy row: "block" (the default) rejects,
    "warn" records and continues. `override_reason` forces a block down to a
    warning, and is recorded on the request so the approver can see it.
    """
    blocking = policy.enforcement == "block" and not override_reason
    warnings: list[str] = []

    def fail_or_warn(message: str) -> None:
        if blocking:
            raise RequestValidationError(
                f"{message} Set an override_reason to submit anyway (it will be "
                "recorded and shown to approvers), or ask HR to relax this "
                f"policy's enforcement for {leave_type_id}."
            )
        warnings.append(message + (" (overridden)" if override_reason else " (advisory)"))

    notice_given = (start - submitted_on).days
    if notice_given < 0:
        if not policy.allow_backdated:
            fail_or_warn(
                f"Backdated request: leave starts {abs(notice_given)} day(s) before it "
                f"was submitted ({submitted_on})."
            )
        else:
            warnings.append(f"Backdated by {abs(notice_given)} day(s); permitted by policy.")
    elif policy.min_notice_days and notice_given < policy.min_notice_days:
        fail_or_warn(
            f"Short notice: {notice_given} day(s) given, policy for {leave_type_id} "
            f"requires {policy.min_notice_days}."
        )

    if policy.max_consecutive_days and duration > policy.max_consecutive_days:
        fail_or_warn(
            f"Too long: {format_days(duration)} days requested, policy for "
            f"{leave_type_id} allows at most {policy.max_consecutive_days} consecutive."
        )

    return warnings


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def submit_leave_request(
    session: Session,
    employee: Employee | int,
    leave_type_id: str,
    start_date: dt.date | str,
    end_date: dt.date | str,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
    submitted_at: dt.datetime | None = None,
    employee_reason: str | None = None,
    override_reason: str | None = None,
    commit: bool = True,
) -> SubmissionResult:
    """Record a leave request as `pending` and classify its paid/unpaid split.

    `duration_days` excludes weekends and the region's public holidays, and
    honours half-day flags at either end.

    Notice periods are measured from `submitted_at` (default: now), never
    from the current clock at read time — so re-examining an old request
    cannot retroactively make it a violation (finding 28).
    """
    start = start_date if isinstance(start_date, dt.date) else dt.date.fromisoformat(start_date)
    end = end_date if isinstance(end_date, dt.date) else dt.date.fromisoformat(end_date)
    if end < start:
        raise RequestValidationError(
            f"end_date ({end}) must be on or after start_date ({start})."
        )

    employee_obj = session.get(Employee, employee) if isinstance(employee, int) else employee
    if employee_obj is None:
        raise RequestValidationError(f"No employee with id {employee!r}.")

    submitted_at = submitted_at or dt.datetime.now(dt.timezone.utc)
    submitted_on = submitted_at.date()

    # --- overlap (finding 20) --------------------------------------------
    clash = find_overlapping_request(session, employee_obj.id, start, end)
    if clash is not None:
        raise OverlappingRequestError(
            f"{employee_obj.name} already has a {clash.status} request "
            f"(#{clash.id}, {clash.leave_type_id}) covering "
            f"{clash.start_date} to {clash.end_date}, which overlaps "
            f"{start} to {end}."
        )

    # --- duration, holidays and half days (findings 17, 27) --------------
    try:
        duration = working_days_between(
            start, end, employee_obj.region, session,
            start_half_day=start_half_day, end_half_day=end_half_day,
        )
        excluded = holidays_in_range(start, end, employee_obj.region, session)
    except UnknownRegionError as exc:
        raise RequestValidationError(str(exc)) from exc

    if duration == 0:
        detail = ""
        if excluded:
            detail = " (" + ", ".join(f"{d} {name}" for d, name in excluded) + ")"
        raise RequestValidationError(
            f"{start} to {end} contains no working days{detail} — nothing to request."
        )

    # --- policy must exist (finding 25) ----------------------------------
    policy = resolve_policy(session, employee_obj, leave_type_id, start)
    if policy is None:
        raise PolicyMissingError(
            f"No active policy for leave type {leave_type_id!r} in "
            f"{employee_obj.region} on {start}. Request rejected as invalid."
        )

    # --- enforcement (findings 22, 23, 24) -------------------------------
    warnings = _enforce_limits(
        policy, start, duration, submitted_on, leave_type_id, override_reason
    )
    if excluded:
        warnings.append(
            "Public holidays excluded from the duration: "
            + ", ".join(f"{d} {name}" for d, name in excluded)
        )

    # --- classification, split across policy changes (finding 21) --------
    spans = policy_spans(
        session, employee_obj, leave_type_id, start, end,
        start_half_day=start_half_day, end_half_day=end_half_day,
    )
    if len(spans) > 1:
        warnings.append(
            f"This request spans {len(spans)} policy periods; each is classified "
            "against the policy in force on its own dates."
        )

    classification = _classify_across_spans(
        session, employee_obj, leave_type_id, duration, spans, start
    )

    request = LeaveRequest(
        employee_id=employee_obj.id,
        leave_type_id=leave_type_id,
        start_date=start,
        end_date=end,
        duration_days=duration,
        status="pending",
        start_half_day=start_half_day,
        end_half_day=end_half_day,
        submitted_at=submitted_at,
        paid_days=classification.paid_days,
        unpaid_days=classification.unpaid_days,
        classified_at=dt.datetime.now(dt.timezone.utc),
        # Per-source breakdown, so settlement can write one ledger row per
        # balance drawn from rather than one combined (and wrong) row.
        classification_sources=json.dumps([
            {"leave_type": s["leave_type"], "days": str(s["days"])}
            for s in classification.sources
        ]),
        # Three distinct reasons, three distinct columns. An employee's
        # private note must never be mistaken for a policy-override
        # justification — they have different meanings and audiences.
        employee_reason=employee_reason,
        override_reason=override_reason,
    )
    session.add(request)

    try:
        session.flush()  # populates request.id for Module 6
    except IntegrityError as exc:
        session.rollback()
        if "ex_leave_request_no_overlap" in str(exc.orig):
            raise OverlappingRequestError(
                f"{employee_obj.name} already has an open request overlapping "
                f"{start} to {end} (detected by the database)."
            ) from exc
        raise

    if commit:
        session.commit()

    log.info(
        "request %s: %s %s %s→%s (%s days) — %s paid / %s unpaid",
        request.id, employee_obj.name, leave_type_id, start, end, duration,
        classification.paid_days, classification.unpaid_days,
    )
    return SubmissionResult(
        request=request,
        classification=classification,
        warnings=warnings,
        holidays_excluded=excluded,
    )


def _classify_across_spans(
    session: Session,
    employee: Employee,
    leave_type_id: str,
    duration: Decimal,
    spans: list[PolicySpan],
    fallback_date: dt.date,
) -> Classification:
    """Classify each policy span separately, then combine (finding 21)."""
    if len(spans) <= 1:
        result = classify_paid_unpaid(
            session, employee, leave_type_id, duration, fallback_date
        )
        return Classification(
            leave_type_id=result.leave_type_id,
            requested_days=result.requested_days,
            paid_days=result.paid_days,
            unpaid_days=result.unpaid_days,
            draws=result.draws,
            reason=result.reason,
            spans=spans,
        )

    paid = Decimal("0")
    unpaid = Decimal("0")
    merged: dict[str, Draw] = {}
    # Days each leave type has already given up to EARLIER spans of this same
    # request. Without this every span re-reads the untouched ledger balance
    # and the request can be paid several times over — a 5-day balance
    # covering two 4-day spans as "8 paid, 0 unpaid".
    drawn_so_far: dict[str, Decimal] = {}

    for span in spans:
        part = classify_paid_unpaid(
            session, employee, leave_type_id, span.days, span.start,
            strict_policy=False, already_drawn=drawn_so_far,
        )
        paid += part.paid_days
        unpaid += part.unpaid_days
        for draw in part.draws:
            if draw.leave_type_id != UNPAID and draw.days > 0:
                drawn_so_far[draw.leave_type_id] = (
                    drawn_so_far.get(draw.leave_type_id, Decimal("0")) + draw.days
                )
        for draw in part.draws:
            existing = merged.get(draw.leave_type_id)
            merged[draw.leave_type_id] = Draw(
                leave_type_id=draw.leave_type_id,
                days=(existing.days if existing else Decimal("0")) + draw.days,
                balance_before=existing.balance_before if existing else draw.balance_before,
                is_substitution=draw.is_substitution if existing is None else existing.is_substitution,
            )

    return Classification(
        leave_type_id=leave_type_id,
        requested_days=duration,
        paid_days=paid,
        unpaid_days=unpaid,
        draws=list(merged.values()),
        reason=(
            f"Split across {len(spans)} policy periods; each classified against "
            "the policy in force on its own dates."
        ),
        spans=spans,
    )


# ---------------------------------------------------------------------------
# Preview — everything the request form needs before anything is written
# ---------------------------------------------------------------------------
def preview_leave_request(
    session: Session,
    employee: Employee | int,
    leave_type_id: str,
    start_date: dt.date | str,
    end_date: dt.date | str,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
    submitted_at: dt.datetime | None = None,
    override_reason: str | None = None,
) -> dict:
    """Dry-run a request: duration, day-by-day working, split, and blockers.

    Writes nothing. Every problem is *reported* rather than raised, because
    the request form needs to show the employee what is wrong while they are
    still editing — an exception would just be an error page.
    """
    from app.holidays import explain_days

    start = start_date if isinstance(start_date, dt.date) else dt.date.fromisoformat(start_date)
    end = end_date if isinstance(end_date, dt.date) else dt.date.fromisoformat(end_date)
    employee_obj = session.get(Employee, employee) if isinstance(employee, int) else employee
    submitted_at = submitted_at or dt.datetime.now(dt.timezone.utc)

    out: dict = {
        "leave_type_id": leave_type_id,
        "start_date": start,
        "end_date": end,
        "start_half_day": start_half_day,
        "end_half_day": end_half_day,
        "days": [],
        "duration_days": Decimal("0"),
        "holidays_excluded": [],
        "warnings": [],
        "blockers": [],
        "can_submit": False,
        "classification": None,
        "balance_before": None,
        "balance_after": None,
        "policy": None,
    }

    if employee_obj is None:
        out["blockers"].append("Employee not found.")
        return out
    if end < start:
        out["blockers"].append(f"End date ({end}) is before the start date ({start}).")
        return out

    try:
        out["days"] = explain_days(
            start, end, employee_obj.region, session,
            start_half_day=start_half_day, end_half_day=end_half_day,
        )
        out["duration_days"] = working_days_between(
            start, end, employee_obj.region, session,
            start_half_day=start_half_day, end_half_day=end_half_day,
        )
        out["holidays_excluded"] = holidays_in_range(
            start, end, employee_obj.region, session
        )
    except UnknownRegionError as exc:
        out["blockers"].append(str(exc))
        return out

    if out["duration_days"] == 0:
        out["blockers"].append(
            "This range contains no working days — every day is a weekend or a "
            "public holiday."
        )
        return out

    policy = resolve_policy(session, employee_obj, leave_type_id, start)
    if policy is None:
        out["blockers"].append(
            f"No active {leave_type_id} policy for {employee_obj.region} on {start}. "
            "Contact HR."
        )
        return out

    out["policy"] = {
        "entitlement_days_per_year": policy.entitlement_days_per_year,
        "is_paid": policy.is_paid,
        "min_notice_days": policy.min_notice_days,
        "max_consecutive_days": policy.max_consecutive_days,
        "enforcement": policy.enforcement,
        "explain": policy.explain(),
    }

    # Enforcement: collect rather than raise, so the form can show them all.
    try:
        out["warnings"] = _enforce_limits(
            policy, start, out["duration_days"], submitted_at.date(),
            leave_type_id, override_reason,
        )
    except RequestValidationError as exc:
        out["blockers"].append(str(exc))

    clash = find_overlapping_request(session, employee_obj.id, start, end)
    if clash is not None:
        out["blockers"].append(
            f"You already have a {clash.status} request (#{clash.id}) from "
            f"{clash.start_date} to {clash.end_date} that overlaps these dates."
        )

    classification = classify_paid_unpaid(
        session, employee_obj, leave_type_id, out["duration_days"], start,
        strict_policy=False,
    )
    out["classification"] = classification

    balance = get_live_balance(session, employee_obj.id, leave_type_id, start)
    out["balance_before"] = balance
    drawn = next(
        (d.days for d in classification.draws if d.leave_type_id == leave_type_id),
        Decimal("0"),
    )
    out["balance_after"] = balance - drawn

    if out["holidays_excluded"]:
        out["warnings"].append(
            "Public holidays excluded: "
            + ", ".join(f"{d} {name}" for d, name in out["holidays_excluded"])
        )

    out["can_submit"] = not out["blockers"]
    return out


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------
def cancel_leave_request(
    session: Session,
    request: LeaveRequest | int,
    *,
    actor: Employee | None = None,
    reason: str | None = None,
    commit: bool = True,
) -> LeaveRequest:
    """Cancel a request, releasing its dates and its reserved balance.

    Only `pending` requests can be cancelled here. An APPROVED request has
    already been deducted from the ledger, so unwinding it is a reversal —
    that belongs with whoever owns the ledger correction path (HR), not with
    a self-service cancel button.
    """
    # Lock the request BEFORE reading its status, with the same lock the
    # approval path takes. Previously cancellation took no lock at all, so an
    # approval and a cancellation could each read "pending" and then both
    # write — leaving a request that was approved and cancelled at once.
    request_id = request if isinstance(request, int) else request.id
    request_obj = session.scalars(
        select(LeaveRequest).where(LeaveRequest.id == request_id).with_for_update()
    ).first()
    if request_obj is None:
        raise RequestValidationError("No such leave request.")

    if actor is not None and actor.id != request_obj.employee_id:
        from app.authorization import AuthorizationError

        if actor.role not in ("hr_admin", "director"):
            raise AuthorizationError(
                "You can only cancel your own leave requests."
            )

    is_admin = actor is not None and actor.role in ("hr_admin", "director")

    if request_obj.status == "approved":
        # An approved request has already moved the ledger. Withdrawing it is
        # a reversal, not a cancellation, so only HR may do it — and the days
        # are given back by appending a mirror entry.
        if not is_admin:
            raise RequestValidationError(
                f"Request #{request_obj.id} is already approved and the days have "
                "been deducted. Ask HR to withdraw it."
            )
        from app.settlement import reverse_settlement

        reverse_settlement(session, request_obj, reason=reason or "request withdrawn")
    elif request_obj.status != "pending":
        raise RequestValidationError(
            f"Request #{request_obj.id} is {request_obj.status}. "
            "Only a pending or approved request can be withdrawn."
        )

    request_obj.status = "cancelled"
    request_obj.cancellation_reason = reason

    # Any approval steps still waiting are moot.
    from app.models import ApprovalStep

    for step in session.scalars(
        select(ApprovalStep).where(ApprovalStep.request_id == request_obj.id)
    ):
        if step.status in ("active", "pending"):
            step.status = "rejected"
            step.decision_reason = "Request cancelled by the employee."

    session.flush()
    if commit:
        session.commit()
    log.info("request %s cancelled", request_obj.id)
    return request_obj


__all__ += ["preview_leave_request", "cancel_leave_request"]
