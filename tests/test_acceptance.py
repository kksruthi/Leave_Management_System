"""Module 1 acceptance checks.

Mirrors the four checks in the brief:
  1. Both regions seed cleanly
  2. Overlapping tenure range for the same region+leave_type is rejected
  3. Empty compliance_note is rejected
  4. Priya and Raj exist with the right join dates and regions

Each rejection is tested TWICE — once through the app-layer validator (readable
error) and once via raw SQL that bypasses it (proving the DB constraint holds).

Run against a migrated + seeded database:  pytest -q
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, engine
from app.models import ApprovalRule, Employee, OrgPolicy
from app.validation import PolicyValidationError, add_policy

D = Decimal


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


def _policy(**overrides) -> OrgPolicy:
    defaults = dict(
        region="India-TamilNadu",
        legal_entity="NorthBridge Technologies India Pvt Ltd",
        leave_type_id="EL",
        tenure_min_years=D("0.5"),
        tenure_max_years=D("2"),
        entitlement_days_per_year=D("20"),
        is_paid=True,
        accrual_method="monthly",
        carryover_max_days=D("10"),
        carryover_expiry="03-31",
        max_consecutive_days=15,
        min_notice_days=7,
        effective_from=dt.date(2020, 1, 1),
        effective_to=None,
        compliance_note="Test row.",
    )
    defaults.update(overrides)
    return OrgPolicy(**defaults)


# --- 1. seeding -------------------------------------------------------------
def test_both_regions_seeded(session):
    regions = set(session.scalars(select(OrgPolicy.region).distinct()))
    assert {"India-TamilNadu", "USA-Texas"} <= regions


def test_tamilnadu_el_tenure_ladder(session):
    rows = session.scalars(
        select(OrgPolicy)
        .where(OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "EL")
        .order_by(OrgPolicy.tenure_min_years)
    ).all()
    ladder = [(r.tenure_min_years, r.tenure_max_years, r.entitlement_days_per_year) for r in rows]
    assert ladder == [
        (D("0.00"), D("1.00"), D("15.00")),
        (D("1.00"), D("3.00"), D("18.00")),
        (D("3.00"), D("5.00"), D("24.00")),
        (D("5.00"), None, D("30.00")),
    ]


def test_texas_el_tenure_ladder(session):
    rows = session.scalars(
        select(OrgPolicy)
        .where(OrgPolicy.region == "USA-Texas", OrgPolicy.leave_type_id == "EL")
        .order_by(OrgPolicy.tenure_min_years)
    ).all()
    assert [r.entitlement_days_per_year for r in rows] == [D("10.00"), D("15.00"), D("20.00"), D("25.00")]


def test_every_seeded_policy_has_a_compliance_note(session):
    empty = session.scalar(
        select(func.count()).select_from(OrgPolicy).where(func.btrim(OrgPolicy.compliance_note) == "")
    )
    assert empty == 0


def test_approval_rules_seeded(session):
    rules = {(r.condition_field, r.operator, r.value, r.adds_tier, r.tier_order)
             for r in session.scalars(select(ApprovalRule))}
    assert ("always", "==", "true", "manager", 1) in rules
    assert ("duration_days", ">", "5", "hr_admin", 2) in rules
    assert ("duration_days", ">", "20", "director", 3) in rules
    assert ("leave_type", "==", "Unpaid", "hr_admin", 2) in rules


# --- 2. overlapping tenure range is rejected --------------------------------
def test_overlapping_tenure_rejected_by_app_layer(session):
    # 0.5-2yr overlaps both the seeded 0-1 and 1-3 brackets.
    with pytest.raises(PolicyValidationError, match="Overlapping policy"):
        add_policy(session, _policy())


def test_overlapping_tenure_rejected_by_db_constraint():
    """Bypass the Python validator entirely — the DB must still refuse."""
    with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc:
            conn.execute(text("""
                INSERT INTO org_policies (region, legal_entity, leave_type_id,
                    tenure_min_years, tenure_max_years, entitlement_days_per_year,
                    is_paid, accrual_method, carryover_max_days, min_notice_days,
                    effective_from, compliance_note)
                VALUES ('India-TamilNadu', 'NorthBridge Technologies India Pvt Ltd', 'EL',
                    0.5, 2, 20, true, 'monthly', 10, 7, DATE '2020-01-01', 'Bypass attempt.')
            """))
        assert "ex_org_policies_no_overlap" in str(exc.value)


def test_open_ended_bracket_overlap_is_caught(session):
    # 6+ years overlaps the seeded "5 years and above" row.
    with pytest.raises(PolicyValidationError, match="Overlapping policy"):
        add_policy(session, _policy(tenure_min_years=D("6"), tenure_max_years=None))


def test_flush_adjacent_brackets_are_allowed(session):
    """0-1 and 1-3 must NOT count as overlapping — brackets are half-open [min, max)."""
    policy = _policy(
        region="India-Karnataka",
        leave_type_id="EL",
        tenure_min_years=D("0"),
        tenure_max_years=D("1"),
        compliance_note="New region smoke test.",
    )
    add_policy(session, policy)
    neighbour = _policy(
        region="India-Karnataka",
        leave_type_id="EL",
        tenure_min_years=D("1"),
        tenure_max_years=D("3"),
        compliance_note="New region smoke test.",
    )
    add_policy(session, neighbour)  # must not raise
    session.rollback()


def test_same_bracket_different_region_is_allowed(session):
    """The 'add a country by inserting rows' claim — no code change needed."""
    add_policy(session, _policy(region="India-Karnataka", tenure_min_years=D("0"),
                                tenure_max_years=D("1"),
                                compliance_note="Karnataka S&E Act minimum."))
    session.rollback()


def test_non_overlapping_effective_window_is_allowed(session):
    """A superseding policy version is fine once the old window is closed off."""
    old = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == "USA-Texas",
            OrgPolicy.leave_type_id == "SL",
        )
    ).one()
    old.effective_to = dt.date(2026, 12, 31)
    session.flush()

    add_policy(session, _policy(
        region="USA-Texas",
        leave_type_id="SL",
        tenure_min_years=D("0"),
        tenure_max_years=None,
        entitlement_days_per_year=D("12"),
        accrual_method="annual_lump",
        effective_from=dt.date(2027, 1, 1),
        compliance_note="2027 uplift, HR/Legal approved.",
    ))
    session.rollback()


# --- 3. empty compliance_note is rejected -----------------------------------
@pytest.mark.parametrize("note", ["", "   ", "\n\t"])
def test_empty_compliance_note_rejected_by_app_layer(session, note):
    with pytest.raises(PolicyValidationError, match="compliance_note is required"):
        add_policy(session, _policy(region="India-Karnataka", compliance_note=note))


def test_empty_compliance_note_rejected_by_db_constraint():
    with engine.begin() as conn:
        with pytest.raises(IntegrityError) as exc:
            conn.execute(text("""
                INSERT INTO org_policies (region, legal_entity, leave_type_id,
                    tenure_min_years, tenure_max_years, entitlement_days_per_year,
                    is_paid, accrual_method, carryover_max_days, min_notice_days,
                    effective_from, compliance_note)
                VALUES ('India-Karnataka', 'NorthBridge Technologies India Pvt Ltd', 'EL',
                    0, 1, 15, true, 'monthly', 10, 7, DATE '2020-01-01', '   ')
            """))
        assert "ck_org_policies_compliance_note" in str(exc.value)


def test_null_compliance_note_rejected_by_db_constraint():
    with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text("""
                INSERT INTO org_policies (region, legal_entity, leave_type_id,
                    tenure_min_years, entitlement_days_per_year, is_paid, accrual_method,
                    carryover_max_days, min_notice_days, effective_from, compliance_note)
                VALUES ('India-Karnataka', 'X', 'EL', 0, 15, true, 'monthly', 0, 0,
                    DATE '2020-01-01', NULL)
            """))


# --- 4. test employees ready for Module 2 -----------------------------------
def test_priya_and_raj_exist(session):
    priya = session.scalars(select(Employee).where(Employee.name == "Priya")).one()
    raj = session.scalars(select(Employee).where(Employee.name == "Raj")).one()

    assert priya.region == "India-TamilNadu"
    assert priya.join_date == dt.date(2025, 4, 1)
    assert raj.region == "USA-Texas"
    assert raj.join_date == dt.date(2025, 4, 1)


def test_test_employees_have_managers(session):
    """Module 2's approval chain needs tier 1 to resolve to a real person."""
    for name in ("Priya", "Raj"):
        emp = session.scalars(select(Employee).where(Employee.name == name)).one()
        assert emp.manager_id is not None
        assert emp.manager.region == emp.region


# --- misc field-level guards ------------------------------------------------
def test_inverted_tenure_range_rejected(session):
    with pytest.raises(PolicyValidationError, match="must be greater than"):
        add_policy(session, _policy(region="India-Karnataka",
                                    tenure_min_years=D("5"), tenure_max_years=D("2")))


def test_bad_accrual_method_rejected(session):
    with pytest.raises(PolicyValidationError, match="accrual_method"):
        add_policy(session, _policy(region="India-Karnataka", accrual_method="weekly"))
