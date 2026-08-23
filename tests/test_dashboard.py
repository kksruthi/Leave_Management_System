"""Module 4 acceptance tests — Live Balance & Dashboard.

Covers what the brief requires:
  1. The worked example: four 1.25-day accruals -> 5.0 days on 2025-09-15.
  2. Future-dated ledger entries are excluded (effective_date <= as_of_date).
  3. Two consecutive calls with no writes between return the identical number.
  4. A new ledger row is visible on the very next call — no cache, no staleness.
  5. `get_dashboard()` entitlement always matches what `resolve_policy()`
     resolves for that `as_of_date` (15 days/yr at 2025-06-01, 18 at 2026-04-01).

Ledger rows are produced by calling Module 3's real accrual job rather than
hand-inserting fixtures, so these tests exercise the actual integration.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accrual import run_annual_grant, run_monthly_accrual
from app.dashboard import (
    Dashboard,
    get_balances,
    get_dashboard,
    get_live_balance,
)
from app.db import SessionLocal
from app.models import ApprovalStep, Employee, LeaveLedger, LeaveRequest, OrgPolicy
from app.policy_engine import resolve_policy

D = Decimal


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


@pytest.fixture()
def four_accruals(session, priya):
    """June-September 2025: four monthly runs at 1.25 days each."""
    for month in (6, 7, 8, 9):
        run_monthly_accrual(session, f"2025-{month:02d}-01", commit=False)
    return priya


# ===========================================================================
# 1. The worked example
# ===========================================================================
def test_balance_after_four_accruals(session, four_accruals, priya):
    assert get_live_balance(session, priya.id, "EL", "2025-09-15") == D("5.000")


def test_balance_keeps_growing_within_the_leave_year(session, four_accruals, priya):
    """Accruals accumulate across the year, and across a tenure bracket."""
    run_monthly_accrual(session, "2026-01-01", commit=False)   # still leave year 2025
    assert get_live_balance(session, priya.id, "EL", "2026-01-15") == D("6.250")


def test_a_new_leave_year_does_not_inherit_the_old_balance(session, four_accruals, priya):
    """The Fatima bug: the ledger goes back years, a spendable balance does not.

    India's leave year ends 31 March. On 15 April 2026 the only EL Priya can
    take is what she has accrued since 1 April — 1.5 days — plus whatever the
    year-end carry-over job explicitly moved forward, which here is nothing
    because that job has not run. Summing the whole ledger would report 6.5
    and let her spend days that belonged to a closed year.
    """
    run_monthly_accrual(session, "2026-04-01", commit=False)
    assert get_live_balance(session, priya.id, "EL", "2026-03-31") == D("5.000")
    assert get_live_balance(session, priya.id, "EL", "2026-04-15") == D("1.500")


def test_balance_is_point_in_time_not_just_current(session, four_accruals, priya):
    """Each intermediate date reports the balance as it stood then."""
    assert get_live_balance(session, priya.id, "EL", "2025-06-15") == D("1.250")
    assert get_live_balance(session, priya.id, "EL", "2025-07-15") == D("2.500")
    assert get_live_balance(session, priya.id, "EL", "2025-08-15") == D("3.750")
    assert get_live_balance(session, priya.id, "EL", "2025-09-15") == D("5.000")


# ===========================================================================
# 2. Future-dated entries are excluded
# ===========================================================================
def test_future_dated_entry_is_excluded(session, four_accruals, priya):
    run_monthly_accrual(session, "2026-01-01", commit=False)

    # As of Sept 2025 the January 2026 entry does not exist yet.
    assert get_live_balance(session, priya.id, "EL", "2025-09-15") == D("5.000")
    # As of January 2026 it does — same leave year, so it simply adds.
    assert get_live_balance(session, priya.id, "EL", "2026-01-15") == D("6.250")


def test_boundary_date_is_inclusive(session, four_accruals, priya):
    """effective_date <= as_of_date — an entry dated exactly today counts."""
    assert get_live_balance(session, priya.id, "EL", "2025-09-01") == D("5.000")
    assert get_live_balance(session, priya.id, "EL", "2025-08-31") == D("3.750")


def test_balance_before_any_history_is_zero_not_none(session, priya):
    balance = get_live_balance(session, priya.id, "EL", "2020-01-01")
    assert balance == D("0")
    assert isinstance(balance, Decimal)


def test_unknown_employee_raises_rather_than_reading_as_zero(session):
    """Finding 13. Was: Decimal('0'). Now: an error.

    A missing employee and an employee with no leave are different facts.
    Conflating them hides typos and broken joins behind a plausible number.
    """
    from app.dashboard import EmployeeNotFoundError

    with pytest.raises(EmployeeNotFoundError, match="No employee with id"):
        get_live_balance(session, 999_999, "EL", "2025-09-15")


def test_zero_can_still_be_requested_explicitly(session):
    assert get_live_balance(session, 999_999, "EL", "2025-09-15", strict=False) == D("0")


# ===========================================================================
# 3 & 4. Freshness — no cache, no stale reads
# ===========================================================================
def test_repeated_calls_return_identical_values(session, four_accruals, priya):
    first = get_live_balance(session, priya.id, "EL", "2025-09-15")
    second = get_live_balance(session, priya.id, "EL", "2025-09-15")
    third = get_live_balance(session, priya.id, "EL", "2025-09-15")
    assert first == second == third == D("5.000")


def test_new_ledger_row_is_visible_on_the_very_next_call(session, four_accruals, priya):
    before = get_live_balance(session, priya.id, "EL", "2025-10-15")
    assert before == D("5.000")

    session.add(LeaveLedger(
        employee_id=priya.id, leave_type_id="EL", amount=D("2.000"),
        reason="manual adjustment", effective_date=dt.date(2025, 10, 1),
    ))
    session.flush()

    assert get_live_balance(session, priya.id, "EL", "2025-10-15") == D("7.000")


def test_deduction_reduces_the_balance_immediately(session, four_accruals, priya):
    """Signed amounts — Module 6's deductions need no new code path here."""
    session.add(LeaveLedger(
        employee_id=priya.id, leave_type_id="EL", amount=D("-3.000"),
        reason="leave taken: 2025-09-20 to 2025-09-22", effective_date=dt.date(2025, 9, 20),
    ))
    session.flush()

    assert get_live_balance(session, priya.id, "EL", "2025-09-30") == D("2.000")


