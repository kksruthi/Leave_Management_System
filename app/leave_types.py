"""Leave types as data, not as code.

Before this, "EL", "CL", "SL" and "Unpaid" were string literals scattered
across the policy seed, the classification substitution chain, the dashboard
colour map and the UI. Adding Bereavement Leave meant a deployment.

`leave_types` makes them rows. The split of responsibility is:

  * **`leave_types`** — what the type IS. Its code, its display name, its
    colour slot, whether it is the terminal unpaid fallback.
  * **`org_policies`** — what it is WORTH, per region and tenure bracket.

That split is why Texas has no Casual Leave without anything anywhere saying
"except Texas": CL exists as a type, and Texas simply has no CL policy row.
Creating a type therefore offers it to nobody until HR writes a policy for it,
which is the safe default — the alternative would be silently granting a new
entitlement to every employee the moment somebody typed a name.

## What is deliberately NOT allowed

**Deleting a type, or changing its code.** Both would orphan history: ledger
rows, leave requests and policies all reference the code, and a request from
2024 has to keep meaning what it meant in 2024. Retiring a type sets
`is_active = false`, which removes it from the request form while leaving
every past record readable. `retire_leave_type()` refuses while an unfinished
request still references it, because withdrawing a type out from under a
pending approval is how an approver ends up looking at a request they cannot
action.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Employee, LeaveRequest, LeaveType, OrgPolicy

log = logging.getLogger("leave_engine.leave_types")

__all__ = [
    "LeaveTypeError",
    "list_leave_types",
    "create_leave_type",
    "update_leave_type",
    "retire_leave_type",
    "reactivate_leave_type",
    "leave_type_usage",
    "COLOR_TOKENS",
]

#: The categorical slots in the validated data-viz palette. A type must wear
#: one of these rather than a free-form hex value, so a new type cannot break
#: the contrast and colour-vision-deficiency guarantees the palette was
#: checked against.
COLOR_TOKENS = ("series-1", "series-2", "series-3", "series-4", "series-5", "neutral")

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,15}$")


class LeaveTypeError(ValueError):
    """Invalid leave-type administration."""


def _require_admin(actor: Employee | None) -> Employee:
    if actor is None:
        raise LeaveTypeError("Leave-type changes must be attributable to a person.")
    if actor.role not in ("hr_admin", "director"):
        raise LeaveTypeError(
            f"{actor.name} has role {actor.role!r}. Only hr_admin or director "
            "may change the leave types the organisation offers."
        )
    return actor


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def list_leave_types(session: Session, *, include_inactive: bool = False) -> list[LeaveType]:
    stmt = select(LeaveType).order_by(LeaveType.display_order, LeaveType.code)
    if not include_inactive:
        stmt = stmt.where(LeaveType.is_active.is_(True))
    return list(session.scalars(stmt))


def leave_type_usage(session: Session, code: str) -> dict:
    """How embedded a type is — shown before anyone retires it."""
    policies = session.scalar(
        select(func.count()).select_from(OrgPolicy).where(OrgPolicy.leave_type_id == code)
    ) or 0
    requests = session.scalar(
        select(func.count()).select_from(LeaveRequest).where(LeaveRequest.leave_type_id == code)
    ) or 0
    open_requests = session.scalar(
        select(func.count()).select_from(LeaveRequest).where(
            LeaveRequest.leave_type_id == code, LeaveRequest.status == "pending"
        )
    ) or 0
    regions = sorted(session.scalars(
        select(OrgPolicy.region).where(OrgPolicy.leave_type_id == code).distinct()
    ))
    return {
        "code": code,
        "policy_count": policies,
        "region_count": len(regions),
        "regions": regions,
        "request_count": requests,
        "open_request_count": open_requests,
        "offered": bool(policies),
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def create_leave_type(
    session: Session,
    code: str,
    name: str,
    *,
    actor: Employee | None = None,
    description: str | None = None,
    color_token: str = "series-4",
    display_order: int | None = None,
    commit: bool = False,
) -> LeaveType:
    """Add a type the organisation offers. It grants nothing until a policy exists."""
    _require_admin(actor)

    code = (code or "").strip().upper()
    if not _CODE_RE.match(code):
        raise LeaveTypeError(
            f"{code!r} is not a valid code. Use 2–16 characters, starting with a "
            "letter: uppercase letters, digits and underscores only (e.g. 'BL', "
            "'MAT_LEAVE'). The code is permanent because history references it."
        )
    if not (name or "").strip():
        raise LeaveTypeError("A leave type needs a display name.")
    if color_token not in COLOR_TOKENS:
        raise LeaveTypeError(
            f"color_token {color_token!r} is not in the validated palette. "
            f"Choose one of: {', '.join(COLOR_TOKENS)}."
        )

    existing = session.scalars(select(LeaveType).where(LeaveType.code == code)).first()
    if existing is not None:
        raise LeaveTypeError(
            f"{code!r} already exists"
            + ("." if existing.is_active else " but is retired — reactivate it instead.")
        )

    if display_order is None:
        display_order = (session.scalar(select(func.max(LeaveType.display_order))) or 0) + 10

    row = LeaveType(
        code=code,
        name=name.strip(),
        description=(description or "").strip() or None,
        color_token=color_token,
        display_order=display_order,
        is_active=True,
        created_by_id=actor.id,
    )
    session.add(row)
    session.flush()
    log.info("leave type %s (%s) created by %s", code, row.name, actor.name)
    if commit:
        session.commit()
    return row


_EDITABLE = ("name", "description", "color_token", "display_order")


def update_leave_type(
    session: Session, code: str, *, actor: Employee | None = None,
    commit: bool = False, **changes,
) -> LeaveType:
    """Change a type's presentation. The code is not editable, ever."""
    _require_admin(actor)
    row = _get(session, code)

    if "code" in changes:
        raise LeaveTypeError(
            "A leave type's code cannot be changed — ledger rows, requests and "
            "policies all reference it, and a 2024 request has to keep meaning "
            "what it meant in 2024. Retire this type and create a new one."
        )
    unknown = set(changes) - set(_EDITABLE)
    if unknown:
        raise LeaveTypeError(
            f"Cannot change {', '.join(sorted(unknown))}. Editable: {', '.join(_EDITABLE)}."
        )
    if changes.get("color_token") and changes["color_token"] not in COLOR_TOKENS:
        raise LeaveTypeError(
            f"color_token must be one of: {', '.join(COLOR_TOKENS)}."
        )
    if "name" in changes and not (changes["name"] or "").strip():
        raise LeaveTypeError("A leave type needs a display name.")

    for key, value in changes.items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    session.flush()
    if commit:
        session.commit()
    return row


