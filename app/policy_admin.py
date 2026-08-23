"""Annual policy lifecycle — the HR admin's yearly review.

Company policies change yearly. HR needs to open next year's policy set,
either **carrying this year forward unchanged** or **raising specific
numbers**, and do it without a developer and without breaking the history
that past accruals were calculated against.

`org_policies` was already versioned by `effective_from` / `effective_to`,
but nothing drove that lifecycle. This module is that driver.

## The rule that makes it safe

**A policy row is never edited once it has been used, and never deleted.**
Module 1's `ON DELETE RESTRICT` on `leave_ledger.policy_snapshot_id` already
makes deletion impossible; this module makes *editing* unnecessary. Rolling a
year forward:

    1. closes the outgoing row  (effective_to = last day of the old year)
    2. inserts a new row        (effective_from = first day of the new year)
    3. links them               (supersedes_id)

So a ledger entry written in 2025 still points at the 2025 row with the 2025
number, and "why did I get 1.25 days that month?" stays answerable forever —
even after HR raises the entitlement for 2026.

The exclusion constraint from Module 1 does the rest: the new row cannot
overlap the old one, because their effective windows are disjoint. An attempt
to open a year twice fails at the database, not at a code review.

## Continue vs change

`roll_forward_year(...)` with no `changes` is the "continue with the existing
policy" path — every number carries over, but a NEW row is created for the new
year anyway. That matters: it records that a human looked at 2026 and decided
it should match 2025, which is a different fact from nobody having looked.
`compliance_note` is re-stated on the new row for the same reason.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee, OrgPolicy
from app.util import format_days
from app.validation import PolicyValidationError, validate_fields, validate_no_overlap

__all__ = [
    "roll_forward_year",
    "preview_roll_forward",
    "leave_year_bounds",
    "policy_set",
    "policy_history",
    "RollForwardPlan",
    "PlannedChange",
    "PolicyAdminError",
]

# Fields HR may vary between years. Deliberately a closed list: region,
# leave_type_id and the tenure bracket define WHICH policy a row is, so
# changing them would silently create a different policy rather than a new
# version of this one.
MUTABLE_FIELDS = frozenset({
    "entitlement_days_per_year",
    "is_paid",
    "accrual_method",
    "carryover_max_days",
    "carryover_expiry",
    "max_consecutive_days",
    "min_notice_days",
    "rounding_dp",
    "proration_method",
    "leave_year_end",
    "enforcement",
    "allow_backdated",
    "allow_negative_balance",
    "compliance_note",
})

IDENTITY_FIELDS = ("region", "leave_type_id", "tenure_min_years", "tenure_max_years")

# Copied verbatim to the new year unless overridden.
_CARRIED_FIELDS = (
    "region", "legal_entity", "leave_type_id", "tenure_min_years", "tenure_max_years",
    "entitlement_days_per_year", "is_paid", "accrual_method", "carryover_max_days",
    "carryover_expiry", "max_consecutive_days", "min_notice_days", "rounding_dp",
    "proration_method", "leave_year_end", "enforcement", "allow_backdated",
    "allow_negative_balance", "compliance_note",
)


class PolicyAdminError(ValueError):
    """The requested policy-administration operation is not valid."""


# ---------------------------------------------------------------------------
# Leave-year arithmetic
# ---------------------------------------------------------------------------
def leave_year_bounds(leave_year_end: str | None, year: int) -> tuple[dt.date, dt.date]:
    """First and last day of the leave year LABELLED `year`.

    The label is the year the leave year *starts* in. With an India-style
    "03-31" year end, leave year 2026 runs 2026-04-01 to 2027-03-31. With a
    calendar "12-31" year end it is simply 2026-01-01 to 2026-12-31.

    `leave_year_end = None` (anniversary-aligned policies) has no organisational
    year to roll, so callers must supply one — see `roll_forward_year`.
    """
    if not leave_year_end:
        raise PolicyAdminError(
            "This policy has no leave_year_end, so it has no organisational "
            "leave year to roll forward. Set leave_year_end first, or pass "
            "an explicit default_leave_year_end."
        )
    month, day = (int(part) for part in leave_year_end.split("-"))
    end_in_year = dt.date(year, month, day)
    if (month, day) == (12, 31):
        return dt.date(year, 1, 1), end_in_year
    # Year end falls mid-calendar-year: the leave year starts the day after
    # the previous one ended and runs into the following calendar year.
    start = dt.date(year, month, day) + dt.timedelta(days=1)
    try:
        end = dt.date(year + 1, month, day)
    except ValueError:  # 29 Feb year end in a non-leap year
        end = dt.date(year + 1, month, day - 1)
    return start, end


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
def policy_set(
    session: Session, region: str, as_of: dt.date | None = None
) -> list[OrgPolicy]:
    """Every policy row in force for a region on a date."""
    as_of = as_of or dt.date.today()
    return list(session.scalars(
        select(OrgPolicy)
        .where(
            OrgPolicy.region == region,
            OrgPolicy.effective_from <= as_of,
            (OrgPolicy.effective_to.is_(None)) | (OrgPolicy.effective_to >= as_of),
        )
        .order_by(OrgPolicy.leave_type_id, OrgPolicy.tenure_min_years)
    ))


def policy_history(session: Session, policy: OrgPolicy) -> list[OrgPolicy]:
    """Walk `supersedes_id` backwards: this row and every version before it."""
    chain, current, seen = [], policy, set()
    while current is not None and current.id not in seen:
        chain.append(current)
        seen.add(current.id)
        current = (
            session.get(OrgPolicy, current.supersedes_id)
            if current.supersedes_id else None
        )
    return chain


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@dataclass
class PlannedChange:
    """What will happen to one policy row."""

    policy_id: int
    leave_type_id: str
    tenure_label: str
    changed: dict[str, tuple]  # field -> (old, new)

    @property
    def is_unchanged(self) -> bool:
        return not self.changed

    def describe(self) -> str:
        if self.is_unchanged:
            return f"{self.leave_type_id} {self.tenure_label}: carried forward unchanged"
        parts = ", ".join(f"{k} {old} -> {new}" for k, (old, new) in self.changed.items())
        return f"{self.leave_type_id} {self.tenure_label}: {parts}"


@dataclass
class RollForwardPlan:
    region: str
    from_year: int
    to_year: int
    old_year_end: dt.date
    new_year_start: dt.date
    new_year_end: dt.date
    changes: list[PlannedChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return sum(1 for c in self.changes if not c.is_unchanged)

    def describe(self) -> str:
        lines = [
            f"{self.region}: leave year {self.from_year} -> {self.to_year}",
            f"  closing {self.old_year_end}, opening {self.new_year_start}"
            f"–{self.new_year_end}",
            f"  {len(self.changes)} policy rows, {self.changed_count} changed",
        ]
        lines += [f"    {c.describe()}" for c in self.changes]
        lines += [f"  WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def _tenure_label(policy: OrgPolicy) -> str:
    hi = "+" if policy.tenure_max_years is None else f"-{policy.tenure_max_years:g}"
    return f"{policy.tenure_min_years:g}{hi}yr"


def _identity(policy: OrgPolicy) -> tuple:
    return tuple(getattr(policy, name) for name in IDENTITY_FIELDS)


def _resolve_changes(
    policy: OrgPolicy, changes: dict | None
) -> dict:
    """Pick out the overrides that apply to this row.

    `changes` may be keyed by:

        "*"                everything
        "EL"               every tenure band of one leave type
        ("EL", Decimal(5)) one band, as a tuple
        "EL@5"             the same band as a STRING

    The string form exists because the tuple cannot survive JSON, so it was
    unreachable from the API — which is why the UI could only ever change a
    whole leave type at once, and "raise EL for 5+ years only" turned into
    "raise EL for everybody".

    More specific keys win: "*" is applied first, then the leave type, then
    the band.
    """
    if not changes:
        return {}
    # `format_days`, not ':g'. `f"{Decimal('5.00'):g}"` is '5.00', not '5', so
    # the band key would never have matched what the UI sends.
    band = f"{policy.leave_type_id}@{format_days(policy.tenure_min_years)}"
    merged: dict = {}
    for key in (
        "*",
        policy.leave_type_id,
        (policy.leave_type_id, policy.tenure_min_years),
        band,
    ):
        candidate = changes.get(key)
        if candidate:
            merged.update(candidate)
    return merged


def preview_roll_forward(
    session: Session,
    region: str,
    from_year: int,
    changes: dict | None = None,
    *,
    default_leave_year_end: str | None = None,
) -> RollForwardPlan:
    """Work out what `roll_forward_year` would do, writing nothing.

    Always run this first in an admin UI — it is the diff HR signs off.
    """
    reference = session.scalars(
        select(OrgPolicy).where(OrgPolicy.region == region).limit(1)
    ).first()
    if reference is None:
        raise PolicyAdminError(f"No policies exist for region {region!r}.")

    year_end = default_leave_year_end or reference.leave_year_end
    if not year_end:
        raise PolicyAdminError(
            f"Region {region!r} has no leave_year_end on its policies, so there is "
            "no organisational leave year to roll. Pass default_leave_year_end "
            "(e.g. '03-31') to establish one."
        )

    old_start, old_end = leave_year_bounds(year_end, from_year)
    new_start, new_end = leave_year_bounds(year_end, from_year + 1)

    current = policy_set(session, region, as_of=old_end)
    if not current:
        raise PolicyAdminError(
            f"No policies in force for {region!r} on {old_end}; nothing to roll forward."
        )

    plan = RollForwardPlan(
        region=region, from_year=from_year, to_year=from_year + 1,
        old_year_end=old_end, new_year_start=new_start, new_year_end=new_end,
    )

    for policy in current:
        overrides = _resolve_changes(policy, changes)
        unknown = set(overrides) - MUTABLE_FIELDS
        if unknown:
            raise PolicyAdminError(
                f"Cannot change {sorted(unknown)} in a year roll-forward. "
                f"Only {sorted(MUTABLE_FIELDS)} may vary between years — the "
                "region, leave type and tenure bracket define which policy a "
                "row IS, so changing them would create a different policy "
                "rather than a new version of this one."
            )

        diff = {}
        for name, new_value in overrides.items():
            old_value = getattr(policy, name)
            if isinstance(old_value, Decimal) and new_value is not None:
                new_value = Decimal(str(new_value))
            if old_value != new_value:
                diff[name] = (old_value, new_value)

        plan.changes.append(PlannedChange(
            policy_id=policy.id,
            leave_type_id=policy.leave_type_id,
            tenure_label=_tenure_label(policy),
            changed=diff,
        ))

        if policy.effective_to is not None and policy.effective_to < old_end:
            plan.warnings.append(
                f"{policy.leave_type_id} {_tenure_label(policy)} already ends "
                f"{policy.effective_to}, before the leave year does."
            )

    if plan.changed_count == 0:
        plan.warnings.append(
            "No values change. A new row is still created for each policy, so the "
            "record shows HR reviewed this year and chose to continue unchanged."
        )
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def roll_forward_year(
    session: Session,
    region: str,
    from_year: int,
    changes: dict | None = None,
    *,
    actor: Employee | int | None = None,
    change_reason: str,
    default_leave_year_end: str | None = None,
    commit: bool = True,
) -> list[OrgPolicy]:
    """Open next year's policy set for a region.

    Args:
        region: e.g. "India-TamilNadu".
        from_year: the leave year being closed. Rolling 2025 opens 2026.
        changes: optional overrides, keyed by "*", by leave type, or by
            (leave_type, tenure_min_years). Omit entirely to carry the whole
            set forward unchanged.
        actor: the HR admin doing this. Must hold the `hr_admin` or `director`
            role — an employee cannot re-write their own entitlement.
        change_reason: required. Free text recorded on every new row.

    Returns the newly created rows.

    Raises `PolicyAdminError` if the actor is not authorised, the year is
    already open, or a change would alter a policy's identity rather than its
    values.
    """
    if not change_reason or not change_reason.strip():
        raise PolicyAdminError(
            "change_reason is required — a policy year must not open without a "
            "recorded reason, even when nothing changes."
        )

    actor_obj = session.get(Employee, actor) if isinstance(actor, int) else actor
    if actor_obj is None:
        raise PolicyAdminError(
            "actor is required: policy changes must be attributable to a person."
        )
    if actor_obj.role not in ("hr_admin", "director"):
        raise PolicyAdminError(
            f"{actor_obj.name} has role {actor_obj.role!r}. Only hr_admin or "
            "director may open a policy year — employees must not be able to "
            "edit their own entitlement."
        )

    plan = preview_roll_forward(
        session, region, from_year, changes,
        default_leave_year_end=default_leave_year_end,
    )

    # Refuse to open a year twice. The exclusion constraint would also catch
    # this, but a clear message beats a constraint-violation string.
    already = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == region,
            OrgPolicy.effective_from == plan.new_year_start,
        ).limit(1)
    ).first()
    if already is not None:
        raise PolicyAdminError(
            f"Leave year {plan.to_year} is already open for {region!r} "
            f"(policy id={already.id} starts {plan.new_year_start})."
        )

    current = policy_set(session, region, as_of=plan.old_year_end)
    by_identity = {_identity(p): p for p in current}
    created: list[OrgPolicy] = []

    for planned in plan.changes:
        old = session.get(OrgPolicy, planned.policy_id)
        overrides = _resolve_changes(old, changes)

        # 1. Close the outgoing row. Never edited beyond its end date, so
        #    every ledger entry that references it stays truthful.
        if old.effective_to is None or old.effective_to > plan.old_year_end:
            old.effective_to = plan.old_year_end

        # 2. Build the successor.
        values = {name: getattr(old, name) for name in _CARRIED_FIELDS}
        values.update(overrides)
        successor = OrgPolicy(
            **values,
            effective_from=plan.new_year_start,
            effective_to=None,
            policy_year=plan.to_year,
            supersedes_id=old.id,
            created_by_id=actor_obj.id,
            change_reason=change_reason.strip(),
        )

        # 3. Same write-time validation any manual insert faces.
        validate_fields(successor)
        session.add(successor)
        created.append(successor)

    session.flush()

    # Overlap is checked after the flush so the closed end dates are visible.
    for successor in created:
        try:
            validate_no_overlap(session, successor, exclude_id=successor.id)
        except PolicyValidationError as exc:
            raise PolicyAdminError(f"Roll-forward would create a conflict: {exc}") from exc

    if commit:
        session.commit()

    _ = by_identity  # retained for future identity-level diffing
    return created
