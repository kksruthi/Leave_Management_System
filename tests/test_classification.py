"""Module 5 acceptance tests — Request & Classification.

The four classification cases from the brief, exactly:

  EL balance 10, request 6 EL              -> 6 paid,  0 unpaid
  EL balance  3, request 6 EL, CL bal 7    -> 6 paid,  0 unpaid  (3 EL + 3 CL)
  EL balance  0, request 4 EL, CL bal 0    -> 0 paid,  4 unpaid
  Unpaid requested directly, 2 days        -> 0 paid,  2 unpaid

Plus sum conservation everywhere, request creation, the business-day counting
convention, and a guard that this module never writes to the ledger.

Balances are set up by writing ledger rows directly (Module 3 owns accrual;
here we just need a known starting balance).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.classification import (
    Classification,
    OverlappingRequestError,
    PolicyMissingError,
    RequestValidationError,
    classify_paid_unpaid,
    submit_leave_request,
    substitution_order,
)
from app.holidays import working_days_between
from app.db import SessionLocal
from app.models import Employee, LeaveLedger, LeaveRequest
from app.policy_engine import resolve_policy

D = Decimal
AS_OF = "2025-06-01"          # Priya is in her 0-1yr bracket: EL 15/yr, paid

# Requests are now enforced against the notice period, measured from
# `submitted_at`. These tests are about classification rather than
# enforcement, so they submit far enough ahead to satisfy any policy.
SUBMITTED = dt.datetime(2025, 5, 1, 9, 0, tzinfo=dt.timezone.utc)


def submit(session, employee, leave_type, start, end, **kwargs):
    kwargs.setdefault("submitted_at", SUBMITTED)
    kwargs.setdefault("commit", False)
    return submit_leave_request(session, employee, leave_type, start, end, **kwargs)


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


@pytest.fixture()
def priya(session) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == "Priya")).one()


@pytest.fixture()
def raj(session) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == "Raj")).one()


def give(session, employee, leave_type_id, days, on="2025-05-01"):
    """Put a known balance on the ledger."""
    session.add(LeaveLedger(
        employee_id=employee.id, leave_type_id=leave_type_id, amount=D(str(days)),
        reason="test fixture", effective_date=dt.date.fromisoformat(on),
    ))
    session.flush()


# ===========================================================================
# The four cases from the brief
# ===========================================================================
def test_case_1_balance_covers_the_request(session, priya):
    give(session, priya, "EL", 10)

    result = classify_paid_unpaid(session, priya, "EL", 6, AS_OF)

    assert result.paid_days == D("6")
    assert result.unpaid_days == D("0")
    assert result.used_substitution is False


def test_case_2_substitution_draws_from_the_fallback_balance(session, priya):
    """3 from EL, then 3 from CL — checked against CL's balance, not EL's."""
    give(session, priya, "EL", 3)
    give(session, priya, "CL", 7)

    result = classify_paid_unpaid(session, priya, "EL", 6, AS_OF)

    assert result.paid_days == D("6")
    assert result.unpaid_days == D("0")
    assert result.used_substitution is True

    by_type = {d.leave_type_id: d for d in result.draws}
    assert by_type["EL"].days == D("3")
    assert by_type["EL"].balance_before == D("3")
    assert by_type["CL"].days == D("3")
    assert by_type["CL"].balance_before == D("7")     # the FALLBACK's balance
    assert by_type["CL"].is_substitution is True


def test_case_3_no_balance_anywhere_falls_through_to_unpaid(session, priya):
    # EL 0, CL 0 by virtue of an empty ledger.
    result = classify_paid_unpaid(session, priya, "EL", 4, AS_OF)

    assert result.paid_days == D("0")
    assert result.unpaid_days == D("4")
    assert result.draws[-1].leave_type_id == "Unpaid"


def test_case_4_requesting_unpaid_directly_short_circuits(session, priya):
    """Even with plenty of paid balance, Unpaid is unpaid."""
    give(session, priya, "EL", 20)
    give(session, priya, "CL", 7)

    result = classify_paid_unpaid(session, priya, "Unpaid", 2, AS_OF)

    assert result.paid_days == D("0")
    assert result.unpaid_days == D("2")
    assert result.used_substitution is False
    assert "unpaid leave type" in result.reason


# ===========================================================================
# Sum conservation — must hold in every case
# ===========================================================================
@pytest.mark.parametrize("el,cl,leave_type,requested", [
    (10, 0, "EL", 6),
    (3, 7, "EL", 6),
    (0, 0, "EL", 4),
    (0, 0, "Unpaid", 2),
    (2, 1, "EL", 10),
    (0, 5, "EL", 5),
    (5, 0, "CL", 8),
    (100, 100, "EL", 1),
    (0, 0, "SL", 3),
])
def test_sum_conservation(session, priya, el, cl, leave_type, requested):
    if el:
        give(session, priya, "EL", el)
    if cl:
        give(session, priya, "CL", cl)

    result = classify_paid_unpaid(session, priya, leave_type, requested, AS_OF)
    assert result.paid_days + result.unpaid_days == D(str(requested))
    assert result.paid_days >= 0 and result.unpaid_days >= 0


def test_conservation_is_enforced_by_the_type_itself(session):
    """A broken split can't even be constructed."""
    with pytest.raises(AssertionError, match="does not conserve days"):
        Classification(
            leave_type_id="EL", requested_days=D("5"),
            paid_days=D("2"), unpaid_days=D("2"),
        )