def retire_leave_type(
    session: Session, code: str, *, actor: Employee | None = None, commit: bool = False
) -> LeaveType:
    """Stop offering a type. History keeps it; the request form loses it."""
    _require_admin(actor)
    row = _get(session, code)

    if row.is_unpaid_fallback:
        raise LeaveTypeError(
            f"{code} is the unpaid fallback every substitution chain ends at. "
            "Retiring it would leave requests that exceed a balance with nowhere "
            "to go."
        )
    usage = leave_type_usage(session, code)
    if usage["open_request_count"]:
        raise LeaveTypeError(
            f"{usage['open_request_count']} request(s) of type {code} are still "
            "awaiting a decision. Clear those first — an approver cannot action a "
            "request for a type that no longer exists."
        )

    row.is_active = False
    session.flush()
    log.info("leave type %s retired by %s", code, actor.name)
    if commit:
        session.commit()
    return row


def reactivate_leave_type(
    session: Session, code: str, *, actor: Employee | None = None, commit: bool = False
) -> LeaveType:
    _require_admin(actor)
    row = _get(session, code)
    row.is_active = True
    session.flush()
    if commit:
        session.commit()
    return row


def _get(session: Session, code: str) -> LeaveType:
    row = session.scalars(
        select(LeaveType).where(LeaveType.code == (code or "").strip().upper())
    ).first()
    if row is None:
        raise LeaveTypeError(f"No leave type {code!r}.")
    return row


def to_dict(session: Session, row: LeaveType) -> dict:
    usage = leave_type_usage(session, row.code)
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "description": row.description,
        "color_token": row.color_token,
        "display_order": row.display_order,
        "is_active": row.is_active,
        "is_unpaid_fallback": row.is_unpaid_fallback,
        **{k: v for k, v in usage.items() if k != "code"},
    }
