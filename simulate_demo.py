"""Build a demo history that actually demonstrates the engine.

    python simulate_demo.py

An empty database makes every screen look the same: full balances, no
history, nothing to audit. The dynamic parts of this system — accrual over
time, leave consumed, carry-over expiring, tenure crossing a bracket, a
region transfer re-pricing an entitlement, a policy year turning over — are
all *time* phenomena, and none of them show up until time has passed.

So this script plays two leave years forward, in order, writing real rows
through the real engine. Nothing here is fixture data poked into tables: every
number below is produced by the same code paths the API calls.

WHAT IT SIMULATES

  1. **Leave year 2025** accrues month by month in both regions.
  2. **Leave is requested, routed and approved** — some by the manager, some
     forwarded to HR — and each approval deducts from the ledger.
  3. **Year end** runs the carry-over job: capped at 18 days, moved into its
     own bucket, expiring three months into the new year, excess forfeited on
     the record.
  4. **Leave year 2026** accrues, and May's leave eats the expiring carry-over
     before touching the new year's days.
  5. **A region transfer** moves Ravi to Texas mid-year; the entitlement
     difference is reconciled into the ledger with a reason anyone can read.
  6. **A tenure crossing** — Arjun passes three years and his EL rate changes
     without anyone editing anything.
  7. **A policy year is published** for 2027, giving 2026 a closing date and
     writing a new version rather than editing the old one.
  8. **Pending requests are left in the queues** so the approvals screen,
     the team calendar and the notification bell all have something in them.

It is destructive: it resets the ledger and rebuilds it. That is the point —
run it whenever the demo data drifts.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select, text

from app.accrual import run_annual_grant, run_monthly_accrual, run_year_end_carryover
from app.approval import forward_request, record_approval_decision, submit_approval_chain
from app.classification import RequestValidationError, submit_leave_request
from app.dashboard import get_balance_buckets, get_live_balance
from app.db import SessionLocal
from app.models import Employee, LeaveLedger, LeaveRequest, Notification
from app.policy_admin import PolicyAdminError
from app.policy_engine import resolve_policy
from app.policy_lifecycle import publish_policy_year
from app.region_transfer import reconcile_region_transfer

D = Decimal
TODAY = dt.date(2026, 8, 19)          # the "now" the demo is built around

# Month ends to accrue, in order. Two full leave years for both regions.
MONTH_ENDS = [
    dt.date(2025, m, 1) for m in range(1, 13)
] + [
    dt.date(2026, m, 1) for m in range(1, 9)
]

# Annual-lump run dates: one inside each leave year of each region. The grant
# is dated to the year it COVERS, so these only need to fall somewhere inside.
ANNUAL_RUNS = [
    dt.date(2025, 2, 1),    # Texas leave year 2025
    dt.date(2025, 5, 1),    # India leave year 2025
    dt.date(2026, 2, 1),    # Texas leave year 2026
    dt.date(2026, 5, 1),    # India leave year 2026
]


def _last_day(d: dt.date) -> dt.date:
    nxt = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def who(session, name: str) -> Employee:
    return session.scalars(select(Employee).where(Employee.name == name)).one()


def say(step: str, detail: str = "") -> None:
    print(f"\n\033[1m{step}\033[0m" + (f"\n  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Helpers that drive the real engine
# ---------------------------------------------------------------------------
def take_leave(
    session, employee_name: str, leave_type: str, start: str, end: str,
    *, decide: str = "approve", via_hr: bool = False, reason: str | None = None,
) -> LeaveRequest | None:
    """Submit, route and decide one request. Returns None if it was rejected
    by validation (a bank holiday week, a notice breach — both realistic)."""
    employee = who(session, employee_name)
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    # Submitted three weeks ahead, so notice rules are satisfied honestly
    # rather than bypassed.
    submitted = dt.datetime.combine(
        start_d - dt.timedelta(days=21), dt.time(9, 30), tzinfo=dt.timezone.utc
    )
    try:
        result = submit_leave_request(
            session, employee, leave_type, start_d, end_d,
            submitted_at=submitted, employee_reason=reason, commit=False,
        )
    except RequestValidationError as exc:
        print(f"  · {employee_name} {leave_type} {start}: not submitted — {exc}")
        return None

    steps = submit_approval_chain(session, result.request)
    request = result.request
    if request.status == "approved":            # a director self-approves
        print(f"  ✓ {employee_name} {leave_type} {start}→{end}: self-approved "
              f"({request.duration_days} days)")
        return request

    step = steps[0]
    approver = session.get(Employee, step.assigned_approver_id)
    if approver is None:
        print(f"  · {employee_name} {leave_type} {start}: nobody to approve it")
        return request

    if decide == "pending":
        print(f"  … {employee_name} {leave_type} {start}→{end}: awaiting "
              f"{approver.name} ({request.duration_days} days)")
        return request

    if via_hr:
        hr_step = forward_request(
            session, request.id, step.tier, "hr_admin", actor=approver,
            note="Over the threshold — HR should see this.",
        )
        hr = session.get(Employee, hr_step.assigned_approver_id)
        if decide == "pending_hr":
            print(f"  … {employee_name} {leave_type} {start}→{end}: "
                  f"{approver.name} forwarded to {hr.name}")
            return request
        record_approval_decision(session, request.id, hr_step.tier, "approved", actor=hr)
        print(f"  ✓ {employee_name} {leave_type} {start}→{end}: "
              f"{approver.name} → {hr.name} approved ({request.duration_days} days)")
        return request

    decision = "approved" if decide == "approve" else "rejected"
    record_approval_decision(
        session, request.id, step.tier, decision, actor=approver,
        decision_reason=None if decision == "approved" else "Two people already off that week.",
    )
    mark = "✓" if decision == "approved" else "✗"
    print(f"  {mark} {employee_name} {leave_type} {start}→{end}: "
          f"{approver.name} {decision} ({request.duration_days} days)")
    return request


def show_balance(session, name: str, leave_type: str = "EL", as_of: dt.date = TODAY) -> None:
    person = who(session, name)
    b = get_balance_buckets(session, person.id, leave_type, as_of)
    policy = resolve_policy(session, person, leave_type, as_of)
    total = b["current"] + b["carryover"]
    entitlement = f"{policy.entitlement_days_per_year:g}/yr" if policy else "—"
    print(f"  {name:<20} {leave_type}  current {b['current']:>8}  "
          f"carry-over {b['carryover']:>7}  available {total:>8}   ({entitlement})")


# ===========================================================================
def main() -> None:
    with SessionLocal() as session:
        # -------------------------------------------------------------------
        say("0. Clearing the ledger",
            "Append-only in normal operation; the demo builder is the one "
            "caller allowed to start over.")
        # The append-only trigger is a production guarantee, so it is lifted
        # explicitly and put straight back — never quietly dropped.
        session.execute(text(
            "ALTER TABLE leave_ledger DISABLE TRIGGER trg_leave_ledger_append_only"))
        session.execute(LeaveLedger.__table__.delete())
        # A request whose deduction has been wiped would be a lie, so the
        # requests (and their approval steps, by cascade) go too.
        session.execute(LeaveRequest.__table__.delete())
        # Old notifications describe requests that no longer exist.
        session.execute(Notification.__table__.delete())
        session.execute(text(
            "ALTER TABLE leave_ledger ENABLE TRIGGER trg_leave_ledger_append_only"))
        # Put Ravi back in Tamil Nadu so step 6 has a transfer to perform on a
        # re-run. Everything else about him is rebuilt from scratch anyway.
        who(session, "Ravi Chandran").region = "India-TamilNadu"
        session.commit()

        # -------------------------------------------------------------------
        say("1. Two leave years of accrual",
            "Monthly EL accrues at annual÷12; CL and SL land as annual lumps "
            "dated to the leave year they cover.")
        for run_date in ANNUAL_RUNS:
            run_annual_grant(session, run_date, commit=False)
        for month_start in MONTH_ENDS:
            run_monthly_accrual(session, _last_day(month_start), commit=False)
        session.commit()
        for name in ("Priya", "Arjun Menon", "Ravi Chandran", "Raj"):
            show_balance(session, name)

        # -------------------------------------------------------------------
        say("2. Leave year 2025 — requests, routing, approvals",
            "Each approval deducts from the ledger in the same transaction as "
            "the status change.")
        take_leave(session, "Priya", "CL", "2025-07-14", "2025-07-15",
                   reason="Family function.")
        take_leave(session, "Arjun Menon", "EL", "2025-09-08", "2025-09-12",
                   reason="Holiday in Kerala.")
        take_leave(session, "Ravi Chandran", "SL", "2025-11-17", "2025-11-18",
                   reason="Fever.")
        take_leave(session, "Nikhil Raghavan", "EL", "2025-12-22", "2025-12-31",
                   via_hr=True, reason="Year-end break.")
        take_leave(session, "Lakshmi Narayan", "EL", "2026-01-05", "2026-01-09",
                   decide="reject")
        take_leave(session, "Raj", "EL", "2025-10-06", "2025-10-10",
                   reason="Moving house.")
        take_leave(session, "Lena Ortiz", "SL", "2025-08-11", "2025-08-12")
        session.commit()

        # -------------------------------------------------------------------
        say("3. Year end — carry-over, capped and dated",
            "Texas closes 31 Dec, India 31 Mar. Up to 18 days move into their "
            "own bucket and expire three months later; the excess is forfeited "
            "on the record, not silently dropped.")
        run_year_end_carryover(session, dt.date(2025, 12, 31), commit=False)
        run_year_end_carryover(session, dt.date(2026, 3, 31), commit=False)
        session.commit()

        for row in session.scalars(
            select(LeaveLedger)
            .where(LeaveLedger.bucket == "carryover")
            .order_by(LeaveLedger.employee_id)
        ):
            person = session.get(Employee, row.employee_id)
            print(f"  {person.name:<20} {row.leave_type_id}  +{row.amount}  "
                  f"from {row.effective_date}  expires {row.expires_on}")

        # -------------------------------------------------------------------
        say("4. Leave year 2026 — the expiring days are spent first",
            "A May request draws on carry-over before touching this year's "
            "accrual, so perishable days are not left to lapse in June.")
        before = {}
        for name in ("Nikhil Raghavan", "Ravi Chandran"):
            person = who(session, name)
            before[name] = get_balance_buckets(session, person.id, "EL", dt.date(2026, 5, 1))

        take_leave(session, "Nikhil Raghavan", "EL", "2026-05-11", "2026-05-15",
                   reason="Cousin's wedding.")
        take_leave(session, "Ravi Chandran", "EL", "2026-06-15", "2026-06-19",
                   reason="Trekking.")
        session.commit()

        for name in ("Nikhil Raghavan", "Ravi Chandran"):
            person = who(session, name)
            after = get_balance_buckets(session, person.id, "EL", dt.date(2026, 7, 1))
            print(f"  {name:<20} carry-over {before[name]['carryover']} → "
                  f"{after['carryover']}   current {before[name]['current']} → "
                  f"{after['current']}")

        # -------------------------------------------------------------------
        say("5. A tenure crossing",
            "Arjun joined 10 Jul 2023, so he passed three years last month. "
            "Nobody edited anything — the bracket is resolved from the date.")
        arjun = who(session, "Arjun Menon")
        for on in (dt.date(2026, 6, 1), dt.date(2026, 8, 1)):
            policy = resolve_policy(session, arjun, "EL", on)
            print(f"  on {on}:  tenure {policy.tenure_years:g} yr  →  "
                  f"{policy.entitlement_days_per_year:g} days/yr "
                  f"({policy.source})")

        # -------------------------------------------------------------------
        say("6. A region transfer",
            "Ravi moves to Texas on 1 Jul 2026. Entitlement is a function of "
            "region, so the difference is reconciled into the ledger with a "
            "reason the employee can check.")
        ravi = who(session, "Ravi Chandran")
        old_region = ravi.region
        if old_region != "USA-Texas":
            ravi.region = "USA-Texas"
            session.flush()
            for a in reconcile_region_transfer(
                session, ravi.id, old_region, "USA-Texas", dt.date(2026, 7, 1),
                commit=False,
            ):
                print(f"  {a.leave_type_id}: {a.old_entitlement:g}/yr → "
                      f"{a.new_entitlement:g}/yr   earned so far "
                      f"{a.old_prorated} → {a.new_prorated}   adjustment "
                      f"{a.adjustment:+}")
            session.commit()
        print(f"  {ravi.name} is now in {ravi.region}; his next request will be "
              "priced, routed and holidayed as a Texas employee.")

        # -------------------------------------------------------------------
        say("7. A policy year is published",
            "The 2026 policy gets a closing date and 2027 is written as a NEW "
            "version. Nothing is edited, so a 2026 ledger row still resolves "
            "to the 2026 number forever.")
        hr = who(session, "Fatima Khan")
        try:
            created = publish_policy_year(
                session, "India-TamilNadu", 2026,
                {"EL": {"entitlement_days_per_year": "20"}},
                actor=hr,
                change_reason="FY2027 review: entry-level EL raised to 20 days.",
                default_leave_year_end="03-31",
                commit=True,
            )
            print(f"  {len(created)} rows published for 2027, in force until "
                  f"{created[0].effective_to}. Everyone in the region was notified.")
        except PolicyAdminError as exc:
            print(f"  (already published: {exc})")

        # -------------------------------------------------------------------
        say("8. Live queues",
            "Left deliberately undecided so the approvals screen, the team "
            "calendar and the notification bell are not empty.")
        take_leave(session, "Priya", "EL", "2026-09-14", "2026-09-18",
                   decide="pending", reason="Diwali travel, booking early.")
        take_leave(session, "Meera", "CL", "2026-09-03", "2026-09-04",
                   decide="pending", reason="House move.")
        take_leave(session, "Aravind Kumar", "EL", "2026-10-05", "2026-10-16",
                   decide="pending_hr", via_hr=True, reason="Sabbatical trip.")
        take_leave(session, "Owen Fletcher", "EL", "2026-09-21", "2026-09-25",
                   decide="pending", reason="Family visit.")
        session.commit()

        # -------------------------------------------------------------------
        say("9. Where everyone stands today", f"as of {TODAY}")
        print(f"  {'Name':<20} {'':<3} {'current':>9} {'carry-over':>12} "
              f"{'available':>11}   entitlement")
        for name in ("Priya", "Arjun Menon", "Nikhil Raghavan", "Ravi Chandran",
                     "Meera", "Kavya", "Raj", "Owen Fletcher"):
            show_balance(session, name, "EL")

        taken = session.scalars(
            select(LeaveRequest).where(LeaveRequest.status == "approved")
        ).all()
        pending = session.scalars(
            select(LeaveRequest).where(LeaveRequest.status == "pending")
        ).all()
        rejected = session.scalars(
            select(LeaveRequest).where(LeaveRequest.status == "rejected")
        ).all()
        rows = session.scalar(select(LeaveLedger.id).order_by(LeaveLedger.id.desc()))
        print(f"\n  {len(taken)} approved · {len(pending)} pending · "
              f"{len(rejected)} rejected · ledger runs to row {rows}")
        print("\n  Every figure above came from the engine, not from a fixture. "
              "The audit screen\n  for any request shows the same numbers with "
              "the policy version they were computed against.\n")


if __name__ == "__main__":
    main()
