"""Manual trigger for the accrual jobs.

Required by the build phases as CORE, not stretch: the jobs must be runnable
on demand so the system can be demonstrated without waiting for a real
scheduler. Wiring these functions to cron, Celery or an HTTP endpoint later is
a deployment concern — the jobs themselves stay plain library calls.

    python -m app.cli monthly  --run-date 2025-06-01
    python -m app.cli annual   --run-date 2025-04-01
    python -m app.cli simulate --start 2025-04-01 --months 15   # the demo
    python -m app.cli request  --employee Priya --leave-type EL \
                               --start 2026-05-04 --end 2026-05-11
    python -m app.cli dashboard --employee Priya --as-of 2026-04-15
    python -m app.cli ledger   --employee Priya
    python -m app.cli policy   --employee Priya --leave-type EL --as-of 2026-04-01
    python -m app.cli reset-ledger                              # clear demo runs

Note: these commands COMMIT. The test suite runs its own jobs with
commit=False inside a rolled-back transaction, so it expects a clean ledger —
run `reset-ledger` after demoing if you then want to run pytest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import func, select

from app.accrual import (
    REASON_TRUE_UP,
    AccrualResult,
    run_annual_grant,
    run_monthly_accrual,
)
from app.classification import RequestValidationError, submit_leave_request
from app.dashboard import get_dashboard
from app.db import SessionLocal
from app.models import Employee, LeaveLedger
from app.policy_engine import resolve_policy
from app.proration import accrual_cycle, prorate_entitlement
from app.util import format_days


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
    )


def _print_result(result: AccrualResult, *, show_skips: bool) -> None:
    print(f"\n{result.job} — run_date {result.run_date}")
    print("-" * 78)
    if result.written:
        print(f"{'Employee':<14} {'Type':<8} {'Amount':>9}  {'Policy':>7}  Reason")
        for e in result.written:
            marker = "  <- true-up" if e.reason == REASON_TRUE_UP else ""
            print(f"{e.employee_name:<14} {e.leave_type_id:<8} {e.amount:>9} "
                  f"{str(e.policy_snapshot_id):>8}  {e.reason}{marker}")
    else:
        print("(no entries written)")

    if show_skips and result.skipped:
        print(f"\nSkipped ({len(result.skipped)}):")
        for s in result.skipped:
            print(f"  {s.employee_name:<14} {s.leave_type_id:<8} {s.reason}")

    print(f"\n{result.summary()}\n")


def _month_starts(start: dt.date, count: int) -> list[dt.date]:
    dates, year, month = [], start.year, start.month
    for _ in range(count):
        dates.append(dt.date(year, month, 1))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return dates


def cmd_monthly(args) -> None:
    with SessionLocal() as session:
        result = run_monthly_accrual(session, args.run_date, true_up=not args.no_true_up)
        _print_result(result, show_skips=args.show_skips)


def cmd_annual(args) -> None:
    with SessionLocal() as session:
        result = run_annual_grant(session, args.run_date)
        _print_result(result, show_skips=args.show_skips)


def cmd_simulate(args) -> None:
    """Run N consecutive monthly accruals — the bracket-crossing demo.

    Nothing in the accrual code knows about anniversaries. The step change in
    the amount column happens purely because resolve_policy() is re-read on
    every run.
    """
    start = dt.date.fromisoformat(args.start)
    names = args.employees or ["Priya", "Raj"]

    with SessionLocal() as session:
        people = {
            e.name: e for e in session.scalars(select(Employee).where(Employee.name.in_(names)))
        }
        header = "".join(f"{n:>22}" for n in names)
        print(f"\n{'Run date':<12}{header}")
        print("-" * (12 + 22 * len(names)))

        for run_date in _month_starts(start, args.months):
            result = run_monthly_accrual(session, run_date, true_up=not args.no_true_up)
            by_employee = {}
            for entry in result.written:
                if entry.leave_type_id != args.leave_type:
                    continue
                by_employee.setdefault(entry.employee_name, []).append(entry)

            cells = ""
            for name in names:
                entries = by_employee.get(name, [])
                if not entries:
                    cells += f"{'—':>22}"
                    continue
                amount = sum((e.amount for e in entries), Decimal("0"))
                policy = resolve_policy(session, people[name], args.leave_type, run_date)
                note = " *" if any(e.reason == REASON_TRUE_UP for e in entries) else ""
                cells += f"{f'{amount} ({policy.entitlement_days_per_year:g}/yr){note}':>22}"
            print(f"{str(run_date):<12}{cells}")

        print("\n* = year-end true-up folded in")
        print("Entitlement steps up on the join anniversary with no anniversary "
              "logic anywhere in the accrual code.\n")

        for name in names:
            emp = people[name]
            total = session.scalar(
                select(func.coalesce(func.sum(LeaveLedger.amount), 0)).where(
                    LeaveLedger.employee_id == emp.id,
                    LeaveLedger.leave_type_id == args.leave_type,
                )
            )
            print(f"  {name:<8} total {args.leave_type} accrued: {total}")
        print()


def cmd_ledger(args) -> None:
    with SessionLocal() as session:
        stmt = select(LeaveLedger).order_by(LeaveLedger.effective_date, LeaveLedger.id)
        if args.employee:
            emp = session.scalars(select(Employee).where(Employee.name == args.employee)).first()
            if emp is None:
                print(f"No employee named {args.employee!r}")
                return
            stmt = stmt.where(LeaveLedger.employee_id == emp.id)

        rows = session.scalars(stmt).all()
        if not rows:
            print("Ledger is empty.")
            return

        names = {e.id: e.name for e in session.scalars(select(Employee))}
        print(f"\n{'Date':<12} {'Employee':<14} {'Type':<8} {'Amount':>9} "
              f"{'Running':>9}  {'Policy':>7}  Reason")
        print("-" * 92)
        running: dict[tuple[int, str], Decimal] = {}
        for r in rows:
            key = (r.employee_id, r.leave_type_id)
            running[key] = running.get(key, Decimal("0")) + Decimal(r.amount)
            print(f"{str(r.effective_date):<12} {names.get(r.employee_id, '?'):<14} "
                  f"{r.leave_type_id:<8} {r.amount:>9} {running[key]:>9} "
                  f"{str(r.policy_snapshot_id):>8}  {r.reason}")
        print()


def cmd_dashboard(args) -> None:
    """Render the Module 4 payload as text.

    This is a debug view, not a UI — Module 4 produces data only. Use
    `--json` for the raw payload a real frontend would consume.
    """
    with SessionLocal() as session:
        emp = session.scalars(select(Employee).where(Employee.name == args.employee)).first()
        if emp is None:
            print(f"No employee named {args.employee!r}")
            return

        dash = get_dashboard(session, emp, args.as_of)

        if args.json:
            import json
            print(json.dumps(dash.to_dict(), indent=2))
            return

        print(f"\n{dash.name} — {dash.region}")
        print(f"joined {dash.join_date} · tenure {dash.tenure_years} yr · "
              f"as of {dash.as_of_date}")
        print("=" * 74)

        for lt in dash.leave_types:
            print(f"\n{lt.leave_type_id}")
            print(f"  Current balance      {lt.balance} days")
            if not lt.policy_found:
                print(f"  ⚠  {lt.note}")
                continue
            print(f"  Annual entitlement   {lt.entitlement_days_per_year} days/year "
                  f"({'paid' if lt.is_paid else 'unpaid'}, {lt.accrual_method})")
            if lt.next_accrual:
                print(f"  Next accrual         +{lt.next_accrual.amount} on "
                      f"{lt.next_accrual.date}")
                if lt.next_accrual.note:
                    print(f"                       {lt.next_accrual.note}")
            if lt.min_notice_days:
                print(f"  Notice required      {lt.min_notice_days} days")

            if lt.history and not args.no_history:
                print(f"  History ({len(lt.history)} entries)")
                shown = lt.history if args.full_history else lt.history[-5:]
                if len(shown) < len(lt.history):
                    print(f"    … {len(lt.history) - len(shown)} earlier entries "
                          f"(use --full-history)")
                for h in shown:
                    print(f"    {h.effective_date}  {h.amount:>8}  "
                          f"running {h.running_total:>8}   {h.reason}")

        print(f"\nPending requests: {len(dash.pending_requests)}")
        for p in dash.pending_requests:
            tier = f"tier {p.current_tier} ({p.current_role})" if p.current_tier else "unrouted"
            print(f"  #{p.request_id} {p.leave_type_id} {p.start_date}→{p.end_date} "
                  f"({p.duration_days}d) — awaiting {tier}")
            if p.routing_reason:
                print(f"      {p.routing_reason}")
        print()


def cmd_request(args) -> None:
    """Submit a leave request and show the paid/unpaid split."""
    with SessionLocal() as session:
        emp = session.scalars(select(Employee).where(Employee.name == args.employee)).first()
        if emp is None:
            print(f"No employee named {args.employee!r}")
            return

        try:
            result = submit_leave_request(
                session, emp, args.leave_type, args.start, args.end,
                commit=not args.dry_run,
            )
        except RequestValidationError as exc:
            print(f"\nRejected: {exc}\n")
            return

        c = result.classification
        print(f"\nRequest #{result.request_id} — {emp.name} · {args.leave_type}")
        print(f"  {result.request.start_date} → {result.request.end_date}  "
              f"({format_days(c.requested_days)} working days)")
        print(f"  Status               {result.request.status}")
        print(f"  Paid                 {format_days(c.paid_days)} days")
        print(f"  Unpaid               {format_days(c.unpaid_days)} days")
        print(f"  {c.explain()}")

        if c.draws:
            print("\n  Substitution walk")
            for d in c.draws:
                if d.days == 0:
                    continue
                have = "no balance needed" if d.balance_before is None \
                    else f"balance {d.balance_before}"
                tag = "fallback" if d.is_substitution else "requested"
                print(f"    {d.leave_type_id:<8} {d.days:>7}  ({have}, {tag})")

        for w in result.warnings:
            print(f"\n  ⚠  {w}")

        if args.dry_run:
            print("\n  (dry run — not committed)")
        print()


def cmd_proration(args) -> None:
    """Show how a partial year of service becomes a partial entitlement."""
    with SessionLocal() as session:
        emp = session.scalars(select(Employee).where(Employee.name == args.employee)).first()
        if emp is None:
            print(f"No employee named {args.employee!r}")
            return

        on_date = dt.date.fromisoformat(args.as_of)
        policy = resolve_policy(session, emp, args.leave_type, on_date)
        if policy is None:
            print(f"No policy for {args.employee} / {args.leave_type} on {on_date}")
            return

        start, end = accrual_cycle(emp, policy, on_date)
        result = prorate_entitlement(session, emp, args.leave_type, start, end)

        fte = "" if emp.employment_fraction == 1 else f" · {emp.employment_fraction} FTE"
        exited = f" · exited {emp.exit_date}" if emp.exit_date else ""
        print(f"\n{emp.name} — {emp.region}{fte}")
        print(f"joined {emp.join_date}{exited}")
        print("=" * 74)
        print(f"  Leave type           {args.leave_type}")
        print(f"  Accrual year         {start} → {end}  ({result.cycle_days} days, "
              f"{'fixed org year' if policy.leave_year_end else 'anniversary-aligned'})")
        print(f"  Eligible service     {result.eligible_start} → {result.eligible_end}  "
              f"({result.eligible_days} days)")
        print(f"  Method / rounding    {result.method} · {result.rounding_dp}dp")
        print(f"  Full-year figure     {result.full_year_entitlement} days")
        print(f"  Pro-rated to         {result.prorated_days} days")

        if len(result.segments) > 1 or result.is_partial:
            print("\n  Segments")
            for s in result.segments:
                print(f"    {s.start} → {s.end}  {s.days:>4}d  @ {s.entitlement:>6}/yr  "
                      f"contributes {s.contribution.quantize(Decimal('0.001'))}")
        print()


def cmd_carryover(args) -> None:
    from app.accrual import run_year_end_carryover

    with SessionLocal() as session:
        _print_result(run_year_end_carryover(session, args.run_date),
                      show_skips=args.show_skips)


def cmd_policy_year(args) -> None:
    """Preview or run HR's annual policy roll-forward."""
    from app.policy_admin import PolicyAdminError, preview_roll_forward, roll_forward_year

    changes = None
    if args.set:
        changes = {}
        for item in args.set:
            target, field, value = item.split(":", 2)
            changes.setdefault(target, {})[field] = value

    with SessionLocal() as session:
        try:
            plan = preview_roll_forward(
                session, args.region, args.from_year, changes,
                default_leave_year_end=args.leave_year_end,
            )
            print("\n" + plan.describe() + "\n")

            if not args.apply:
                print("Preview only. Re-run with --apply --actor NAME "
                      "--reason '...' to open the year.\n")
                return

            actor = session.scalars(
                select(Employee).where(Employee.name == args.actor)
            ).first()
            if actor is None:
                print(f"No employee named {args.actor!r}")
                return

            created = roll_forward_year(
                session, args.region, args.from_year, changes,
                actor=actor, change_reason=args.reason,
                default_leave_year_end=args.leave_year_end,
            )
            print(f"Opened leave year {plan.to_year}: {len(created)} policy rows "
                  f"created by {actor.name}.\n")
        except PolicyAdminError as exc:
            print(f"\nRefused: {exc}\n")


