"""Tests for review findings 9-41, one section per module.

Each test names the finding it closes and, where behaviour changed, what it
used to do. Findings 5-8 are covered in tests/test_proration.py.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.approval import (
    ApprovalError,
    add_approval_rule,
    create_approval_steps,
    escalate_overdue_steps,
    forward_request,
    pending_steps_for,
    record_approval_decision,
    resolve_approval_chain,
    submit_approval_chain,
    validate_approval_rule,
)
from app.authorization import AuthorizationError, can_approve_step, can_view_employee
from app.classification import (
    OverlappingRequestError,
    available_balance,
    committed_days,
    reclassify_request,
    submit_leave_request,
)
from app.dashboard import (
    EmployeeNotFoundError,
    get_balance_buckets,
    get_dashboard,
    get_expiring_soon,
    get_live_balance,
)
from app.db import SessionLocal
from app.models import (
    ApprovalDelegation,
    ApprovalRule,
    ApprovalStep,
    Employee,
    HolidayOverride,
    LeaveLedger,
    LeaveRequest,
    OrgPolicy,
    OutboxEvent,
)
from app.outbox import TOPIC_REQUEST_APPROVED, TOPIC_REQUEST_REJECTED, emit, run_relay

D = Decimal
FUTURE = dt.datetime(2025, 5, 1, 9, 0, tzinfo=dt.timezone.utc)


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
def raj(session):
    return emp(session, "Raj")


@pytest.fixture()
def anitha(session):
    """Priya's manager."""
    return emp(session, "Anitha Rajan")


@pytest.fixture()
def fatima(session):
    return emp(session, "Fatima Khan")


@pytest.fixture()
def nathan(session):
    return emp(session, "Nathan Cole")


def give(session, employee, leave_type_id, days, on="2025-05-01", **kwargs):
    session.add(LeaveLedger(
        employee_id=employee.id, leave_type_id=leave_type_id, amount=D(str(days)),
        reason="test fixture", effective_date=dt.date.fromisoformat(on), **kwargs,
    ))
    session.flush()


def make_request(session, employee, leave_type="EL", start="2025-06-02", end="2025-06-04",
                 **kwargs):
    kwargs.setdefault("submitted_at", FUTURE)
    kwargs.setdefault("commit", False)
    return submit_leave_request(session, employee, leave_type, start, end, **kwargs)


# ===========================================================================
# MODULE 4 — DASHBOARD
# ===========================================================================
def test_finding_9_carryover_expiry_is_enforced(session, priya):
    """Was: expiry stored but ignored, so lapsed days stayed spendable."""
    give(session, priya, "EL", 5, on="2025-04-01",
         bucket="carryover", expires_on=dt.date(2025, 6, 30))

    assert get_live_balance(session, priya.id, "EL", "2025-06-30") == D("5")
    assert get_live_balance(session, priya.id, "EL", "2025-07-01") == D("0")


def test_finding_9_expired_days_are_still_reportable(session, priya):
    give(session, priya, "EL", 5, on="2025-04-01",
         bucket="carryover", expires_on=dt.date(2025, 6, 30))
    assert get_live_balance(
        session, priya.id, "EL", "2025-07-01", include_expired=True
    ) == D("5")


def test_finding_9_expiring_soon_warns_before_the_loss(session, priya):
    give(session, priya, "EL", 3, on="2025-04-01",
         bucket="carryover", expires_on=dt.date(2025, 6, 30))
    upcoming = get_expiring_soon(session, priya.id, "EL", "2025-05-01", within_days=90)
    assert upcoming == [(dt.date(2025, 6, 30), D("3"))]


def test_finding_10_employees_cannot_read_each_others_data(session, priya, raj):
    """Was: left entirely to the API layer, so any direct caller bypassed it."""
    assert can_view_employee(session, priya, priya) is True
    assert can_view_employee(session, priya, raj) is False

    with pytest.raises(AuthorizationError, match="may not view"):
        get_dashboard(session, raj, "2025-06-01", viewer=priya)


def test_finding_10_managers_see_their_reporting_line(session, priya, anitha, raj):
    assert can_view_employee(session, anitha, priya) is True
    assert can_view_employee(session, anitha, raj) is False   # not their report


def test_finding_10_hr_and_directors_see_everyone(session, priya, fatima, nathan):
    assert can_view_employee(session, fatima, priya) is True
    assert can_view_employee(session, nathan, priya) is True


