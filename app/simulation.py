"""A "show me" layer over the engine.

The engine is dynamic in ways a static dashboard cannot show. Entitlement
changes when tenure crosses a bracket, when a region changes, when HR
publishes a new policy year; balances change when a month accrues, when a year
ends, when leave is approved. All of it is real, and all of it is invisible if
you open the app on one Tuesday and look at one number.

This module exists so the UI can *perform* those changes and show the
difference. Every action here is a thin wrapper around the same function the
scheduled job or the HR screen would call — nothing is faked, nothing is a
special "demo mode" code path. The only thing this adds is a **before and
after snapshot** either side of the call, so the change has somewhere to
appear.

    snapshot(session, employee)   -> what the engine says right now
    run_action(session, ...)      -> before, do the real thing, after, diff

Because these write real rows, `run_action` is HR-only at the API layer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accrual import run_annual_grant, run_monthly_accrual, run_year_end_carryover
from app.dashboard import (
    get_balance_buckets,
    get_days_taken,
    get_live_balance,
    leave_year_start,
)
from app.models import Employee, LeaveLedger
from app.policy_admin import PolicyAdminError
from app.policy_engine import resolve_policy, years_between
from app.policy_lifecycle import policy_year_status, publish_policy_year
from app.region_transfer import relocate_employee

__all__ = ["ACTIONS", "snapshot", "run_action", "timeline", "SimulationError"]


class SimulationError(ValueError):
    """The requested simulation cannot be run in the current state."""


#: What the Simulation screen offers, in the order it offers it.
ACTIONS = [
    {
        "key": "accrue_month",
        "label": "Run one month's accrual",
        "detail": "Runs the real monthly job for the next un-accrued month. "
                  "EL goes up by annual ÷ 12 — pro-rated if they joined or "
                  "left partway through it.",
        "shows": "Accrual is a job, not a stored number.",
    },
    {
        "key": "cross_tenure",
        "label": "Show the tenure brackets",
        "detail": "Resolves this person's entitlement on the day before and "
                  "the day after each tenure boundary. Nothing is edited — "
                  "the bracket is a function of the date.",
        "shows": "Entitlement changes itself as people stay.",
    },
    {
        "key": "transfer_region",
        "label": "Transfer to the other region",
        "detail": "Moves them between Tamil Nadu and Texas and reconciles the "
                  "entitlement difference into the ledger, with a reason.",
        "shows": "Policy is a function of region, resolved live.",
    },
    {
        "key": "year_end",
        "label": "Run year end (carry-over)",
        "detail": "Closes this region's leave year: up to 18 days move into a "
                  "separate carry-over bucket expiring three months later, "
                  "and the excess is forfeited on the record.",
        "shows": "Carry-over is capped, bucketed and perishable.",
    },
    {
        "key": "publish_policy",
        "label": "Publish next policy year",
        "detail": "Writes a NEW version of every policy row for the next year "
                  "and closes the old one. Past ledger entries still resolve "
                  "to the old numbers.",
        "shows": "Policy is versioned, never overwritten.",
    },
]

_OTHER_REGION = {
    "India-TamilNadu": "USA-Texas",
    "USA-Texas": "India-TamilNadu",
}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def _types(session: Session, employee: Employee) -> list[str]:
    return sorted(session.scalars(
        select(LeaveLedger.leave_type_id)
        .where(LeaveLedger.employee_id == employee.id).distinct()
    ).all()) or ["EL"]


def snapshot(
    session: Session, employee: Employee, as_of: dt.date | None = None
) -> dict:
    """What the engine currently says about one person. Read-only."""
    as_of = as_of or dt.date.today()
    rows = []
    from app.accrual import candidate_leave_types

    for leave_type_id in candidate_leave_types(session, employee):
        policy = resolve_policy(session, employee, leave_type_id, as_of)
        buckets = get_balance_buckets(session, employee.id, leave_type_id, as_of)
        rows.append({
            "leave_type_id": leave_type_id,
            "entitlement": str(policy.entitlement_days_per_year) if policy else None,
            "accrual_method": policy.accrual_method if policy else None,
            "current": str(buckets["current"]),
            "carryover": str(buckets["carryover"]),
            "balance": str(get_live_balance(session, employee.id, leave_type_id, as_of)),
            "taken": str(get_days_taken(session, employee.id, leave_type_id, as_of)),
        })

    return {
        "employee_id": employee.id,
        "name": employee.name,
        "region": employee.region,
        "tenure_years": years_between(employee.join_date, as_of),
        "leave_year_start": leave_year_start(employee, as_of).isoformat(),
        "as_of": as_of.isoformat(),
        "policy_year": policy_year_status(session, employee.region, as_of).policy_year,
        "types": rows,
    }


def _diff(before: dict, after: dict) -> list[dict]:
    """Only what actually moved — a diff of zero changes is a wasted screen."""
    out = []
    if before["region"] != after["region"]:
        out.append({"what": "Region", "from": before["region"], "to": after["region"]})
    if before["policy_year"] != after["policy_year"]:
        out.append({"what": "Policy year", "from": str(before["policy_year"]),
                    "to": str(after["policy_year"])})

    by_type = {r["leave_type_id"]: r for r in before["types"]}
    for row in after["types"]:
        old = by_type.get(row["leave_type_id"])
        if old is None:
            continue
        for field, label in (("entitlement", "entitlement/yr"),
                             ("current", "current-year balance"),
                             ("carryover", "carry-over"),
                             ("balance", "available")):
            if old[field] != row[field]:
                out.append({
                    "what": f"{row['leave_type_id']} {label}",
                    "from": old[field], "to": row[field],
                })
    return out


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------
def timeline(session: Session, employee: Employee, limit: int = 40) -> list[dict]:
    """Every ledger movement, newest first, in plain language.

    This is the audit trail the review asked for, rendered for a human: each
    row says what happened, when it took effect, and what it did to the
    balance.
    """
    rows = list(session.scalars(
        select(LeaveLedger)
        .where(LeaveLedger.employee_id == employee.id)
        .order_by(LeaveLedger.effective_date.desc(), LeaveLedger.id.desc())
        .limit(limit)
    ))
    return [
        {
            "id": r.id,
            "date": r.effective_date.isoformat(),
            "leave_type_id": r.leave_type_id,
            "amount": str(r.amount),
            "bucket": r.bucket,
            "expires_on": r.expires_on.isoformat() if r.expires_on else None,
            "reason": r.reason,
            "policy_snapshot_id": r.policy_snapshot_id,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def run_action(
    session: Session,
    action: str,
    employee: Employee,
    actor: Employee,
    as_of: dt.date | None = None,
) -> dict:
    """Run one simulation and report what changed."""
    as_of = as_of or dt.date.today()
    before = snapshot(session, employee, as_of)

    after_as_of = as_of
    if action == "accrue_month":
        note, after_as_of = _accrue_next_month(session, employee, as_of)
    elif action == "cross_tenure":
        # The only read-only action: it proves a point without writing.
        return {
            "action": action,
            "note": "Entitlement resolved on each side of every tenure "
                    "boundary. No rows were written — the number simply "
                    "depends on the date.",
            "viewed_at": as_of.isoformat(),
            "time_travelled": False,
            "before": before, "after": before, "changes": [],
            "brackets": _tenure_brackets(session, employee),
        }
    elif action == "transfer_region":
        note = _transfer(session, employee, as_of)
    elif action == "year_end":
        note, after_as_of = _year_end(session, employee, as_of)
    elif action == "publish_policy":
        note, after_as_of = _publish(session, employee, actor, as_of)
    else:
        raise SimulationError(f"Unknown simulation {action!r}.")

    session.commit()
    session.refresh(employee)
    after = snapshot(session, employee, after_as_of)
    return {
        "action": action,
        "note": note,
        # Some actions take effect on a future date — next month's accrual, a
        # year end, a policy year. The "after" is read on THAT date, because
        # reading it today would show a change that has not happened yet and
        # report nothing moved.
        "viewed_at": after_as_of.isoformat(),
        "time_travelled": after_as_of != as_of,
        "before": before,
        "after": after,
        "changes": _diff(before, after),
    }


def _accrue_next_month(
    session: Session, employee: Employee, as_of: dt.date
) -> tuple[str, dt.date]:
    """Accrue the first month after the latest accrual already on file."""
    last = session.scalar(
        select(LeaveLedger.effective_date)
        .where(
            LeaveLedger.employee_id == employee.id,
            LeaveLedger.reason == "monthly accrual",
        )
        .order_by(LeaveLedger.effective_date.desc())
        .limit(1)
    )
    start = (last or as_of) + dt.timedelta(days=1)
    run_date = _month_end(start)
    result = run_monthly_accrual(session, run_date, commit=False)
    mine = [e for e in result.written if e.employee_id == employee.id]
    if not mine:
        return (
            f"Ran the monthly job for {run_date:%b %Y}. Nothing was written "
            f"for {employee.name} — that month is already accrued, or they "
            "were not in service.",
            max(as_of, run_date),
        )
    detail = ", ".join(f"{e.leave_type_id} +{e.amount}" for e in mine)
    return (
        f"Monthly accrual for {run_date:%b %Y}: {detail}. The whole company "
        f"got {len(result.written)} rows in the same run.",
        max(as_of, run_date),
    )


def _month_end(d: dt.date) -> dt.date:
    nxt = dt.date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def _tenure_brackets(session: Session, employee: Employee) -> list[dict]:
    """Entitlement either side of each anniversary that matters."""
    out = []
    for years in (1, 3, 5):
        boundary = _anniversary(employee.join_date, years)
        before = resolve_policy(session, employee, "EL", boundary - dt.timedelta(days=1))
        after = resolve_policy(session, employee, "EL", boundary)
        if before is None or after is None:
            continue
        out.append({
            "years": years,
            "date": boundary.isoformat(),
            "reached": boundary <= dt.date.today(),
            "before": str(before.entitlement_days_per_year),
            "after": str(after.entitlement_days_per_year),
            "changed": before.entitlement_days_per_year != after.entitlement_days_per_year,
        })
    return out


def _anniversary(join_date: dt.date, years: int) -> dt.date:
    try:
        return join_date.replace(year=join_date.year + years)
    except ValueError:                      # 29 February
        return join_date.replace(year=join_date.year + years, day=28)


def _transfer(session: Session, employee: Employee, as_of: dt.date) -> str:
    new_region = _OTHER_REGION.get(employee.region)
    if new_region is None:
        raise SimulationError(f"No other region configured for {employee.region!r}.")
    old_region = employee.region

    result = relocate_employee(session, employee, new_region, as_of, commit=False)
    moved = [a for a in result["adjustments"] if Decimal(a["adjustment"]) != 0]
    kept = [a for a in result["adjustments"] if Decimal(a["adjustment"]) == 0]

    parts = [
        f"{employee.name} moved from {old_region} to {new_region} on {as_of}. "
        f"Their leave year changes from {result['leave_year_before']} to "
        f"{result['leave_year_after']}, so the carry-over deadline, the "
        "holiday calendar and the entitlement bracket all move with them."
    ]
    if moved:
        parts.append(
            "Adjusted: "
            + "; ".join(f"{a['leave_type_id']} {a['adjustment']}" for a in moved)
            + "."
        )
    if kept:
        parts.append(
            "No adjustment for "
            + ", ".join(a["leave_type_id"] for a in kept)
            + " — days already earned are never taken back."
        )
    return " ".join(parts)


def _year_end(
    session: Session, employee: Employee, as_of: dt.date
) -> tuple[str, dt.date]:
    """Close a leave year for this employee's region.

    Tries the year that has already closed first. If that one has been
    processed — which it will have been the second time anyone presses the
    button — it runs the year end that is COMING, and reports the result as
    of the day after, so the carry-over bucket is visible rather than a
    balance that has not been touched yet.
    """
    year_start = leave_year_start(employee, as_of)
    candidates = [
        year_start - dt.timedelta(days=1),              # the year just closed
        _next_year_end(employee, year_start),           # the one coming up
    ]
    for year_end in candidates:
        result = run_year_end_carryover(session, year_end, commit=False)
        mine = [e for e in result.written if e.employee_id == employee.id]
        if mine:
            detail = ", ".join(f"{e.leave_type_id} {e.amount} ({e.reason})" for e in mine)
            return (
                f"Year end {year_end}: {detail}. Carried days sit in their own "
                "bucket and expire three months into the new year.",
                max(as_of, year_end + dt.timedelta(days=1)),
            )
    return (
        f"Ran the year-end job for {candidates[0]} and {candidates[1]}. Nothing "
        f"moved for {employee.name} — both year ends are already processed, or "
        "there is no balance left to carry.",
        as_of,
    )


def _next_year_end(employee: Employee, year_start: dt.date) -> dt.date:
    """The end of the leave year currently running."""
    try:
        return year_start.replace(year=year_start.year + 1) - dt.timedelta(days=1)
    except ValueError:
        return year_start.replace(year=year_start.year + 1, day=28)


def _publish(
    session: Session, employee: Employee, actor: Employee, as_of: dt.date
) -> tuple[str, dt.date]:
    """Publish the next unopened policy year for this region.

    Pressing the button twice should not fail: the second press publishes the
    year after. It walks forward until it finds a year that is not already
    open, rather than refusing because somebody already did 2027.
    """
    status = policy_year_status(session, employee.region, as_of)
    from_year = status.suggested_from_year
    if from_year is None:
        raise SimulationError(
            f"{employee.region} has no policy year to roll forward from."
        )

    last_error = None
    for attempt in range(6):                 # six years is plenty for a demo
        try:
            created = publish_policy_year(
                session, employee.region, from_year + attempt, None,
                actor=actor,
                change_reason=(
                    f"Simulation: {from_year + attempt + 1} policy year published."
                ),
                default_leave_year_end=status.leave_year_end,
                commit=False,
            )
        except PolicyAdminError as exc:
            last_error = exc
            continue
        return (
            f"Published {created[0].policy_year} for {employee.region}: "
            f"{len(created)} new policy rows, in force until "
            f"{created[0].effective_to}. The {from_year + attempt} rows were "
            "closed, not edited — every past ledger entry still resolves to "
            "its own version.",
            created[0].effective_from,
        )
    raise SimulationError(str(last_error))
