"""Module 2 acceptance tests — the Policy Engine.

Covers the six cases the brief requires:
  1-4. The Priya / Raj worked example, both regions, both sides of the
       tenure boundary.
  5.   An active employee_exceptions row beats the regional bracket.
  6.   An unmatched combination returns the explicit not-found result rather
       than raising or silently defaulting.

Plus the tenure-arithmetic edge cases that would silently corrupt every
downstream accrual if they were wrong.

Runs against the Module 1 migrated + seeded database. Every test rolls back,
so the seed is left intact.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Employee, EmployeeException
from app.policy_engine import (
    PolicyNotFoundError,
    ResolvedPolicy,
    resolve_policy,
    resolve_policy_or_raise,
    years_between,
)

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


# ===========================================================================
# 1-4. The worked example — must pass exactly
# ===========================================================================
def test_priya_before_first_anniversary(session, priya):
    r = resolve_policy(session, priya, "EL", "2025-06-01")
    assert r is not None
    assert r.entitlement_days_per_year == D("15")
    assert r.tenure_years == 0
    assert r.region == "India-TamilNadu"
    assert r.source == "org_policy"


def test_priya_after_first_anniversary(session, priya):
    r = resolve_policy(session, priya, "EL", "2026-04-01")
    assert r is not None
    assert r.entitlement_days_per_year == D("18")
    assert r.tenure_years == 1


def test_raj_before_first_anniversary(session, raj):
    r = resolve_policy(session, raj, "EL", "2025-06-01")
    assert r is not None
    assert r.entitlement_days_per_year == D("10")
    assert r.tenure_years == 0
    assert r.region == "USA-Texas"


def test_raj_after_first_anniversary(session, raj):
    r = resolve_policy(session, raj, "EL", "2026-04-01")
    assert r is not None
    assert r.entitlement_days_per_year == D("15")
    assert r.tenure_years == 1


def test_same_function_different_region_is_the_only_difference(session, priya, raj):
    """The pitch's proof point, asserted rather than claimed."""
    on = "2026-04-01"
    p = resolve_policy(session, priya, "EL", on)
    r = resolve_policy(session, raj, "EL", on)

    assert p.tenure_years == r.tenure_years          # identical tenure
    assert p.accrual_method == r.accrual_method      # identical mechanics
    assert p.entitlement_days_per_year != r.entitlement_days_per_year  # different data
    assert (p.region, r.region) == ("India-TamilNadu", "USA-Texas")


def test_monthly_accrual_matches_design_doc(session, priya, raj):
    """Section 10 of the design doc, to three decimals."""
    assert resolve_policy(session, priya, "EL", "2025-06-01").monthly_accrual == D("1.250")
    assert resolve_policy(session, priya, "EL", "2026-04-01").monthly_accrual == D("1.500")
    assert resolve_policy(session, raj, "EL", "2025-06-01").monthly_accrual == D("0.833")
    assert resolve_policy(session, raj, "EL", "2026-04-01").monthly_accrual == D("1.250")


def test_annual_lump_types_have_no_monthly_accrual(session, priya):
    cl = resolve_policy(session, priya, "CL", "2025-06-01")
    assert cl.accrual_method == "annual_lump"
    assert cl.monthly_accrual == D("0")
    assert cl.entitlement_days_per_year == D("7")


def test_full_tenure_ladder_walks_correctly(session, priya):
    """Every bracket boundary, from the same employee's timeline."""
    expected = [
        ("2025-04-01", 0, D("15")),
        ("2026-03-31", 0, D("15")),   # day before anniversary
        ("2026-04-01", 1, D("18")),   # anniversary
        ("2028-03-31", 2, D("18")),
        ("2028-04-01", 3, D("24")),
        ("2030-03-31", 4, D("24")),
        ("2030-04-01", 5, D("30")),
        ("2040-04-01", 15, D("30")),  # open-ended top bracket
    ]
    for date, tenure, days in expected:
        r = resolve_policy(session, priya, "EL", date)
        assert r is not None, f"no policy on {date}"
        assert (r.tenure_years, r.entitlement_days_per_year) == (tenure, days), date


# ===========================================================================
# 5. Exception precedence
# ===========================================================================
def test_exception_beats_regional_bracket(session, priya):
    """The regional bracket would also match — the exception must still win."""
    baseline = resolve_policy(session, priya, "EL", "2025-06-01")
    assert baseline.entitlement_days_per_year == D("15")

    session.add(EmployeeException(
        employee_id=priya.id,
        leave_type_id="EL",
        entitlement_days_per_year=D("22"),
        reason="Retention agreement signed 2025-05, approved by CHRO.",
        effective_from=dt.date(2025, 5, 1),
        effective_to=None,
    ))
    session.flush()

    r = resolve_policy(session, priya, "EL", "2025-06-01")
    assert r.entitlement_days_per_year == D("22")
    assert r.source == "exception"
    assert r.exception_id is not None
    assert "Retention agreement" in r.exception_reason


def test_exception_inherits_operational_fields_from_regional_row(session, priya):
    """An exception carries a number only — accrual mechanics come from the region.

    Without this, Module 3 would not know how to accrue an exception holder's
    leave. `policy_snapshot_id` still points at the regional row so the ledger
    stays auditable.
    """
    session.add(EmployeeException(
        employee_id=priya.id, leave_type_id="EL", entitlement_days_per_year=D("24"),
        reason="Special arrangement.", effective_from=dt.date(2025, 5, 1), effective_to=None,
    ))
    session.flush()

    r = resolve_policy(session, priya, "EL", "2025-06-01")
    assert r.accrual_method == "monthly"
    assert r.is_paid is True
    assert r.policy_snapshot_id is not None
    assert r.monthly_accrual == D("2.000")