# ===========================================================================
# Substitution order
# ===========================================================================
def test_substitution_order_starts_with_the_requested_type(session):
    """Now read from `substitution_rules`, not a Python constant (finding 26)."""
    assert substitution_order(session, "EL") == ["EL", "CL", "Unpaid"]
    assert substitution_order(session, "CL") == ["CL", "EL", "Unpaid"]
    assert substitution_order(session, "SL") == ["SL", "CL", "EL", "Unpaid"]


def test_substitution_order_is_region_specific(session):
    """Texas has no Casual Leave, so its chain genuinely differs — in data."""
    assert substitution_order(session, "EL", "USA-Texas") == ["EL", "Unpaid"]
    assert substitution_order(session, "EL", "India-TamilNadu") == ["EL", "CL", "Unpaid"]


def test_substitution_order_is_configurable_without_code_change(session):
    from app.models import SubstitutionRule

    session.execute(
        SubstitutionRule.__table__.delete().where(
            SubstitutionRule.leave_type_id == "EL", SubstitutionRule.region.is_(None)
        )
    )
    session.add_all([
        SubstitutionRule(leave_type_id="EL", region=None, position=0,
                         fallback_leave_type_id="EL"),
        SubstitutionRule(leave_type_id="EL", region=None, position=1,
                         fallback_leave_type_id="SL"),
        SubstitutionRule(leave_type_id="EL", region=None, position=2,
                         fallback_leave_type_id="Unpaid"),
    ])
    session.flush()
    assert substitution_order(session, "EL") == ["EL", "SL", "Unpaid"]


def test_unpaid_never_substitutes(session):
    assert substitution_order(session, "Unpaid") == ["Unpaid"]


def test_unpaid_is_always_last(session):
    for leave_type in ("EL", "CL", "SL", "Unpaid"):
        assert substitution_order(session, leave_type)[-1] == "Unpaid"


def test_no_type_appears_twice_in_a_chain(session):
    for leave_type in ("EL", "CL", "SL", "Unpaid"):
        order = substitution_order(session, leave_type)
        assert len(order) == len(set(order)), f"{leave_type} chain double-counts"


def test_sl_walks_the_full_chain(session, priya):
    """SL short -> CL -> EL -> Unpaid, drawing from each in turn."""
    give(session, priya, "SL", 2)
    give(session, priya, "CL", 3)
    give(session, priya, "EL", 1)

    result = classify_paid_unpaid(session, priya, "SL", 10, AS_OF)

    by_type = {d.leave_type_id: d.days for d in result.draws}
    assert by_type == {"SL": D("2"), "CL": D("3"), "EL": D("1"), "Unpaid": D("4")}
    assert result.paid_days == D("6")
    assert result.unpaid_days == D("4")


