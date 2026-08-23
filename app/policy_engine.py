"""Module 2 — the Policy Engine.

ONE function answers the only question the rest of the system ever asks:

    given an employee's REGION + TENURE + LEAVE TYPE, on a given date,
    what are they entitled to?

Every downstream module (accrual, live balance, paid/unpaid classification,
approval routing) calls `resolve_policy()`. None of them re-implement policy
logic, and none of them read `org_policies` directly.

Precedence is exactly two levels, no more:

    1. employee_exceptions   (per-person override — wins outright)
    2. org_policies          (regional tenure bracket)

There is deliberately NO third statutory-minimum layer and no compliance
cross-check at read time. Each `org_policies` row is already the compliant
number for its region, established at data-authoring time via Module 1's
required `compliance_note`.

This module is a pure read. No writes, no side effects, no HTTP, no scheduler
coupling — import it directly:

    from app.policy_engine import resolve_policy, years_between
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Employee, EmployeeException, OrgPolicy

__all__ = [
    "ResolvedPolicy",
    "resolve_policy",
    "resolve_policy_or_raise",
    "PolicyNotFoundError",
    "years_between",
    "tenure_bracket_boundaries",
]


# ---------------------------------------------------------------------------
# Tenure
# ---------------------------------------------------------------------------
def years_between(join_date: dt.date, as_of_date: dt.date) -> int:
    """Full elapsed years between two dates — the employee's tenure.

    Anniversary-based, not 365-day-based, so it matches how a human reads a
    service date:

        joined 2025-04-01, as_of 2026-03-15  -> 0   (anniversary not reached)
        joined 2025-04-01, as_of 2026-04-01  -> 1   (anniversary reached)

    Returns 0 for a future join date rather than a negative number, so a
    mis-keyed start date degrades to "newest bracket" instead of matching
    nothing at all.

    Leap-day note: someone who joined 29 Feb reaches their anniversary on
    28 Feb in non-leap years, which is the convention Indian and US payroll
    both use.
    """
    if as_of_date < join_date:
        return 0

    years = as_of_date.year - join_date.year
    # Not yet reached this year's anniversary?
    if (as_of_date.month, as_of_date.day) < (join_date.month, join_date.day):
        # Guard the 29 Feb case: 2024-02-29 -> 2025-02-28 counts as a full year.
        if not (
            join_date.month == 2
            and join_date.day == 29
            and as_of_date.month == 2
            and as_of_date.day == 28
        ):
            years -= 1
    return max(years, 0)


def tenure_bracket_boundaries(join_date: dt.date, bracket_max_years) -> dt.date | None:
    """Date on which this employee leaves the given tenure bracket.

    Convenience for dashboards ("your entitlement rises to 18 days on
    2026-04-01"). Returns None for an open-ended bracket.
    """
    if bracket_max_years is None:
        return None
    whole = int(Decimal(bracket_max_years))
    try:
        return join_date.replace(year=join_date.year + whole)
    except ValueError:  # 29 Feb into a non-leap year
        return join_date.replace(year=join_date.year + whole, day=28)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ResolvedPolicy:
    """What the engine resolved, normalised across both precedence levels.

    Callers read the same fields regardless of whether an exception or a
    regional bracket won — `source` tells them which, for display and audit.
    """

    # --- what the employee gets -------------------------------------------
    # Already scaled by the employee's employment_fraction — a 0.5 FTE
    # employee on an 18-day policy sees 9. Callers never multiply again.
    entitlement_days_per_year: Decimal
    is_paid: bool
    accrual_method: str  # "monthly" | "annual_lump" | "none"

    # --- request-time rules -----------------------------------------------
    carryover_max_days: Decimal
    carryover_expiry: str | None
    max_consecutive_days: int | None
    min_notice_days: int

    # --- provenance --------------------------------------------------------
    source: str  # "exception" | "org_policy"
    region: str
    leave_type_id: str
    tenure_years: int
    as_of_date: dt.date
    # FK target for leave_ledger.policy_snapshot_id — makes every accrual
    # explainable later. Populated even when an exception won, so the ledger
    # still records which regional row was in force.
    policy_snapshot_id: int | None = None
    exception_id: int | None = None
    exception_reason: str | None = None
    compliance_note: str | None = None
    effective_from: dt.date | None = None
    effective_to: dt.date | None = None

    # --- working pattern and arithmetic rules ------------------------------
    # The unscaled policy number, kept so a payslip can show "18 days/yr at
    # 0.5 FTE = 9" rather than an unexplained 9.
    full_time_entitlement_days_per_year: Decimal = Decimal("0")
    employment_fraction: Decimal = Decimal("1.000")
    # Rounding and pro-ration are POLICY, carried on the row, not constants in
    # the accrual code. See app/proration.py.
    rounding_dp: int = 3
    proration_method: str = "daily"
    # "MM-DD" end of the organisation's leave year, or None for an
    # anniversary-aligned cycle. See app/proration.accrual_cycle().
    leave_year_end: str | None = None
    # --- request-time enforcement, carried from the policy row -------------
    # "block" (default) rejects a request breaching min_notice_days or
    # max_consecutive_days; "warn" lets it through with a note.
    enforcement: str = "block"
    allow_backdated: bool = False
    allow_negative_balance: bool = False

    @property
    def is_part_time(self) -> bool:
        return self.employment_fraction < Decimal("1")

    @property
    def monthly_accrual(self) -> Decimal:
        """Days added per monthly accrual run. Zero for non-monthly types.

        Module 3 owns the actual ledger write; this is just the arithmetic so
        the number is defined in exactly one place. Rounded to the policy's
        own `rounding_dp`, not a hardcoded constant.
        """
        if self.accrual_method != "monthly":
            return Decimal("0")
        quantum = Decimal(1).scaleb(-int(self.rounding_dp))
        return (self.entitlement_days_per_year / Decimal(12)).quantize(
            quantum, rounding=ROUND_HALF_UP
        )

    def explain(self) -> str:
        """One-line human explanation — for dashboards and audit trails."""
        if self.is_part_time:
            return (
                f"{self.leave_type_id}: {self.entitlement_days_per_year} days/yr "
                f"({self.full_time_entitlement_days_per_year} full-time x "
                f"{self.employment_fraction} FTE, {self.region}, "
                f"{self.tenure_years}-year tenure)"
            )
        if self.source == "exception":
            return (
                f"{self.leave_type_id}: {self.entitlement_days_per_year} days/yr from a "
                f"personal exception (id={self.exception_id})"
                + (f" — {self.exception_reason}" if self.exception_reason else "")
            )
        return (
            f"{self.leave_type_id}: {self.entitlement_days_per_year} days/yr from the "
            f"{self.region} policy for {self.tenure_years}-year tenure "
            f"(policy id={self.policy_snapshot_id})"
        )


class PolicyNotFoundError(LookupError):
    """Raised only by `resolve_policy_or_raise`. The default path returns None."""


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------
def _scale_for_fte(full_time_days: Decimal, fraction: Decimal, rounding_dp: int) -> Decimal:
    """Scale a full-time entitlement to the employee's working pattern.

    Part-time is modelled as a single fraction on the employee rather than a
    parallel set of part-time policy rows: one number, applied uniformly, and
    HR cannot forget to author the part-time variant of a new region.

    A 0.6 FTE employee on 18 days/yr gets 10.8, rounded to the policy's own
    `rounding_dp`. Full-time employees (fraction == 1) pass through untouched,
    so this is a no-op for everyone already in the system.
    """
    if fraction == Decimal("1"):
        return Decimal(full_time_days)
    quantum = Decimal(1).scaleb(-int(rounding_dp))
    return (Decimal(full_time_days) * Decimal(fraction)).quantize(
        quantum, rounding=ROUND_HALF_UP
    )


def _active_on(column_from, column_to, as_of_date: dt.date):
    """effective_from <= as_of_date <= effective_to, treating NULL 'to' as open."""
    return (column_from <= as_of_date) & or_(column_to.is_(None), column_to >= as_of_date)


def _find_exception(
    session: Session, employee_id: int, leave_type_id: str, as_of_date: dt.date
) -> EmployeeException | None:
    stmt = (
        select(EmployeeException)
        .where(
            EmployeeException.employee_id == employee_id,
            EmployeeException.leave_type_id == leave_type_id,
            _active_on(
                EmployeeException.effective_from, EmployeeException.effective_to, as_of_date
            ),
        )
        # Defensive: if two exceptions somehow overlap, the most recently
        # authored one wins. Module 1 does not constrain this table.
        .order_by(EmployeeException.effective_from.desc(), EmployeeException.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _find_regional_policy(
    session: Session, region: str, leave_type_id: str, tenure_years: int, as_of_date: dt.date
) -> OrgPolicy | None:
    stmt = (
        select(OrgPolicy)
        .where(
            OrgPolicy.region == region,
            OrgPolicy.leave_type_id == leave_type_id,
            OrgPolicy.tenure_min_years <= tenure_years,
            or_(
                OrgPolicy.tenure_max_years.is_(None),
                OrgPolicy.tenure_max_years > tenure_years,
            ),
            _active_on(OrgPolicy.effective_from, OrgPolicy.effective_to, as_of_date),
        )
        # Module 1's exclusion constraint means at most one row can match, so
        # this ordering is belt-and-braces. Kept exactly as the spec writes it.
        .order_by(OrgPolicy.tenure_min_years.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def resolve_policy(
    session: Session,
    employee: Employee | int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
) -> ResolvedPolicy | None:
    """Resolve an employee's entitlement for one leave type on one date.

    Args:
        session: an open SQLAlchemy session (read-only use).
        employee: an `Employee` instance, or an employee id.
        leave_type_id: "EL", "CL", "SL", "Unpaid", ...
        as_of_date: date or ISO string. Defaults to today.

    Returns:
        A `ResolvedPolicy`, or **None** when nothing matches — a gap in the
        tenure brackets, an unknown leave type, a date outside every effective
        window, or an unseeded region. Returning None rather than raising is
        deliberate: the caller decides whether that is fatal. Use
        `resolve_policy_or_raise` if you want the strict variant.
    """
    as_of_date = _coerce_date(as_of_date)

    if isinstance(employee, int):
        employee_obj = session.get(Employee, employee)
        if employee_obj is None:
            return None
    else:
        employee_obj = employee

    tenure_years = years_between(employee_obj.join_date, as_of_date)
    region = employee_obj.region
    fraction = Decimal(employee_obj.employment_fraction or 1)

    # The regional row is looked up regardless, because even when an exception
    # wins it supplies only an entitlement number — see the note below.
    policy = _find_regional_policy(session, region, leave_type_id, tenure_years, as_of_date)

    # ---- level 1: personal exception wins outright -----------------------
    exception = _find_exception(session, employee_obj.id, leave_type_id, as_of_date)
    if exception is not None:
        # `employee_exceptions` carries an entitlement and nothing else — no
        # is_paid, no accrual_method, no carryover rules. The exception's
        # NUMBER wins outright, exactly as the spec requires; the operational
        # mechanics it cannot express are inherited from the regional row so
        # that Module 3 still knows how to accrue it. If no regional row exists
        # at all, safe defaults apply (paid, monthly, no carryover).
        exception_full_time = Decimal(exception.entitlement_days_per_year)
        return ResolvedPolicy(
            entitlement_days_per_year=_scale_for_fte(
                exception_full_time, fraction, policy.rounding_dp if policy else 3
            ),
            is_paid=policy.is_paid if policy else True,
            accrual_method=policy.accrual_method if policy else "monthly",
            carryover_max_days=Decimal(policy.carryover_max_days) if policy else Decimal("0"),
            carryover_expiry=policy.carryover_expiry if policy else None,
            max_consecutive_days=policy.max_consecutive_days if policy else None,
            min_notice_days=policy.min_notice_days if policy else 0,
            source="exception",
            region=region,
            leave_type_id=leave_type_id,
            tenure_years=tenure_years,
            as_of_date=as_of_date,
            policy_snapshot_id=policy.id if policy else None,
            exception_id=exception.id,
            exception_reason=exception.reason,
            compliance_note=policy.compliance_note if policy else None,
            effective_from=exception.effective_from,
            effective_to=exception.effective_to,
            full_time_entitlement_days_per_year=exception_full_time,
            employment_fraction=fraction,
            rounding_dp=policy.rounding_dp if policy else 3,
            proration_method=policy.proration_method if policy else "daily",
            leave_year_end=policy.leave_year_end if policy else None,
            enforcement=policy.enforcement if policy else "block",
            allow_backdated=policy.allow_backdated if policy else False,
            allow_negative_balance=policy.allow_negative_balance if policy else False,
        )

    # ---- level 2: regional tenure bracket --------------------------------
    if policy is None:
        return None

    full_time = Decimal(policy.entitlement_days_per_year)
    return ResolvedPolicy(
        entitlement_days_per_year=_scale_for_fte(full_time, fraction, policy.rounding_dp),
        is_paid=policy.is_paid,
        accrual_method=policy.accrual_method,
        carryover_max_days=Decimal(policy.carryover_max_days),
        carryover_expiry=policy.carryover_expiry,
        max_consecutive_days=policy.max_consecutive_days,
        min_notice_days=policy.min_notice_days,
        source="org_policy",
        region=region,
        leave_type_id=leave_type_id,
        tenure_years=tenure_years,
        as_of_date=as_of_date,
        policy_snapshot_id=policy.id,
        compliance_note=policy.compliance_note,
        effective_from=policy.effective_from,
        effective_to=policy.effective_to,
        full_time_entitlement_days_per_year=full_time,
        employment_fraction=fraction,
        rounding_dp=policy.rounding_dp,
        proration_method=policy.proration_method,
        leave_year_end=policy.leave_year_end,
        enforcement=policy.enforcement,
        allow_backdated=policy.allow_backdated,
        allow_negative_balance=policy.allow_negative_balance,
    )


def resolve_policy_or_raise(
    session: Session,
    employee: Employee | int,
    leave_type_id: str,
    as_of_date: dt.date | str | None = None,
) -> ResolvedPolicy:
    """Strict variant for callers that treat a missing policy as a bug."""
    resolved = resolve_policy(session, employee, leave_type_id, as_of_date)
    if resolved is None:
        who = employee if isinstance(employee, int) else employee.name
        raise PolicyNotFoundError(
            f"No policy found for employee={who!r} leave_type={leave_type_id!r} "
            f"as_of={_coerce_date(as_of_date)}. Check that org_policies covers this "
            "region, leave type, tenure bracket and date window."
        )
    return resolved


def _coerce_date(value: dt.date | str | None) -> dt.date:
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


# ---------------------------------------------------------------------------
# Demo — the region-driven proof point, side by side
# ---------------------------------------------------------------------------
def _demo() -> None:  # pragma: no cover - illustrative CLI
    from app.db import SessionLocal

    dates = ["2025-06-01", "2026-04-01"]
    with SessionLocal() as session:
        people = session.scalars(
            select(Employee).where(Employee.name.in_(["Priya", "Raj"])).order_by(Employee.name)
        ).all()

        print(f"\n{'Employee':<8} {'Region':<17} {'As of':<12} {'Tenure':<8} "
              f"{'EL days/yr':<11} {'Per month':<10} Source")
        print("-" * 82)
        for person in people:
            for date in dates:
                r = resolve_policy(session, person, "EL", date)
                if r is None:
                    print(f"{person.name:<8} {person.region:<17} {date:<12} — no policy found")
                    continue
                print(f"{person.name:<8} {person.region:<17} {date:<12} "
                      f"{r.tenure_years} yr{'':<4} {r.entitlement_days_per_year:<11} "
                      f"{r.monthly_accrual:<10} {r.source}")
        print("\nSame function, same code path. Only the region row differs.\n")


if __name__ == "__main__":  # pragma: no cover
    _demo()
