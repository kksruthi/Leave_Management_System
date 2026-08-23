"""Module 3 acceptance tests — the Accrual Engine.

Covers what the brief requires:
  1-4. The Priya / Raj monthly worked example, both sides of the tenure
       boundary, asserting on the actual `leave_ledger` rows.
  5.   `run_annual_grant()` writes a full lump for annual_lump types.
  6.   A null policy skips that pair and does not crash the batch.
  7.   Every written row carries a `policy_snapshot_id` pointing at the exact
       `org_policies` row the engine resolved.

Plus the rounding, idempotency and true-up behaviour agreed for this module.

Every test runs inside a transaction that is rolled back, so the ledger is
left empty for the next test and the Module 1 seed stays intact.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.accrual import (
    REASON_ANNUAL,
    REASON_MONTHLY,
    REASON_TRUE_UP,
    active_employees,
    candidate_leave_types,
    cycle_bounds,
    run_annual_grant,
    run_monthly_accrual,
)
from app.db import SessionLocal
from app.models import Employee, EmployeeException, LeaveLedger, OrgPolicy
from app.policy_engine import resolve_policy

D = Decimal


@pytest.fixture()
def session():
    """A session whose writes are always rolled back.

    The jobs commit by default; tests pass commit=False so the whole run stays
    inside this transaction.
    """
    with SessionLocal() as s:
        yield s
        s.rollback()


@pytest.fixture()
def priya(session) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == "Priya")).one()


@pytest.fixture()
def raj(session) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == "Raj")).one()


def ledger_rows(session, employee, leave_type_id=None, reason=None):
    stmt = select(LeaveLedger).where(LeaveLedger.employee_id == employee.id)
    if leave_type_id:
        stmt = stmt.where(LeaveLedger.leave_type_id == leave_type_id)
    if reason:
        stmt = stmt.where(LeaveLedger.reason == reason)
    return session.scalars(stmt.order_by(LeaveLedger.effective_date, LeaveLedger.id)).all()


def one_row(session, employee, leave_type_id, run_date, reason=REASON_MONTHLY):
    rows = [r for r in ledger_rows(session, employee, leave_type_id, reason)
            if r.effective_date == dt.date.fromisoformat(run_date)]
    assert len(rows) == 1, f"expected exactly 1 row, got {len(rows)}"
    return rows[0]


# ===========================================================================
# 1-4. The worked example — must pass exactly
# ===========================================================================
def test_priya_monthly_accrual_before_anniversary(session, priya):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    row = one_row(session, priya, "EL", "2025-06-01")
    assert row.amount == D("1.250")
    assert row.leave_type_id == "EL"
    assert row.reason == REASON_MONTHLY
    assert row.effective_date == dt.date(2025, 6, 1)


def test_priya_monthly_accrual_after_anniversary(session, priya):
    """The whole point: same call, bigger number, zero anniversary logic."""
    run_monthly_accrual(session, "2026-04-01", commit=False)
    row = one_row(session, priya, "EL", "2026-04-01")
    assert row.amount == D("1.500")


def test_raj_monthly_accrual_before_anniversary(session, raj):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    row = one_row(session, raj, "EL", "2025-06-01")
    assert row.amount == D("0.833")          # 10/12 at 3dp
    assert round(row.amount, 2) == D("0.83")  # the brief's figure, at 2dp


def test_raj_monthly_accrual_after_anniversary(session, raj):
    run_monthly_accrual(session, "2026-04-01", commit=False)
    row = one_row(session, raj, "EL", "2026-04-01")
    assert row.amount == D("1.250")


def test_both_employees_same_call_different_amounts(session, priya, raj):
    """One batch, one code path, two regions."""
    result = run_monthly_accrual(session, "2025-06-01", commit=False)
    amounts = {
        (e.employee_name, e.leave_type_id): e.amount
        for e in result.written if e.leave_type_id == "EL"
    }
    assert amounts[("Priya", "EL")] == D("1.250")
    assert amounts[("Raj", "EL")] == D("0.833")


# ===========================================================================
# 5. Annual lump grant
# ===========================================================================
def test_annual_grant_writes_full_entitlement(session, priya):
    run_annual_grant(session, "2025-04-01", commit=False)

    cl = one_row(session, priya, "CL", "2025-04-01", reason=REASON_ANNUAL)
    sl = one_row(session, priya, "SL", "2025-04-01", reason=REASON_ANNUAL)
    assert cl.amount == D("7.000")
    assert sl.amount == D("10.000")


def test_annual_grant_respects_region(session, raj):
    """Texas has SL but no CL — the grant reflects the data, not a default.

    The amount is pro-rated, not the full 8 days: Texas runs a CALENDAR leave
    year, and Raj joined on 2025-04-01, so he is a mid-year joiner and earns
    275/365 of it. Before pro-ration existed this asserted a flat 8.000 —
    which over-granted every mid-year joiner by the part of the year they
    had not worked. See tests/test_proration.py.
    """
    run_annual_grant(session, "2025-04-01", commit=False)
    granted = one_row(session, raj, "SL", "2025-04-01", reason=REASON_ANNUAL).amount
    assert granted == D("6.027")            # 8 x 275/365
    assert ledger_rows(session, raj, "CL") == []


def test_annual_grant_is_full_for_someone_employed_all_year(session):
    """Control for the test above: a long-tenured employee still gets the lot."""
    dana = session.scalars(select(Employee).where(Employee.name == "Dana Whitfield")).one()
    run_annual_grant(session, "2025-04-01", commit=False)
    # Dated to the start of the leave year it covers (Texas: 1 Jan), not to
    # the day the job was run.
    assert one_row(session, dana, "SL", "2025-01-01", reason=REASON_ANNUAL).amount \
        == D("8.000")


def test_annual_grant_ignores_monthly_types(session, priya):
    run_annual_grant(session, "2025-04-01", commit=False)
    assert ledger_rows(session, priya, "EL") == []


def test_monthly_job_ignores_annual_lump_types(session, priya):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    assert ledger_rows(session, priya, "CL") == []
    assert ledger_rows(session, priya, "SL") == []


def test_unpaid_type_never_accrues(session, priya):
    """accrual_method 'none' must produce nothing in either job."""
    run_monthly_accrual(session, "2025-06-01", commit=False)
    run_annual_grant(session, "2025-06-01", commit=False)
    assert ledger_rows(session, priya, "Unpaid") == []


# ===========================================================================
# 6. A null policy skips, it does not crash the batch
# ===========================================================================
def _punch_bracket_gap(session):
    """Delete only the 0-1yr EL bracket for Tamil Nadu.

    This is the real-world null-policy case: EL is still configured for the
    region, so it stays a candidate leave type, but no bracket covers a
    zero-tenure employee. resolve_policy() returns None and the batch has to
    cope. Deleting ALL the EL rows would be a weaker test — the leave type
    would vanish from the candidate list and never be resolved at all.
    """
    session.execute(
        OrgPolicy.__table__.delete().where(
            OrgPolicy.region == "India-TamilNadu",
            OrgPolicy.leave_type_id == "EL",
            OrgPolicy.tenure_min_years == D("0"),
        )
    )
    session.flush()


def test_missing_policy_skips_without_crashing(session, priya):
    _punch_bracket_gap(session)
    assert resolve_policy(session, priya, "EL", "2025-06-01") is None  # precondition

    result = run_monthly_accrual(session, "2025-06-01", commit=False)  # must not raise

    assert ledger_rows(session, priya, "EL") == []
    skipped = [s for s in result.skipped if s.employee_name == "Priya" and s.leave_type_id == "EL"]
    assert len(skipped) == 1
    assert "no match" in skipped[0].reason


def test_skip_does_not_stop_other_employees(session, priya, raj):
    """Raj must still accrue even though Priya's bracket is broken."""
    _punch_bracket_gap(session)

    run_monthly_accrual(session, "2025-06-01", commit=False)
    assert ledger_rows(session, priya, "EL") == []
    assert one_row(session, raj, "EL", "2025-06-01").amount == D("0.833")


