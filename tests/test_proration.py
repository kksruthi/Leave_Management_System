"""Tests for the four review findings.

  1. No pro-rating for employees who join partway through a year
  2. Part-time employee entitlement is not defined
  3. Termination / final accrual rules are not fully defined
  4. Rounding rules need to be explicitly defined

Plus the case the follow-up specifically called out: pro-ration must stay
correct when the policy or tenure bracket changes partway through the cycle.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accrual import (
    REASON_ANNUAL,
    REASON_MONTHLY,
    REASON_TRUE_UP,
    active_employees,
    run_annual_grant,
    run_monthly_accrual,
)
from app.db import SessionLocal
from app.models import Employee, OrgPolicy
from app.policy_engine import resolve_policy
from app.proration import (
    accrual_cycle,
    eligible_days,
    eligible_period,
    prorate_entitlement,
    round_days,
)

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
def meera(session):
    """Joined 2025-10-15 — mid-cycle joiner."""
    return emp(session, "Meera")


@pytest.fixture()
def kavya(session):
    """0.5 FTE, joined 2025-04-01 — part-time."""
    return emp(session, "Kavya")


@pytest.fixture()
def tom(session):
    """Joined 2025-04-01, exited 2026-01-31 — leaver."""
    return emp(session, "Tom")


def ledger_for(session, employee, leave_type_id, reason=None):
    from app.models import LeaveLedger
    stmt = select(LeaveLedger).where(
        LeaveLedger.employee_id == employee.id,
        LeaveLedger.leave_type_id == leave_type_id,
    )
    if reason:
        stmt = stmt.where(LeaveLedger.reason == reason)
    return session.scalars(stmt.order_by(LeaveLedger.effective_date)).all()


# ===========================================================================
# FINDING 1 — pro-ration for mid-cycle joiners
# ===========================================================================
def test_eligible_period_clips_to_the_join_date(meera):
    window = eligible_period(meera, dt.date(2025, 4, 1), dt.date(2026, 3, 31))
    assert window == (dt.date(2025, 10, 15), dt.date(2026, 3, 31))


def test_full_year_employee_is_not_prorated(session, priya):
    result = prorate_entitlement(
        session, priya, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    assert result.is_partial is False
    assert result.prorated_days == D("7.000")       # the full lump
    assert result.eligible_days == result.cycle_days


def test_mid_cycle_joiner_gets_a_proportional_share(session, meera):
    """Meera joined 2025-10-15: 168 of 365 days of the 2025-26 leave year."""
    result = prorate_entitlement(
        session, meera, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    assert result.is_partial is True
    assert result.cycle_days == 365
    assert result.eligible_days == 168
    # 7 days/yr x 168/365 = 3.2219...
    assert result.prorated_days == D("3.222")
    assert result.full_year_entitlement == D("7.00")


def test_annual_grant_writes_the_prorated_amount(session, meera):
    run_annual_grant(session, "2025-10-15", commit=False)
    rows = ledger_for(session, meera, "CL", REASON_ANNUAL)
    assert len(rows) == 1
    assert rows[0].amount == D("3.222")            # not the full 7


def test_full_year_employee_still_gets_the_whole_lump(session, priya):
    run_annual_grant(session, "2025-04-01", commit=False)
    rows = ledger_for(session, priya, "CL", REASON_ANNUAL)
    assert rows[0].amount == D("7.000")


def test_joiner_first_month_accrual_is_prorated(session, meera):
    """Joined the 15th of a 31-day month -> 17/31 of that month's accrual."""
    run_monthly_accrual(session, "2025-10-01", commit=False)
    rows = ledger_for(session, meera, "EL", REASON_MONTHLY)
    # 15 days/yr / 12 = 1.25 for a full month; x 17/31 = 0.685483...
    assert rows[0].amount == D("0.685")


def test_joiner_second_month_is_a_full_accrual(session, meera):
    run_monthly_accrual(session, "2025-11-01", commit=False)
    rows = ledger_for(session, meera, "EL", REASON_MONTHLY)
    assert rows[-1].amount == D("1.250")


def test_employee_not_yet_joined_gets_nothing(session, meera):
    result = prorate_entitlement(
        session, meera, "CL", dt.date(2024, 4, 1), dt.date(2025, 3, 31)
    )
    assert result.eligible_days == 0
    assert result.prorated_days == D("0.000")


# ===========================================================================
# FINDING 2 — part-time entitlement
# ===========================================================================
def test_part_time_entitlement_is_halved(session, kavya, priya):
    part = resolve_policy(session, kavya, "EL", "2025-06-01")
    full = resolve_policy(session, priya, "EL", "2025-06-01")

    assert full.entitlement_days_per_year == D("15.00")
    assert part.entitlement_days_per_year == D("7.500")
    assert part.full_time_entitlement_days_per_year == D("15.00")
    assert part.employment_fraction == D("0.500")
    assert part.is_part_time is True


