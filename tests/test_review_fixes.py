"""Regression tests for the second code review.

Each test names the defect and, where it matters, the number that used to
come out. These are the tests that would have caught the bugs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.approval import (
    ApprovalError,
    forward_options,
    forward_request,
    recommended_reviewers,
    record_approval_decision,
    submit_approval_chain,
)
from app.authorization import AuthorizationError, can_approve_step
from app.classification import (
    _classify_across_spans,
    cancel_leave_request,
    policy_spans,
    submit_leave_request,
)
from app.dashboard import get_live_balance
from app.db import SessionLocal
from app.models import (
    ApprovalDelegation,
    ApprovalStep,
    Employee,
    LeaveLedger,
    LeaveRequest,
    OrgPolicy,
)
from app.settlement import is_settled, settlement_rows

D = Decimal


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


def emp(session, name) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == name)).one()


@pytest.fixture()
def priya(session):
    return emp(session, "Priya")


@pytest.fixture()
def anitha(session):
    return emp(session, "Anitha Rajan")


@pytest.fixture()
def fatima(session):
    return emp(session, "Fatima Khan")


# Credits are dated inside the SAME leave year as the requests below
# (India runs 1 Apr – 31 Mar), because a spendable balance is now scoped to
# the current leave year rather than the whole ledger history.
def give(session, employee, leave_type_id, days, on="2027-04-01"):
    session.add(LeaveLedger(
        employee_id=employee.id, leave_type_id=leave_type_id, amount=D(str(days)),
        reason="test fixture", effective_date=dt.date.fromisoformat(on),
    ))
    session.flush()


def book(session, employee, leave_type="EL", start=None, days=4, **kwargs):
    start = start or dt.date(2027, 6, 7)
    kwargs.setdefault(
        "submitted_at", dt.datetime(2027, 5, 1, 9, 0, tzinfo=dt.timezone.utc)
    )
    kwargs.setdefault("commit", False)
    result = submit_leave_request(
        session, employee, leave_type, start, start + dt.timedelta(days=days - 1), **kwargs
    )
    submit_approval_chain(session, result.request)
    return result


# ===========================================================================
# 1. Approver ownership — the security bug
# ===========================================================================
def test_another_holder_of_the_role_cannot_approve_an_assigned_step(session, priya, fatima):
    """THE BUG. A second HR admin could sign off a step assigned to Fatima.

    The old check fell through to `if actor.role == step.role: return True`
    once the assignee test failed, so the stored assignment was decorative and
    the effective rule was "be the assignee, or merely hold the right role".
    """
    intruder = Employee(
        name="Other HR", email="other.hr@northbridge.example",
        join_date=dt.date(2020, 1, 1), region="India-TamilNadu", role="hr_admin",
    )
    session.add(intruder)
    session.flush()

    request = LeaveRequest(
        employee_id=priya.id, leave_type_id="EL",
        start_date=dt.date(2027, 3, 1), end_date=dt.date(2027, 3, 2),
        duration_days=2, status="pending",
    )
    session.add(request)
    session.flush()
    step = ApprovalStep(
        request_id=request.id, tier=2, role="hr_admin", status="active",
        assigned_approver_id=fatima.id,
    )
    session.add(step)
    session.flush()

    allowed, reason, _ = can_approve_step(session, intruder, step, request)
    assert allowed is False
    assert "belongs to Fatima Khan" in reason

    allowed, _, _ = can_approve_step(session, fatima, step, request)
    assert allowed is True


def test_another_manager_cannot_approve_a_step_assigned_to_a_peer(session, priya, anitha):
    """The case the review said was missing: manager vs manager."""
    other_manager = emp(session, "Dana Whitfield")
    request = LeaveRequest(
        employee_id=priya.id, leave_type_id="EL",
        start_date=dt.date(2027, 4, 5), end_date=dt.date(2027, 4, 6),
        duration_days=2, status="pending",
    )
    session.add(request)
    session.flush()
    step = ApprovalStep(
        request_id=request.id, tier=1, role="manager", status="active",
        assigned_approver_id=anitha.id,
    )
    session.add(step)
    session.flush()

    allowed, reason, _ = can_approve_step(session, other_manager, step, request)
    assert allowed is False
    assert "belongs to Anitha Rajan" in reason


def test_the_api_path_refuses_the_wrong_approver(session, priya, anitha):
    """Not just the predicate — the write path must refuse too."""
    give(session, priya, "EL", 20)
    result = book(session, priya)
    dana = emp(session, "Dana Whitfield")

    with pytest.raises(AuthorizationError):
        record_approval_decision(session, result.request.id, 1, "approved", actor=dana)


def test_a_delegate_may_still_act(session, priya, anitha, fatima):
    """Delegation is the sanctioned way for someone else to decide."""
    give(session, priya, "EL", 20)
    result = book(session, priya)
    session.add(ApprovalDelegation(
        delegator_id=anitha.id, delegate_id=fatima.id,
        from_date=dt.date(2020, 1, 1), to_date=dt.date(2040, 1, 1),
    ))
    session.flush()

    step = record_approval_decision(
        session, result.request.id, 1, "approved", actor=fatima
    )
    assert step.acted_by == fatima.id
    assert step.acted_on_behalf_of == anitha.id


# ===========================================================================
# 2. Balance conservation across policy spans
# ===========================================================================
def test_spans_share_one_balance(session, priya):
    """THE BUG. Two spans each read the untouched ledger.

    With a 5-day balance and two spans of 3 and 7 days, the old code reported
    8 paid — it paid the same 5 days twice over.
    """
    give(session, priya, "CL", 5)

    old = session.scalars(select(OrgPolicy).where(
        OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "CL"
    )).one()
    old.effective_to = dt.date(2027, 3, 31)
    session.flush()
    session.add(OrgPolicy(
        region="India-TamilNadu", legal_entity=old.legal_entity, leave_type_id="CL",
        tenure_min_years=D("0"), tenure_max_years=None,
        entitlement_days_per_year=D("10"), is_paid=True, accrual_method="annual_lump",
        carryover_max_days=D("0"), min_notice_days=1, max_consecutive_days=30,
        effective_from=dt.date(2027, 4, 1), effective_to=None,
        compliance_note="FY2027 uplift.",
    ))
    session.flush()

    start, end = dt.date(2027, 3, 29), dt.date(2027, 4, 9)
    spans = policy_spans(session, priya, "CL", start, end)
    assert len(spans) == 2, "this test needs a genuine mid-request policy change"

    total = sum(s.days for s in spans)
    result = _classify_across_spans(session, priya, "CL", total, spans, start)

    assert result.paid_days + result.unpaid_days == total      # conserves
    assert result.paid_days <= D("5")                          # within balance
    assert result.paid_days == D("5")
    assert result.unpaid_days == total - D("5")


def test_single_span_is_unaffected(session, priya):
    """The common case must give the same answer it always did."""
    give(session, priya, "EL", 10)
    start, end = dt.date(2027, 6, 7), dt.date(2027, 6, 11)
    spans = policy_spans(session, priya, "EL", start, end)
    total = sum(s.days for s in spans)
    result = _classify_across_spans(session, priya, "EL", total, spans, start)
    assert result.paid_days == total


# ===========================================================================
# 3. Approval moves the balance
# ===========================================================================
def test_approval_writes_a_ledger_deduction(session, priya, anitha):
    """THE BUG. status became 'approved' and the balance never moved."""
    give(session, priya, "EL", 10)
    start = dt.date(2027, 6, 7)
    before = get_live_balance(session, priya.id, "EL", start)
    assert before == D("10.000")

    result = book(session, priya, start=start, days=5)
    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()

    assert session.get(LeaveRequest, result.request.id).status == "approved"
    after = get_live_balance(session, priya.id, "EL", start)
    assert after == before - result.request.paid_days
    assert is_settled(session, result.request.id)


def test_settlement_writes_one_row_per_source_type(session, priya, anitha):
    """A 3-EL + 2-CL split must produce two deductions, not one combined row."""
    give(session, priya, "EL", 3)
    give(session, priya, "CL", 7)
    start = dt.date(2027, 9, 6)

    result = book(session, priya, start=start, days=6)
    assert len(result.classification.sources) == 2

    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()

    rows = {r.leave_type_id: D(r.amount) for r in settlement_rows(session, result.request.id)}
    assert rows == {"EL": D("-3.000"), "CL": D("-2.000")}
    assert get_live_balance(session, priya.id, "EL", start) == D("0.000")
    assert get_live_balance(session, priya.id, "CL", start) == D("5.000")


def test_rejection_writes_nothing(session, priya, anitha):
    give(session, priya, "EL", 10)
    start = dt.date(2027, 6, 7)
    result = book(session, priya, start=start)
    record_approval_decision(
        session, result.request.id, 1, "rejected", actor=anitha,
        decision_reason="Peak period",
    )
    session.flush()

    assert settlement_rows(session, result.request.id) == []
    assert get_live_balance(session, priya.id, "EL", start) == D("10.000")


def test_settlement_is_idempotent(session, priya, anitha):
    from app.settlement import settle_approved_request

    give(session, priya, "EL", 10)
    result = book(session, priya)
    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()

    assert settle_approved_request(session, result.request) == []


def test_withdrawing_an_approved_request_gives_the_days_back(session, priya, anitha, fatima):
    """Reversal appends a mirror entry; the original deduction is never erased."""
    give(session, priya, "EL", 10)
    start = dt.date(2027, 6, 7)
    result = book(session, priya, start=start)
    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()
    spent = get_live_balance(session, priya.id, "EL", start)

    cancel_leave_request(
        session, result.request, actor=fatima, reason="Trip cancelled", commit=False
    )
    session.flush()

    assert get_live_balance(session, priya.id, "EL", start) == D("10.000")
    assert spent < D("10.000")
    rows = settlement_rows(session, result.request.id)
    assert len(rows) == 2                       # deduction AND reversal, both kept
    assert session.get(LeaveRequest, result.request.id).cancellation_reason == "Trip cancelled"


def test_an_employee_cannot_withdraw_their_own_approved_leave(session, priya, anitha):
    give(session, priya, "EL", 10)
    result = book(session, priya)
    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()

    with pytest.raises(Exception, match="Ask HR"):
        cancel_leave_request(session, result.request, actor=priya, commit=False)


# ===========================================================================
# 4. Serialised state transitions
# ===========================================================================
def test_cancellation_locks_the_request(session):
    """Both paths must take the same lock, or they never serialise."""
    import inspect

    from app.approval import record_approval_decision as decide
    from app.classification import cancel_leave_request as cancel

    assert "with_for_update" in inspect.getsource(cancel)
    assert "_lock_request" in inspect.getsource(decide)


def test_cancelling_a_decided_request_is_refused(session, priya, anitha):
    give(session, priya, "EL", 10)
    result = book(session, priya)
    record_approval_decision(session, result.request.id, 1, "rejected",
                             actor=anitha, decision_reason="No")
    session.flush()

    with pytest.raises(Exception, match="Only a pending or approved"):
        cancel_leave_request(session, result.request, actor=priya, commit=False)


def test_a_decided_request_takes_no_further_decisions(session, priya, anitha):
    give(session, priya, "EL", 10)
    result = book(session, priya)
    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()

    with pytest.raises(ApprovalError, match="already"):
        record_approval_decision(session, result.request.id, 1, "rejected", actor=anitha)


# ===========================================================================
# 5. Three reasons, three columns
# ===========================================================================
def test_an_employee_note_is_not_an_override(session, priya):
    """THE BUG. A private comment used to populate `override_reason`, so the
    system believed a policy breach had been justified."""
    give(session, priya, "EL", 20)
    result = book(session, priya, employee_reason="Family wedding")

    assert result.request.employee_reason == "Family wedding"
    assert result.request.override_reason is None
    assert result.request.cancellation_reason is None


def test_the_three_reasons_stay_separate(session, priya, anitha):
    give(session, priya, "EL", 20)
    result = submit_leave_request(
        session, priya, "EL", dt.date(2027, 4, 6), dt.date(2027, 4, 8),
        submitted_at=dt.datetime(2027, 4, 5, tzinfo=dt.timezone.utc),
        employee_reason="Personal", override_reason="Emergency, agreed verbally",
        commit=False,
    )
    submit_approval_chain(session, result.request)
    cancel_leave_request(session, result.request, actor=priya,
                         reason="No longer needed", commit=False)
    session.flush()

    row = session.get(LeaveRequest, result.request.id)
    assert row.employee_reason == "Personal"
    assert row.override_reason == "Emergency, agreed verbally"
    assert row.cancellation_reason == "No longer needed"


# ===========================================================================
# 6. Manual routing
# ===========================================================================
def test_submission_creates_only_the_first_tier(session, priya):
    """The system no longer decides that HR is needed — the manager does."""
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)          # well over the 5-day threshold
    steps = session.scalars(
        select(ApprovalStep).where(ApprovalStep.request_id == result.request.id)
    ).all()
    assert [(s.tier, s.role) for s in steps] == [(1, "manager")]


def test_the_rules_still_recommend_hr(session, priya):
    """The policy is not lost — it is surfaced to the approver as advice."""
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    advice = recommended_reviewers(session, result.request)
    assert any(a["role"] == "hr_admin" for a in advice)
    assert any("exceeds" in a["reason"] for a in advice)


def test_a_manager_can_forward_to_hr(session, priya, anitha, fatima):
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)

    step = forward_request(session, result.request.id, 1, "hr_admin",
                           actor=anitha, note="Long absence")
    assert step.role == "hr_admin"
    assert step.assigned_approver_id == fatima.id
    assert step.status == "active"

    first = session.scalars(select(ApprovalStep).where(
        ApprovalStep.request_id == result.request.id, ApprovalStep.tier == 1
    )).one()
    assert first.status == "forwarded"       # not "approved" — they did not grant it
    assert first.forwarded_to_role == "hr_admin"
    assert session.get(LeaveRequest, result.request.id).status == "pending"


def test_hr_can_forward_to_the_director(session, priya, anitha, fatima):
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    forward_request(session, result.request.id, 1, "hr_admin", actor=anitha)
    step = forward_request(session, result.request.id, 2, "director", actor=fatima)
    assert step.role == "director"


def test_a_manager_cannot_forward_straight_to_the_director(session, priya, anitha):
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    with pytest.raises(ApprovalError, match="cannot forward"):
        forward_request(session, result.request.id, 1, "director", actor=anitha)


def test_a_request_cannot_be_forwarded_back(session, priya, anitha, fatima):
    """No loops: HR cannot bounce a request back to HR, or to the manager.

    Two guards catch this, and the ladder is the outer one — a role can only
    forward strictly upward — so that is the message that surfaces. The
    "already been through" check behind it is defence in depth for the day
    someone widens the ladder.
    """
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    forward_request(session, result.request.id, 1, "hr_admin", actor=anitha)
    with pytest.raises(ApprovalError, match="cannot forward"):
        forward_request(session, result.request.id, 2, "hr_admin", actor=fatima)
    with pytest.raises(ApprovalError, match="cannot forward"):
        forward_request(session, result.request.id, 2, "manager", actor=fatima)


def test_forwarding_needs_the_assigned_approver(session, priya):
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    dana = emp(session, "Dana Whitfield")
    with pytest.raises(AuthorizationError):
        forward_request(session, result.request.id, 1, "hr_admin", actor=dana)


def test_approving_is_final_even_when_hr_was_recommended(session, priya, anitha):
    """A manager who approves an 11-day request has granted it. Their call."""
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    record_approval_decision(session, result.request.id, 1, "approved", actor=anitha)
    session.flush()

    assert session.get(LeaveRequest, result.request.id).status == "approved"
    assert is_settled(session, result.request.id)


def test_forward_options_stop_at_the_director(session, priya, anitha, fatima):
    give(session, priya, "EL", 30)
    result = book(session, priya, days=12)
    forward_request(session, result.request.id, 1, "hr_admin", actor=anitha)
    step2 = session.scalars(select(ApprovalStep).where(
        ApprovalStep.request_id == result.request.id, ApprovalStep.tier == 2
    )).one()
    # HR's one onward option is the director...
    assert [o["role"] for o in forward_options(session, step2)] == ["director"]

    forward_request(session, result.request.id, 2, "director", actor=fatima)
    step3 = session.scalars(select(ApprovalStep).where(
        ApprovalStep.request_id == result.request.id, ApprovalStep.tier == 3
    )).one()
    # ...and once used, it is gone from HR's list and absent from the director's.
    assert forward_options(session, step2) == []
    assert forward_options(session, step3) == []      # nowhere left to go
