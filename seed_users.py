"""Give the seeded employees logins, departments and a demo organisation.

Idempotent: run it as often as you like.

## Why the cast is this big

A two-person demo cannot show what the engine actually does. Routing only
becomes interesting when there is more than one manager to be wrong about,
more than one HR admin to route regionally between, and a director sitting
above HR. Balances only become interesting when people have different tenure,
different regions, part-time fractions and mid-year join dates.

So the cast is built to exercise specific behaviours rather than to look full:

  * **Two regions** — Tamil Nadu (03-31 leave year, EL/CL/SL) and Texas
    (12-31 leave year, EL/SL, no Casual Leave). The same code produces
    different entitlements, holidays and rounding for the two.
  * **Two HR admins, one per region** — proves `resolve_assigned_approver`
    routes a Chennai request to Chennai HR, not to whoever has the lowest id.
  * **Two directors** — so HR's own leave has somewhere to go.
  * **Five managers** — so "another manager cannot approve my report's leave"
    is a thing that can actually be demonstrated.
  * **Tenure spread** — 2013 to 2026 join dates, crossing every EL tenure
    bracket (0–1, 1–3, 3–5, 5+ years).
  * **Edge cases kept from the original seed** — a mid-cycle joiner, a
    half-time employee, and a terminated employee.

## Passwords

Every demo account shares one password, printed by this script when it runs.
It is deliberately NOT shown on the login page: a screen that lists working
credentials teaches everyone who sees a screenshot how to sign in, and the
review asked for it to go.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal
from app.models import Employee

DEMO_PASSWORD = "leave1234"

IN = "India-TamilNadu"
US = "USA-Texas"


def _p(name, email, role, region, department, join, **kw):
    return dict(
        name=name, email=email, role=role, region=region,
        department=department, join_date=join, **kw,
    )


# ---------------------------------------------------------------------------
# Leadership — no manager above them; the role ladder handles their own leave
# ---------------------------------------------------------------------------
LEADERSHIP = [
    _p("Nathan Cole", "nathan.cole@northbridge.example",
       "director", US, "Executive", dt.date(2013, 2, 1)),
    _p("Vikram Suresh", "vikram.suresh@northbridge.example",
       "director", IN, "Executive", dt.date(2014, 8, 18)),
    _p("Fatima Khan", "fatima.khan@northbridge.example",
       "hr_admin", IN, "People", dt.date(2016, 2, 1)),
    _p("Grace Bennett", "grace.bennett@northbridge.example",
       "hr_admin", US, "People", dt.date(2019, 5, 6)),
]

# ---------------------------------------------------------------------------
# Managers — each owns a department in one region
# ---------------------------------------------------------------------------
MANAGERS = [
    _p("Anitha Rajan", "anitha.rajan@northbridge.example",
       "manager", IN, "Engineering", dt.date(2018, 6, 1)),
    _p("Karthik Balan", "karthik.balan@northbridge.example",
       "manager", IN, "Design", dt.date(2019, 9, 2)),
    _p("Deepa Nair", "deepa.nair@northbridge.example",
       "manager", IN, "Support", dt.date(2021, 1, 11)),
    _p("Dana Whitfield", "dana.whitfield@northbridge.example",
       "manager", US, "Sales", dt.date(2017, 9, 15)),
    _p("Marcus Reid", "marcus.reid@northbridge.example",
       "manager", US, "Engineering", dt.date(2020, 4, 20)),
]

A = "anitha.rajan@northbridge.example"
K = "karthik.balan@northbridge.example"
D = "deepa.nair@northbridge.example"
W = "dana.whitfield@northbridge.example"
M = "marcus.reid@northbridge.example"

# ---------------------------------------------------------------------------
# Individual contributors
# ---------------------------------------------------------------------------
STAFF = [
    # --- Anitha's engineers (Tamil Nadu): a full tenure spread -------------
    _p("Priya Venkatesan", "priya@northbridge.example",
       "employee", IN, "Engineering", dt.date(2025, 4, 1), manager_email=A),
    _p("Arjun Menon", "arjun.menon@northbridge.example",
       "employee", IN, "Engineering", dt.date(2023, 7, 10), manager_email=A),
    _p("Nikhil Raghavan", "nikhil.raghavan@northbridge.example",
       "employee", IN, "Engineering", dt.date(2019, 3, 4), manager_email=A),
    _p("Divya Shankar", "divya.shankar@northbridge.example",
       "employee", IN, "Engineering", dt.date(2026, 2, 2), manager_email=A),
    _p("Ravi Chandran", "ravi.chandran@northbridge.example",
       "employee", IN, "Engineering", dt.date(2021, 11, 22), manager_email=A),

    # --- Karthik's designers, including the part-timers --------------------
    _p("Meera Sundaram", "meera@northbridge.example",
       "employee", IN, "Design", dt.date(2025, 10, 15), manager_email=K),
    _p("Kavya Prakash", "kavya@northbridge.example",
       "employee", IN, "Design", dt.date(2025, 4, 1),
       employment_fraction=Decimal("0.500"), manager_email=K),
    _p("Sneha Iyer", "sneha.iyer@northbridge.example",
       "employee", IN, "Design", dt.date(2024, 11, 4),
       employment_fraction=Decimal("0.800"), manager_email=K),
    _p("Aravind Kumar", "aravind.kumar@northbridge.example",
       "employee", IN, "Design", dt.date(2022, 6, 13), manager_email=K),

    # --- Deepa's support team ---------------------------------------------
    _p("Lakshmi Narayan", "lakshmi.narayan@northbridge.example",
       "employee", IN, "Support", dt.date(2020, 8, 3), manager_email=D),
    _p("Suresh Pillai", "suresh.pillai@northbridge.example",
       "employee", IN, "Support", dt.date(2024, 1, 15), manager_email=D),
    _p("Bhavana Rao", "bhavana.rao@northbridge.example",
       "employee", IN, "Support", dt.date(2026, 6, 1), manager_email=D),

    # --- Dana's sales team (Texas) ----------------------------------------
    _p("Raj Patel", "raj@northbridge.example",
       "employee", US, "Sales", dt.date(2025, 4, 1), manager_email=W),
    _p("Lena Ortiz", "lena.ortiz@northbridge.example",
       "employee", US, "Sales", dt.date(2022, 3, 14), manager_email=W),
    _p("Tom Alvarez", "tom@northbridge.example",
       "employee", US, "Sales", dt.date(2025, 4, 1),
       exit_date=dt.date(2026, 1, 31), status="terminated", manager_email=W),
    _p("Chloe Barnes", "chloe.barnes@northbridge.example",
       "employee", US, "Sales", dt.date(2018, 10, 29), manager_email=W),

    # --- Marcus's engineers (Texas) ---------------------------------------
    _p("Owen Fletcher", "owen.fletcher@northbridge.example",
       "employee", US, "Engineering", dt.date(2021, 5, 17), manager_email=M),
    _p("Priscilla Adeyemi", "priscilla.adeyemi@northbridge.example",
       "employee", US, "Engineering", dt.date(2024, 9, 9), manager_email=M),
    _p("Jonah Weiss", "jonah.weiss@northbridge.example",
       "employee", US, "Engineering", dt.date(2026, 3, 16), manager_email=M),
    _p("Isabel Moreno", "isabel.moreno@northbridge.example",
       "employee", US, "Engineering", dt.date(2016, 7, 25),
       employment_fraction=Decimal("0.600"), manager_email=M),
]


def main() -> None:
    with SessionLocal() as session:
        by_email = {e.email: e for e in session.scalars(select(Employee))}
        created = 0

        # Leadership and managers first: staff reference them by email.
        for spec in LEADERSHIP + MANAGERS:
            person = by_email.get(spec["email"])
            if person is None:
                person = Employee(**spec)
                session.add(person)
                by_email[spec["email"]] = person
                created += 1
            else:
                person.role = spec["role"]
                person.department = spec["department"]
                person.region = spec["region"]
        session.flush()

        for spec in STAFF:
            spec = dict(spec)
            manager_email = spec.pop("manager_email", None)
            manager = by_email.get(manager_email) if manager_email else None
            person = by_email.get(spec["email"])
            if person is None:
                person = Employee(manager_id=manager.id if manager else None, **spec)
                session.add(person)
                by_email[spec["email"]] = person
                created += 1
            else:
                # Keep an existing person's history; only re-point the org chart.
                person.department = spec["department"]
                person.role = spec["role"]
                if manager is not None:
                    person.manager_id = manager.id
        session.flush()

        # Managers, HR and directors are left with manager_id = NULL on
        # purpose. `resolve_assigned_approver` walks the ROLE ladder for
        # anyone with no line manager, and that ladder is what sends a
        # manager's own leave to HR and HR's own leave to a director. Wiring
        # a manager_id here would bypass the behaviour the demo exists to show.

        for person in by_email.values():
            if person.password_hash is None:
                person.password_hash = hash_password(DEMO_PASSWORD)
                # Demo accounts skip the forced reset; the point is to sign in
                # and look at the dashboard, not to run a password ceremony.
                person.must_change_password = False

        session.commit()

        people = list(session.scalars(
            select(Employee).where(Employee.password_hash.is_not(None))
            .order_by(Employee.region, Employee.role, Employee.name)
        ))
        print(f"{created} new people. {len(people)} demo logins "
              f"(password for all: {DEMO_PASSWORD})\n")
        print(f"{'Region':<17} {'Role':<10} {'Name':<22} Email")
        print("-" * 92)
        for person in people:
            print(f"{person.region:<17} {person.role:<10} {person.name:<22} {person.email}")


if __name__ == "__main__":
    main()
