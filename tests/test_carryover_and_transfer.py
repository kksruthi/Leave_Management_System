"""The two changes asked for after the MVP policy guide.

  1. Carry-over: capped at 18 days organisation-wide, held in its own bucket,
     expiring three months after the new leave year starts, and consumed
     BEFORE the new year's accrual.
  2. Region transfer: a mid-year move writes the difference between what the
     old region entitled the employee to and what the new one does.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accrual import (
    CARRYOVER_CEILING_DAYS,
    REASON_CARRYOVER_IN,
    REASON_CARRYOVER_LAPSE,
    _carryover_expiry_date,
    run_year_end_carryover,
)
from app.approval import record_approval_decision, submit_approval_chain
from app.classification import submit_leave_request
from app.dashboard import get_balance_buckets, get_dashboard, get_live_balance
from app.db import SessionLocal
from app.models import Employee, LeaveLedger
from app.policy_engine import resolve_policy
from app.region_transfer import reconcile_region_transfer, relocate_employee
from app.settlement import settlement_rows

D = Decimal


@pytest.fixture()
def session():
    with SessionLocal() as s:
        yield s
        s.rollback()


def emp(session, name) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == name)).one()


# ===========================================================================
# 1. Carry-over
# ===========================================================================
def test_expiry_is_three_months_after_the_new_year_starts(session):
    """The worked example from the brief: 1 Apr 2026 carry-over dies 30 Jun."""
    priya = emp(session, "Priya")
    policy = resolve_policy(session, priya, "EL", dt.date(2026, 4, 1))
    assert _carryover_expiry_date(policy, dt.date(2026, 4, 1)) == dt.date(2026, 6, 30)
    # And it travels with the leave year, so Texas (January) gets March.
    assert _carryover_expiry_date(policy, dt.date(2026, 1, 1)) == dt.date(2026, 3, 31)


def test_carryover_is_capped_at_eighteen_days(session):
    """Nikhil's tenure bracket allows 24; the organisation ceiling is 18."""
    nikhil = emp(session, "Nikhil Raghavan")
    year_end = dt.date(2026, 3, 31)

    policy = resolve_policy(session, nikhil, "EL", year_end)
    assert policy.carryover_max_days > CARRYOVER_CEILING_DAYS   # the cap would allow more

    # A balance well above both numbers, so only the ceiling can bind.
    session.add(LeaveLedger(
        employee_id=nikhil.id, leave_type_id="EL", amount=D("40"),
        reason="test fixture", effective_date=dt.date(2025, 4, 1),
    ))
    session.flush()

    run_year_end_carryover(session, year_end, commit=False)
    session.flush()

    carried = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == nikhil.id,
            LeaveLedger.reason == REASON_CARRYOVER_IN,
        )
    ).all()
    assert len(carried) == 1
    row = carried[0]
    assert D(row.amount) == CARRYOVER_CEILING_DAYS
    assert row.bucket == "carryover"
    assert row.effective_date == dt.date(2026, 4, 1)
    assert row.expires_on == dt.date(2026, 6, 30)


def test_the_excess_is_forfeited_on_the_record(session):
    """Days above the ceiling are written off explicitly, not silently dropped."""
    nikhil = emp(session, "Nikhil Raghavan")
    year_end = dt.date(2026, 3, 31)
    session.add(LeaveLedger(
        employee_id=nikhil.id, leave_type_id="EL", amount=D("40"),
        reason="test fixture", effective_date=dt.date(2025, 4, 1),
    ))
    session.flush()

    before = get_live_balance(session, nikhil.id, "EL", year_end)
    run_year_end_carryover(session, year_end, commit=False)
    session.flush()

    lapsed = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == nikhil.id,
            LeaveLedger.reason == REASON_CARRYOVER_LAPSE,
        )
    ).all()
    assert lapsed, "the forfeited days must appear in the history"
    assert -D(lapsed[0].amount) == before - CARRYOVER_CEILING_DAYS


def test_carryover_sits_in_its_own_bucket(session):
    """Carried days are not mixed into the new year's entitlement."""
    nikhil = emp(session, "Nikhil Raghavan")
    session.add(LeaveLedger(
        employee_id=nikhil.id, leave_type_id="EL", amount=D("40"),
        reason="test fixture", effective_date=dt.date(2025, 4, 1),
    ))
    session.flush()
    run_year_end_carryover(session, dt.date(2026, 3, 31), commit=False)
    session.flush()

    buckets = get_balance_buckets(session, nikhil.id, "EL", dt.date(2026, 4, 2))
    assert buckets["carryover"] == CARRYOVER_CEILING_DAYS


