"""Approval copilot — the context a manager would otherwise have to go and find.

A manager looking at a leave request can see the dates, the days and the
balance. What they actually need to decide is none of those things: it is
**who else is off that week**. Today they find that by opening the team
calendar in another tab, counting, and hoping they did not miss a pending
request. Most of the time they skip it and approve.

So this assembles the same count for them and says what it implies.

## Deliberately rules-based, not a model

Every number below is a query, and every sentence is generated from a number.
That is not a limitation to be apologised for — it is the requirement. A
manager who is told "risky" must be able to ask *why*, get "3 of your 5
people are already off on 14–15 September", and check it. An opinion nobody
can audit is worse than no opinion, because it still gets followed.

## What it looks at

  1. **Peak overlap** — the largest number of teammates off on any single day
     of the request, counting approved AND pending leave. Pending counts
     because two managers approving in sequence is exactly how a team ends up
     empty.
  2. **Coverage** — that peak as a share of the team, which is what turns
     "2 people" into "2 of 3" or "2 of 12".
  3. **Cost** — unpaid days, and whether the request over-draws the balance.
  4. **Notice** — how much warning the team got.
  5. **Fairness** — how much leave this person has taken this year against the
     team median, so "they are always off" can be checked rather than felt.

## The verdict

`safe` / `check` / `risky`, from the coverage thresholds below. It is a
recommendation on **cover**, and it says so — it is not a judgement about
whether the leave is deserved, and it never blocks anything. The manager
still decides; the point is that they decide having seen the week.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.dashboard import get_days_taken, get_live_balance
from app.models import Employee, LeaveRequest

__all__ = ["advise", "TEAM_COVERAGE_THRESHOLDS"]

#: Share of the team that may be away before the copilot starts objecting.
#:
#: These are deliberately visible constants rather than a formula, because the
#: right numbers are an organisational choice and someone will want to change
#: them. They are set where they are because 2 of 5 away — 40%, three people
#: still working — is an ordinary week, and a tool that calls an ordinary week
#: risky gets ignored within a fortnight. Half the team away is the first
#: point worth a second look.
TEAM_COVERAGE_THRESHOLDS = {"check": Decimal("0.50"), "risky": Decimal("0.66")}

#: However thin the percentage, being down to this many people is thin cover
#: in absolute terms. In a team of three, one person left IS the problem, and
#: 66% would not have caught it in a team of two.
MIN_COMFORTABLE_REMAINING = 1

#: Below this many people, percentages stop meaning anything and the absolute
#: count is what matters — in a team of two, one person off is 50%.
_SMALL_TEAM = 4


def _teammates(session: Session, employee: Employee) -> list[Employee]:
    """Who this person's absence actually affects.

    Peers under the same manager, or — for someone with no manager — the rest
    of their department in their region. Not the whole company: a Chennai
    designer being off has nothing to do with cover in Texas sales.
    """
    stmt = select(Employee).where(
        Employee.id != employee.id, Employee.status == "active"
    )
    if employee.manager_id is not None:
        stmt = stmt.where(Employee.manager_id == employee.manager_id)
    elif employee.department:
        stmt = stmt.where(
            Employee.department == employee.department,
            Employee.region == employee.region,
        )
    else:
        return []
    return list(session.scalars(stmt.order_by(Employee.name)))


def _overlapping(
    session: Session, ids: list[int], start: dt.date, end: dt.date
) -> list[LeaveRequest]:
    """Approved or pending leave for these people that touches these dates."""
    if not ids:
        return []
    return list(session.scalars(
        select(LeaveRequest).where(
            LeaveRequest.employee_id.in_(ids),
            LeaveRequest.status.in_(("approved", "pending")),
            LeaveRequest.start_date <= end,
            LeaveRequest.end_date >= start,
        ).order_by(LeaveRequest.start_date)
    ))


def _daily_overlap(
    requests: list[LeaveRequest], start: dt.date, end: dt.date
) -> list[dict]:
    """Per-day head count of who is away, across the requested range."""
    out = []
    day = start
    while day <= end:
        away = [r for r in requests if r.start_date <= day <= r.end_date]
        out.append({
            "date": day.isoformat(),
            "count": len(away),
            "employee_ids": [r.employee_id for r in away],
        })
        day += dt.timedelta(days=1)
    return out


def advise(session: Session, request: LeaveRequest) -> dict:
    """Everything the manager needs to decide, and what it adds up to."""
    employee = session.get(Employee, request.employee_id)
    if employee is None:
        return {"verdict": "check", "headline": "Employee not found.", "signals": []}

    team = _teammates(session, employee)
    team_size = len(team) + 1                      # including the requester
    by_id = {p.id: p for p in team}

    overlaps = _overlapping(
        session, list(by_id), request.start_date, request.end_date
    )
    daily = _daily_overlap(overlaps, request.start_date, request.end_date)
    peak = max((d["count"] for d in daily), default=0)
    peak_day = next((d for d in daily if d["count"] == peak), None)

    # Peak INCLUDING this request, because that is the state being decided.
    peak_with = peak + 1
    coverage = Decimal(peak_with) / Decimal(team_size) if team_size else Decimal(0)

    signals: list[dict] = []

    # --- 1. who else is off ------------------------------------------------
    if peak == 0:
        signals.append({
            "tone": "good",
            "label": "Nobody else is off",
            "detail": f"No approved or pending leave in {employee.name}'s team "
                      f"touches {_span(request)}.",
        })
    else:
        names = ", ".join(
            sorted({by_id[i].name for d in daily for i in d["employee_ids"] if i in by_id})
        )
        tone = "warning" if coverage >= TEAM_COVERAGE_THRESHOLDS["check"] else "info"
        verb = "overlaps" if len(set(
            i for d in daily for i in d["employee_ids"] if i in by_id
        )) == 1 else "overlap"
        signals.append({
            "tone": tone,
            "label": f"{peak} already off at the busiest point",
            "detail": (
                f"{names} {verb} these dates. On "
                f"{_pretty(peak_day['date'])} that would be {peak_with} of "
                f"{team_size} away — {int(coverage * 100)}% of the team."
            ),
        })

    # --- 2. cover ----------------------------------------------------------
    remaining = team_size - peak_with
    if team_size <= _SMALL_TEAM:
        signals.append({
            "tone": "info",
            "label": f"Small team ({team_size} people)",
            "detail": (
                f"{remaining} would still be working. In a team this size the "
                "count matters more than the percentage."
            ),
        })

    # --- 3. cost -----------------------------------------------------------
    unpaid = Decimal(request.unpaid_days or 0)
    if unpaid > 0:
        signals.append({
            "tone": "warning",
            "label": f"{unpaid:g} day(s) would be unpaid",
            "detail": "Their balance does not cover the whole request. Approving "
                      "it means agreeing to loss of pay — worth a conversation "
                      "before, not after.",
        })

    balance = get_live_balance(session, employee.id, request.leave_type_id)
    after = balance - Decimal(request.paid_days or 0)
    if after <= 0 and unpaid == 0:
        signals.append({
            "tone": "warning",
            "label": "This uses their whole balance",
            "detail": f"{employee.name} would have {after:g} days of "
                      f"{request.leave_type_id} left for the rest of the year.",
        })

    # --- 4. notice ---------------------------------------------------------
    if request.submitted_at is not None:
        notice = (request.start_date - request.submitted_at.date()).days
        if notice < 3:
            signals.append({
                "tone": "warning",
                "label": f"Short notice — {notice} day(s)",
                "detail": "The team has little time to arrange cover.",
            })
        elif notice >= 21:
            signals.append({
                "tone": "good",
                "label": f"Well-flagged — {notice} days' notice",
                "detail": "Plenty of warning to arrange cover.",
            })

    # --- 5. fairness -------------------------------------------------------
    taken = get_days_taken(session, employee.id, request.leave_type_id)
    peers = sorted(
        get_days_taken(session, p.id, request.leave_type_id) for p in team
    )
    if peers:
        median = peers[len(peers) // 2]
        if taken > median * 2 and taken - median >= 3:
            signals.append({
                "tone": "info",
                "label": "Above the team's usual",
                "detail": f"{employee.name} has taken {taken:g} days of "
                          f"{request.leave_type_id} this year; the team median "
                          f"is {median:g}. Context, not an objection.",
            })
        elif taken == 0 and median >= 3:
            signals.append({
                "tone": "good",
                "label": "Has taken none this year",
                "detail": f"The team median is {median:g} days. Declining this "
                          "would leave them well behind their colleagues.",
            })

    # --- verdict -----------------------------------------------------------
    thin = (
        coverage >= TEAM_COVERAGE_THRESHOLDS["risky"]
        or (team_size > 1 and remaining <= MIN_COMFORTABLE_REMAINING)
    )
    if peak == 0:
        verdict, headline = "safe", (
            f"Safe to approve — nobody else in the team is off {_span(request)}."
        )
    elif thin:
        verdict, headline = "risky", (
            f"Thin cover — {peak_with} of {team_size} would be away on "
            f"{_pretty(peak_day['date'])}, leaving "
            f"{remaining if remaining else 'nobody'}. Worth asking if the "
            "dates can move."
        )
    elif coverage >= TEAM_COVERAGE_THRESHOLDS["check"]:
        verdict, headline = "check", (
            f"Check the cover — {peak_with} of {team_size} away at the peak, "
            f"leaving {remaining} working."
        )
    else:
        verdict, headline = "safe", (
            f"Cover looks fine — {peak_with} of {team_size} away at most, "
            f"leaving {remaining} working."
        )

    # A warning about cost or notice bumps a cover-safe request to "check" —
    # and the headline has to move with it, or the panel says "safe" above a
    # verdict that says otherwise.
    warnings = [s for s in signals if s["tone"] == "warning"]
    if warnings and verdict == "safe":
        verdict = "check"
        headline = f"Cover is fine, but: {warnings[0]['label'].lower()}."

    return {
        "verdict": verdict,
        "headline": headline,
        "team_size": team_size,
        "peak_away": peak_with,
        "remaining": remaining,
        "coverage_pct": int(coverage * 100),
        "signals": signals,
        "daily": daily,
        "overlapping": [
            {
                "employee": by_id[r.employee_id].name,
                "leave_type_id": r.leave_type_id,
                "start_date": r.start_date.isoformat(),
                "end_date": r.end_date.isoformat(),
                "status": r.status,
            }
            for r in overlaps if r.employee_id in by_id
        ],
        "basis": "Counts approved AND pending leave for this person's team. "
                 "Advice about cover only — it is not a judgement on the "
                 "request, and it never blocks a decision.",
    }


def _span(request: LeaveRequest) -> str:
    if request.start_date == request.end_date:
        return request.start_date.strftime("%d %b")
    return f"{request.start_date:%d %b}–{request.end_date:%d %b}"


def _pretty(iso: str) -> str:
    return dt.date.fromisoformat(iso).strftime("%a %d %b")