def test_partial_coverage_reports_the_shortfall(session, priya):
    give(session, priya, "EL", 2)
    give(session, priya, "CL", 1)

    result = classify_paid_unpaid(session, priya, "EL", 10, AS_OF)
    assert result.paid_days == D("3")
    assert result.unpaid_days == D("7")
    assert result.is_fully_paid is False


def test_a_type_absent_in_the_region_is_skipped(session, raj):
    """Texas has no CL — the chain must step over it, not crash."""
    give(session, raj, "EL", 1)

    result = classify_paid_unpaid(session, raj, "EL", 5, AS_OF)
    assert result.paid_days == D("1")
    assert result.unpaid_days == D("4")
    assert "CL" not in {d.leave_type_id for d in result.draws}


def test_negative_balance_is_not_treated_as_available(session, priya):
    """An over-drawn balance must not silently subtract from the request."""
    give(session, priya, "EL", -5)
    give(session, priya, "CL", 4)

    result = classify_paid_unpaid(session, priya, "EL", 4, AS_OF)
    assert result.paid_days == D("4")      # all from CL
    assert result.unpaid_days == D("0")


# ===========================================================================
# Live balance is genuinely live
# ===========================================================================
def test_classification_reflects_a_balance_change_immediately(session, priya):
    first = classify_paid_unpaid(session, priya, "EL", 5, AS_OF)
    assert first.paid_days == D("0")

    give(session, priya, "EL", 5)

    second = classify_paid_unpaid(session, priya, "EL", 5, AS_OF)
    assert second.paid_days == D("5")


def test_balance_respects_as_of_date(session, priya):
    """Days accrued after the request date aren't available to it."""
    give(session, priya, "EL", 10, on="2025-12-01")

    assert classify_paid_unpaid(session, priya, "EL", 5, "2025-06-01").paid_days == D("0")
    assert classify_paid_unpaid(session, priya, "EL", 5, "2025-12-15").paid_days == D("5")


def test_missing_policy_is_rejected_not_silently_unpaid(session, priya):
    """Finding 25. Was: classified unpaid. Now: refused.

    Converting an unrecognised leave type into loss of pay is worse than
    refusing it, because the employee only finds out on their payslip.
    """
    with pytest.raises(PolicyMissingError, match="rejected"):
        classify_paid_unpaid(session, priya, "SABBATICAL", 3, AS_OF)


def test_missing_policy_can_still_be_classified_explicitly(session, priya):
    """The old behaviour remains available, but you have to ask for it."""
    result = classify_paid_unpaid(
        session, priya, "SABBATICAL", 3, AS_OF, strict_policy=False
    )
    assert result.unpaid_days == D("3")


# ===========================================================================
# This module must not write to the ledger
# ===========================================================================
def test_classification_writes_nothing(session, priya):
    give(session, priya, "EL", 10)
    before = session.scalar(select(func.count()).select_from(LeaveLedger))

    classify_paid_unpaid(session, priya, "EL", 6, AS_OF)

    assert session.scalar(select(func.count()).select_from(LeaveLedger)) == before


def test_submission_writes_no_ledger_rows(session, priya):
    give(session, priya, "EL", 10)
    before = session.scalar(select(func.count()).select_from(LeaveLedger))

    submit(session, priya, "EL", "2025-06-02", "2025-06-06")

    assert session.scalar(select(func.count()).select_from(LeaveLedger)) == before


def test_module_does_not_import_ledger_writes():
    """Deduction belongs to Module 6/7 — structurally, not just by convention."""
    import ast
    import inspect

    import app.classification as classification

    tree = ast.parse(inspect.getsource(classification))
    writes = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and "LeaveLedger" in ast.unparse(node)
    ]
    assert writes == [], f"module constructs ledger rows: {writes}"


def test_no_approval_steps_created(session, priya):
    """Chain resolution is Module 6's job."""
    from app.models import ApprovalStep

    before = session.scalar(select(func.count()).select_from(ApprovalStep))
    submit(session, priya, "EL", "2025-06-02", "2025-06-06")
    assert session.scalar(select(func.count()).select_from(ApprovalStep)) == before


