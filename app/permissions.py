"""Explicit permissions, rather than `if role == "HR"` scattered everywhere.

Roles are a shorthand for a **set of permissions**. Code asks for the
permission it needs; the role→permission map is the only place that knows
which roles have it. Adding a "Team Lead" who can approve but not view
balances is then one line here, not a search for every `role ==` in the
codebase.

Two rules this file exists to make unavoidable:

  * **Every endpoint names the permission it requires.** No endpoint decides
    for itself what a role means.
  * **A permission is necessary but not sufficient.** `view_team_leave` says
    a manager may see *their team*; it does not say which team. Row-level
    scoping stays in `app/authorization.py`, which knows the reporting line.
    Holding `approve_team_leave` and being *this* employee's manager are two
    separate checks, and both must pass.
"""

from __future__ import annotations

__all__ = [
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "ADMIN_ROLES",
    "permissions_for",
    "has_permission",
    "require_permission",
    "PermissionDenied",
]


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------
PERMISSIONS: dict[str, str] = {
    # --- self ------------------------------------------------------------
    "view_own_leave": "See your own balances, history and requests",
    "create_leave_request": "Submit a leave request for yourself",
    "cancel_own_request": "Cancel your own request while it is still pending",
    # --- team ------------------------------------------------------------
    "view_team_leave": "See balances and leave for your direct reporting line",
    "approve_team_leave": "Approve a request at a tier you own",
    "reject_team_leave": "Reject a request at a tier you own",
    # --- organisation -----------------------------------------------------
    "view_all_leave": "See any employee's leave data",
    "manage_employees": "Create and edit employee records",
    "manage_policies": "Author and roll forward leave policies",
    "manage_holidays": "Maintain the holiday calendar",
    "manage_exceptions": "Grant per-employee entitlement exceptions",
    "view_audit": "Read the audit trail for any request",
    "correct_ledger": "Append a correcting entry to the ledger",
    "view_reports": "Organisation-wide reporting",
}

# ---------------------------------------------------------------------------
# Who holds what
# ---------------------------------------------------------------------------
_EMPLOYEE = {
    "view_own_leave",
    "create_leave_request",
    "cancel_own_request",
}

_MANAGER = _EMPLOYEE | {
    "view_team_leave",
    "approve_team_leave",
    "reject_team_leave",
}

# HR administers the leave system. Note what is NOT here: HR does not get a
# blanket "edit anything" permission, and `correct_ledger` is a distinct grant
# because appending a correction to an append-only audit trail deserves to be
# named separately from ordinary employee administration.
_HR_ADMIN = _MANAGER | {
    "view_all_leave",
    "manage_employees",
    "manage_policies",
    "manage_holidays",
    "manage_exceptions",
    "view_audit",
    "correct_ledger",
    "view_reports",
}

# A director sees everything HR does and signs off HR's own requests. They are
# deliberately NOT given `manage_policies` beyond what HR has — the point of
# the role here is approval authority at the top of the chain, not a second
# administrative surface.
_DIRECTOR = _HR_ADMIN

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "employee": frozenset(_EMPLOYEE),
    "manager": frozenset(_MANAGER),
    "hr_admin": frozenset(_HR_ADMIN),
    "director": frozenset(_DIRECTOR),
}

# Roles that appear under "Administration" in the UI.
ADMIN_ROLES = ("hr_admin", "director")


class PermissionDenied(PermissionError):
    """The actor's role does not carry the required permission."""


def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: str, permission: str) -> bool:
    if permission not in PERMISSIONS:
        # A typo'd permission must never silently pass. Failing loudly here
        # turns "the check quietly did nothing" into an immediate error.
        raise KeyError(
            f"Unknown permission {permission!r}. Declare it in PERMISSIONS first. "
            f"Known: {', '.join(sorted(PERMISSIONS))}"
        )
    return permission in permissions_for(role)


def require_permission(role: str, permission: str) -> None:
    if not has_permission(role, permission):
        raise PermissionDenied(
            f"Role {role!r} does not have permission {permission!r} "
            f"({PERMISSIONS[permission]})."
        )
