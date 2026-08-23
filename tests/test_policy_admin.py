"""Tests for the annual policy lifecycle (HR's yearly review)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Employee, OrgPolicy
from app.policy_admin import (
    PolicyAdminError,
    leave_year_bounds,
    policy_history,
    policy_set,
    preview_roll_forward,
    roll_forward_year,
)
from app.policy_engine import resolve_policy

D = Decimal


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


@pytest.fixture()
def hr(session):
    person = session.scalars(select(Employee).where(Employee.name == "Fatima Khan")).one()
    return person


@pytest.fixture()
def priya(session):
    return session.scalars(select(Employee).where(Employee.name == "Priya")).one()


def roll(session, hr, **kwargs):
    kwargs.setdefault("region", "India-TamilNadu")
    kwargs.setdefault("from_year", 2025)
    kwargs.setdefault("actor", hr)
    kwargs.setdefault("change_reason", "FY2026 annual policy review")
    kwargs.setdefault("commit", False)
    kwargs.setdefault("default_leave_year_end", "03-31")
    return roll_forward_year(session, **kwargs)


# --- leave-year arithmetic --------------------------------------------------
def test_india_leave_year_runs_april_to_march():
    assert leave_year_bounds("03-31", 2026) == (dt.date(2026, 4, 1), dt.date(2027, 3, 31))


def test_us_leave_year_is_the_calendar_year():
    assert leave_year_bounds("12-31", 2026) == (dt.date(2026, 1, 1), dt.date(2026, 12, 31))


# --- preview ---------------------------------------------------------------
def test_preview_writes_nothing(session, hr):
    before = len(policy_set(session, "India-TamilNadu"))
    preview_roll_forward(session, "India-TamilNadu", 2025, default_leave_year_end="03-31")
    assert len(policy_set(session, "India-TamilNadu")) == before


def test_preview_shows_the_diff(session, hr):
    plan = preview_roll_forward(
        session, "India-TamilNadu", 2025,
        changes={"CL": {"entitlement_days_per_year": D("9")}},
        default_leave_year_end="03-31",
    )
    assert plan.changed_count == 1
    changed = [c for c in plan.changes if not c.is_unchanged][0]
    assert changed.leave_type_id == "CL"
    assert changed.changed["entitlement_days_per_year"] == (D("7.00"), D("9"))


def test_preview_flags_a_no_op_year(session, hr):
    plan = preview_roll_forward(
        session, "India-TamilNadu", 2025, default_leave_year_end="03-31"
    )
    assert plan.changed_count == 0
    assert any("continue unchanged" in w for w in plan.warnings)


# --- execution -------------------------------------------------------------
def test_roll_forward_opens_the_new_year(session, hr):
    created = roll(session, hr)
    assert len(created) == 7
    assert all(p.effective_from == dt.date(2026, 4, 1) for p in created)
    assert all(p.policy_year == 2026 for p in created)


def test_old_rows_are_closed_not_edited(session, hr, priya):
    """The 2025 number must stay true for anything that referenced it."""
    roll(session, hr, changes={"CL": {"entitlement_days_per_year": D("9")}})

    assert resolve_policy(session, priya, "CL", "2026-03-31").entitlement_days_per_year == D("7.00")
    assert resolve_policy(session, priya, "CL", "2026-04-01").entitlement_days_per_year == D("9")


def test_history_chains_backwards(session, hr):
    created = roll(session, hr, changes={"CL": {"entitlement_days_per_year": D("9")}})
    cl = [c for c in created if c.leave_type_id == "CL"][0]
    history = policy_history(session, cl)
    assert len(history) == 2
    assert history[1].entitlement_days_per_year == D("7.00")


def test_continue_unchanged_still_records_a_review(session, hr):
    created = roll(session, hr, change_reason="Reviewed for FY2026; no changes")
    assert len(created) == 7
    assert all(p.change_reason == "Reviewed for FY2026; no changes" for p in created)
    assert all(p.created_by_id == hr.id for p in created)


def test_a_year_cannot_be_opened_twice(session, hr):
    roll(session, hr)
    with pytest.raises(PolicyAdminError, match="already open"):
        roll(session, hr)


# --- guards ----------------------------------------------------------------
def test_employees_cannot_change_policy(session, priya):
    with pytest.raises(PolicyAdminError, match="Only hr_admin or director"):
        roll(session, priya)


def test_a_reason_is_required(session, hr):
    with pytest.raises(PolicyAdminError, match="change_reason is required"):
        roll(session, hr, change_reason="   ")


def test_an_actor_is_required(session):
    with pytest.raises(PolicyAdminError, match="attributable to a person"):
        roll(session, None, actor=None)


def test_identity_fields_cannot_be_changed(session, hr):
    with pytest.raises(PolicyAdminError, match="Cannot change"):
        roll(session, hr, changes={"CL": {"region": "India-Karnataka"}})


def test_compliance_note_is_still_required(session, hr):
    from app.validation import PolicyValidationError

    with pytest.raises((PolicyAdminError, PolicyValidationError)):
        roll(session, hr, changes={"*": {"compliance_note": "  "}})


def test_directors_may_also_roll_forward(session):
    director = session.scalars(
        select(Employee).where(Employee.name == "Nathan Cole")
    ).one()
    created = roll_forward_year(
        session, "USA-Texas", 2025, actor=director,
        change_reason="FY2026 US review", commit=False,
        default_leave_year_end="12-31",
    )
    assert created
