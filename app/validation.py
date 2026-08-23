"""Insert-time validation for `org_policies`.

Two layers, deliberately:

  * The DATABASE is the hard floor. `ck_org_policies_compliance_note` and the
    `ex_org_policies_no_overlap` GiST exclusion constraint make a bad row
    impossible to write, even from a psql prompt or a future service that
    forgets to call this module.

  * THIS MODULE is the friendly layer. It runs the same two checks in Python
    before the INSERT so callers get a readable message naming the conflicting
    row, instead of a raw Postgres constraint-violation string.

Anything beyond these two checks is out of scope for Module 1 — there is no
runtime compliance engine anywhere in this system by design.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ACCRUAL_METHODS, OrgPolicy


class PolicyValidationError(ValueError):
    """Raised when a proposed org_policies row would violate a write-time rule."""


# ---------------------------------------------------------------------------
# Field-level checks
# ---------------------------------------------------------------------------
def validate_fields(policy: OrgPolicy) -> None:
    if not policy.compliance_note or not policy.compliance_note.strip():
        raise PolicyValidationError(
            "compliance_note is required and must be non-empty. State which law or "
            "internal minimum this entitlement satisfies "
            "(e.g. 'Meets OSH&WC Code 2020 Sec 32 minimum for TN')."
        )

    if policy.accrual_method not in ACCRUAL_METHODS:
        raise PolicyValidationError(
            f"accrual_method must be one of {ACCRUAL_METHODS}, got {policy.accrual_method!r}."
        )

    if policy.tenure_min_years is None or Decimal(policy.tenure_min_years) < 0:
        raise PolicyValidationError("tenure_min_years must be >= 0.")

    if policy.tenure_max_years is not None and Decimal(policy.tenure_max_years) <= Decimal(
        policy.tenure_min_years
    ):
        raise PolicyValidationError(
            f"tenure_max_years ({policy.tenure_max_years}) must be greater than "
            f"tenure_min_years ({policy.tenure_min_years}), or NULL for 'and above'."
        )

    if policy.effective_to is not None and policy.effective_to < policy.effective_from:
        raise PolicyValidationError("effective_to must be on or after effective_from.")


# ---------------------------------------------------------------------------
# Overlap check — mirrors ex_org_policies_no_overlap
# ---------------------------------------------------------------------------
def _ranges_overlap(
    a_lo: Decimal, a_hi: Decimal | None, b_lo: Decimal, b_hi: Decimal | None
) -> bool:
    """Half-open [lo, hi) overlap. None upper bound == unbounded."""
    if a_hi is not None and Decimal(a_hi) <= Decimal(b_lo):
        return False
    if b_hi is not None and Decimal(b_hi) <= Decimal(a_lo):
        return False
    return True


def _dates_overlap(
    a_lo: dt.date, a_hi: dt.date | None, b_lo: dt.date, b_hi: dt.date | None
) -> bool:
    """Closed [lo, hi] overlap. None upper bound == open-ended."""
    if a_hi is not None and a_hi < b_lo:
        return False
    if b_hi is not None and b_hi < a_lo:
        return False
    return True


def find_conflicting_policy(
    session: Session, policy: OrgPolicy, *, exclude_id: int | None = None
) -> OrgPolicy | None:
    """Return an existing row this policy would collide with, or None."""
    stmt = select(OrgPolicy).where(
        OrgPolicy.region == policy.region,
        OrgPolicy.leave_type_id == policy.leave_type_id,
    )
    for existing in session.scalars(stmt):
        if exclude_id is not None and existing.id == exclude_id:
            continue
        if existing.id is not None and existing.id == policy.id:
            continue
        if _ranges_overlap(
            policy.tenure_min_years,
            policy.tenure_max_years,
            existing.tenure_min_years,
            existing.tenure_max_years,
        ) and _dates_overlap(
            policy.effective_from,
            policy.effective_to,
            existing.effective_from,
            existing.effective_to,
        ):
            return existing
    return None


def validate_no_overlap(
    session: Session, policy: OrgPolicy, *, exclude_id: int | None = None
) -> None:
    conflict = find_conflicting_policy(session, policy, exclude_id=exclude_id)
    if conflict is not None:
        def _bracket(p: OrgPolicy) -> str:
            hi = "+" if p.tenure_max_years is None else f"-{p.tenure_max_years}"
            return f"{p.tenure_min_years}{hi} yrs"

        raise PolicyValidationError(
            f"Overlapping policy for region={policy.region!r} "
            f"leave_type={policy.leave_type_id!r}: proposed tenure bracket "
            f"{_bracket(policy)} (effective {policy.effective_from} → "
            f"{policy.effective_to or 'open'}) overlaps existing row id={conflict.id} "
            f"covering {_bracket(conflict)} (effective {conflict.effective_from} → "
            f"{conflict.effective_to or 'open'}). "
            "Close off the existing row's effective_to before adding a replacement."
        )


# ---------------------------------------------------------------------------
# The one entry point callers should use
# ---------------------------------------------------------------------------
def add_policy(session: Session, policy: OrgPolicy, *, flush: bool = True) -> OrgPolicy:
    """Validate and stage an org_policies row.

    Raises PolicyValidationError with a readable message on any violation.
    The DB constraints still stand behind this as the real guarantee.
    """
    validate_fields(policy)
    validate_no_overlap(session, policy)

    session.add(policy)
    if flush:
        try:
            session.flush()
        except IntegrityError as exc:  # DB caught something Python missed
            session.rollback()
            raise PolicyValidationError(
                f"Database rejected this policy row: {exc.orig}"
            ) from exc
    return policy


def update_policy(session: Session, policy: OrgPolicy, **changes) -> OrgPolicy:
    """Apply changes to an existing policy row, re-running both checks."""
    for key, value in changes.items():
        setattr(policy, key, value)
    validate_fields(policy)
    validate_no_overlap(session, policy, exclude_id=policy.id)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise PolicyValidationError(f"Database rejected this policy update: {exc.orig}") from exc
    return policy