def test_part_time_accrual_is_halved(session, kavya):
    run_monthly_accrual(session, "2025-06-01", commit=False)
    rows = ledger_for(session, kavya, "EL", REASON_MONTHLY)
    assert rows[0].amount == D("0.625")            # 7.5 / 12


def test_part_time_annual_lump_is_halved(session, kavya):
    run_annual_grant(session, "2025-04-01", commit=False)
    assert ledger_for(session, kavya, "CL", REASON_ANNUAL)[0].amount == D("3.500")


def test_part_time_scales_across_tenure_brackets(session, kavya):
    """One column, applied to whatever the bracket resolves to."""
    assert resolve_policy(session, kavya, "EL", "2026-04-01") \
        .entitlement_days_per_year == D("9.000")    # 18 x 0.5
    assert resolve_policy(session, kavya, "EL", "2030-04-01") \
        .entitlement_days_per_year == D("15.000")   # 30 x 0.5


def test_full_time_employees_are_untouched_by_the_fte_change(session, priya):
    """Regression guard: fraction 1.000 must be an exact no-op."""
    policy = resolve_policy(session, priya, "EL", "2025-06-01")
    assert policy.entitlement_days_per_year == D("15.00")
    assert policy.is_part_time is False
    assert policy.monthly_accrual == D("1.250")


def test_part_time_explanation_shows_the_arithmetic(session, kavya):
    text = resolve_policy(session, kavya, "EL", "2025-06-01").explain()
    assert "7.500" in text and "15.00" in text and "0.500" in text