def test_skip_does_not_stop_other_leave_types(session, priya):
    """Priya's EL is broken; her CL grant must still go through."""
    _punch_bracket_gap(session)

    run_annual_grant(session, "2025-06-01", commit=False)
    # India's leave year starts 1 April, which is also Priya's join date.
    assert one_row(session, priya, "CL", "2025-04-01", reason=REASON_ANNUAL).amount == D("7.000")


def test_skip_is_logged_with_identifying_detail(session, caplog):
    _punch_bracket_gap(session)

    with caplog.at_level(logging.WARNING, logger="leave_engine.accrual"):
        run_monthly_accrual(session, "2025-06-01", commit=False)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Priya" in m and "EL" in m for m in warnings), warnings


def test_future_joiner_does_not_accrue(session):
    """Someone who hasn't started yet must not bank leave."""
    newbie = Employee(name="Future Hire", email="future@northbridge.example",
                      join_date=dt.date(2030, 1, 1), region="India-TamilNadu")
    session.add(newbie)
    session.flush()

    run_monthly_accrual(session, "2025-06-01", commit=False)
    assert ledger_rows(session, newbie) == []
    assert newbie not in active_employees(session, dt.date(2025, 6, 1))


# ===========================================================================
# 7. policy_snapshot_id points at the exact row that was resolved
# ===========================================================================
def test_snapshot_id_matches_resolved_policy(session, priya, raj):
    for employee, run_date in [(priya, "2025-06-01"), (raj, "2025-06-01"),
                               (priya, "2026-04-01"), (raj, "2026-04-01")]:
        run_monthly_accrual(session, run_date, commit=False)
        row = one_row(session, employee, "EL", run_date)
        expected = resolve_policy(session, employee, "EL", run_date)
        assert row.policy_snapshot_id == expected.policy_snapshot_id


