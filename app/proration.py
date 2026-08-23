"""Pro-ration for partial years of service.

Closes the review finding: *"No pro-rating for employees who join partway
through a year"* — and, per the follow-up, the same problem at the other end
for leavers.

## The rule

An entitlement is expressed per YEAR. Someone who was only employed for part
of that year should earn part of it. So:

    prorated = SUM over segments of ( entitlement_in_segment x segment_weight )

where the cycle is split into **segments** at every date the answer could
change, and each segment is weighted by how much of the cycle it covers.

## Why segments, rather than one multiply

A naive `entitlement x eligible_days / cycle_days` is wrong whenever the
entitlement itself changes mid-cycle. Two ways that happens:

  * **Policy change** — HR edits a policy, or closes one row's `effective_to`
    and opens a replacement, halfway through the year.
  * **Tenure change** — the employee crosses a bracket mid-cycle. This cannot
    happen for an anniversary-aligned cycle (tenure is constant across it by
    construction), but it absolutely can for a calendar-year cycle, which is
    what a fixed 1-April grant date gives you.

Segmenting handles both without special-casing either. A cycle with no changes
collapses to a single segment and the formula degenerates to the naive one.

## Rounding

The number of decimal places comes from the policy row (`rounding_dp`), not
from a constant in this file — see the module docstring in `accrual.py` for
the wider rounding convention.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Employee, OrgPolicy
from app.policy_engine import ResolvedPolicy, resolve_policy

__all__ = [
    "accrual_cycle",
    "eligible_period",
    "eligible_days",
    "policy_segments",
    "prorate_entitlement",
    "ProrationResult",
    "Segment",
    "round_days",
]


def round_days(amount: Decimal, rounding_dp: int) -> Decimal:
    """ROUND_HALF_UP to the policy's decimal places. The one rounding rule."""
    quantum = Decimal(1).scaleb(-int(rounding_dp))  # 3 -> 0.001, 2 -> 0.01
    return Decimal(amount).quantize(quantum, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Which cycle are we pro-rating?
# ---------------------------------------------------------------------------
def accrual_cycle(
    employee: Employee, policy: ResolvedPolicy | None, on_date: dt.date
) -> tuple[dt.date, dt.date]:
    """The accrual year containing `on_date`, as [start, end] INCLUSIVE.

    Two bases, chosen by the policy row:

    * **Fixed organisational leave year** — when `leave_year_end` is set
      ("03-31" in India, "12-31" in the US). This is what makes "joined
      partway through the year" a meaningful statement, and therefore what
      makes pro-ration meaningful. Normally used for annual-lump types.

    * **Anniversary-aligned** — when `leave_year_end` is NULL. Each employee's
      leave year starts on their own join date, so they are never a mid-year
      joiner. Normally used for monthly types, where partial service is
      already handled month by month.

    That distinction matters: under an anniversary basis a joiner is fully
    eligible for their own first cycle by construction, so pro-rating the
    front end would be double-counting.
    """
    leave_year_end = getattr(policy, "leave_year_end", None) if policy else None

    if leave_year_end:
        month, day = (int(part) for part in leave_year_end.split("-"))
        end_this_year = _safe_date(on_date.year, month, day)
        if on_date <= end_this_year:
            end = end_this_year
        else:
            end = _safe_date(on_date.year + 1, month, day)
        start = _safe_date(end.year - 1, month, day) + dt.timedelta(days=1)
        return start, end

    # Anniversary-aligned fallback.
    years = 0
    while _anniversary_on(employee.join_date, years + 1) <= on_date:
        years += 1
    start = _anniversary_on(employee.join_date, years)
    return start, _anniversary_on(employee.join_date, years + 1) - dt.timedelta(days=1)


def _safe_date(year: int, month: int, day: int) -> dt.date:
    """Clamp to the last valid day — 29 Feb in a non-leap year becomes 28 Feb."""
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day, last))


def _anniversary_on(join_date: dt.date, offset: int) -> dt.date:
    return _safe_date(join_date.year + offset, join_date.month, join_date.day)


# ---------------------------------------------------------------------------
# Eligible service window
# ---------------------------------------------------------------------------
def eligible_period(
    employee: Employee, period_start: dt.date, period_end: dt.date
) -> tuple[dt.date, dt.date] | None:
    """Intersect a cycle with the employee's actual service dates.

    `period_end` is INCLUSIVE. Returns None when the employee was not employed
    at any point in the window — a joiner whose start date is after the cycle,
    or a leaver who left before it began.
    """
    start = max(period_start, employee.join_date)
    end = period_end
    if employee.exit_date is not None:
        end = min(end, employee.exit_date)
    if start > end:
        return None
    return start, end


