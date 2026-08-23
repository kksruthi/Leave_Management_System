"""Seed script for Module 1.

Populates `org_policies` (two regions), `approval_rules`, and two test
employees (Priya / Raj) that Module 2's tests are written against.

Every policy row goes through `app.validation.add_policy`, so the seed
exercises the same write-time checks a live HR Admin insert would.

Usage:
    python -m app.seed              # seed (skips if already seeded)
    python -m app.seed --reset      # wipe seeded tables first, then seed
    python -m app.seed --strict-spec  # omit the extra Unpaid policy rows
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import (
    ApprovalRule,
    Employee,
    LeaveLedger,
    LeaveRequest,
    OrgPolicy,
    SubstitutionRule,
)
from app.validation import add_policy

D = Decimal

# Versioning window used for every seeded policy: live since 2020, no end date.
EFFECTIVE_FROM = dt.date(2020, 1, 1)
EFFECTIVE_TO = None  # NULL = open-ended

INDIA_ENTITY = "NorthBridge Technologies India Pvt Ltd"
USA_ENTITY = "NorthBridge Systems USA Inc."

# End of the ORGANISATION's leave year per region, "MM-DD". India runs an
# April-March year; the US runs the calendar year.
#
# Applied to annual-lump types only. Those are granted once per leave year, so
# a fixed year is what makes "joined partway through it" meaningful and
# pro-ratable. Monthly types are left anniversary-aligned (NULL), because
# partial service there is already handled month by month — pro-rating the
# annual figure as well would double-count it.
REGION_LEAVE_YEAR_END = {
    "India-TamilNadu": "03-31",
    "USA-Texas": "12-31",
}


# ---------------------------------------------------------------------------
# org_policies — exactly the rows specified in the Module 1 brief
# ---------------------------------------------------------------------------
# (region, entity, leave_type, ten_min, ten_max, days, is_paid, accrual,
#  carryover_max, carryover_expiry, max_consecutive, min_notice, compliance_note)
CORE_POLICIES = [
    # --- India, Tamil Nadu -------------------------------------------------
    ("India-TamilNadu", INDIA_ENTITY, "EL", D("0"), D("1"), D("15"), True, "monthly",
     D("15"), "03-31", 15, 7,
     "Meets OSH&WC Code 2020 Sec 32 annual-leave minimum for Tamil Nadu (1 day per 20 days worked); "
     "NorthBridge grants a flat 15 days from day one, which exceeds the statutory accrual for a "
     "first-year employee."),
    ("India-TamilNadu", INDIA_ENTITY, "EL", D("1"), D("3"), D("18"), True, "monthly",
     D("18"), "03-31", 15, 7,
     "Exceeds OSH&WC Code 2020 Sec 32 minimum for Tamil Nadu; tenure uplift is a NorthBridge "
     "retention policy approved by HR/Legal, not a statutory requirement."),
    ("India-TamilNadu", INDIA_ENTITY, "EL", D("3"), D("5"), D("24"), True, "monthly",
     D("24"), "03-31", 20, 7,
     "Exceeds OSH&WC Code 2020 Sec 32 minimum for Tamil Nadu; 3-5yr retention band, HR/Legal "
     "approved 2020-01-01."),
    ("India-TamilNadu", INDIA_ENTITY, "EL", D("5"), None, D("30"), True, "monthly",
     D("30"), "03-31", 20, 7,
     "Exceeds OSH&WC Code 2020 Sec 32 minimum for Tamil Nadu; senior tenure band, HR/Legal "
     "approved 2020-01-01."),
    ("India-TamilNadu", INDIA_ENTITY, "CL", D("0"), None, D("7"), True, "annual_lump",
     D("0"), None, 3, 1,
     "Casual leave is customary practice in Tamil Nadu (not separately mandated by the OSH&WC "
     "Code); 7 days/yr matches prevailing local industry standard. Lapses at year end."),
    ("India-TamilNadu", INDIA_ENTITY, "SL", D("0"), None, D("10"), True, "annual_lump",
     D("0"), None, 5, 0,
     "Meets ESI Act sick-leave expectations and Tamil Nadu Shops & Establishments Act practice; "
     "10 paid sick days/yr, non-carryover."),

    # --- USA, Texas --------------------------------------------------------
    ("USA-Texas", USA_ENTITY, "EL", D("0"), D("1"), D("10"), True, "monthly",
     D("5"), "12-31", 10, 14,
     "Texas has no statutory PTO mandate; 10 days/yr is NorthBridge's contractual floor for "
     "first-year US staff, benchmarked to US market practice. FLSA imposes no accrual minimum."),
    ("USA-Texas", USA_ENTITY, "EL", D("1"), D("3"), D("15"), True, "monthly",
     D("5"), "12-31", 10, 14,
     "No Texas statutory minimum applies; contractual tenure band per US employee handbook, "
     "HR/Legal approved 2020-01-01."),
    ("USA-Texas", USA_ENTITY, "EL", D("3"), D("5"), D("20"), True, "monthly",
     D("10"), "12-31", 15, 14,
     "No Texas statutory minimum applies; contractual tenure band per US employee handbook, "
     "HR/Legal approved 2020-01-01."),
    ("USA-Texas", USA_ENTITY, "EL", D("5"), None, D("25"), True, "monthly",
     D("10"), "12-31", 15, 14,
     "No Texas statutory minimum applies; senior contractual tenure band per US employee "
     "handbook, HR/Legal approved 2020-01-01."),
    ("USA-Texas", USA_ENTITY, "SL", D("0"), None, D("8"), True, "annual_lump",
     D("0"), None, 5, 0,
     "Texas has no state paid-sick-leave mandate; 8 days/yr is NorthBridge's voluntary US "
     "benefit, non-carryover."),
]

# Not in the brief's seed list, but resolvePolicy() in Module 2 needs a row to
# find for the Unpaid type — otherwise the final fallback in the substitution
# order has no policy to resolve. Skip these with --strict-spec.
UNPAID_POLICIES = [
    ("India-TamilNadu", INDIA_ENTITY, "Unpaid", D("0"), None, D("0"), False, "none",
     D("0"), None, 30, 7,
     "Loss-of-pay leave. No statutory entitlement or minimum applies; granted at management "
     "discretion with no balance requirement."),
    ("USA-Texas", USA_ENTITY, "Unpaid", D("0"), None, D("0"), False, "none",
     D("0"), None, 30, 14,
     "Unpaid leave. No Texas statutory entitlement applies; granted at management discretion "
     "with no balance requirement. FMLA eligibility handled separately by HR."),
]


# ---------------------------------------------------------------------------
# approval_rules
# ---------------------------------------------------------------------------
# Manager is tier 1 unconditionally. It is stored as a row rather than
# hardcoded so the whole routing chain stays inspectable as data.
APPROVAL_RULES = [
    dict(condition_field="always", operator="==", value="true", adds_tier="manager",
         tier_order=1, sla_hours=48, escalate_to_role="hr_admin",
         description="Manager is tier 1 on every request, unconditionally."),
    dict(condition_field="duration_days", operator=">", value="5", adds_tier="hr_admin",
         tier_order=2, sla_hours=72, escalate_to_role="director",
         description="HR added because duration exceeds the 5-day threshold."),
    dict(condition_field="duration_days", operator=">", value="20", adds_tier="director",
         tier_order=3, sla_hours=120, escalate_to_role=None,
         description="Director added because duration exceeds the 20-day threshold."),
    dict(condition_field="leave_type", operator="==", value="Unpaid", adds_tier="hr_admin",
         tier_order=2, sla_hours=72, escalate_to_role="director",
         description="HR added because the request is unpaid leave, regardless of duration."),
]

# ---------------------------------------------------------------------------
# substitution_rules — the fallback order, as data rather than a Python list
# ---------------------------------------------------------------------------
# position 0 is the requested type itself; higher positions are fallbacks.
# region=None is the default chain. Texas gets its own because it has no CL.
SUBSTITUTION_RULES = [
    # Default (India and anywhere else): EL -> CL -> Unpaid
    dict(leave_type_id="EL", region=None, position=0, fallback_leave_type_id="EL"),
    dict(leave_type_id="EL", region=None, position=1, fallback_leave_type_id="CL"),
    dict(leave_type_id="EL", region=None, position=2, fallback_leave_type_id="Unpaid"),
    dict(leave_type_id="CL", region=None, position=0, fallback_leave_type_id="CL"),
    dict(leave_type_id="CL", region=None, position=1, fallback_leave_type_id="EL"),
    dict(leave_type_id="CL", region=None, position=2, fallback_leave_type_id="Unpaid"),
    dict(leave_type_id="SL", region=None, position=0, fallback_leave_type_id="SL"),
    dict(leave_type_id="SL", region=None, position=1, fallback_leave_type_id="CL"),
    dict(leave_type_id="SL", region=None, position=2, fallback_leave_type_id="EL"),
    dict(leave_type_id="SL", region=None, position=3, fallback_leave_type_id="Unpaid"),
    # USA-Texas has no Casual Leave, so its chains skip it entirely.
    dict(leave_type_id="EL", region="USA-Texas", position=0, fallback_leave_type_id="EL"),
    dict(leave_type_id="EL", region="USA-Texas", position=1, fallback_leave_type_id="Unpaid"),
    dict(leave_type_id="SL", region="USA-Texas", position=0, fallback_leave_type_id="SL"),
    dict(leave_type_id="SL", region="USA-Texas", position=1, fallback_leave_type_id="EL"),
    dict(leave_type_id="SL", region="USA-Texas", position=2, fallback_leave_type_id="Unpaid"),
]


# ---------------------------------------------------------------------------
# employees
# ---------------------------------------------------------------------------
MANAGERS = [
    dict(name="Anitha Rajan", email="anitha.rajan@northbridge.example",
         join_date=dt.date(2018, 6, 1), region="India-TamilNadu", role="manager"),
    dict(name="Dana Whitfield", email="dana.whitfield@northbridge.example",
         join_date=dt.date(2017, 9, 15), region="USA-Texas", role="manager"),
    # Approval tiers 2 and 3 need real people to own them, otherwise a step
    # resolves to a role nobody holds and sits in nobody's queue.
    dict(name="Fatima Khan", email="fatima.khan@northbridge.example",
         join_date=dt.date(2016, 2, 1), region="India-TamilNadu", role="hr_admin"),
    dict(name="Nathan Cole", email="nathan.cole@northbridge.example",
         join_date=dt.date(2015, 5, 4), region="USA-Texas", role="director"),
]

EMPLOYEES = [
    dict(name="Priya", email="priya@northbridge.example",
         join_date=dt.date(2025, 4, 1), region="India-TamilNadu",
         manager_email="anitha.rajan@northbridge.example"),
    dict(name="Raj", email="raj@northbridge.example",
         join_date=dt.date(2025, 4, 1), region="USA-Texas",
         manager_email="dana.whitfield@northbridge.example"),

    # --- cases the review flagged as undefined ----------------------------
    # Mid-cycle joiner: started 6 months into the leave year, so both the
    # annual lump and the first month's accrual must be pro-rated.
    dict(name="Meera", email="meera@northbridge.example",
         join_date=dt.date(2025, 10, 15), region="India-TamilNadu",
         manager_email="anitha.rajan@northbridge.example"),
    # Part-time: a half-time schedule earns half the full-time entitlement,
    # from one column rather than a parallel set of policy rows.
    dict(name="Kavya", email="kavya@northbridge.example",
         join_date=dt.date(2025, 4, 1), region="India-TamilNadu",
         employment_fraction=Decimal("0.500"),
         manager_email="anitha.rajan@northbridge.example"),
    # Leaver: accrual must stop at the exit date and the final partial cycle
    # must be pro-rated, rather than accruing forever.
    dict(name="Tom", email="tom@northbridge.example",
         join_date=dt.date(2025, 4, 1), region="USA-Texas",
         exit_date=dt.date(2026, 1, 31), status="terminated",
         manager_email="dana.whitfield@northbridge.example"),
]


def _policy_from_tuple(row) -> OrgPolicy:
    (region, entity, leave_type, ten_min, ten_max, days, is_paid, accrual,
     carry_max, carry_expiry, max_consec, min_notice, note) = row
    return OrgPolicy(
        region=region,
        legal_entity=entity,
        leave_type_id=leave_type,
        tenure_min_years=ten_min,
        tenure_max_years=ten_max,
        entitlement_days_per_year=days,
        is_paid=is_paid,
        accrual_method=accrual,
        carryover_max_days=carry_max,
        carryover_expiry=carry_expiry,
        max_consecutive_days=max_consec,
        min_notice_days=min_notice,
        effective_from=EFFECTIVE_FROM,
        effective_to=EFFECTIVE_TO,
        compliance_note=note,
        leave_year_end=(
            REGION_LEAVE_YEAR_END.get(region) if accrual == "annual_lump" else None
        ),
    )


def reset(session) -> None:
    """Wipe everything this script seeds, children first."""
    session.execute(delete(LeaveLedger))
    session.execute(delete(LeaveRequest))
    session.execute(delete(OrgPolicy))
    session.execute(delete(ApprovalRule))
    session.execute(delete(SubstitutionRule))
    session.execute(delete(Employee))
    session.commit()


def seed(session, *, strict_spec: bool = False) -> dict[str, int]:
    counts = {"policies": 0, "rules": 0, "employees": 0, "substitution_rules": 0}

    # --- policies ---------------------------------------------------------
    rows = CORE_POLICIES if strict_spec else CORE_POLICIES + UNPAID_POLICIES
    if session.scalar(select(func.count()).select_from(OrgPolicy)) == 0:
        for row in rows:
            add_policy(session, _policy_from_tuple(row))
            counts["policies"] += 1

    # --- approval rules ---------------------------------------------------
    if session.scalar(select(func.count()).select_from(ApprovalRule)) == 0:
        for rule in APPROVAL_RULES:
            session.add(ApprovalRule(**rule))
            counts["rules"] += 1

    # --- substitution rules -----------------------------------------------
    if session.scalar(select(func.count()).select_from(SubstitutionRule)) == 0:
        for rule in SUBSTITUTION_RULES:
            session.add(SubstitutionRule(**rule))
            counts["substitution_rules"] = counts.get("substitution_rules", 0) + 1

    # --- employees --------------------------------------------------------
    # Idempotent per person rather than "skip if the table is non-empty", so
    # that employees added to this list later (the pro-ration / part-time /
    # leaver cases) land in an already-seeded database without a full --reset.
    existing = {
        e.email: e for e in session.scalars(select(Employee))
    }

    for mgr in MANAGERS:
        if mgr["email"] in existing:
            continue
        emp = Employee(**mgr)
        session.add(emp)
        existing[emp.email] = emp
        counts["employees"] += 1
    session.flush()

    for spec in EMPLOYEES:
        if spec["email"] in existing:
            continue
        spec = dict(spec)
        manager = existing[spec.pop("manager_email")]
        emp = Employee(manager_id=manager.id, **spec)
        session.add(emp)
        existing[emp.email] = emp
        counts["employees"] += 1

    session.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Leave Engine data layer.")
    parser.add_argument("--reset", action="store_true", help="wipe seeded tables first")
    parser.add_argument("--strict-spec", action="store_true",
                        help="seed only the policy rows listed in the Module 1 brief "
                             "(omits the two Unpaid rows)")
    args = parser.parse_args()

    with SessionLocal() as session:
        if args.reset:
            reset(session)
            print("Reset: cleared org_policies, approval_rules, employees, ledger, requests.")

        counts = seed(session, strict_spec=args.strict_spec)

        if sum(counts.values()) == 0:
            print("Already seeded — nothing to do. Use --reset to re-seed from scratch.")
        else:
            print(f"Seeded {counts['policies']} policies, {counts['rules']} approval rules, "
                  f"{counts['substitution_rules']} substitution rules, "
                  f"{counts['employees']} employees.")

        total = session.scalar(select(func.count()).select_from(OrgPolicy))
        regions = session.scalars(select(OrgPolicy.region).distinct().order_by(OrgPolicy.region)).all()
        print(f"org_policies now holds {total} rows across regions: {', '.join(regions)}")


if __name__ == "__main__":
    main()
