"""Populate balances and a few requests so the dashboards have something to show."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.accrual import run_annual_grant, run_monthly_accrual
from app.approval import record_approval_decision, submit_approval_chain
from app.classification import submit_leave_request
from app.db import SessionLocal
from app.models import Employee, LeaveRequest

TODAY = dt.date.today()


def main() -> None:
    with SessionLocal() as s:
        # Two years of accrual so balances look realistic.
        year = TODAY.year - 1
        run_annual_grant(s, dt.date(year, 4, 1), commit=True)
        run_annual_grant(s, dt.date(year, 1, 1), commit=True)
        d = dt.date(year, 1, 1)
        while d <= TODAY:
            run_monthly_accrual(s, d, commit=True)
            d = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)

        people = {e.name: e for e in s.scalars(select(Employee))}
        existing = s.scalar(select(LeaveRequest.id).limit(1))
        if existing:
            print("Requests already present — skipping.")
            return

        def book(name, leave_type, start_offset, days, half=False):
            person = people.get(name)
            if person is None:
                return None
            start = TODAY + dt.timedelta(days=start_offset)
            end = start + dt.timedelta(days=days - 1)
            try:
                res = submit_leave_request(
                    s, person, leave_type, start, end,
                    start_half_day=half, commit=False,
                )
                submit_approval_chain(s, res.request)
                s.commit()
                return res.request
            except Exception as exc:  # noqa: BLE001 — demo data is best-effort
                s.rollback()
                print(f"  skipped {name} {leave_type}: {exc}")
                return None

        # A pending manager decision, an approved absence, and one in HR's queue.
        r1 = book("Priya", "EL", 21, 3)
        r2 = book("Arjun Menon", "EL", 12, 7)
        r3 = book("Raj", "EL", 30, 2)
        book("Sneha Iyer", "CL", 40, 2, half=True)
        book("Lena Ortiz", "EL", 18, 4)
        # A manager's own leave — this one routes to HR, not to another manager.
        book("Anitha Rajan", "EL", 45, 3)

        anitha = people.get("Anitha Rajan")
        if r1 and anitha:
            record_approval_decision(s, r1.id, 1, "approved", actor=anitha,
                                     decision_reason="Cover arranged with the team.")
            s.commit()
        if r2 and anitha:
            record_approval_decision(s, r2.id, 1, "approved", actor=anitha,
                                     decision_reason="Fine — forwarding to HR.")
            s.commit()

        print("Demo requests created.")


if __name__ == "__main__":
    main()