# ===========================================================================
# submit_leave_request
# ===========================================================================
def test_submission_creates_a_pending_row(session, priya):
    result = submit(session, priya, "EL", "2025-06-02", "2025-06-06")

    row = session.get(LeaveRequest, result.request_id)
    assert row.status == "pending"
    assert row.employee_id == priya.id
    assert row.leave_type_id == "EL"
    assert row.start_date == dt.date(2025, 6, 2)
    assert row.end_date == dt.date(2025, 6, 6)
    assert row.duration_days == D("5")
    assert row.created_at is not None


def test_submission_returns_an_id_for_module_6(session, priya):
    result = submit(session, priya, "EL", "2025-06-02", "2025-06-03")
    assert isinstance(result.request_id, int)
    assert result.request_id > 0


def test_submission_classifies_at_the_same_time(session, priya):
    give(session, priya, "EL", 10)
    result = submit(session, priya, "EL", "2025-06-02", "2025-06-06")

    assert result.classification.requested_days == D("5")
    assert result.classification.paid_days == D("5")
    assert result.classification.unpaid_days == D("0")


def test_submission_classifies_against_the_start_date(session, priya):
    """A request for next April is judged by next April's policy."""
    result = submit(session, priya, "EL", "2026-04-02", "2026-04-06",
                    submitted_at=dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.timezone.utc))
    policy = resolve_policy(session, priya, "EL", "2026-04-02")
    assert policy.entitlement_days_per_year == D("18.00")   # crossed bracket
    assert result.classification.requested_days > 0


def test_submission_rejects_a_backwards_range(session, priya):
    with pytest.raises(RequestValidationError, match="on or after"):
        submit(session, priya, "EL", "2025-06-06", "2025-06-02")


def test_submission_rejects_a_weekend_only_range(session, priya):
    with pytest.raises(RequestValidationError, match="no working days"):
        submit(session, priya, "EL", "2025-06-07", "2025-06-08")


def test_submission_rejects_an_unknown_employee(session):
    with pytest.raises(RequestValidationError, match="No employee"):
        submit(session, 999_999, "EL", "2025-06-02", "2025-06-06")


def test_max_consecutive_is_enforced_not_warned(session, priya):
    """Finding 24. Was: recorded with a warning. Now: rejected."""
    with pytest.raises(RequestValidationError, match="allows at most"):
        submit(session, priya, "EL", "2025-06-02", "2025-06-30")


def test_min_notice_is_enforced_not_warned(session, priya):
    """Finding 23. India EL requires 7 days notice."""
    with pytest.raises(RequestValidationError, match="Short notice"):
        submit(session, priya, "EL", "2025-05-05", "2025-05-07",
               submitted_at=dt.datetime(2025, 5, 1, 9, 0, tzinfo=dt.timezone.utc))


def test_backdated_leave_is_rejected(session, priya):
    """Finding 22. Was: a warning. Now: refused."""
    with pytest.raises(RequestValidationError, match="Backdated"):
        submit(session, priya, "EL", "2025-04-07", "2025-04-09")


def test_an_override_reason_downgrades_a_block_to_a_warning(session, priya):
    """The escape hatch: recorded on the request and shown to approvers."""
    result = submit(session, priya, "EL", "2025-04-07", "2025-04-09",
                    override_reason="Family emergency, agreed verbally with manager")
    assert result.request.status == "pending"
    assert result.request.override_reason.startswith("Family emergency")
    assert any("overridden" in w for w in result.warnings)


def test_enforcement_can_be_relaxed_in_policy_data(session, priya):
    """A region may choose advisory limits without a code change."""
    from app.models import OrgPolicy

    session.execute(
        OrgPolicy.__table__.update()
        .where(OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "EL")
        .values(enforcement="warn")
    )
    session.flush()

    result = submit(session, priya, "EL", "2025-06-02", "2025-06-30")
    assert result.request.status == "pending"
    assert any("advisory" in w for w in result.warnings)