def test_leave_eats_the_expiring_carryover_first(session):
    """The point of the separate bucket: perishable days go first.

    Taking from `current` first would let the carry-over lapse in June while
    an unexpiring balance sat untouched — the employee losing days purely to
    the order the code deducted in.
    """
    ravi = emp(session, "Ravi Chandran")
    # 5 carried days, expiring 30 June; 20 ordinary days that never expire.
    session.add(LeaveLedger(
        employee_id=ravi.id, leave_type_id="EL", amount=D("5"),
        reason=REASON_CARRYOVER_IN, effective_date=dt.date(2026, 4, 1),
        bucket="carryover", expires_on=dt.date(2026, 6, 30),
    ))
    session.add(LeaveLedger(
        employee_id=ravi.id, leave_type_id="EL", amount=D("20"),
        reason="test fixture", effective_date=dt.date(2026, 4, 1),
    ))
    session.flush()

    start = dt.date(2026, 5, 11)              # a Monday, well inside the window
    result = submit_leave_request(
        session, ravi, "EL", start, start + dt.timedelta(days=6),
        submitted_at=dt.datetime(2026, 4, 20, 9, 0, tzinfo=dt.timezone.utc),
        commit=False,
    )
    submit_approval_chain(session, result.request)
    record_approval_decision(
        session, result.request.id, 1, "approved", actor=emp(session, "Anitha Rajan")
    )
    session.flush()

    rows = [r for r in settlement_rows(session, result.request.id)
            if r.reason.startswith("leave taken")]
    by_bucket = {r.bucket: -D(r.amount) for r in rows}

    # 5 working days requested: all 5 perishable days first, nothing else.
    assert by_bucket.get("carryover") == D("5.000")
    assert "current" not in by_bucket

    buckets = get_balance_buckets(session, ravi.id, "EL", start)
    assert buckets["carryover"] == D("0.000")
    assert buckets["current"] == D("20.000")


def test_a_long_request_spills_into_the_current_year(session):
    """More days than the carry-over holds: the rest comes from `current`."""
    aravind = emp(session, "Aravind Kumar")
    session.add(LeaveLedger(
        employee_id=aravind.id, leave_type_id="EL", amount=D("2"),
        reason=REASON_CARRYOVER_IN, effective_date=dt.date(2026, 4, 1),
        bucket="carryover", expires_on=dt.date(2026, 6, 30),
    ))
    session.add(LeaveLedger(
        employee_id=aravind.id, leave_type_id="EL", amount=D("20"),
        reason="test fixture", effective_date=dt.date(2026, 4, 1),
    ))
    session.flush()

    start = dt.date(2026, 5, 11)
    result = submit_leave_request(
        session, aravind, "EL", start, start + dt.timedelta(days=6),
        submitted_at=dt.datetime(2026, 4, 20, 9, 0, tzinfo=dt.timezone.utc),
        commit=False,
    )
    submit_approval_chain(session, result.request)
    record_approval_decision(
        session, result.request.id, 1, "approved", actor=emp(session, "Karthik Balan")
    )
    session.flush()

    rows = [r for r in settlement_rows(session, result.request.id)
            if r.reason.startswith("leave taken")]
    by_bucket = {r.bucket: -D(r.amount) for r in rows}
    assert by_bucket["carryover"] == D("2.000")
    assert by_bucket["current"] == D("3.000")


# ===========================================================================
# 2. Region transfer
# ===========================================================================
def test_days_already_earned_are_never_taken_back(session):
    """THE BUG. The old formula re-priced the whole year to date.

        India EL earned so far = 11.392
        Texas EL earned so far =  9.493
        adjustment             = -1.899   <- days removed retroactively

    Someone who worked five months in Chennai earned those days under the
    policy in force while they earned them. A later move to Texas does not
    make them unearned.
    """
    ravi = emp(session, "Ravi Chandran")
    if ravi.region != "India-TamilNadu":
        ravi.region = "India-TamilNadu"
        session.flush()
    transfer_date = dt.date(2026, 7, 1)
    before = get_live_balance(session, ravi.id, "EL", transfer_date)

    result = relocate_employee(session, ravi, "USA-Texas", transfer_date, commit=False)
    el = next(a for a in result["adjustments"] if a["leave_type_id"] == "EL")

    # EL accrues monthly, so nothing was granted in advance and there is
    # nothing to reconcile. The next accrual just uses the new rate.
    assert D(el["adjustment"]) == D("0")
    assert get_live_balance(session, ravi.id, "EL", transfer_date) == before
    assert "already earned" in el["basis"]


