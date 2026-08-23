"""Module 6 — Approval Engine.

Decides who must sign off a leave request, and runs the sequential
approve/reject workflow across those tiers.

Built on the uploaded Module 6 implementation; the rule-evaluation and
routing-reason design is kept, with the review findings closed:

  29  Approver authorization is enforced, not merely recorded.
  30  Decisions take a row lock, so two concurrent approvals cannot both win.
  31  Steps store the specific assigned approver, not just the role.
  32  Approval and rejection reasons are captured.
  33  The manager tier comes from the rules table; the hardcoded fallback is
      now an explicit, logged repair rather than silent behaviour.
  34  Two rules cannot put the same role on different tiers, and a DB unique
      constraint backs it up.
  35  Rule configuration is validated when the rule is created.
  36  Delegation covers an absent approver.
  37  Overdue steps escalate.
  38  Steps carry an SLA deadline from the rule that created them.
  39  Final approval writes to the transactional outbox instead of calling
      listeners before the transaction commits.
  40  The outbox is durable and cross-process, replacing the in-memory hook.
  41  Duplicate chain creation is prevented by uq(request_id, role) and
      uq(request_id, tier) at the database level.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.authorization import (
    AuthorizationError,
    is_top_of_chain,
    require_approve_step,
    resolve_assigned_approver,
)
from app.models import ApprovalRule, ApprovalStep, Employee, LeaveRequest
from app.notifications import (
    notify_request_decided,
    notify_request_forwarded,
    notify_request_submitted,
)
from app.settlement import notify_payroll, settle_approved_request
from app.outbox import (
    TOPIC_REQUEST_APPROVED,
    TOPIC_REQUEST_REJECTED,
    TOPIC_STEP_ESCALATED,
    emit,
)

log = logging.getLogger("leave_engine.approval")

DECISIONS = ("approved", "rejected")
_ROLE_LABELS = {"manager": "Manager", "hr_admin": "HR", "director": "Director"}

__all__ = [
    "ApprovalError",
    "evaluate",
    "resolve_approval_chain",
    "create_approval_steps",
    "submit_approval_chain",
    "record_approval_decision",
    "validate_approval_rule",
    "add_approval_rule",
    "escalate_overdue_steps",
    "pending_steps_for",
    "ChainEntry",
    "OPERATORS",
]


class ApprovalError(ValueError):
    """Invalid approval-engine operation: bad rule, bad tier, wrong state."""


# ---------------------------------------------------------------------------
# Rule evaluation — data-driven, unchanged in spirit from the uploaded module
# ---------------------------------------------------------------------------
OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# Fields a rule may test. A closed list so a typo is caught at rule-creation
# time rather than exploding mid-approval (finding 35).
CONDITION_FIELDS = ("always", "leave_type", "duration_days", "paid_days", "unpaid_days")


def _request_field(request: LeaveRequest, field_name: str) -> Any:
    if field_name == "always":
        return True
    if field_name == "leave_type":
        return request.leave_type_id
    if field_name in ("duration_days", "paid_days", "unpaid_days"):
        value = getattr(request, field_name)
        if value is None:
            return Decimal("0")
        return value if isinstance(value, Decimal) else Decimal(str(value))
    if hasattr(request, field_name):
        return getattr(request, field_name)
    raise ApprovalError(
        f"approval_rules.condition_field {field_name!r} has no matching field "
        f"on leave_request. Valid fields: {', '.join(CONDITION_FIELDS)}."
    )


def _coerce_value(field_name: str, raw_value: str, sample: Any) -> Any:
    if field_name == "always" or isinstance(sample, bool):
        return raw_value.strip().lower() == "true"
    if isinstance(sample, Decimal):
        try:
            return Decimal(raw_value)
        except InvalidOperation as exc:
            raise ApprovalError(
                f"approval_rules.value {raw_value!r} is not numeric for "
                f"condition_field {field_name!r}."
            ) from exc
    return raw_value


def evaluate(rule: ApprovalRule, request: LeaveRequest) -> bool:
    """request[condition_field] <operator> value — rules stay rows, not code."""
    if rule.operator not in OPERATORS:
        raise ApprovalError(
            f"Unknown operator {rule.operator!r} on approval_rules.id={rule.id}."
        )
    actual = _request_field(request, rule.condition_field)
    expected = _coerce_value(rule.condition_field, rule.value, actual)
    return OPERATORS[rule.operator](actual, expected)


# ---------------------------------------------------------------------------
# Rule validation at creation time (finding 35)
# ---------------------------------------------------------------------------
def validate_approval_rule(
    session: Session, rule: ApprovalRule, *, exclude_id: int | None = None
) -> None:
    """Reject a malformed rule when it is authored, not when it fires.

    A bad `condition_field` or a non-numeric threshold used to surface as an
    exception during an approval — that is, in front of an employee waiting on
    their leave, long after whoever typed it had moved on.
    """
    if rule.condition_field not in CONDITION_FIELDS:
        raise ApprovalError(
            f"condition_field {rule.condition_field!r} is not supported. "
            f"Valid fields: {', '.join(CONDITION_FIELDS)}."
        )
    if rule.operator not in OPERATORS:
        raise ApprovalError(
            f"operator {rule.operator!r} is not supported. "
            f"Valid operators: {', '.join(OPERATORS)}."
        )
    if rule.adds_tier not in _ROLE_LABELS:
        raise ApprovalError(
            f"adds_tier {rule.adds_tier!r} is not a known role. "
            f"Valid roles: {', '.join(_ROLE_LABELS)}."
        )
    if rule.tier_order is None or rule.tier_order < 1:
        raise ApprovalError("tier_order must be 1 or greater.")

    # Numeric conditions need a numeric threshold.
    if rule.condition_field in ("duration_days", "paid_days", "unpaid_days"):
        try:
            Decimal(rule.value)
        except (InvalidOperation, TypeError) as exc:
            raise ApprovalError(
                f"condition_field {rule.condition_field!r} compares numbers, but "
                f"value {rule.value!r} is not numeric."
            ) from exc
    if rule.condition_field == "always" and rule.value.strip().lower() not in ("true", "false"):
        raise ApprovalError("condition_field 'always' expects value 'true' or 'false'.")

    # Finding 34: a role must not be reachable at two different tiers, or the
    # chain order becomes ambiguous.
    stmt = select(ApprovalRule).where(
        ApprovalRule.adds_tier == rule.adds_tier,
        ApprovalRule.tier_order != rule.tier_order,
        ApprovalRule.is_active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(ApprovalRule.id != exclude_id)
    conflict = session.scalars(stmt.limit(1)).first()
    if conflict is not None:
        raise ApprovalError(
            f"Role {rule.adds_tier!r} is already assigned to tier "
            f"{conflict.tier_order} by rule id={conflict.id}; this rule puts it at "
            f"tier {rule.tier_order}. A role must occupy exactly one tier, "
            "otherwise the chain order depends on which rule happens to fire."
        )

    if rule.escalate_to_role and rule.escalate_to_role not in _ROLE_LABELS:
        raise ApprovalError(f"escalate_to_role {rule.escalate_to_role!r} is not a known role.")


def add_approval_rule(session: Session, rule: ApprovalRule, *, flush: bool = True) -> ApprovalRule:
    """Validated insert — the path an admin UI should use."""
    validate_approval_rule(session, rule)
    session.add(rule)
    if flush:
        session.flush()
    return rule


# ---------------------------------------------------------------------------
# Routing reasons
# ---------------------------------------------------------------------------
def _format_duration(value: Decimal) -> str:
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    as_int = value.to_integral_value()
    return str(as_int) if value == as_int else str(value.normalize())


def _role_label(role: str) -> str:
    return _ROLE_LABELS.get(role, role.replace("_", " ").title())


def _routing_reason(rule: ApprovalRule, request: LeaveRequest) -> str:
    role_label = _role_label(rule.adds_tier)
    if rule.condition_field == "always":
        return "Manager approval required for all requests."
    if rule.condition_field == "duration_days":
        duration = _format_duration(request.duration_days)
        return (f"{role_label} added because duration ({duration} days) exceeds "
                f"{rule.value}-day threshold.")
    if rule.condition_field == "leave_type":
        return (f"{role_label} added because the request is {request.leave_type_id} "
                "leave, regardless of duration.")
    return (f"{role_label} added because {rule.condition_field} "
            f"{rule.operator} {rule.value}.")


# ---------------------------------------------------------------------------
# 1. Resolve the chain
# ---------------------------------------------------------------------------
@dataclass
class ChainEntry:
    tier: int
    role: str
    routing_reason: str
    sla_hours: int | None = None
    escalate_to_role: str | None = None


def recommended_reviewers(session: Session, request: LeaveRequest) -> list[dict]:
    """What the approval RULES suggest — advice, not instruction.

    Routing is now the approver's decision (see `forward_request`), so the
    rules no longer build the chain. They still encode the organisation's
    policy — "anything over 5 days should reach HR" — and that belongs in
    front of the manager as a recommendation, with the reason attached.

    A manager who overrides the recommendation is making a judgement call, on
    the record; a manager who never saw it is just uninformed.
    """
    out = []
    for rule in session.scalars(
        select(ApprovalRule).where(ApprovalRule.is_active.is_(True)).order_by(ApprovalRule.id)
    ):
        if rule.condition_field == "always" or rule.adds_tier == "manager":
            continue
        try:
            fires = evaluate(rule, request)
        except ApprovalError:
            continue
        if fires:
            out.append({
                "role": rule.adds_tier,
                "reason": _routing_reason(rule, request),
                "rule_id": rule.id,
            })
    # One recommendation per role, the first that fired.
    seen, unique = set(), []
    for item in out:
        if item["role"] in seen:
            continue
        seen.add(item["role"])
        unique.append(item)
    return unique


def resolve_approval_chain(session: Session, request: LeaveRequest) -> list[ChainEntry]:
    """Every tier whose rule fires, de-duplicated by role, ordered by tier."""
    if request.duration_days is None or request.leave_type_id is None:
        raise ApprovalError(
            "leave_request must have duration_days and leave_type_id populated "
            "before an approval chain can be resolved."
        )

    rules = session.scalars(
        select(ApprovalRule).where(ApprovalRule.is_active.is_(True)).order_by(ApprovalRule.id)
    ).all()

    fired: dict[str, ChainEntry] = {}
    for rule in rules:
        if not evaluate(rule, request):
            continue
        if rule.adds_tier in fired:
            # An 8-day Unpaid request fires both duration>5 and
            # leave_type==Unpaid for hr_admin. One HR tier, not two.
            continue
        fired[rule.adds_tier] = ChainEntry(
            tier=rule.tier_order,
            role=rule.adds_tier,
            routing_reason=_routing_reason(rule, request),
            sla_hours=rule.sla_hours,
            escalate_to_role=rule.escalate_to_role,
        )

    # Finding 33: the manager tier should come from the rules table. If the
    # "always" rule is missing or deactivated, the configuration is wrong —
    # repair it, but say so loudly rather than silently papering over it.
    if "manager" not in fired:
        log.warning(
            "No active approval rule produced a manager tier for request %s. "
            "Applying the safety fallback — check that the "
            "condition_field='always' rule exists and is active.",
            request.id,
        )
        fired["manager"] = ChainEntry(
            tier=1, role="manager",
            routing_reason=(
                "Manager approval required for all requests "
                "(applied by safety fallback: no active 'always' rule found)."
            ),
        )

    chain = sorted(fired.values(), key=lambda e: e.tier)

    # Finding 34, again at resolve time: two roles cannot share a tier.
    tiers = [e.tier for e in chain]
    if len(tiers) != len(set(tiers)):
        clashing = [e for e in chain if tiers.count(e.tier) > 1]
        raise ApprovalError(
            "Approval rules put more than one role on the same tier: "
            + ", ".join(f"tier {e.tier}={e.role}" for e in clashing)
            + ". Fix the tier_order values in approval_rules."
        )
    return chain


# ---------------------------------------------------------------------------
# 2. Create the steps
# ---------------------------------------------------------------------------
def create_approval_steps(
    session: Session,
    request: LeaveRequest,
    chain: list[ChainEntry] | None = None,
    *,
    now: dt.datetime | None = None,
) -> list[ApprovalStep]:
    """tier 1 -> active, the rest -> pending. Call once, after the request row.

    Each step is stamped with its assigned approver (finding 31) and, when the
    rule specifies an SLA, a deadline (finding 38).

    Calling this twice for one request raises: `uq_approval_steps_request_tier`
    and `uq_approval_steps_request_role` make a duplicate chain impossible at
    the database level, not merely unlikely (finding 41).
    """
    if request.id is None:
        raise ApprovalError(
            "leave_request must be flushed/have an id before creating approval_steps."
        )

    existing = session.scalars(
        select(ApprovalStep).where(ApprovalStep.request_id == request.id).limit(1)
    ).first()
    if existing is not None:
        raise ApprovalError(
            f"Request {request.id} already has an approval chain. Creating a "
            "second one would let a request be approved twice."
        )

    if chain is None:
        # Only the first tier is created up front. Later tiers appear when an
        # approver forwards, so the chain reflects decisions actually taken
        # rather than what a rules table predicted.
        chain = [resolve_approval_chain(session, request)[0]]
    if not chain:
        raise ApprovalError("Resolved approval chain is empty — tier 1 is always required.")

    now = now or dt.datetime.now(dt.timezone.utc)
    steps: list[ApprovalStep] = []
    for entry in chain:
        is_first = entry.tier == min(e.tier for e in chain)
        approver = resolve_assigned_approver(session, request, entry.role)
        step = ApprovalStep(
            request_id=request.id,
            tier=entry.tier,
            role=entry.role,
            status="active" if is_first else "pending",
            routing_reason=entry.routing_reason,
            assigned_approver_id=approver.id if approver else None,
            activated_at=now if is_first else None,
            due_at=(
                now + dt.timedelta(hours=entry.sla_hours)
                if is_first and entry.sla_hours else None
            ),
        )
        session.add(step)
        steps.append(step)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ApprovalError(
            f"Could not create the approval chain for request {request.id}: {exc.orig}"
        ) from exc

    _auto_approve_if_top_of_chain(session, request, steps, now)

    active = next((s for s in steps if s.status == "active"), None)
    if active is not None and active.assigned_approver_id:
        notify_request_submitted(
            session, request, session.get(Employee, active.assigned_approver_id)
        )
    return steps


def _auto_approve_if_top_of_chain(
    session: Session,
    request: LeaveRequest,
    steps: list[ApprovalStep],
    now: dt.datetime,
) -> None:
    """Close out a request nobody outranks the requester to approve.

    A director with no manager above them has no one to sign off their leave.
    The alternatives are worse: leave the step assigned to nobody and the
    request pends forever, or invent a peer approver who has no actual
    authority. So the chain is marked approved and *labelled* as a
    self-approval, which is an auditable fact rather than a silent gap.

    Only applies when EVERY tier resolves to the requester or to nobody. A
    director's 25-day request still needs whatever other tiers the rules add,
    if those resolve to somebody else.
    """
    requester = session.get(Employee, request.employee_id)
    if requester is None or not is_top_of_chain(session, requester):
        return
    if any(
        s.assigned_approver_id is not None and s.assigned_approver_id != requester.id
        for s in steps
    ):
        return

    for step in steps:
        step.status = "approved"
        step.acted_by = requester.id
        step.acted_at = now
        step.assigned_approver_id = requester.id
        step.decision_reason = (
            "Self-approved: requester is at the top of the approval chain, "
            "with no higher authority to sign off."
        )
        step.routing_reason = (step.routing_reason or "") + " [self-approved]"

    request.status = "approved"
    session.flush()
    # A self-approval is still an approval: the balance must move.
    settle_approved_request(session, request)
    emit(
        session, TOPIC_REQUEST_APPROVED, "leave_request", request.id,
        {
            "request_id": request.id,
            "employee_id": request.employee_id,
            "leave_type_id": request.leave_type_id,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "duration_days": request.duration_days,
            "paid_days": request.paid_days,
            "unpaid_days": request.unpaid_days,
            "approved_at": now,
            "self_approved": True,
        },
    )
    session.flush()
    log.info(
        "request %s self-approved: %s is top of the approval chain",
        request.id, requester.name,
    )


def submit_approval_chain(session: Session, request: LeaveRequest) -> list[ApprovalStep]:
    """Resolve + create the FIRST tier only.

    Routing beyond tier 1 is a human decision now: the manager chooses whether
    HR needs to see it, HR chooses whether the director does. Pre-creating the
    whole chain would put those steps in the database before anyone asked for
    them, and the UI would show an HR tier that HR may never be given.
    `resolve_approval_chain()` still exists — it powers the "recommended"
    hint on the approver's screen.
    """
    return create_approval_steps(session, request, chain=None)


# ---------------------------------------------------------------------------
# 3. Sequential execution
# ---------------------------------------------------------------------------
def _sla_for_role(session: Session, role: str) -> tuple[int | None, str | None]:
    rule = session.scalars(
        select(ApprovalRule)
        .where(ApprovalRule.adds_tier == role, ApprovalRule.is_active.is_(True))
        .order_by(ApprovalRule.id)
        .limit(1)
    ).first()
    return (rule.sla_hours, rule.escalate_to_role) if rule else (None, None)


def record_approval_decision(
    session: Session,
    request_id: int,
    tier: int,
    decision: str,
    *,
    actor: Employee | int | None = None,
    decision_reason: str | None = None,
    now: dt.datetime | None = None,
) -> ApprovalStep:
    """Approve or reject the active tier.

    **approved** — this approver is granting the leave. There is no automatic
    next tier any more: if they wanted HR to look at it they would have
    forwarded it (`forward_request`). So approving is final, and the request
    is settled against the ledger IN THIS TRANSACTION. Status and balance move
    together or not at all, which is the defect the review found: the old code
    set `status = 'approved'` and never wrote a deduction.

    **rejected** — finalises immediately; nothing is deducted.

    Concurrency: the REQUEST row is locked first, then the step. Locking only
    the step left cancellation free to interleave, because the two paths took
    different locks and so never serialised against each other.

    Authorization: `actor` must be the assigned approver or their delegate.
    `None` means a trusted internal caller (the pipeline, tests).
    """
    if decision not in DECISIONS:
        raise ApprovalError(f"decision must be one of {DECISIONS}, got {decision!r}.")

    now = now or dt.datetime.now(dt.timezone.utc)

    # 1. Lock the request. Every path that changes its status takes this lock.
    request = _lock_request(session, request_id)
    if request.status != "pending":
        raise ApprovalError(
            f"Request {request_id} is already {request.status!r}; no further "
            "decisions can be recorded."
        )

    # 2. Lock the step, then read its state.
    step = session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.request_id == request_id, ApprovalStep.tier == tier)
        .with_for_update()
    ).first()
    if step is None:
        raise ApprovalError(f"No approval_steps row for request_id={request_id} tier={tier}.")
    if step.status != "active":
        raise ApprovalError(
            f"Tier {tier} ({step.role}) is {step.status!r}, not 'active' — a decision "
            "can only be recorded on the currently active tier."
        )

    actor_obj = session.get(Employee, actor) if isinstance(actor, int) else actor
    on_behalf_of = require_approve_step(session, actor_obj, step, request)

    step.status = decision
    step.acted_by = actor_obj.id if actor_obj else None
    step.acted_at = now
    step.decision_reason = decision_reason
    step.acted_on_behalf_of = on_behalf_of.id if on_behalf_of else None

    if decision == "rejected":
        request.status = "rejected"
        # Any later steps that somehow exist never activate.
        for later in session.scalars(
            select(ApprovalStep).where(
                ApprovalStep.request_id == request_id, ApprovalStep.tier > tier
            )
        ):
            if later.status in ("pending", "active"):
                later.status = "rejected"
                later.decision_reason = f"Request rejected at tier {tier}."

        emit(
            session, TOPIC_REQUEST_REJECTED, "leave_request", request.id,
            {
                "request_id": request.id,
                "employee_id": request.employee_id,
                "leave_type_id": request.leave_type_id,
                "rejected_at_tier": tier,
                "rejected_by": step.acted_by,
                "reason": decision_reason,
            },
        )
        notify_request_decided(session, request, actor_obj, "rejected", decision_reason)
        session.flush()
        return step

    # ---- approved: final, and settled ------------------------------------
    request.status = "approved"
    session.flush()

    settle_approved_request(session, request)
    notify_payroll(session, request)

    emit(
        session, TOPIC_REQUEST_APPROVED, "leave_request", request.id,
        {
            "request_id": request.id,
            "employee_id": request.employee_id,
            "leave_type_id": request.leave_type_id,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "duration_days": request.duration_days,
            "paid_days": request.paid_days,
            "unpaid_days": request.unpaid_days,
            "approved_at": now,
            "approved_by": step.acted_by,
        },
    )
    notify_request_decided(session, request, actor_obj, "approved", decision_reason)
    session.flush()
    return step


# ---------------------------------------------------------------------------
# 4. SLA and escalation (findings 37, 38)
# ---------------------------------------------------------------------------
def pending_steps_for(session: Session, approver: Employee) -> list[ApprovalStep]:
    """An approver's queue — steps assigned to them and awaiting a decision."""
    return list(session.scalars(
        select(ApprovalStep)
        .where(
            ApprovalStep.status == "active",
            ApprovalStep.assigned_approver_id == approver.id,
        )
        .order_by(ApprovalStep.due_at.nulls_last(), ApprovalStep.id)
    ))


