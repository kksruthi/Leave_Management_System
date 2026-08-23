"""Who may see what, and who may approve what.

Closes findings 10 (dashboard authorization pushed entirely to the API layer)
and 29 (approver authorization not enforced).

## Why this is not "the API's problem"

The original note said the UI must stop an employee reading a colleague's
balance. That is true but insufficient: the dashboard and approval functions
are a *library*, and Module 7's pipeline, a scheduled job, a CLI and a future
HTTP layer all call them directly. A rule enforced only in the outermost layer
is a rule enforced nowhere, because the next caller in won't know about it.

So the check lives next to the data, and every entry point takes an explicit
`viewer` / `actor`. Passing `None` means "trusted internal caller" — used by
accrual jobs, which act for the system rather than a person — and that is a
deliberate, greppable decision rather than an accident.

## The model

  * **employee**  — own records only.
  * **manager**   — own records, plus anyone in their reporting line
                    (transitively, so a skip-level manager works).
  * **hr_admin**  — everyone. HR administers leave; that is the job.
  * **director**  — everyone.

Approval authority is deliberately stricter than the role name suggests: being
*a* manager is not enough to approve *this* request. The tier-1 approver must
be that employee's actual manager (or their delegate). Otherwise any manager
in the company could sign off any request, which is the finding.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApprovalDelegation, ApprovalStep, Employee, LeaveRequest

__all__ = [
    "AuthorizationError",
    "can_view_employee",
    "require_view_employee",
    "can_approve_step",
    "require_approve_step",
    "resolve_assigned_approver",
    "is_top_of_chain",
    "active_delegate_for",
    "management_chain",
]

_ALL_ACCESS_ROLES = ("hr_admin", "director")


class AuthorizationError(PermissionError):
    """The actor is not permitted to perform this action."""


# ---------------------------------------------------------------------------
# Reporting line
# ---------------------------------------------------------------------------
def management_chain(session: Session, employee: Employee, max_depth: int = 10) -> list[Employee]:
    """Everyone above this employee, nearest first.

    Depth-capped so a cyclic `manager_id` (which the schema permits beyond the
    self-reference check) cannot hang a request.
    """
    chain, current, seen = [], employee, {employee.id}
    for _ in range(max_depth):
        if current.manager_id is None or current.manager_id in seen:
            break
        manager = session.get(Employee, current.manager_id)
        if manager is None:
            break
        chain.append(manager)
        seen.add(manager.id)
        current = manager
    return chain


# ---------------------------------------------------------------------------
# Viewing
# ---------------------------------------------------------------------------
def can_view_employee(session: Session, viewer: Employee | None, subject: Employee) -> bool:
    """May `viewer` see `subject`'s leave data? `None` = trusted internal caller."""
    if viewer is None:
        return True
    if viewer.id == subject.id:
        return True
    if viewer.role in _ALL_ACCESS_ROLES:
        return True
    if viewer.role == "manager":
        return any(m.id == viewer.id for m in management_chain(session, subject))
    return False


def require_view_employee(session: Session, viewer: Employee | None, subject: Employee) -> None:
    if not can_view_employee(session, viewer, subject):
        raise AuthorizationError(
            f"{viewer.name} (role {viewer.role!r}) may not view leave data for "
            f"{subject.name}. Employees see only their own records; managers see "
            f"their reporting line; hr_admin and director see everyone."
        )


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------
def active_delegate_for(
    session: Session, approver_id: int, on_date: dt.date | None = None
) -> Employee | None:
    """Who is covering for this approver today, if anyone."""
    on_date = on_date or dt.date.today()
    delegation = session.scalars(
        select(ApprovalDelegation)
        .where(
            ApprovalDelegation.delegator_id == approver_id,
            ApprovalDelegation.is_active.is_(True),
            ApprovalDelegation.from_date <= on_date,
            ApprovalDelegation.to_date >= on_date,
        )
        .order_by(ApprovalDelegation.from_date.desc())
        .limit(1)
    ).first()
    return session.get(Employee, delegation.delegate_id) if delegation else None


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------
# Who signs off for someone with no manager above them. An employee's leave
# goes to their manager; a manager's goes to HR; HR's goes to a director; a
# director is the top of the chain and self-approves.
_ESCALATION_LADDER = {
    "employee": "manager",
    "manager": "hr_admin",
    "hr_admin": "director",
    "director": None,
}


def _first_active_holder(
    session: Session, role: str, exclude_id: int | None = None,
    region: str | None = None,
) -> Employee | None:
    """The person a role-based tier lands on.

    Region matters. Once an organisation has more than one HR admin, picking
    the lowest id sends a Chennai request to a Texas HR admin who does not
    know Tamil Nadu's leave rules, holidays or statutory minimums. So a holder
    in the requester's own region wins; falling back to any holder only when
    the region has none, because an unassigned step is worse than a distant
    approver.
    """
    def _pick(**extra) -> Employee | None:
        stmt = select(Employee).where(
            Employee.role == role, Employee.status == "active"
        )
        if exclude_id is not None:
            stmt = stmt.where(Employee.id != exclude_id)
        if extra.get("region"):
            stmt = stmt.where(Employee.region == extra["region"])
        return session.scalars(stmt.order_by(Employee.id).limit(1)).first()

    return (_pick(region=region) if region else None) or _pick()