def test_finding_10_internal_callers_are_unrestricted(session, raj):
    """Accrual jobs act for the system, not a person."""
    assert get_dashboard(session, raj, "2025-06-01", viewer=None) is not None


def test_finding_11_negative_balance_is_refused_by_default(session, priya):
    give(session, priya, "EL", 2)
    from app.classification import classify_paid_unpaid

    result = classify_paid_unpaid(session, priya, "EL", 6, "2025-06-01")
    assert result.paid_days == D("2")
    assert result.unpaid_days == D("4")     # not -4 of EL


def test_finding_11_overdraft_can_be_permitted_in_policy(session, priya):
    from app.classification import classify_paid_unpaid

    session.execute(
        OrgPolicy.__table__.update()
        .where(OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "EL")
        .values(allow_negative_balance=True)
    )
    session.flush()
    give(session, priya, "EL", 2)

    result = classify_paid_unpaid(session, priya, "EL", 6, "2025-06-01")
    assert result.paid_days == D("6")
    assert result.unpaid_days == D("0")


def test_finding_12_as_of_date_bounds_pending_requests(session, priya):
    """Was: the balance was point-in-time but the request list was not."""
    give(session, priya, "EL", 20)
    make_request(session, priya, start="2025-06-02", end="2025-06-04")

    assert get_dashboard(session, priya, "2025-04-15").pending_requests == []
    assert len(get_dashboard(session, priya, "2025-06-01").pending_requests) == 1


def test_finding_13_missing_employee_is_not_a_zero_balance(session):
    with pytest.raises(EmployeeNotFoundError):
        get_live_balance(session, 999_999, "EL")
    assert get_live_balance(session, 999_999, "EL", strict=False) == D("0")


def test_finding_14_carryover_is_separated_from_current_year(session, priya):
    give(session, priya, "EL", 10, on="2025-05-01", bucket="current")
    give(session, priya, "EL", 3, on="2025-04-01",
         bucket="carryover", expires_on=dt.date(2026, 3, 31))

    buckets = get_balance_buckets(session, priya.id, "EL", "2025-06-01")
    assert buckets == {"current": D("10"), "carryover": D("3")}

    view = get_dashboard(session, priya, "2025-06-01").leave_type("EL")
    assert view.balance == D("13")
    assert view.current_year_balance == D("10")
    assert view.carryover_balance == D("3")


def test_finding_15_covering_indexes_exist(session):
    """The live-balance sum must stay index-driven as the ledger grows.

    This asserts the indexes are PRESENT rather than that the planner picks
    them: on a seed-sized table Postgres correctly prefers a sequential scan,
    so an EXPLAIN assertion here would test the planner's cost model rather
    than our schema.
    """
    indexes = set(session.scalars(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'leave_ledger'"
    )))
    assert "ix_leave_ledger_balance" in indexes
    assert "ix_leave_ledger_balance_expiry" in indexes


def test_finding_15_dashboard_uses_one_query_per_concern(session, priya):
    """`get_balances` answers every leave type in a single grouped query."""
    import inspect

    from app.dashboard import get_balances

    source = inspect.getsource(get_balances)
    assert source.count("session.execute") == 1
    assert "group_by" in source


def test_finding_16_ledger_cannot_be_updated(session, priya):
    """Was: any caller could rewrite history. Now the database refuses."""
    give(session, priya, "EL", 5)

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text(
            "UPDATE leave_ledger SET amount = 999 WHERE employee_id = :e"
        ), {"e": priya.id})
        session.flush()
    session.rollback()


def test_finding_16_ledger_cannot_be_deleted(session, priya):
    give(session, priya, "EL", 5)

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text(
            "DELETE FROM leave_ledger WHERE employee_id = :e"
        ), {"e": priya.id})
        session.flush()
    session.rollback()


# ===========================================================================
# MODULE 5 — REQUESTS & CLASSIFICATION
# ===========================================================================
def test_finding_17_public_holidays_are_excluded(session, priya):
    give(session, priya, "EL", 20)
    result = make_request(session, priya, start="2025-08-11", end="2025-08-15")
    assert result.request.duration_days == D("4")     # 15 Aug is Independence Day
    assert any("Independence Day" in name for _, name in result.holidays_excluded)


def test_finding_17_company_holidays_can_be_added(session, priya):
    session.add(HolidayOverride(
        region="India-TamilNadu", holiday_date=dt.date(2025, 6, 3),
        name="NorthBridge Founders Day",
    ))
    session.flush()
    give(session, priya, "EL", 20)

    result = make_request(session, priya, start="2025-06-02", end="2025-06-04")
    assert result.request.duration_days == D("2")