def test_nothing_is_cached_between_dashboard_calls(session, four_accruals, priya):
    first = get_dashboard(session, priya, "2025-10-15")
    assert first.leave_type("EL").balance == D("5.000")

    session.add(LeaveLedger(
        employee_id=priya.id, leave_type_id="EL", amount=D("1.000"),
        reason="correction", effective_date=dt.date(2025, 10, 2),
    ))
    session.flush()

    second = get_dashboard(session, priya, "2025-10-15")
    assert second.leave_type("EL").balance == D("6.000")


def test_module_declares_no_cache():
    """Guard the design rule structurally, not just in prose.

    Checks the parsed AST rather than raw text — the module docstring
    legitimately contains the word 'lru_cache' while explaining why there
    isn't one, and a substring search would flag its own documentation.
    """
    import ast
    import inspect

    import app.dashboard as dashboard

    tree = ast.parse(inspect.getsource(dashboard))

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "functools" not in imported, "caching helpers have no place in a live balance"

    decorators = [
        ast.unparse(d)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for d in node.decorator_list
    ]
    assert not any("cache" in d for d in decorators), f"cached function found: {decorators}"


def test_dashboard_does_not_write(session, four_accruals, priya):
    get_dashboard(session, priya, "2025-09-15")
    assert not session.new and not session.dirty and not session.deleted


# ===========================================================================
# 5. Entitlement always matches resolve_policy()
# ===========================================================================
def test_entitlement_before_the_bracket_crossing(session, priya):
    view = get_dashboard(session, priya, "2025-06-01").leave_type("EL")
    assert view.entitlement_days_per_year == D("15.00")


def test_entitlement_after_the_bracket_crossing(session, priya):
    view = get_dashboard(session, priya, "2026-04-01").leave_type("EL")
    assert view.entitlement_days_per_year == D("18.00")


@pytest.mark.parametrize("as_of", [
    "2025-06-01", "2026-03-31", "2026-04-01", "2028-04-01", "2030-04-01",
])
def test_entitlement_never_diverges_from_the_policy_engine(session, priya, as_of):
    """The acceptance rule: never a stale or hardcoded entitlement."""
    view = get_dashboard(session, priya, as_of).leave_type("EL")
    expected = resolve_policy(session, priya, "EL", as_of)
    assert view.entitlement_days_per_year == expected.entitlement_days_per_year
    assert view.accrual_method == expected.accrual_method
    assert view.is_paid == expected.is_paid