def resolve_assigned_approver(
    session: Session, request: LeaveRequest, role: str
) -> Employee | None:
    """The specific person expected to act on a tier.

    The `manager` tier means "the requester's line authority", which is not
    always someone holding the manager role:

      * an employee -> their own manager;
      * a manager (who has no manager above them) -> HR;
      * an HR admin -> a director;
      * a director -> nobody, because they are the top of the chain. The
        chain builder detects that and self-approves, rather than leaving a
        step assigned to no one.

    HR and director tiers added by duration rules resolve to the first active
    holder of that role, excluding the requester — a real deployment would use
    a queue or round-robin, but naming *somebody* is what turns a role into an
    actionable inbox item rather than an orphaned row.
    """
    employee = session.get(Employee, request.employee_id)
    if employee is None:
        return None

    if role == "manager":
        if employee.manager_id is not None:
            manager = session.get(Employee, employee.manager_id)
            if manager is not None and manager.status == "active":
                return manager
        # No line manager: walk the role ladder instead.
        target = _ESCALATION_LADDER.get(employee.role)
        while target is not None:
            holder = _first_active_holder(
                session, target, exclude_id=employee.id, region=employee.region
            )
            if holder is not None:
                return holder
            target = _ESCALATION_LADDER.get(target)
        return None

    return _first_active_holder(
        session, role, exclude_id=employee.id, region=employee.region
    )


def is_top_of_chain(session: Session, employee: Employee) -> bool:
    """True when nobody in the organisation outranks this person.

    Used to decide whether a request can be self-approved. A director with no
    manager above them is the top; anyone else is not.
    """
    if employee.manager_id is not None:
        return False
    target = _ESCALATION_LADDER.get(employee.role)
    while target is not None:
        if _first_active_holder(session, target, exclude_id=employee.id) is not None:
            return False
        target = _ESCALATION_LADDER.get(target)
    return True


def can_approve_step(
    session: Session,
    actor: Employee | None,
    step: ApprovalStep,
    request: LeaveRequest,
    on_date: dt.date | None = None,
) -> tuple[bool, str, Employee | None]:
    """May `actor` decide this step?

    Returns (allowed, reason, acting_on_behalf_of).

    The third element is set when the actor is covering under a delegation,
    so the audit trail can record both the person who clicked and the person
    whose authority they used.
    """
    if actor is None:
        return True, "trusted internal caller", None

    # Nobody approves their own leave, whatever their role.
    if actor.id == request.employee_id:
        return False, "an employee may not approve their own leave request", None

    if actor.status != "active":
        return False, f"{actor.name} is not an active employee", None

    assigned_id = step.assigned_approver_id

    # ------------------------------------------------------------------
    # An ASSIGNED step belongs to that person. Full stop.
    #
    # This used to fall through to a role check when the actor was not the
    # assignee, which made the effective rule "be the assignee OR merely hold
    # the right role" — so any HR admin could sign off a step sitting in a
    # different HR admin's queue, and the stored assignment was decorative.
    # An approval is a named act of authority; "someone with a similar job
    # title" is not the same person.
    #
    # The legitimate ways for somebody else to act are explicit and audited:
    #   * a delegation the assignee set up (recorded via acted_on_behalf_of),
    #   * an escalation, which REASSIGNS the step rather than bypassing it.
    # ------------------------------------------------------------------
    if assigned_id is not None:
        if actor.id == assigned_id:
            return True, "assigned approver", None

        delegate = active_delegate_for(session, assigned_id, on_date)
        if delegate is not None and delegate.id == actor.id:
            return True, "acting under an active delegation", session.get(Employee, assigned_id)

        assignee = session.get(Employee, assigned_id)
        return False, (
            f"this decision belongs to {assignee.name if assignee else 'another approver'}. "
            f"{actor.name} holds the {actor.role!r} role but is not the assigned "
            "approver and has no active delegation from them. If they are "
            "unavailable, use a delegation or let the SLA escalate the step"
        ), None

    # ------------------------------------------------------------------
    # UNASSIGNED step — nobody owns it, so role rules decide. This happens
    # when no holder of the role existed at chain-creation time.
    # ------------------------------------------------------------------
    if step.role == "manager":
        subject = session.get(Employee, request.employee_id)
        if subject is not None and any(
            m.id == actor.id for m in management_chain(session, subject)
        ):
            return True, "in the requester's management chain", None
        return False, (
            f"tier {step.tier} must be approved by the requester's own manager, "
            f"and {actor.name} is not in their reporting line"
        ), None

    if actor.role == step.role:
        return True, f"holds the {step.role} role", None

    # A director may act on an HR tier; the reverse is not true.
    if step.role == "hr_admin" and actor.role == "director":
        return True, "director acting on an HR tier", None

    return False, (
        f"tier {step.tier} requires the {step.role!r} role and {actor.name} "
        f"has role {actor.role!r}"
    ), None


def require_approve_step(
    session: Session,
    actor: Employee | None,
    step: ApprovalStep,
    request: LeaveRequest,
    on_date: dt.date | None = None,
) -> Employee | None:
    allowed, reason, on_behalf_of = can_approve_step(session, actor, step, request, on_date)
    if not allowed:
        raise AuthorizationError(f"Not authorised to approve: {reason}.")
    return on_behalf_of