def test_an_annual_lump_is_repriced_only_for_the_remaining_year(session):
    """SL was granted for the whole year up front, so the rest is re-priced.

    India grants 10, Texas 8. Moving on 1 July leaves ~74% of India's
    Apr–Mar year ahead, so the adjustment is (8 - 10) x 0.74, NOT the full
    two-day difference and certainly not a re-pricing of the months already
    served.
    """
    ravi = emp(session, "Ravi Chandran")
    if ravi.region != "India-TamilNadu":
        ravi.region = "India-TamilNadu"
        session.flush()
    transfer_date = dt.date(2026, 7, 1)
    # He needs the lump on file for there to be anything to re-price; the
    # test database does not run the accrual jobs.
    session.add(LeaveLedger(
        employee_id=ravi.id, leave_type_id="SL", amount=D("10"),
        reason="annual grant", effective_date=dt.date(2026, 4, 1),
    ))
    session.flush()

    result = relocate_employee(session, ravi, "USA-Texas", transfer_date, commit=False)
    sl = next(a for a in result["adjustments"] if a["leave_type_id"] == "SL")

    adjustment = D(sl["adjustment"])
    assert adjustment < 0                       # Texas grants fewer sick days
    assert adjustment > D("-2")                 # ...but not the whole difference
    assert D(sl["old_entitlement"]) == D("10.00")
    assert D(sl["new_entitlement"]) == D("8.00")

    row = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == ravi.id,
            LeaveLedger.effective_date == transfer_date,
            LeaveLedger.leave_type_id == "SL",
        )
    ).one()
    assert D(row.amount) == adjustment
    assert "Relocation" in row.reason


def test_a_relocation_can_never_leave_a_negative_balance(session):
    """Whatever the arithmetic says, nobody ends up owing days."""
    person = emp(session, "Divya Shankar")
    if person.region != "India-TamilNadu":
        person.region = "India-TamilNadu"
        session.flush()
    transfer_date = dt.date(2026, 7, 1)

    relocate_employee(session, person, "USA-Texas", transfer_date, commit=False)
    session.flush()
    for leave_type in ("EL", "SL"):
        assert get_live_balance(
            session, person.id, leave_type, transfer_date
        ) >= 0


def test_the_leave_year_moves_with_the_region(session):
    """India runs Apr–Mar, Texas Jan–Dec. Everything dated follows."""
    ravi = emp(session, "Ravi Chandran")
    if ravi.region != "India-TamilNadu":
        ravi.region = "India-TamilNadu"
        session.flush()

    result = relocate_employee(
        session, ravi, "USA-Texas", dt.date(2026, 7, 1), commit=False
    )
    assert result["leave_year_before"].startswith("2026-04-01")
    assert result["leave_year_after"].startswith("2026-01-01")


def test_a_transfer_moves_the_live_balance(session):
    ravi = emp(session, "Ravi Chandran")
    transfer_date = dt.date(2026, 7, 1)
    before = get_live_balance(session, ravi.id, "EL", transfer_date)

    old_region = ravi.region
    ravi.region = "USA-Texas"
    session.flush()
    adjustments = reconcile_region_transfer(
        session, ravi.id, old_region, "USA-Texas", transfer_date, commit=False
    )
    el = next(a for a in adjustments if a.leave_type_id == "EL")

    after = get_live_balance(session, ravi.id, "EL", transfer_date)
    assert after == before + el.adjustment


def test_a_type_the_new_region_lacks_is_kept_not_confiscated(session):
    """Texas has no Casual Leave.

    The days Ravi was granted under Tamil Nadu were granted. Deleting them
    because he moved would be a confiscation; the honest outcome is that the
    balance stays spendable and simply stops growing.
    """
    ravi = emp(session, "Ravi Chandran")
    if ravi.region != "India-TamilNadu":
        ravi.region = "India-TamilNadu"
        session.flush()
    transfer_date = dt.date(2026, 7, 1)
    before = get_live_balance(session, ravi.id, "CL", transfer_date)

    result = relocate_employee(session, ravi, "USA-Texas", transfer_date, commit=False)
    cl = next(a for a in result["adjustments"] if a["leave_type_id"] == "CL")

    assert D(cl["adjustment"]) == D("0")
    assert cl["new_entitlement"] is None
    assert "not confiscated" in cl["basis"]
    assert get_live_balance(session, ravi.id, "CL", transfer_date) == before