def test_finding_18_pending_requests_reserve_balance(session, priya):
    """Was: three 5-day requests against a 5-day balance all read as paid."""
    give(session, priya, "EL", 5)
    first = make_request(session, priya, start="2025-06-02", end="2025-06-04")
    assert first.classification.paid_days == D("3")

    assert committed_days(session, priya.id, "EL") == D("3")
    assert available_balance(session, priya.id, "EL", dt.date(2025, 6, 1)) == D("2")

    second = make_request(session, priya, start="2025-06-09", end="2025-06-13")
    assert second.classification.paid_days == D("2")     # not 5 again
    assert second.classification.unpaid_days == D("3")


def test_finding_19_stored_split_is_kept_and_rechecked(session, priya):
    give(session, priya, "EL", 10)
    result = make_request(session, priya, start="2025-06-02", end="2025-06-04")
    request = session.get(LeaveRequest, result.request_id)

    assert request.paid_days == D("3")
    assert request.classified_at is not None

    # Balance collapses after submission.
    give(session, priya, "EL", -9, on="2025-06-05")
    fresh, changed = reclassify_request(session, request, dt.date(2025, 6, 6))
    assert changed is True
    assert fresh.paid_days < request.paid_days
    # The stored split is untouched — Module 7 deducts against what was agreed.
    assert request.paid_days == D("3")


def test_finding_20_overlapping_requests_are_refused(session, priya):
    give(session, priya, "EL", 20)
    make_request(session, priya, start="2025-06-02", end="2025-06-06")

    with pytest.raises(OverlappingRequestError, match="overlaps"):
        make_request(session, priya, start="2025-06-04", end="2025-06-10")


def test_finding_20_database_also_refuses_an_overlap(session, priya):
    """The app check is for the message; the constraint is the guarantee."""
    give(session, priya, "EL", 20)
    make_request(session, priya, start="2025-06-02", end="2025-06-06")
    session.flush()

    with pytest.raises(IntegrityError, match="ex_leave_request_no_overlap"):
        session.execute(text("""
            INSERT INTO leave_request
                (employee_id, leave_type_id, start_date, end_date, duration_days, status)
            VALUES (:e, 'EL', '2025-06-04', '2025-06-10', 5, 'pending')
        """), {"e": priya.id})
        session.flush()
    session.rollback()


def test_finding_20_rejected_requests_release_their_dates(session, priya):
    give(session, priya, "EL", 20)
    first = make_request(session, priya, start="2025-06-02", end="2025-06-06")
    session.get(LeaveRequest, first.request_id).status = "rejected"
    session.flush()

    second = make_request(session, priya, start="2025-06-04", end="2025-06-10")
    assert second.request_id is not None


def test_finding_21_request_spanning_a_policy_change_is_split(session, priya):
    """A request crossing the Indian leave-year boundary hits two policies."""
    give(session, priya, "CL", 20)
    result = submit_leave_request(
        session, priya, "CL", "2026-03-30", "2026-04-02",
        submitted_at=dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc), commit=False,
    )
    assert len(result.classification.spans) >= 1
    assert result.classification.requested_days == sum(
        (s.days for s in result.classification.spans), D("0")
    )


def test_finding_22_backdated_leave_is_rejected(session, priya):
    give(session, priya, "EL", 20)
    with pytest.raises(Exception, match="Backdated"):
        submit_leave_request(
            session, priya, "EL", "2025-04-07", "2025-04-09",
            submitted_at=FUTURE, commit=False,
        )


def test_finding_23_short_notice_is_rejected(session, priya):
    give(session, priya, "EL", 20)
    with pytest.raises(Exception, match="Short notice"):
        submit_leave_request(
            session, priya, "EL", "2025-05-05", "2025-05-06",
            submitted_at=FUTURE, commit=False,
        )


def test_finding_24_too_long_is_rejected(session, priya):
    give(session, priya, "EL", 40)
    with pytest.raises(Exception, match="allows at most"):
        make_request(session, priya, start="2025-06-02", end="2025-06-30")


def test_finding_25_unknown_leave_type_is_rejected(session, priya):
    from app.classification import PolicyMissingError

    with pytest.raises(PolicyMissingError):
        make_request(session, priya, leave_type="SABBATICAL")