def cmd_outbox(args) -> None:
    """Show queued events — what a relay would publish."""
    from app.models import OutboxEvent

    with SessionLocal() as session:
        rows = session.scalars(
            select(OutboxEvent).order_by(OutboxEvent.id.desc()).limit(args.limit)
        ).all()
        if not rows:
            print("Outbox is empty.")
            return
        print(f"\n{'id':>5} {'topic':<28} {'aggregate':<22} {'published':<10} payload")
        print("-" * 100)
        for e in rows:
            agg = f"{e.aggregate_type}:{e.aggregate_id}"
            state = "yes" if e.published_at else "queued"
            print(f"{e.id:>5} {e.topic:<28} {agg:<22} {state:<10} {e.payload[:40]}")
        print()


def cmd_reset_ledger(args) -> None:
    """Clear the ledger so a demo can be re-run from scratch.

    The ledger is append-only at the database level (finding 16), so this has
    to disable the guard trigger for the duration. That is deliberate: wiping
    an audit trail should be an explicit, obviously-privileged act, not
    something an ordinary DELETE can do by accident.

    NOT for production. In production a mistaken entry is corrected by
    appending a reversing row, which leaves both the error and the fix
    visible.
    """
    from sqlalchemy import text

    if not args.i_understand:
        print(
            "Refusing to wipe the ledger.\n"
            "This deletes an append-only audit trail and is intended only for "
            "resetting a demo database.\n"
            "Re-run with --i-understand if that is really what you want."
        )
        return

    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(LeaveLedger))
        session.execute(text(
            "ALTER TABLE leave_ledger DISABLE TRIGGER trg_leave_ledger_append_only"))
        session.execute(LeaveLedger.__table__.delete())
        session.execute(text(
            "ALTER TABLE leave_ledger ENABLE TRIGGER trg_leave_ledger_append_only"))
        session.commit()
        print(f"Deleted {count} ledger rows. Policies and employees untouched.")