def test_expired_exception_falls_back_to_regional_bracket(session, priya):
    session.add(EmployeeException(
        employee_id=priya.id, leave_type_id="EL", entitlement_days_per_year=D("22"),
        reason="One-year arrangement.",
        effective_from=dt.date(2025, 4, 1), effective_to=dt.date(2025, 5, 31),
    ))
    session.flush()

    inside = resolve_policy(session, priya, "EL", "2025-05-15")
    assert (inside.entitlement_days_per_year, inside.source) == (D("22"), "exception")

    after = resolve_policy(session, priya, "EL", "2025-06-01")
    assert (after.entitlement_days_per_year, after.source) == (D("15"), "org_policy")


def test_exception_is_scoped_to_one_leave_type_and_one_person(session, priya, raj):
    session.add(EmployeeException(
        employee_id=priya.id, leave_type_id="EL", entitlement_days_per_year=D("22"),
        reason="EL only.", effective_from=dt.date(2025, 4, 1), effective_to=None,
    ))
    session.flush()

    # Priya's CL is untouched...
    assert resolve_policy(session, priya, "CL", "2025-06-01").source == "org_policy"
    # ...and Raj is entirely unaffected.
    assert resolve_policy(session, raj, "EL", "2025-06-01").entitlement_days_per_year == D("10")


# ===========================================================================
# 6. Not-found is explicit, never an exception or a silent default
# ===========================================================================
def test_unknown_leave_type_returns_none(session, priya):
    assert resolve_policy(session, priya, "SABBATICAL", "2025-06-01") is None


def test_leave_type_not_offered_in_region_returns_none(session, raj):
    """CL exists for Tamil Nadu but not for Texas — a real data gap, not a bug."""
    assert resolve_policy(session, raj, "CL", "2025-06-01") is None
    assert resolve_policy(session, raj, "SL", "2025-06-01") is not None  # control


def test_date_before_any_effective_window_returns_none(session, priya):
    assert resolve_policy(session, priya, "EL", "2019-01-01") is None


def test_unknown_employee_id_returns_none(session):
    assert resolve_policy(session, 999_999, "EL", "2025-06-01") is None


def test_not_found_does_not_raise(session, priya):
    """The brief is explicit: return a marker, let the caller decide."""
    result = resolve_policy(session, priya, "NOPE", "2025-06-01")
    assert result is None  # no exception escaped


def test_strict_variant_raises_for_callers_that_want_it(session, priya):
    with pytest.raises(PolicyNotFoundError, match="No policy found"):
        resolve_policy_or_raise(session, priya, "NOPE", "2025-06-01")


def test_strict_variant_returns_normally_when_found(session, priya):
    r = resolve_policy_or_raise(session, priya, "EL", "2025-06-01")
    assert r.entitlement_days_per_year == D("15")


# ===========================================================================
# years_between — tenure arithmetic
# ===========================================================================
@pytest.mark.parametrize("join,as_of,expected", [
    ("2025-04-01", "2025-04-01", 0),   # day one
    ("2025-04-01", "2026-03-15", 0),   # brief's example: not yet
    ("2025-04-01", "2026-03-31", 0),   # day before
    ("2025-04-01", "2026-04-01", 1),   # brief's example: anniversary
    ("2025-04-01", "2026-04-02", 1),
    ("2025-04-01", "2030-04-01", 5),
    ("2024-02-29", "2025-02-28", 1),   # leap-day joiner, non-leap anniversary
    ("2024-02-29", "2028-02-29", 4),   # leap-day joiner, leap anniversary
    ("2025-12-31", "2026-01-01", 0),   # year rolls over, anniversary does not
    ("2025-04-01", "2020-01-01", 0),   # as_of before join: clamps to 0
])
def test_years_between(join, as_of, expected):
    assert years_between(dt.date.fromisoformat(join), dt.date.fromisoformat(as_of)) == expected


# ===========================================================================
# Library contract
# ===========================================================================
def test_resolve_policy_is_side_effect_free(session, priya):
    """A pure read — nothing staged, nothing written."""
    resolve_policy(session, priya, "EL", "2025-06-01")
    assert not session.new and not session.dirty and not session.deleted


def test_accepts_employee_id_as_well_as_object(session, priya):
    by_obj = resolve_policy(session, priya, "EL", "2025-06-01")
    by_id = resolve_policy(session, priya.id, "EL", "2025-06-01")
    assert by_obj.entitlement_days_per_year == by_id.entitlement_days_per_year


def test_accepts_iso_string_or_date_object(session, priya):
    as_str = resolve_policy(session, priya, "EL", "2025-06-01")
    as_date = resolve_policy(session, priya, "EL", dt.date(2025, 6, 1))
    assert as_str == as_date


def test_defaults_to_today(session, priya):
    r = resolve_policy(session, priya, "EL")
    assert r is not None
    assert r.as_of_date == dt.date.today()


def test_result_is_immutable(session, priya):
    r = resolve_policy(session, priya, "EL", "2025-06-01")
    assert isinstance(r, ResolvedPolicy)
    with pytest.raises(Exception):
        r.entitlement_days_per_year = D("999")


def test_explain_is_human_readable(session, priya):
    r = resolve_policy(session, priya, "EL", "2026-04-01")
    text = r.explain()
    assert "18" in text and "India-TamilNadu" in text and "1-year tenure" in text


def test_importable_without_db_session_module():
    """No HTTP layer, no scheduler, no implicit global session."""
    import app.policy_engine as engine
    src = engine.__dict__
    assert "SessionLocal" not in src, "library must not bind to a global session"