def test_snapshot_id_changes_when_the_bracket_changes(session, priya):
    """Two different months resolve to two different policy rows."""
    run_monthly_accrual(session, "2025-06-01", commit=False)
    run_monthly_accrual(session, "2026-04-01", commit=False)

    before = one_row(session, priya, "EL", "2025-06-01")
    after = one_row(session, priya, "EL", "2026-04-01")
    assert before.policy_snapshot_id != after.policy_snapshot_id


def test_every_written_row_has_a_valid_snapshot_id(session):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    run_annual_grant(session, "2025-06-01", commit=False)

    orphaned = session.scalar(
        select(func.count())
        .select_from(LeaveLedger)
        .outerjoin(OrgPolicy, LeaveLedger.policy_snapshot_id == OrgPolicy.id)
        .where(OrgPolicy.id.is_(None))
    )
    assert orphaned == 0


def test_snapshot_makes_past_accruals_explainable(session, priya):
    """'Why did I get 1.5 days that month?' answered from the ledger row alone."""
    run_monthly_accrual(session, "2026-04-01", commit=False)
    row = one_row(session, priya, "EL", "2026-04-01")

    policy = session.get(OrgPolicy, row.policy_snapshot_id)
    assert policy.entitlement_days_per_year == D("18.00")
    assert policy.region == "India-TamilNadu"
    assert policy.tenure_min_years == D("1.00")
    assert policy.compliance_note


# ===========================================================================
# Idempotency
# ===========================================================================
def test_rerunning_the_same_date_does_not_double_credit(session, priya):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    second = run_monthly_accrual(session, "2025-06-01", commit=False)

    assert len(ledger_rows(session, priya, "EL", REASON_MONTHLY)) == 1
    assert any(s.employee_name == "Priya" and "already has" in s.reason for s in second.skipped)
    assert not any(e.employee_name == "Priya" and e.leave_type_id == "EL"
                   for e in second.written)