def cmd_policy(args) -> None:
    with SessionLocal() as session:
        emp = session.scalars(select(Employee).where(Employee.name == args.employee)).first()
        if emp is None:
            print(f"No employee named {args.employee!r}")
            return
        resolved = resolve_policy(session, emp, args.leave_type, args.as_of)
        if resolved is None:
            print(f"No policy found for {args.employee} / {args.leave_type} on {args.as_of}")
            return
        print(f"\n{resolved.explain()}")
        print(f"  tenure          {resolved.tenure_years} years")
        print(f"  accrual         {resolved.accrual_method} ({resolved.monthly_accrual}/month)")
        print(f"  paid            {resolved.is_paid}")
        print(f"  carryover       max {resolved.carryover_max_days}, "
              f"expires {resolved.carryover_expiry or 'n/a'}")
        print(f"  notice          {resolved.min_notice_days} days")
        print(f"  compliance      {resolved.compliance_note}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Manual triggers for the Leave Engine accrual jobs.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show INFO logging")
    sub = parser.add_subparsers(dest="command", required=True)

    today = dt.date.today().isoformat()

    p = sub.add_parser("monthly", help="run the monthly accrual job")
    p.add_argument("--run-date", default=today, help="YYYY-MM-DD (default: today)")
    p.add_argument("--no-true-up", action="store_true", help="skip the year-end correction")
    p.add_argument("--show-skips", action="store_true", default=True)
    p.set_defaults(func=cmd_monthly)

    p = sub.add_parser("annual", help="run the annual lump-sum grant job")
    p.add_argument("--run-date", default=today, help="YYYY-MM-DD (default: today)")
    p.add_argument("--show-skips", action="store_true", default=True)
    p.set_defaults(func=cmd_annual)

    p = sub.add_parser("simulate", help="run N months of accrual back to back (demo)")
    p.add_argument("--start", dest="start", default="2025-04-01", help="first run date")
    p.add_argument("--months", type=int, default=24)
    p.add_argument("--leave-type", default="EL")
    p.add_argument("--employees", nargs="*", default=None)
    p.add_argument("--no-true-up", action="store_true")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("ledger", help="print ledger rows with a running total")
    p.add_argument("--employee", default=None, help="filter by employee name")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("dashboard", help="show what an employee sees (Module 4 payload)")
    p.add_argument("--employee", required=True)
    p.add_argument("--as-of", default=today)
    p.add_argument("--json", action="store_true", help="print the raw payload")
    p.add_argument("--full-history", action="store_true", help="show every ledger row")
    p.add_argument("--no-history", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("request", help="submit a leave request and classify it")
    p.add_argument("--employee", required=True)
    p.add_argument("--leave-type", default="EL")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="classify without saving")
    p.set_defaults(func=cmd_request)

    p = sub.add_parser("proration", help="explain a partial-year entitlement")
    p.add_argument("--employee", required=True)
    p.add_argument("--leave-type", default="CL")
    p.add_argument("--as-of", default=today)
    p.set_defaults(func=cmd_proration)

    p = sub.add_parser("policy-year", help="HR's annual policy roll-forward")
    p.add_argument("--region", required=True)
    p.add_argument("--from-year", type=int, required=True,
                   help="the leave year being closed")
    p.add_argument("--set", nargs="*", metavar="TYPE:FIELD:VALUE",
                   help="e.g. CL:entitlement_days_per_year:9")
    p.add_argument("--leave-year-end", default=None, help="e.g. 03-31")
    p.add_argument("--apply", action="store_true", help="write it (default: preview)")
    p.add_argument("--actor", help="HR admin or director doing this")
    p.add_argument("--reason", help="recorded on every new row")
    p.set_defaults(func=cmd_policy_year)

    p = sub.add_parser("outbox", help="show queued events")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_outbox)

    p = sub.add_parser("carryover", help="run the year-end carry-over job")
    p.add_argument("--run-date", required=True, help="last day of the leave year")
    p.add_argument("--show-skips", action="store_true", default=False)
    p.set_defaults(func=cmd_carryover)

    p = sub.add_parser("reset-ledger", help="delete all ledger rows (demo cleanup)")
    p.add_argument("--i-understand", action="store_true",
                   help="confirm wiping the append-only audit trail")
    p.set_defaults(func=cmd_reset_ledger)

    p = sub.add_parser("policy", help="resolve a policy without writing anything")
    p.add_argument("--employee", required=True)
    p.add_argument("--leave-type", default="EL")
    p.add_argument("--as-of", default=today)
    p.set_defaults(func=cmd_policy)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