def escalate_overdue_steps(
    session: Session, now: dt.datetime | None = None, *, commit: bool = True
) -> list[ApprovalStep]:
    """Flag and escalate active steps past their deadline.

    Escalation reassigns the step to the configured `escalate_to_role` and
    emits an event. It deliberately **never auto-approves**: whether an
    unattended request should be granted is a policy judgement the system is
    not entitled to make on HR's behalf. What it can do is stop the request
    sitting in a dead inbox forever, which was the finding.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    overdue = session.scalars(
        select(ApprovalStep).where(
            ApprovalStep.status == "active",
            ApprovalStep.due_at.is_not(None),
            ApprovalStep.due_at < now,
            ApprovalStep.escalated_at.is_(None),
        )
    ).all()

    escalated = []
    for step in overdue:
        _, escalate_to = _sla_for_role(session, step.role)
        step.escalated_at = now

        new_approver = None
        if escalate_to:
            request = session.get(LeaveRequest, step.request_id)
            new_approver = resolve_assigned_approver(session, request, escalate_to)
            if new_approver is not None:
                step.assigned_approver_id = new_approver.id
                step.routing_reason = (
                    (step.routing_reason or "")
                    + f" [escalated to {escalate_to} after SLA lapsed at {step.due_at}]"
                )

        emit(
            session, TOPIC_STEP_ESCALATED, "approval_step", step.id,
            {
                "step_id": step.id,
                "request_id": step.request_id,
                "tier": step.tier,
                "role": step.role,
                "due_at": step.due_at,
                "escalated_to_role": escalate_to,
                "escalated_to_employee_id": new_approver.id if new_approver else None,
            },
        )
        escalated.append(step)
        log.warning(
            "approval step %s (request %s tier %s) overdue since %s — escalated to %s",
            step.id, step.request_id, step.tier, step.due_at, escalate_to or "nobody",
        )

    session.flush()
    if commit:
        session.commit()
    return escalated


# Re-exported so callers catching approval problems catch authorization too.
__all__.append("AuthorizationError")


# ---------------------------------------------------------------------------
# 5. Chain introspection — what the approver's button should say
# ---------------------------------------------------------------------------
def next_tier_after(
    session: Session, request_id: int, tier: int
) -> ApprovalStep | None:
    """The step that becomes active once `tier` approves, if any."""
    return session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.request_id == request_id, ApprovalStep.tier > tier)
        .order_by(ApprovalStep.tier)
        .limit(1)
    ).first()


def decision_options(session: Session, step: ApprovalStep) -> dict:
    """How this decision should be presented.

    Under manual routing the active step is always the last one that exists —
    later tiers are only created when somebody forwards — so **approving
    always grants the leave**. There is no longer an "Approve & send to HR"
    state, because approving and forwarding are now two different buttons
    that do two different things:

      * *Approve* grants the leave and deducts it from the balance, full stop.
      * *Forward* explicitly does NOT grant it; it hands the decision on.

    The old wording implied a manager could approve *and* still have HR decide.
    They cannot, and the review was right that pretending otherwise is how a
    manager grants three weeks by accident.
    """
    forwards = forward_options(session, step)
    request = session.get(LeaveRequest, step.request_id)
    return {
        "approve_label": "Approve",
        "is_final": True,
        "forwards_to_role": None,
        "forward_options": forwards,
        "recommended": recommended_reviewers(session, request) if request else [],
        "help": "Approving grants this leave and deducts it from the balance. "
                "To have someone else decide, forward it instead.",
    }


__all__ += ["next_tier_after", "decision_options"]


# ===========================================================================
# 6. Manual routing — approvers decide where a request goes next
# ===========================================================================
#
# The system no longer decides that an 11-day request "needs HR". It tells the
# manager that policy recommends HR review, and the manager decides. The
# reason is accountability: an approval is a person taking responsibility, and
# a chain assembled by a rules table lets everyone point at the table. A
# manager who forwards has chosen to; a manager who approves outright has
# chosen that too, and their name is on it either way.
#
# Who may forward to whom. A role can only pass upward, and the director is
# terminal — there is nobody above them to escalate to.
FORWARD_TARGETS: dict[str, tuple[str, ...]] = {
    "manager": ("hr_admin",),
    "hr_admin": ("director",),
    "director": (),
}

TOPIC_REQUEST_FORWARDED = "leave_request.forwarded"


def _lock_request(session: Session, request_id: int) -> LeaveRequest:
    """Take the request row lock BEFORE reading its status.

    Approval, forwarding and cancellation all mutate the same request. The
    approval path used to lock only the approval STEP, which left cancellation
    free to interleave — two transactions could each read `status == 'pending'`
    and then write conflicting outcomes. Locking the request itself in every
    path gives all three a single serialisation point.
    """
    request = session.scalars(
        select(LeaveRequest).where(LeaveRequest.id == request_id).with_for_update()
    ).first()
    if request is None:
        raise ApprovalError(f"No leave_request with id={request_id}.")
    return request


def forward_request(
    session: Session,
    request_id: int,
    tier: int,
    to_role: str,
    *,
    actor: Employee | int | None = None,
    note: str | None = None,
    now: dt.datetime | None = None,
) -> ApprovalStep:
    """Send a request on to the next approver instead of deciding it.

    The current step is closed as `forwarded` (not approved — the approver has
    explicitly NOT granted the leave), and a new active step is created for
    the target role.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    request = _lock_request(session, request_id)
    if request.status != "pending":
        raise ApprovalError(
            f"Request {request_id} is already {request.status!r}; it cannot be forwarded."
        )

    step = session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.request_id == request_id, ApprovalStep.tier == tier)
        .with_for_update()
    ).first()
    if step is None:
        raise ApprovalError(f"No approval step for request {request_id} tier {tier}.")
    if step.status != "active":
        raise ApprovalError(
            f"Tier {tier} is {step.status!r}, not 'active' — only the current "
            "approver can forward a request."
        )

    actor_obj = session.get(Employee, actor) if isinstance(actor, int) else actor
    require_approve_step(session, actor_obj, step, request)

    allowed = FORWARD_TARGETS.get(step.role, ())
    if to_role not in allowed:
        raise ApprovalError(
            f"A {step.role!r} cannot forward to {to_role!r}. "
            + (f"Allowed: {', '.join(allowed)}." if allowed
               else "This role is the final approver and must decide the request.")
        )

    existing_roles = {
        s.role for s in session.scalars(
            select(ApprovalStep).where(ApprovalStep.request_id == request_id)
        )
    }
    if to_role in existing_roles:
        raise ApprovalError(
            f"This request has already been through {to_role!r}; forwarding back "
            "would loop. Approve or reject it."
        )

    recipient = resolve_assigned_approver(session, request, to_role)

    step.status = "forwarded"
    step.acted_by = actor_obj.id if actor_obj else None
    step.acted_at = now
    step.decision_reason = note
    step.forwarded_to_role = to_role
    step.forwarded_to_id = recipient.id if recipient else None

    sla_hours, _ = _sla_for_role(session, to_role)
    next_step = ApprovalStep(
        request_id=request_id,
        tier=step.tier + 1,
        role=to_role,
        status="active",
        routing_reason=(
            f"Forwarded by {actor_obj.name} for {_role_label(to_role)} review."
            + (f" “{note}”" if note else "")
        ),
        assigned_approver_id=recipient.id if recipient else None,
        activated_at=now,
        due_at=now + dt.timedelta(hours=sla_hours) if sla_hours else None,
    )
    session.add(next_step)

    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ApprovalError(f"Could not forward request {request_id}: {exc.orig}") from exc

    emit(
        session, TOPIC_REQUEST_FORWARDED, "leave_request", request_id,
        {
            "request_id": request_id,
            "from_role": step.role,
            "from_employee_id": step.acted_by,
            "to_role": to_role,
            "to_employee_id": recipient.id if recipient else None,
            "note": note,
        },
    )
    if actor_obj is not None:
        notify_request_forwarded(session, request, actor_obj, recipient, note)

    log.info(
        "request %s forwarded from %s to %s (%s)",
        request_id, step.role, to_role, recipient.name if recipient else "unassigned",
    )
    return next_step


def forward_options(session: Session, step: ApprovalStep) -> list[dict]:
    """Targets this approver may forward to, with who would receive it."""
    request = session.get(LeaveRequest, step.request_id)
    used = {
        s.role for s in session.scalars(
            select(ApprovalStep).where(ApprovalStep.request_id == step.request_id)
        )
    }
    out = []
    for role in FORWARD_TARGETS.get(step.role, ()):
        if role in used:
            continue
        recipient = resolve_assigned_approver(session, request, role)
        out.append({
            "role": role,
            "label": f"Forward to {_role_label(role)}",
            "recipient": recipient.name if recipient else None,
        })
    return out


__all__ += [
    "forward_request",
    "forward_options",
    "recommended_reviewers",
    "FORWARD_TARGETS",
    "TOPIC_REQUEST_FORWARDED",
]