# ===========================================================================
# business_days_between
# ===========================================================================
@pytest.mark.parametrize("start,end,expected", [
    ("2025-06-02", "2025-06-02", 1),   # single Monday
    ("2025-06-02", "2025-06-06", 5),   # Mon-Fri, both endpoints count
    ("2025-06-02", "2025-06-08", 5),   # Mon-Sun, weekend excluded
    ("2025-06-06", "2025-06-09", 2),   # Fri-Mon spans a weekend
    ("2025-06-07", "2025-06-08", 0),   # Sat-Sun only
    ("2025-06-02", "2025-06-13", 10),  # two full weeks
    ("2025-06-02", "2025-06-27", 20),  # four weeks, no TN holiday in June
])
def test_working_days_between(session, start, end, expected):
    assert working_days_between(
        dt.date.fromisoformat(start), dt.date.fromisoformat(end),
        "India-TamilNadu", session,
    ) == expected


@pytest.mark.parametrize("start_half,end_half,expected", [
    (False, False, "5"),
    (True, False, "4.5"),
    (False, True, "4.5"),
    (True, True, "4"),
])
def test_half_day_flags(session, start_half, end_half, expected):
    """Finding 27: 4.5 days is now expressible."""
    assert working_days_between(
        dt.date(2025, 6, 2), dt.date(2025, 6, 6), "India-TamilNadu", session,
        start_half_day=start_half, end_half_day=end_half,
    ) == D(expected)


def test_single_day_half_is_half_not_zero(session):
    assert working_days_between(
        dt.date(2025, 6, 2), dt.date(2025, 6, 2), "India-TamilNadu", session,
        start_half_day=True, end_half_day=True,
    ) == D("0.5")


def test_working_days_rejects_inverted_range():
    with pytest.raises(ValueError):
        working_days_between(dt.date(2025, 6, 6), dt.date(2025, 6, 2), "India-TamilNadu")


def test_public_holidays_are_excluded(session):
    """Finding 17. Was: 5 days. Now: 4.

    2025-08-15 is Indian Independence Day, and falls on a Friday. The
    calendar comes from the `holidays` package, so Pongal and the movable
    feasts are right too, without anyone maintaining a table.
    """
    assert working_days_between(
        dt.date(2025, 8, 11), dt.date(2025, 8, 15), "India-TamilNadu", session
    ) == 4


def test_holidays_are_regional(session):
    """Thanksgiving is a Texas holiday and not an Indian one."""
    assert working_days_between(
        dt.date(2025, 11, 24), dt.date(2025, 11, 28), "USA-Texas", session
    ) == 3
    assert working_days_between(
        dt.date(2025, 11, 24), dt.date(2025, 11, 28), "India-TamilNadu", session
    ) == 5


# ===========================================================================
# Payload
# ===========================================================================
def test_classification_serialises(session, priya):
    import json

    give(session, priya, "EL", 3)
    give(session, priya, "CL", 7)
    payload = classify_paid_unpaid(session, priya, "EL", 6, AS_OF).to_dict()

    json.dumps(payload)      # must not raise
    # Raw Decimal precision is preserved in the payload; formatting for humans
    # is the caller's job (see `explain()` / app.util.format_days).
    assert Decimal(payload["paid_days"]) == D("6")
    assert payload["used_substitution"] is True
    assert len(payload["draws"]) == 2


def test_explain_names_the_draws(session, priya):
    give(session, priya, "EL", 3)
    give(session, priya, "CL", 7)
    text = classify_paid_unpaid(session, priya, "EL", 6, AS_OF).explain()
    assert "3 from EL" in text and "3 from CL" in text


def test_submitted_request_appears_on_the_dashboard(session, priya):
    """End-to-end with Module 4's pending-requests hook."""
    from app.dashboard import get_dashboard

    result = submit(session, priya, "EL", "2025-06-02", "2025-06-06")
    pending = get_dashboard(session, priya, "2025-06-01").pending_requests

    assert [p.request_id for p in pending] == [result.request_id]
    assert pending[0].duration_days == D("5")