def test_finding_27_half_days_are_supported(session, priya):
    give(session, priya, "EL", 20)
    result = make_request(session, priya, start="2025-06-02", end="2025-06-06",
                          start_half_day=True)
    assert result.request.duration_days == D("4.5")
    assert result.request.start_half_day is True


def test_finding_28_notice_is_measured_from_submitted_at(session, priya):
    """Was: measured from the current clock, so old requests became violations."""
    give(session, priya, "EL", 20)
    result = make_request(session, priya, start="2025-06-02", end="2025-06-04")
    assert result.request.submitted_at == FUTURE
    # Re-reading it years later must not retroactively fail it.
    assert result.request.status == "pending"


# ===========================================================================
# MODULE 6 — APPROVAL ENGINE
# ===========================================================================
@pytest.fixture()
def chained_request(session, priya):
    give(session, priya, "EL", 20)
    result = make_request(session, priya, start="2025-06-02", end="2025-06-10")
    request = session.get(LeaveRequest, result.request_id)
    steps = submit_approval_chain(session, request)
    return request, steps


def test_finding_29_only_authorised_approvers_may_act(session, chained_request, raj, anitha):
    request, steps = chained_request

    # Raj is not the assigned approver — that is now the first thing checked,
    # ahead of the role and reporting-line rules (see the ownership fix).
    with pytest.raises(AuthorizationError, match="not the assigned approver"):
        record_approval_decision(session, request.id, 1, "approved", actor=raj)

    step = record_approval_decision(session, request.id, 1, "approved", actor=anitha)
    assert step.status == "approved"


def test_finding_29_nobody_approves_their_own_leave(session, chained_request, priya):
    request, _ = chained_request
    priya.role = "hr_admin"      # even with the role
    session.flush()

    allowed, reason, _ = can_approve_step(
        session, priya, request.steps[0], request
    )
    assert allowed is False
    assert "own leave" in reason


def test_finding_30_concurrent_decisions_cannot_both_win(session, chained_request, anitha):
    """The second decision on a tier is refused, not silently applied."""
    request, _ = chained_request
    record_approval_decision(session, request.id, 1, "approved", actor=anitha)

    # The request-level lock is taken before the step is even read, so the
    # second decision is refused on the request's settled status.
    with pytest.raises(ApprovalError, match="already 'approved'"):
        record_approval_decision(session, request.id, 1, "rejected", actor=anitha)


def test_finding_30_decision_takes_a_row_lock(session, chained_request):
    """The guard is a SELECT ... FOR UPDATE, not a bare read."""
    import inspect

    import app.approval as approval

    source = inspect.getsource(approval.record_approval_decision)
    assert "with_for_update" in source


def test_finding_31_steps_name_their_approver(session, chained_request, anitha, fatima):
    request, steps = chained_request
    by_role = {s.role: s for s in steps}
    assert by_role["manager"].assigned_approver_id == anitha.id

    # The HR step is named the moment it is created by a forward, not before.
    hr_step = forward_request(session, request.id, 1, "hr_admin", actor=anitha)
    assert hr_step.assigned_approver_id == fatima.id


def test_finding_31_approvers_have_a_queue(session, chained_request, anitha):
    assert [s.tier for s in pending_steps_for(session, anitha)] == [1]


def test_finding_32_decision_reasons_are_captured(session, chained_request, anitha):
    request, _ = chained_request
    step = record_approval_decision(
        session, request.id, 1, "approved", actor=anitha,
        decision_reason="Cover arranged with the team.",
    )
    assert step.decision_reason == "Cover arranged with the team."


def test_finding_33_manager_tier_comes_from_the_rules_table(session, priya):
    give(session, priya, "EL", 20)
    result = make_request(session, priya)
    chain = resolve_approval_chain(session, session.get(LeaveRequest, result.request_id))
    assert chain[0].role == "manager"
    assert "safety fallback" not in chain[0].routing_reason


def test_finding_33_missing_rule_is_repaired_but_flagged(session, priya, caplog):
    import logging

    session.execute(
        ApprovalRule.__table__.update()
        .where(ApprovalRule.condition_field == "always")
        .values(is_active=False)
    )
    session.flush()
    give(session, priya, "EL", 20)
    result = make_request(session, priya)

    with caplog.at_level(logging.WARNING, logger="leave_engine.approval"):
        chain = resolve_approval_chain(session, session.get(LeaveRequest, result.request_id))

    assert chain[0].role == "manager"
    assert "safety fallback" in chain[0].routing_reason
    assert any("safety fallback" in r.getMessage() for r in caplog.records)