def test_fte_must_be_a_sensible_fraction(session):
    """DB constraint: 0 < fraction <= 1."""
    from sqlalchemy.exc import IntegrityError

    session.add(Employee(name="Bad FTE", email="badfte@northbridge.example",
                         join_date=dt.date(2025, 1, 1), region="India-TamilNadu",
                         employment_fraction=D("1.500")))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_part_time_joiner_compounds_both_effects(session, kavya):
    """0.5 FTE for half a year should give a quarter of the full-time year."""
    kavya.join_date = dt.date(2025, 10, 1)
    session.flush()

    result = prorate_entitlement(
        session, kavya, "CL", dt.date(2025, 10, 1), dt.date(2026, 9, 30)
    )
    # Full cycle at 0.5 FTE = 3.5 days.
    assert result.prorated_days == D("3.500")

    partial = prorate_entitlement(
        session, kavya, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    # 0.5 FTE x 182/365 of the year ~= 1.745
    assert partial.prorated_days == D("1.745")


# ===========================================================================
# FINDING 3 — termination / final accrual
# ===========================================================================
def test_leaver_is_excluded_from_accrual_after_exit(session, tom):
    assert tom not in active_employees(session, dt.date(2026, 3, 1))
    assert tom in active_employees(session, dt.date(2025, 12, 1))


def test_leaver_stops_accruing(session, tom):
    run_monthly_accrual(session, "2026-03-01", commit=False)
    assert ledger_for(session, tom, "EL", REASON_MONTHLY) == []


def test_leaver_accrues_up_to_their_exit(session, tom):
    run_monthly_accrual(session, "2025-12-01", commit=False)
    assert len(ledger_for(session, tom, "EL", REASON_MONTHLY)) == 1


def test_leaver_final_month_is_prorated(session, tom):
    """Tom exits 2026-01-31 — a full January, so a full month's accrual."""
    run_monthly_accrual(session, "2026-01-01", commit=False)
    assert ledger_for(session, tom, "EL", REASON_MONTHLY)[-1].amount == D("0.833")


def test_leaver_mid_month_exit_is_prorated(session, tom):
    """Move the exit to the 10th: 10/31 of that month's accrual."""
    tom.exit_date = dt.date(2026, 1, 10)
    session.flush()

    run_monthly_accrual(session, "2026-01-01", commit=False)
    rows = ledger_for(session, tom, "EL", REASON_MONTHLY)
    # 10 days/yr / 12 = 0.8333 full month; x 10/31 = 0.2688...
    assert rows[-1].amount == D("0.269")


def test_leaver_final_cycle_entitlement_is_prorated(session, tom):
    """Texas runs a calendar leave year, so Tom's 2025 year ends 31 Dec."""
    start, end = accrual_cycle(
        tom, resolve_policy(session, tom, "SL", "2025-06-01"), dt.date(2025, 6, 1)
    )
    assert (start, end) == (dt.date(2025, 1, 1), dt.date(2025, 12, 31))

    result = prorate_entitlement(session, tom, "SL", start, end)
    assert result.eligible_start == dt.date(2025, 4, 1)   # clipped to his join
    assert result.eligible_days == 275
    # 8 days/yr x 275/365 = 6.027...
    assert result.prorated_days == D("6.027")


def test_leaver_final_partial_year_is_prorated(session, tom):
    """His 2026 year is only January — he leaves on the 31st."""
    result = prorate_entitlement(
        session, tom, "SL", dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    )
    assert result.eligible_end == dt.date(2026, 1, 31)
    assert result.eligible_days == 31
    # 8 x 31/365 = 0.679...
    assert result.prorated_days == D("0.679")


def test_terminated_status_requires_an_exit_date(session):
    from sqlalchemy.exc import IntegrityError

    session.add(Employee(name="No Exit", email="noexit@northbridge.example",
                         join_date=dt.date(2025, 1, 1), region="India-TamilNadu",
                         status="terminated"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_exit_date_cannot_precede_join_date(session):
    from sqlalchemy.exc import IntegrityError

    session.add(Employee(name="Time Traveller", email="tt@northbridge.example",
                         join_date=dt.date(2025, 1, 1), region="India-TamilNadu",
                         exit_date=dt.date(2024, 1, 1)))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_leaver_true_up_targets_the_prorated_entitlement(session, tom):
    """Tom's final cycle trues up to what he earned, not to a full year."""
    for date in [f"2025-{m:02d}-01" for m in range(4, 13)] + ["2026-01-01"]:
        run_monthly_accrual(session, date, commit=False)

    rows = ledger_for(session, tom, "EL")
    total = sum((D(r.amount) for r in rows), D("0"))
    expected = prorate_entitlement(
        session, tom, "EL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    ).prorated_days
    assert total == expected


# ===========================================================================
# FINDING 4 — rounding rules stated explicitly
# ===========================================================================
def test_rounding_is_policy_data_not_a_constant(session, priya):
    policy = resolve_policy(session, priya, "EL", "2025-06-01")
    assert policy.rounding_dp == 3
    assert policy.proration_method == "daily"


@pytest.mark.parametrize("value,dp,expected", [
    ("0.8333333", 3, "0.833"),
    ("0.8333333", 2, "0.83"),
    ("0.8333333", 0, "1"),
    ("0.005", 2, "0.01"),      # ROUND_HALF_UP, not banker's rounding
    ("0.015", 2, "0.02"),      # a float would give 0.01 here
    ("1.5", 0, "2"),
])
def test_round_days_is_half_up(value, dp, expected):
    assert round_days(D(value), dp) == D(expected)


def test_policy_rounding_dp_changes_the_accrued_amount(session):
    """A region can move to 2dp in DATA, with no code change."""
    raj = emp(session, "Raj")
    session.execute(
        OrgPolicy.__table__.update()
        .where(OrgPolicy.region == "USA-Texas", OrgPolicy.leave_type_id == "EL")
        .values(rounding_dp=2)
    )
    session.flush()

    run_monthly_accrual(session, "2025-06-01", commit=False)
    assert ledger_for(session, raj, "EL", REASON_MONTHLY)[0].amount == D("0.830")


def test_two_dp_drift_is_closed_by_the_true_up(session):
    """At 2dp the residual is bigger — the true-up still lands it exactly."""
    raj = emp(session, "Raj")
    session.execute(
        OrgPolicy.__table__.update()
        .where(OrgPolicy.region == "USA-Texas", OrgPolicy.leave_type_id == "EL")
        .values(rounding_dp=2)
    )
    session.flush()

    for date in [f"2025-{m:02d}-01" for m in range(4, 13)] + \
                [f"2026-{m:02d}-01" for m in range(1, 4)]:
        run_monthly_accrual(session, date, commit=False)

    rows = ledger_for(session, raj, "EL")
    assert sum((D(r.amount) for r in rows), D("0")) == D("10.000")
    true_ups = [r for r in rows if r.reason == REASON_TRUE_UP]
    assert true_ups[0].amount == D("0.040")     # 12 x 0.83 = 9.96


def test_rounding_dp_is_constrained(session):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        session.execute(
            OrgPolicy.__table__.update()
            .where(OrgPolicy.leave_type_id == "EL")
            .values(rounding_dp=7)
        )
        session.flush()
    session.rollback()


# ===========================================================================
# Mid-cycle policy and tenure changes
# ===========================================================================
def test_midcycle_policy_change_is_weighted_by_segment(session, priya):
    """HR raises CL from 7 to 13 halfway through the year.

    The answer must be the time-weighted blend, not whichever number happened
    to be in force on the day the job ran.
    """
    old = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "CL"
        )
    ).one()
    old.effective_to = dt.date(2025, 9, 30)
    session.flush()

    session.add(OrgPolicy(
        region="India-TamilNadu", legal_entity=old.legal_entity, leave_type_id="CL",
        tenure_min_years=D("0"), tenure_max_years=None,
        entitlement_days_per_year=D("13"), is_paid=True, accrual_method="annual_lump",
        carryover_max_days=D("0"), min_notice_days=1, max_consecutive_days=3,
        effective_from=dt.date(2025, 10, 1), effective_to=None,
        compliance_note="Uplift approved by HR/Legal, effective Oct 2025.",
    ))
    session.flush()

    result = prorate_entitlement(
        session, priya, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )

    assert len(result.segments) == 2
    assert result.segments[0].entitlement == D("7.00")     # Apr-Sep, 183 days
    assert result.segments[1].entitlement == D("13.00")    # Oct-Mar, 182 days
    # 7 x 183/365 + 13 x 182/365 = 3.5096 + 6.4822 = 9.992
    assert result.prorated_days == D("9.992")
    # Emphatically not either raw number
    assert result.prorated_days not in (D("7.000"), D("13.000"))


def test_midcycle_tenure_change_is_weighted(session, priya):
    """A calendar-year cycle straddles Priya's April anniversary."""
    result = prorate_entitlement(
        session, priya, "EL", dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    )

    entitlements = [s.entitlement for s in result.segments]
    assert D("15.00") in entitlements and D("18.00") in entitlements
    # 15 x 90/365 + 18 x 275/365 = 3.6986 + 13.5616 = 17.260
    assert result.prorated_days == D("17.260")


def test_anniversary_cycle_has_no_tenure_change(session, priya):
    """By construction — which is why the anniversary cycle is the default."""
    result = prorate_entitlement(
        session, priya, "EL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    assert len({s.entitlement for s in result.segments}) == 1


def test_joiner_and_policy_change_compose(session, meera):
    """Both effects at once, still one formula."""
    old = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "CL"
        )
    ).one()
    old.effective_to = dt.date(2025, 12, 31)
    session.flush()
    session.add(OrgPolicy(
        region="India-TamilNadu", legal_entity=old.legal_entity, leave_type_id="CL",
        tenure_min_years=D("0"), tenure_max_years=None,
        entitlement_days_per_year=D("10"), is_paid=True, accrual_method="annual_lump",
        carryover_max_days=D("0"), min_notice_days=1, max_consecutive_days=3,
        effective_from=dt.date(2026, 1, 1), effective_to=None,
        compliance_note="Uplift effective Jan 2026.",
    ))
    session.flush()

    result = prorate_entitlement(
        session, meera, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    # Eligible 2025-10-15 to 2026-03-31 only, split at 2026-01-01:
    #   78 days at 7/yr  + 90 days at 10/yr
    #   7 x 78/365 + 10 x 90/365 = 1.4959 + 2.4658 = 3.962
    assert result.eligible_days == 168
    assert [s.days for s in result.segments] == [78, 90]
    assert result.prorated_days == D("3.962")


# ===========================================================================
# Pro-ration method is configurable per policy
# ===========================================================================
def test_proration_method_none_disables_it(session, meera):
    session.execute(
        OrgPolicy.__table__.update()
        .where(OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "CL")
        .values(proration_method="none")
    )
    session.flush()

    result = prorate_entitlement(
        session, meera, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    assert result.prorated_days == D("7.00")      # full lump despite joining late


def test_proration_method_monthly_uses_whole_months(session, meera):
    session.execute(
        OrgPolicy.__table__.update()
        .where(OrgPolicy.region == "India-TamilNadu", OrgPolicy.leave_type_id == "CL")
        .values(proration_method="monthly")
    )
    session.flush()

    result = prorate_entitlement(
        session, meera, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    )
    # Oct through Mar inclusive = 6 months; 7 x 6/12 = 3.5
    assert result.method == "monthly"
    assert result.prorated_days == D("3.500")


# ===========================================================================
# Reporting
# ===========================================================================
def test_result_explains_itself(session, meera):
    text = prorate_entitlement(
        session, meera, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    ).explain()
    assert "168 of 365 days" in text and "daily" in text


def test_result_serialises(session, meera):
    import json

    payload = prorate_entitlement(
        session, meera, "CL", dt.date(2025, 4, 1), dt.date(2026, 3, 31)
    ).to_dict()
    json.dumps(payload)
    assert payload["is_partial"] is True
    assert payload["prorated_days"] == "3.222"


def test_eligible_days_helper(meera, tom):
    assert eligible_days(meera, dt.date(2025, 4, 1), dt.date(2026, 3, 31)) == 168
    assert eligible_days(tom, dt.date(2025, 4, 1), dt.date(2026, 3, 31)) == 306
    assert eligible_days(meera, dt.date(2024, 1, 1), dt.date(2024, 12, 31)) == 0