def test_history_is_never_rewritten_by_a_transfer(session):
    """The adjustment is appended; nothing before the transfer date moves."""
    ravi = emp(session, "Ravi Chandran")
    transfer_date = dt.date(2026, 7, 1)
    before_rows = session.scalars(
        select(LeaveLedger.id).where(
            LeaveLedger.employee_id == ravi.id,
            LeaveLedger.effective_date < transfer_date,
        )
    ).all()
    before_sum = get_live_balance(session, ravi.id, "EL", transfer_date - dt.timedelta(days=1))

    old_region = ravi.region
    ravi.region = "USA-Texas"
    session.flush()
    reconcile_region_transfer(
        session, ravi.id, old_region, "USA-Texas", transfer_date, commit=False
    )

    after_rows = session.scalars(
        select(LeaveLedger.id).where(
            LeaveLedger.employee_id == ravi.id,
            LeaveLedger.effective_date < transfer_date,
        )
    ).all()
    assert set(before_rows) == set(after_rows)
    assert get_live_balance(
        session, ravi.id, "EL", transfer_date - dt.timedelta(days=1)
    ) == before_sum


# ===========================================================================
# 3. Annual grants belong to their leave year, not to the run date
# ===========================================================================
def test_an_annual_grant_is_dated_to_the_year_it_covers(session):
    """CL and SL read 7 and 10, whenever the job happens to be run.

    The grant used to be stamped with the RUN DATE. Run the annual job on
    1 January for an India employee — whose leave year runs 1 Apr – 31 Mar —
    and the credit landed in the previous leave year, so a balance scoped to
    the current one showed zero for leave types that had definitely been
    granted.
    """
    from app.accrual import REASON_ANNUAL, run_annual_grant

    priya = emp(session, "Priya")
    # Deliberately a run date in the middle of the PREVIOUS leave year.
    run_annual_grant(session, dt.date(2027, 1, 15), commit=False)
    session.flush()

    row = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == priya.id,
            LeaveLedger.leave_type_id == "CL",
            LeaveLedger.reason == REASON_ANNUAL,
            LeaveLedger.effective_date == dt.date(2026, 4, 1),
        )
    ).first()
    assert row is not None, "the grant must be dated to the leave year it covers"
    assert D(row.amount) == D("7.000")

    assert get_live_balance(session, priya.id, "CL", dt.date(2027, 1, 15)) == D("7.000")
    assert get_live_balance(session, priya.id, "SL", dt.date(2027, 1, 15)) == D("10.000")


def test_a_joiner_is_never_credited_before_their_first_day(session):
    """The grant date is clamped to the join date."""
    from app.accrual import REASON_ANNUAL, run_annual_grant

    bhavana = emp(session, "Bhavana Rao")          # joined 2026-06-01
    run_annual_grant(session, dt.date(2026, 8, 1), commit=False)
    session.flush()

    rows = session.scalars(
        select(LeaveLedger).where(
            LeaveLedger.employee_id == bhavana.id,
            LeaveLedger.reason == REASON_ANNUAL,
        )
    ).all()
    assert rows
    assert all(r.effective_date >= bhavana.join_date for r in rows)


# ===========================================================================
# 4. Approving future leave must not hand the days back
# ===========================================================================
def test_approving_future_leave_keeps_the_days_committed(session):
    """The bug: approve a November request in August and the balance went UP.

    Settlement dates the deduction at the request's START date, which is
    correct — that is when the leave is consumed. But a balance is summed to
    today, so a November row does not touch an August balance. Meanwhile the
    request stopped being `pending`, so `pending_days` fell to zero and the
    available figure sprang back to where it started. The employee was told
    they still had days they had already spent.
    """
    from app.approval import forward_request

    priya = emp(session, "Priya")
    anitha = emp(session, "Anitha Rajan")
    fatima = emp(session, "Fatima Khan")

    session.add(LeaveLedger(
        employee_id=priya.id, leave_type_id="EL", amount=D("12"),
        reason="test fixture", effective_date=dt.date(2026, 4, 1),
    ))
    session.flush()

    def available() -> D:
        view = [t for t in get_dashboard(session, priya).leave_types
                if t.leave_type_id == "EL"][0]
        return view.available_days

    before = available()

    result = submit_leave_request(
        session, priya, "EL", dt.date(2026, 11, 9), dt.date(2026, 11, 11),
        submitted_at=dt.datetime(2026, 8, 19, 9, tzinfo=dt.timezone.utc),
        commit=False,
    )
    steps = submit_approval_chain(session, result.request)
    session.flush()
    while_pending = available()
    assert while_pending == before - D("3.000")

    hr_step = forward_request(
        session, result.request.id, steps[0].tier, "hr_admin", actor=anitha
    )
    session.flush()
    assert available() == while_pending          # forwarding changes nothing

    record_approval_decision(
        session, result.request.id, hr_step.tier, "approved", actor=fatima
    )
    session.flush()

    # THE ASSERTION. This used to be `before` — the days came back.
    assert available() == while_pending

    view = [t for t in get_dashboard(session, priya).leave_types
            if t.leave_type_id == "EL"][0]
    assert view.pending_days == D("0")           # no longer awaiting anyone
    assert view.scheduled_days == D("3.00")      # ...but still spent


