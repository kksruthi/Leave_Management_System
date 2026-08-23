"""The approval copilot: does it tell a manager the truth about cover?

Each test is a situation a manager actually faces, and asserts the verdict
plus the number the verdict rests on. A recommendation that cannot be traced
back to a count is a recommendation nobody should follow.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.approval import submit_approval_chain
from app.classification import submit_leave_request
from app.copilot import TEAM_COVERAGE_THRESHOLDS, advise
from app.db import SessionLocal
from app.models import Employee, LeaveLedger

D = Decimal
SUBMITTED = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)
WEEK = (dt.date(2026, 12, 7), dt.date(2026, 12, 11))


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


def emp(session, name) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == name)).one()


def top_up(session, employee, days=30):
    """Give them enough balance that cover, not cost, drives the verdict."""
    session.add(LeaveLedger(
        employee_id=employee.id, leave_type_id="EL", amount=D(str(days)),
        reason="test fixture", effective_date=dt.date(2026, 4, 1),
    ))
    session.flush()


def book(session, name, start=None, end=None, chain=True):
    person = emp(session, name)
    top_up(session, person)
    result = submit_leave_request(
        session, person, "EL", start or WEEK[0], end or WEEK[1],
        submitted_at=SUBMITTED, commit=False,
    )
    if chain:
        submit_approval_chain(session, result.request)
    session.flush()
    return result.request


# ===========================================================================
def test_an_empty_week_is_safe(session):
    request = book(session, "Meera", chain=False)
    advice = advise(session, request)

    assert advice["verdict"] == "safe"
    assert advice["peak_away"] == 1               # just the requester
    assert "Nobody else is off" in advice["signals"][0]["label"]


def test_one_teammate_off_is_still_safe_in_a_bigger_team(session):
    """Anitha has five engineers; one already off is not a staffing problem."""
    book(session, "Priya")
    request = book(session, "Arjun Menon", chain=False)
    advice = advise(session, request)

    assert advice["team_size"] == 5
    assert advice["peak_away"] == 2
    assert advice["verdict"] == "safe"
    assert advice["remaining"] == 3


def test_half_the_team_off_is_risky(session):
    """Karthik has four designers. Two already off makes this the third."""
    book(session, "Meera")
    book(session, "Kavya")
    request = book(session, "Sneha Iyer", chain=False)
    advice = advise(session, request)

    assert advice["team_size"] == 4
    assert advice["peak_away"] == 3
    assert advice["verdict"] == "risky"
    assert advice["remaining"] == 1
    assert "1" in advice["headline"]


def test_pending_leave_counts_too(session):
    """Two managers approving in sequence is how a team ends up empty.

    A teammate's request that nobody has decided yet is still a claim on the
    same week, so it counts. Ignoring it means each approval looks safe in
    isolation and the team is deserted in aggregate.
    """
    book(session, "Meera")                        # left pending on purpose
    book(session, "Kavya")
    request = book(session, "Sneha Iyer", chain=False)
    advice = advise(session, request)

    statuses = {o["status"] for o in advice["overlapping"]}
    assert statuses == {"pending"}
    assert advice["verdict"] == "risky"


def test_only_the_overlapping_days_count(session):
    """A teammate off a different week is not cover for this one."""
    book(session, "Meera", dt.date(2026, 12, 21), dt.date(2026, 12, 24))
    request = book(session, "Kavya", chain=False)
    advice = advise(session, request)

    assert advice["peak_away"] == 1
    assert advice["overlapping"] == []
    assert advice["verdict"] == "safe"


def test_a_colleague_in_another_team_is_irrelevant(session):
    """Cover is a team question. Texas sales does not cover Chennai design."""
    book(session, "Lena Ortiz")                   # Dana's team, USA
    request = book(session, "Meera", chain=False)  # Karthik's team, India
    advice = advise(session, request)

    assert advice["overlapping"] == []
    assert advice["verdict"] == "safe"


def test_the_daily_breakdown_is_per_day(session):
    """The peak is a single day, not a total, so the shape has to be visible."""
    book(session, "Meera", dt.date(2026, 12, 7), dt.date(2026, 12, 8))
    request = book(session, "Kavya", chain=False)
    advice = advise(session, request)

    by_date = {d["date"]: d["count"] for d in advice["daily"]}
    assert by_date["2026-12-07"] == 1
    assert by_date["2026-12-11"] == 0


def test_an_overdrawn_request_is_flagged_even_when_cover_is_fine(session):
    """Cover is only half the decision — approving loss of pay is the other."""
    priya = emp(session, "Priya")
    request = submit_leave_request(
        session, priya, "EL", *WEEK, submitted_at=SUBMITTED, commit=False,
    ).request
    session.flush()
    advice = advise(session, request)

    assert advice["verdict"] != "safe"            # bumped by the cost warning
    assert any(s["tone"] == "warning" for s in advice["signals"])
    # ...and the headline moves with the verdict rather than still saying "safe".
    assert not advice["headline"].startswith("Safe to approve")


def test_short_notice_is_called_out(session):
    person = emp(session, "Meera")
    top_up(session, person)
    start = dt.date(2026, 12, 7)
    request = submit_leave_request(
        session, person, "EL", start, start,
        submitted_at=dt.datetime.combine(
            start - dt.timedelta(days=1), dt.time(9), tzinfo=dt.timezone.utc
        ),
        # The notice rule blocks by default; an override is the honest way to
        # submit late, and the copilot's job is to make sure the approver sees
        # that it was late.
        override_reason="Bereavement — travelling tomorrow.",
        commit=False,
    ).request
    session.flush()
    advice = advise(session, request)

    assert any("Short notice" in s["label"] for s in advice["signals"])


def test_the_thresholds_are_configuration_not_magic(session):
    """Someone will want to change these; they must be reachable."""
    assert TEAM_COVERAGE_THRESHOLDS["check"] < TEAM_COVERAGE_THRESHOLDS["risky"]


def test_being_down_to_one_person_is_risky_however_small_the_team(session):
    """A percentage alone would miss this.

    In a team of three, two away is 66% — right on the line. What actually
    matters is that one person is left holding it, and the absolute floor is
    what catches that.
    """
    from app.copilot import MIN_COMFORTABLE_REMAINING

    assert MIN_COMFORTABLE_REMAINING >= 1
    book(session, "Lakshmi Narayan")               # Deepa's team of 3
    request = book(session, "Suresh Pillai", chain=False)
    advice = advise(session, request)

    assert advice["team_size"] == 3
    assert advice["remaining"] == 1
    assert advice["verdict"] == "risky"


def test_advice_never_blocks_a_decision(session):
    """It is advice. The payload has no field that could refuse anything."""
    request = book(session, "Meera", chain=False)
    advice = advise(session, request)

    assert set(advice) >= {"verdict", "headline", "signals", "daily", "basis"}
    assert "blocked" not in advice and "allowed" not in advice