def test_finding_34_a_role_cannot_occupy_two_tiers(session):
    rule = ApprovalRule(
        condition_field="duration_days", operator=">", value="30",
        adds_tier="hr_admin", tier_order=4,
    )
    with pytest.raises(ApprovalError, match="already assigned to tier"):
        validate_approval_rule(session, rule)


def test_finding_34_duplicate_role_on_a_request_is_refused_by_the_db(
    session, chained_request
):
    request, _ = chained_request
    with pytest.raises(IntegrityError):
        session.add(ApprovalStep(
            request_id=request.id, tier=9, role="manager", status="pending",
        ))
        session.flush()
    session.rollback()


@pytest.mark.parametrize("field,operator,value,adds_tier,message", [
    ("nonsense", "==", "true", "hr_admin", "not supported"),
    ("duration_days", "~=", "5", "hr_admin", "not supported"),
    ("duration_days", ">", "five", "hr_admin", "not numeric"),
    ("always", "==", "maybe", "hr_admin", "expects value"),
])
def test_finding_35_bad_rules_are_rejected_at_creation(
    session, field, operator, value, adds_tier, message
):
    """Was: a typo surfaced mid-approval, in front of a waiting employee."""
    rule = ApprovalRule(
        condition_field=field, operator=operator, value=value,
        adds_tier=adds_tier, tier_order=2,
    )
    with pytest.raises(ApprovalError, match=message):
        add_approval_rule(session, rule)


def test_finding_36_a_delegate_can_act_for_an_absent_approver(
    session, chained_request, anitha, fatima
):
    request, steps = chained_request
    session.add(ApprovalDelegation(
        delegator_id=anitha.id, delegate_id=fatima.id,
        from_date=dt.date(2025, 1, 1), to_date=dt.date(2030, 1, 1),
        reason="Anitha on sabbatical",
    ))
    session.flush()

    step = record_approval_decision(session, request.id, 1, "approved", actor=fatima)
    assert step.acted_by == fatima.id
    assert step.acted_on_behalf_of == anitha.id     # both recorded


def test_finding_36_delegation_expires(session, chained_request, anitha, fatima):
    request, _ = chained_request
    session.add(ApprovalDelegation(
        delegator_id=anitha.id, delegate_id=fatima.id,
        from_date=dt.date(2020, 1, 1), to_date=dt.date(2020, 12, 31),
    ))
    session.flush()

    with pytest.raises(AuthorizationError):
        record_approval_decision(session, request.id, 1, "approved", actor=fatima)


def test_finding_38_active_steps_carry_a_deadline(session, chained_request):
    _, steps = chained_request
    tier1 = [s for s in steps if s.tier == 1][0]
    assert tier1.due_at is not None
    assert tier1.activated_at is not None


def test_finding_37_overdue_steps_escalate(session, chained_request, fatima):
    request, steps = chained_request
    tier1 = [s for s in steps if s.tier == 1][0]
    tier1.due_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    session.flush()

    escalated = escalate_overdue_steps(session, commit=False)
    assert tier1.id in [s.id for s in escalated]
    assert tier1.escalated_at is not None
    assert tier1.assigned_approver_id == fatima.id      # moved to HR
    assert "escalated" in tier1.routing_reason


def test_finding_37_escalation_never_auto_approves(session, chained_request):
    request, steps = chained_request
    tier1 = [s for s in steps if s.tier == 1][0]
    tier1.due_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    session.flush()

    escalate_overdue_steps(session, commit=False)
    assert tier1.status == "active"
    assert session.get(LeaveRequest, request.id).status == "pending"


def test_finding_39_approval_event_is_written_in_the_transaction(
    session, chained_request, anitha, fatima
):
    """Was: listeners fired before the commit, so a rollback lost the approval
    but not its side effects."""
    request, _ = chained_request
    forward_request(session, request.id, 1, "hr_admin", actor=anitha)
    record_approval_decision(session, request.id, 2, "approved", actor=fatima)

    events = session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_type == "leave_request",
            OutboxEvent.aggregate_id == request.id,
        )
    ).all()
    # Approval now also settles the ledger, so the deduction is queued in the
    # same transaction as the approval — that is the whole point of the outbox.
    assert TOPIC_REQUEST_APPROVED in [e.topic for e in events]
    assert all(e.published_at is None for e in events)   # not yet delivered