def test_hr_policy_edit_shows_on_the_next_dashboard_load(session, priya):
    before = get_dashboard(session, priya, "2025-06-01").leave_type("EL")
    assert before.entitlement_days_per_year == D("15.00")

    bracket = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == "India-TamilNadu",
            OrgPolicy.leave_type_id == "EL",
            OrgPolicy.tenure_min_years == D("0"),
        )
    ).one()
    bracket.entitlement_days_per_year = D("21")
    session.flush()

    after = get_dashboard(session, priya, "2025-06-01").leave_type("EL")
    assert after.entitlement_days_per_year == D("21")


# ===========================================================================
# Dashboard payload shape
# ===========================================================================
def test_dashboard_covers_all_leave_types_in_one_call(session, priya):
    dash = get_dashboard(session, priya, "2025-06-01")
    types = {lt.leave_type_id for lt in dash.leave_types}
    assert types == {"EL", "CL", "SL", "Unpaid"}


def test_dashboard_reflects_regional_differences(session, raj):
    """Texas has no CL — the payload shows what the data says."""
    types = {lt.leave_type_id for lt in get_dashboard(session, raj, "2025-06-01").leave_types}
    assert types == {"EL", "SL", "Unpaid"}


def test_dashboard_header_fields(session, priya):
    dash = get_dashboard(session, priya, "2026-04-01")
    assert isinstance(dash, Dashboard)
    assert dash.name == "Priya"
    assert dash.region == "India-TamilNadu"
    assert dash.join_date == dt.date(2025, 4, 1)
    assert dash.tenure_years == 1
    assert dash.as_of_date == dt.date(2026, 4, 1)


def test_unknown_employee_dashboard_raises(session):
    from app.dashboard import EmployeeNotFoundError

    with pytest.raises(EmployeeNotFoundError):
        get_dashboard(session, 999_999)
    assert get_dashboard(session, 999_999, strict=False) is None


def test_accepts_id_or_object(session, priya):
    by_obj = get_dashboard(session, priya, "2025-06-01")
    by_id = get_dashboard(session, priya.id, "2025-06-01")
    assert by_obj.to_dict() == by_id.to_dict()


# ---- history ---------------------------------------------------------------
def test_history_carries_running_totals(session, four_accruals, priya):
    history = get_dashboard(session, priya, "2025-09-15").leave_type("EL").history
    assert [h.running_total for h in history] == [D("1.250"), D("2.500"), D("3.750"), D("5.000")]
    assert [h.effective_date.month for h in history] == [6, 7, 8, 9]
    assert all(h.reason == "monthly accrual" for h in history)


def test_history_rows_link_back_to_the_policy_that_produced_them(session, four_accruals, priya):
    """The 'why did I get this amount?' drill-down."""
    line = get_dashboard(session, priya, "2025-09-15").leave_type("EL").history[0]
    policy = session.get(OrgPolicy, line.policy_snapshot_id)
    assert policy.entitlement_days_per_year == D("15.00")
    assert policy.compliance_note


def test_history_excludes_future_entries(session, four_accruals, priya):
    run_monthly_accrual(session, "2026-04-01", commit=False)
    history = get_dashboard(session, priya, "2025-09-15").leave_type("EL").history
    assert len(history) == 4
    assert max(h.effective_date for h in history) == dt.date(2025, 9, 1)


# ---- next accrual projection ----------------------------------------------
def test_next_accrual_projects_the_first_of_next_month(session, priya):
    nxt = get_dashboard(session, priya, "2025-06-15").leave_type("EL").next_accrual
    assert nxt.date == dt.date(2025, 7, 1)
    assert nxt.amount == D("1.250")


def test_next_accrual_shows_a_bracket_crossing_before_it_happens(session, priya):
    """On the March dashboard, April's larger amount is already visible."""
    nxt = get_dashboard(session, priya, "2026-03-15").leave_type("EL").next_accrual
    assert nxt.date == dt.date(2026, 4, 1)
    assert nxt.amount == D("1.500")           # not 1.250
    assert "rises from 15 to 18" in nxt.note


def test_no_crossing_note_when_the_bracket_is_unchanged(session, priya):
    assert get_dashboard(session, priya, "2025-06-15").leave_type("EL").next_accrual.note is None


def test_next_accrual_rolls_over_the_year_end(session, priya):
    assert get_dashboard(session, priya, "2025-12-20").leave_type("EL").next_accrual.date \
        == dt.date(2026, 1, 1)


def test_annual_lump_projects_the_next_leave_year(session, priya):
    nxt = get_dashboard(session, priya, "2025-06-01").leave_type("CL").next_accrual
    assert nxt.date == dt.date(2026, 4, 1)   # next anniversary
    assert nxt.amount == D("7.00")
    assert "lump sum" in nxt.note