def test_rerunning_annual_grant_does_not_double_credit(session, priya):
    run_annual_grant(session, "2025-04-01", commit=False)
    run_annual_grant(session, "2025-04-01", commit=False)
    assert len(ledger_rows(session, priya, "CL", REASON_ANNUAL)) == 1


def test_different_dates_are_not_treated_as_duplicates(session, priya):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    run_monthly_accrual(session, "2025-07-01", commit=False)
    assert len(ledger_rows(session, priya, "EL", REASON_MONTHLY)) == 2


# ===========================================================================
# Rounding and the year-end true-up
# ===========================================================================
def test_twelve_runs_sum_exactly_to_entitlement_for_raj(session, raj):
    """10/12 doesn't divide evenly — the true-up closes the 0.004 gap."""
    for date in [f"2025-{m:02d}-01" for m in range(4, 13)] + \
                [f"2026-{m:02d}-01" for m in range(1, 4)]:
        run_monthly_accrual(session, date, commit=False)

    rows = ledger_rows(session, raj, "EL")
    assert len(rows) == 13  # 12 monthly + 1 true-up
    assert sum((D(r.amount) for r in rows), D("0")) == D("10.000")

    true_ups = [r for r in rows if r.reason == REASON_TRUE_UP]
    assert len(true_ups) == 1
    assert true_ups[0].amount == D("0.004")
    assert true_ups[0].effective_date == dt.date(2026, 3, 1)


def test_no_true_up_when_division_is_exact(session, priya):
    """15/12 = 1.25 exactly — nothing to correct, no noise row."""
    for date in [f"2025-{m:02d}-01" for m in range(4, 13)] + \
                [f"2026-{m:02d}-01" for m in range(1, 4)]:
        run_monthly_accrual(session, date, commit=False)

    rows = ledger_rows(session, priya, "EL")
    assert [r for r in rows if r.reason == REASON_TRUE_UP] == []
    assert sum((D(r.amount) for r in rows), D("0")) == D("15.000")


def test_no_true_up_on_an_incomplete_cycle(session, raj):
    """Only 3 of 12 months run — the gap is under-accrual, not rounding."""
    for date in ["2026-01-01", "2026-02-01", "2026-03-01"]:
        run_monthly_accrual(session, date, commit=False)

    assert [r for r in ledger_rows(session, raj, "EL") if r.reason == REASON_TRUE_UP] == []


def test_true_up_can_be_disabled(session, raj):
    for date in [f"2025-{m:02d}-01" for m in range(4, 13)] + \
                [f"2026-{m:02d}-01" for m in range(1, 4)]:
        run_monthly_accrual(session, date, commit=False, true_up=False)

    rows = ledger_rows(session, raj, "EL")
    assert len(rows) == 12
    assert sum((D(r.amount) for r in rows), D("0")) == D("9.996")  # the drift, visible


def test_cycle_bounds_follow_the_anniversary(priya):
    start, end = cycle_bounds(dt.date(2025, 4, 1), dt.date(2025, 6, 1))
    assert (start, end) == (dt.date(2025, 4, 1), dt.date(2026, 4, 1))

    start, end = cycle_bounds(dt.date(2025, 4, 1), dt.date(2026, 4, 1))
    assert (start, end) == (dt.date(2026, 4, 1), dt.date(2027, 4, 1))


# ===========================================================================
# Dynamism — the design's central claim, asserted end to end
# ===========================================================================
def test_accrual_amount_steps_up_across_the_bracket_with_no_special_casing(session, priya):
    """24 consecutive runs. The step happens because policy is re-read, full stop."""
    dates = [f"2025-{m:02d}-01" for m in range(4, 13)] + \
            [f"2026-{m:02d}-01" for m in range(1, 13)] + ["2027-01-01"]
    for date in dates:
        run_monthly_accrual(session, date, commit=False)

    by_date = {r.effective_date: D(r.amount)
               for r in ledger_rows(session, priya, "EL", REASON_MONTHLY)}

    assert by_date[dt.date(2026, 3, 1)] == D("1.250")   # last month of 0-1yr
    assert by_date[dt.date(2026, 4, 1)] == D("1.500")   # first month of 1-3yr
    assert by_date[dt.date(2027, 1, 1)] == D("1.500")   # still in 1-3yr