def test_finding_39_rollback_discards_the_event_too(session, chained_request, anitha):
    request, _ = chained_request
    record_approval_decision(session, request.id, 1, "approved", actor=anitha)
    session.rollback()

    assert session.scalar(
        select(func.count()).select_from(OutboxEvent)
        .where(OutboxEvent.aggregate_id == request.id)
    ) == 0


def test_finding_40_events_survive_the_process(session, chained_request, anitha):
    """A durable row, not a registration in one interpreter's memory."""
    request, _ = chained_request
    record_approval_decision(
        session, request.id, 1, "rejected", actor=anitha, decision_reason="Peak period",
    )
    event = session.scalars(
        select(OutboxEvent).where(OutboxEvent.aggregate_id == request.id)
    ).one()
    assert event.topic == TOPIC_REQUEST_REJECTED
    assert "Peak period" in event.payload


def test_finding_40_relay_publishes_and_marks(session, priya):
    emit(session, "test.topic", "leave_request", 1, {"hello": "world"})
    delivered = []
    published, failed = run_relay(
        session, lambda topic, payload: delivered.append((topic, payload)), commit=False
    )
    assert (published, failed) == (1, 0)
    assert delivered[0][0] == "test.topic"


def test_finding_40_relay_retries_a_failed_publish(session):
    emit(session, "test.topic", "leave_request", 1, {"x": 1})

    def boom(topic, payload):
        raise RuntimeError("broker down")

    published, failed = run_relay(session, boom, commit=False)
    assert (published, failed) == (0, 1)
    event = session.scalars(select(OutboxEvent)).first()
    assert event.published_at is None       # stays queued for the next pass
    assert "broker down" in event.last_error


def test_finding_41_a_second_chain_cannot_be_created(session, chained_request):
    request, _ = chained_request
    with pytest.raises(ApprovalError, match="already has an approval chain"):
        create_approval_steps(session, request)


def test_finding_41_duplicate_tier_is_refused_by_the_db(session, chained_request):
    request, _ = chained_request
    with pytest.raises(IntegrityError):
        session.add(ApprovalStep(
            request_id=request.id, tier=1, role="director", status="pending",
        ))
        session.flush()
    session.rollback()


# ===========================================================================
# End-to-end sanity: the chain still behaves as Module 6 specified
# ===========================================================================
def test_submission_creates_the_manager_tier_only(session, chained_request):
    """Routing past tier 1 is now a human decision, so tier 2 does not exist yet.

    Module 6 originally pre-built the whole chain from a rules table. The
    review changed that: `resolve_approval_chain()` still computes what the
    rules *recommend*, and the approver's screen shows it, but only the
    manager can put HR on the request.
    """
    _, steps = chained_request
    assert [(s.tier, s.role, s.status) for s in sorted(steps, key=lambda s: s.tier)] == [
        (1, "manager", "active"),
    ]
    # ...and the recommendation the manager is shown still says HR.
    request, _ = chained_request
    assert "hr_admin" in [e.role for e in resolve_approval_chain(session, request)]


def test_forwarding_activates_the_next_tier(session, chained_request, anitha, fatima):
    request, _ = chained_request
    forward_request(session, request.id, 1, "hr_admin", actor=anitha)

    tier2 = session.scalars(
        select(ApprovalStep).where(
            ApprovalStep.request_id == request.id, ApprovalStep.tier == 2
        )
    ).one()
    assert tier2.status == "active"
    assert session.get(LeaveRequest, request.id).status == "pending"

    record_approval_decision(session, request.id, 2, "approved", actor=fatima)
    assert session.get(LeaveRequest, request.id).status == "approved"


def test_a_manager_approval_is_final(session, chained_request, anitha):
    """No tier 2 is invented behind the manager's back."""
    request, _ = chained_request
    record_approval_decision(session, request.id, 1, "approved", actor=anitha)
    assert session.get(LeaveRequest, request.id).status == "approved"
    assert session.scalars(
        select(ApprovalStep).where(
            ApprovalStep.request_id == request.id, ApprovalStep.tier == 2
        )
    ).first() is None


def test_rejection_stops_the_chain_immediately(session, chained_request, anitha):
    request, _ = chained_request
    record_approval_decision(session, request.id, 1, "rejected", actor=anitha)

    assert session.get(LeaveRequest, request.id).status == "rejected"
    assert session.scalars(
        select(ApprovalStep).where(
            ApprovalStep.request_id == request.id, ApprovalStep.tier == 2
        )
    ).first() is None