def eligible_days(employee: Employee, period_start: dt.date, period_end: dt.date) -> int:
    window = eligible_period(employee, period_start, period_end)
    if window is None:
        return 0
    start, end = window
    return (end - start).days + 1


def _eligible_months(employee: Employee, period_start: dt.date, period_end: dt.date) -> int:
    """Calendar months in the cycle containing at least one eligible day.

    The 'monthly' pro-ration convention: an employee who starts on the 28th
    still earns that whole month. Cruder than daily, but it is what a lot of
    HR handbooks actually say, so it is offered as an explicit choice.
    """
    window = eligible_period(employee, period_start, period_end)
    if window is None:
        return 0
    start, end = window

    months = 0
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months += 1
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Segment:
    """A stretch of the cycle over which the resolved policy does not change."""

    start: dt.date
    end: dt.date  # inclusive
    policy: ResolvedPolicy | None
    days: int
    weight: Decimal  # days / cycle_days
    contribution: Decimal  # entitlement x weight

    @property
    def entitlement(self) -> Decimal:
        return (
            Decimal("0") if self.policy is None
            else Decimal(self.policy.entitlement_days_per_year)
        )


def _anniversaries_in(join_date: dt.date, start: dt.date, end: dt.date) -> list[dt.date]:
    """Tenure-bracket boundaries falling inside a window."""
    out = []
    for offset in range(0, (end.year - join_date.year) + 2):
        try:
            anniversary = join_date.replace(year=join_date.year + offset)
        except ValueError:  # 29 Feb into a non-leap year
            anniversary = join_date.replace(year=join_date.year + offset, day=28)
        if start < anniversary <= end:
            out.append(anniversary)
    return out


def _policy_boundaries(
    session: Session, region: str, leave_type_id: str, start: dt.date, end: dt.date
) -> list[dt.date]:
    """Dates inside the window where a policy row starts or stops applying."""
    rows = session.execute(
        select(OrgPolicy.effective_from, OrgPolicy.effective_to).where(
            OrgPolicy.region == region,
            OrgPolicy.leave_type_id == leave_type_id,
            OrgPolicy.effective_from <= end,
            or_(OrgPolicy.effective_to.is_(None), OrgPolicy.effective_to >= start),
        )
    ).all()

    boundaries = []
    for effective_from, effective_to in rows:
        if start < effective_from <= end:
            boundaries.append(effective_from)
        if effective_to is not None:
            day_after = effective_to + dt.timedelta(days=1)
            if start < day_after <= end:
                boundaries.append(day_after)
    return boundaries