def test_the_deduction_lands_when_the_leave_starts(session):
    """Committed days move from `scheduled` into the balance on the start date.

    The total never changes — only which column it sits in.
    """
    from app.approval import forward_request

    priya = emp(session, "Priya")
    anitha = emp(session, "Anitha Rajan")
    fatima = emp(session, "Fatima Khan")

    session.add(LeaveLedger(
        employee_id=priya.id, leave_type_id="EL", amount=D("12"),
        reason="test fixture", effective_date=dt.date(2026, 4, 1),
    ))
    session.flush()

    result = submit_leave_request(
        session, priya, "EL", dt.date(2026, 11, 9), dt.date(2026, 11, 11),
        submitted_at=dt.datetime(2026, 8, 19, 9, tzinfo=dt.timezone.utc),
        commit=False,
    )
    steps = submit_approval_chain(session, result.request)
    hr_step = forward_request(
        session, result.request.id, steps[0].tier, "hr_admin", actor=anitha
    )
    record_approval_decision(
        session, result.request.id, hr_step.tier, "approved", actor=fatima
    )
    session.flush()

    on_day = get_balance_buckets(session, priya.id, "EL", dt.date(2026, 11, 9))
    day_before = get_balance_buckets(session, priya.id, "EL", dt.date(2026, 11, 8))
    assert (day_before["current"] + day_before["carryover"]
            - on_day["current"] - on_day["carryover"]) == D("3.000")


# ===========================================================================
# 5. Policy changes can target ONE tenure band
# ===========================================================================
def test_a_change_can_target_a_single_tenure_band(session):
    """"Raise EL for 5+ years" must not raise it for everybody.

    `_resolve_changes` always supported a per-band key, but only as a Python
    tuple — which cannot survive JSON, so the API and the UI could only ever
    address a whole leave type. The string form `"EL@5"` closes that gap.

    The band key is built with `format_days`, not `:g`: `f"{Decimal('5.00'):g}"`
    is `'5.00'`, so the key would otherwise never have matched.
    """
    from app.policy_lifecycle import preview_publish

    plan = preview_publish(
        session, "India-TamilNadu", 2026,
        {"EL@5": {"entitlement_days_per_year": "32"}},
        default_leave_year_end="03-31",
    )
    changed = [c for c in plan["changes"] if c["changed"]]
    assert len(changed) == 1
    assert changed[0]["leave_type_id"] == "EL"
    assert changed[0]["tenure"].startswith("5")
    assert changed[0]["changed"]["entitlement_days_per_year"][1] == "32"


def test_targeting_a_whole_leave_type_still_hits_every_band(session):
    """The blunt form still works — it is a different, deliberate choice."""
    from app.policy_lifecycle import preview_publish

    plan = preview_publish(
        session, "India-TamilNadu", 2026,
        {"EL": {"entitlement_days_per_year": "32"}},
        default_leave_year_end="03-31",
    )
    changed = [c for c in plan["changes"] if c["changed"]]
    assert {c["leave_type_id"] for c in changed} == {"EL"}
    assert len(changed) == 4                       # every tenure band


def test_a_region_with_policies_is_never_reported_as_unconfigured(session):
    """The red banner said "No policy configured" above a live 224-day term.

    `state` returned "unconfigured" whenever `policy_year` was NULL, which is
    true of the seeded baseline rows even though they are in force.
    """
    from app.policy_lifecycle import policy_year_status

    status = policy_year_status(session, "India-TamilNadu")
    assert status.leave_type_ids                   # something IS in force
    assert status.state != "unconfigured"
    assert "No policy configured" not in status.headline