def test_exception_holder_accrues_the_exception_amount(session, priya):
    """Module 2's precedence flows straight through into the ledger."""
    session.add(EmployeeException(
        employee_id=priya.id, leave_type_id="EL", entitlement_days_per_year=D("24"),
        reason="Retention agreement.", effective_from=dt.date(2025, 4, 1), effective_to=None,
    ))
    session.flush()

    run_monthly_accrual(session, "2025-06-01", commit=False)
    assert one_row(session, priya, "EL", "2025-06-01").amount == D("2.000")  # 24/12


def test_policy_edit_changes_next_run_without_touching_this_module(session, priya):
    """HR raises the number; the very next run reflects it. No deploy."""
    run_monthly_accrual(session, "2025-06-01", commit=False)
    assert one_row(session, priya, "EL", "2025-06-01").amount == D("1.250")

    bracket = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == "India-TamilNadu",
            OrgPolicy.leave_type_id == "EL",
            OrgPolicy.tenure_min_years == D("0"),
        )
    ).one()
    bracket.entitlement_days_per_year = D("24")
    session.flush()

    run_monthly_accrual(session, "2025-07-01", commit=False)
    assert one_row(session, priya, "EL", "2025-07-01").amount == D("2.000")


# ===========================================================================
# Batch reporting contract
# ===========================================================================
def test_result_reports_written_and_skipped(session):
    result = run_monthly_accrual(session, "2025-06-01", commit=False)
    assert result.job == "monthly accrual"
    assert result.run_date == dt.date(2025, 6, 1)
    assert result.written and all(e.ledger_id is not None for e in result.written)
    assert result.total_days > 0
    assert "monthly accrual for 2025-06-01" in result.summary()


def test_managers_accrue_too(session):
    """'active_employees' means everyone who has joined, not just ICs."""
    result = run_monthly_accrual(session, "2025-06-01", commit=False)
    names = {e.employee_name for e in result.written}
    assert {"Priya", "Raj", "Anitha Rajan", "Dana Whitfield"} <= names


def test_candidate_leave_types_covers_region_and_exceptions(session, raj):
    assert "CL" not in candidate_leave_types(session, raj)  # not offered in Texas

    session.add(EmployeeException(
        employee_id=raj.id, leave_type_id="CL", entitlement_days_per_year=D("5"),
        reason="Negotiated on hire.", effective_from=dt.date(2025, 4, 1), effective_to=None,
    ))
    session.flush()
    assert "CL" in candidate_leave_types(session, raj)


def test_accrual_does_not_reimplement_policy_logic():
    """Module 3 must call Module 2, never read entitlements out of org_policies itself.

    The one permitted org_policies query is `select(OrgPolicy.leave_type_id)`
    in candidate_leave_types — a listing of which types exist for a region,
    not a policy decision. Anything selecting whole OrgPolicy rows here would
    mean this module had started resolving policy on its own.
    """
    import inspect

    import app.accrual as accrual

    source = inspect.getsource(accrual)
    assert "resolve_policy(" in source, "must delegate to Module 2"
    assert "select(OrgPolicy)" not in source, "must not load policy rows directly"
    assert "tenure_min_years" not in source, "must not re-implement bracket matching"


def test_accrual_reads_entitlement_only_from_resolved_policy(session, priya):
    """Sanity: the amount tracks the resolved policy, whatever it says."""
    resolved = resolve_policy(session, priya, "EL", "2025-06-01")
    run_monthly_accrual(session, "2025-06-01", commit=False)
    written = one_row(session, priya, "EL", "2025-06-01")
    assert written.amount == resolved.monthly_accrual