def policy_segments(
    session: Session,
    employee: Employee,
    leave_type_id: str,
    start: dt.date,
    end: dt.date,
    cycle_days: int,
) -> list[Segment]:
    """Split [start, end] wherever the resolved policy could change."""
    cuts = {start}
    cuts.update(_anniversaries_in(employee.join_date, start, end))
    cuts.update(_policy_boundaries(session, employee.region, leave_type_id, start, end))
    ordered = sorted(c for c in cuts if start <= c <= end)

    segments = []
    for index, segment_start in enumerate(ordered):
        segment_end = (
            ordered[index + 1] - dt.timedelta(days=1) if index + 1 < len(ordered) else end
        )
        if segment_end < segment_start:
            continue

        days = (segment_end - segment_start).days + 1
        weight = Decimal(days) / Decimal(cycle_days)
        policy = resolve_policy(session, employee, leave_type_id, segment_start)
        entitlement = (
            Decimal("0") if policy is None
            else Decimal(policy.entitlement_days_per_year)
        )
        segments.append(Segment(
            start=segment_start,
            end=segment_end,
            policy=policy,
            days=days,
            weight=weight,
            contribution=entitlement * weight,
        ))
    return segments


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class ProrationResult:
    leave_type_id: str
    cycle_start: dt.date
    cycle_end: dt.date  # inclusive
    cycle_days: int
    eligible_start: dt.date | None
    eligible_end: dt.date | None
    eligible_days: int
    method: str
    rounding_dp: int
    full_year_entitlement: Decimal  # what a full year at this policy would give
    prorated_days: Decimal  # what this partial year actually earns
    segments: list[Segment] = field(default_factory=list)
    employment_fraction: Decimal = Decimal("1.000")

    @property
    def is_partial(self) -> bool:
        return self.eligible_days < self.cycle_days

    @property
    def fraction_of_year(self) -> Decimal:
        if not self.cycle_days:
            return Decimal("0")
        return Decimal(self.eligible_days) / Decimal(self.cycle_days)

    def explain(self) -> str:
        if self.eligible_days == 0:
            return (
                f"{self.leave_type_id}: not employed during "
                f"{self.cycle_start}–{self.cycle_end}; no entitlement."
            )
        if not self.is_partial and len(self.segments) <= 1:
            return (
                f"{self.leave_type_id}: full year of service; "
                f"{self.prorated_days} days."
            )
        parts = [
            f"{s.start}–{s.end} ({s.days}d @ {s.entitlement}/yr)"
            for s in self.segments
        ]
        return (
            f"{self.leave_type_id}: {self.eligible_days} of {self.cycle_days} days "
            f"eligible ({self.method} pro-ration) -> {self.prorated_days} days "
            f"[{'; '.join(parts)}]"
        )

    def to_dict(self) -> dict:
        return {
            "leave_type_id": self.leave_type_id,
            "cycle_start": self.cycle_start.isoformat(),
            "cycle_end": self.cycle_end.isoformat(),
            "cycle_days": self.cycle_days,
            "eligible_days": self.eligible_days,
            "fraction_of_year": str(self.fraction_of_year),
            "method": self.method,
            "rounding_dp": self.rounding_dp,
            "employment_fraction": str(self.employment_fraction),
            "full_year_entitlement": str(self.full_year_entitlement),
            "prorated_days": str(self.prorated_days),
            "is_partial": self.is_partial,
            "segments": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "days": s.days,
                    "entitlement_days_per_year": str(s.entitlement),
                    "contribution": str(s.contribution),
                }
                for s in self.segments
            ],
        }


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def prorate_entitlement(
    session: Session,
    employee: Employee,
    leave_type_id: str,
    cycle_start: dt.date,
    cycle_end: dt.date,
) -> ProrationResult:
    """Entitlement for one accrual cycle, adjusted for partial service.

    Args:
        cycle_start: first day of the accrual year.
        cycle_end: last day of it, INCLUSIVE.

    The `employment_fraction` scaling is already baked into every
    `resolve_policy()` answer, so it needs no separate step here — a 0.5 FTE
    joiner who worked half the year correctly gets a quarter of the full-time
    number, from the two effects composing naturally.
    """
    cycle_days = (cycle_end - cycle_start).days + 1
    window = eligible_period(employee, cycle_start, cycle_end)

    # Policy at the start of eligible service drives method/rounding choice.
    reference_date = window[0] if window else cycle_start
    reference = resolve_policy(session, employee, leave_type_id, reference_date)
    method = reference.proration_method if reference else "daily"
    rounding_dp = reference.rounding_dp if reference else 3
    full_year = Decimal(reference.entitlement_days_per_year) if reference else Decimal("0")

    if window is None:
        return ProrationResult(
            leave_type_id=leave_type_id,
            cycle_start=cycle_start, cycle_end=cycle_end, cycle_days=cycle_days,
            eligible_start=None, eligible_end=None, eligible_days=0,
            method=method, rounding_dp=rounding_dp,
            full_year_entitlement=full_year,
            prorated_days=Decimal("0"),
            employment_fraction=Decimal(employee.employment_fraction),
        )

    start, end = window
    days = (end - start).days + 1
    segments = policy_segments(session, employee, leave_type_id, start, end, cycle_days)

    if method == "none":
        # No pro-ration: a partial year still earns the full entitlement.
        prorated = full_year
    elif method == "monthly":
        months = _eligible_months(employee, cycle_start, cycle_end)
        # Weight each segment's entitlement by its share of eligible days, then
        # scale the whole thing to whole months — keeps mid-cycle policy
        # changes correct while honouring the coarser monthly convention.
        weighted = sum((s.contribution for s in segments), Decimal("0"))
        share = weighted / Decimal(days) * Decimal(cycle_days) if days else Decimal("0")
        prorated = share * Decimal(months) / Decimal(12)
    else:  # "daily"
        prorated = sum((s.contribution for s in segments), Decimal("0"))

    return ProrationResult(
        leave_type_id=leave_type_id,
        cycle_start=cycle_start, cycle_end=cycle_end, cycle_days=cycle_days,
        eligible_start=start, eligible_end=end, eligible_days=days,
        method=method, rounding_dp=rounding_dp,
        full_year_entitlement=full_year,
        prorated_days=round_days(prorated, rounding_dp),
        segments=segments,
        employment_fraction=Decimal(employee.employment_fraction),
    )


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