def test_unpaid_has_no_next_accrual(session, priya):
    view = get_dashboard(session, priya, "2025-06-01").leave_type("Unpaid")
    assert view.accrual_method == "none"
    assert view.next_accrual is None


# ---- missing policy --------------------------------------------------------
def test_balance_still_shown_when_no_policy_resolves(session, four_accruals, priya):
    """An earned balance must not vanish because HR retired a bracket.

    The bracket is EXPIRED, not deleted — Module 1's `ON DELETE RESTRICT` on
    `leave_ledger.policy_snapshot_id` makes deleting a policy that has already
    produced accruals impossible, which is the point of that constraint.
    Closing off `effective_to` is the real-world action.
    """
    bracket = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == "India-TamilNadu",
            OrgPolicy.leave_type_id == "EL",
            OrgPolicy.tenure_min_years == D("0"),
        )
    ).one()
    bracket.effective_to = dt.date(2025, 8, 31)
    session.flush()

    assert resolve_policy(session, priya, "EL", "2025-09-15") is None  # precondition

    view = get_dashboard(session, priya, "2025-09-15").leave_type("EL")
    assert view.policy_found is False
    assert view.balance == D("5.000")          # still theirs
    assert view.entitlement_days_per_year is None
    assert view.next_accrual is None
    assert "Contact HR" in view.note
    assert len(view.history) == 4              # history survives too


def test_used_policy_rows_cannot_be_deleted(session, four_accruals):
    """Module 1's RESTRICT, asserted from the module that depends on it."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        session.execute(
            OrgPolicy.__table__.delete().where(
                OrgPolicy.region == "India-TamilNadu",
                OrgPolicy.leave_type_id == "EL",
                OrgPolicy.tenure_min_years == D("0"),
            )
        )
        session.flush()
    session.rollback()


# ---- pending requests hook -------------------------------------------------
def test_pending_requests_is_empty_but_present(session, priya):
    dash = get_dashboard(session, priya, "2025-06-01")
    assert dash.pending_requests == []
    assert "pending_requests" in dash.to_dict()


def test_pending_requests_populate_when_modules_5_6_write_rows(session, priya):
    """Forward-compatibility check for the data Modules 5/6 will produce."""
    req = LeaveRequest(
        employee_id=priya.id, leave_type_id="EL",
        start_date=dt.date(2025, 10, 1), end_date=dt.date(2025, 10, 7),
        duration_days=D("7"), status="pending",
        # `as_of_date` now bounds pending requests too (finding 12), so the
        # request must have been submitted by the date being viewed.
        submitted_at=dt.datetime(2025, 9, 1, 9, 0, tzinfo=dt.timezone.utc),
    )
    session.add(req)
    session.flush()
    session.add_all([
        ApprovalStep(request_id=req.id, tier=1, role="manager", status="approved"),
        ApprovalStep(request_id=req.id, tier=2, role="hr_admin", status="active",
                     routing_reason="HR added because duration (7 days) exceeds 5-day threshold"),
    ])
    session.flush()

    pending = get_dashboard(session, priya, "2025-10-15").pending_requests
    assert len(pending) == 1
    assert pending[0].current_tier == 2
    assert pending[0].current_role == "hr_admin"
    assert "exceeds 5-day threshold" in pending[0].routing_reason
    assert len(pending[0].chain) == 2


# ---- serialisation ---------------------------------------------------------
def test_to_dict_is_json_serialisable(session, four_accruals, priya):
    import json

    payload = get_dashboard(session, priya, "2025-09-15").to_dict()
    text = json.dumps(payload)          # must not raise
    assert '"balance": "5.000"' in text
    assert '"as_of_date": "2025-09-15"' in text


def test_to_dict_can_emit_numbers(session, four_accruals, priya):
    payload = get_dashboard(session, priya, "2025-09-15").to_dict(decimals_as_str=False)
    el = next(lt for lt in payload["leave_types"] if lt["leave_type_id"] == "EL")
    assert el["balance"] == 5.0


# ---- get_balances convenience ---------------------------------------------
def test_get_balances_matches_individual_lookups(session, four_accruals, priya):
    run_annual_grant(session, "2025-04-01", commit=False)
    balances = get_balances(session, priya.id, "2025-09-15")

    assert balances["EL"] == D("5.000")
    assert balances["CL"] == D("7.000")
    assert balances["SL"] == D("10.000")
    for leave_type, total in balances.items():
        assert total == get_live_balance(session, priya.id, leave_type, "2025-09-15")
