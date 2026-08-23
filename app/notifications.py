"""In-app notifications.

Written in the SAME transaction as the event they describe, so a notification
can never survive a rolled-back approval. That is the opposite trade-off from
`app/outbox.py`, and deliberately so:

  * **outbox** — messages leaving the system (payroll, email, queues). Must
    survive a crash, so it is delivered at-least-once by a separate relay.
  * **notifications** — rows in this database, read by this UI. There is no
    delivery step to fail, so the simplest correct thing is to write them
    inline and let them commit or roll back with the state change.

Every helper takes a session and writes without committing; the caller owns
the transaction.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Employee, LeaveRequest, Notification

__all__ = [
    "notify",
    "notify_role",
    "unread_count",
    "inbox",
    "mark_read",
    "mark_all_read",
    "notify_request_submitted",
    "notify_request_decided",
    "notify_request_forwarded",
    "notify_policy_published",
    "KIND_APPROVAL_NEEDED",
    "KIND_REQUEST_DECIDED",
    "KIND_POLICY",
    "KIND_BALANCE",
]

KIND_APPROVAL_NEEDED = "approval_needed"
KIND_REQUEST_DECIDED = "request_decided"
KIND_POLICY = "policy"
KIND_BALANCE = "balance"


def notify(
    session: Session,
    employee_id: int,
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> Notification:
    row = Notification(
        employee_id=employee_id, kind=kind, title=title, body=body, link=link
    )
    session.add(row)
    return row


def notify_role(
    session: Session,
    role: str,
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    exclude_id: int | None = None,
) -> list[Notification]:
    """Notify every active holder of a role — used for policy announcements."""
    stmt = select(Employee).where(Employee.role == role, Employee.status == "active")
    if exclude_id is not None:
        stmt = stmt.where(Employee.id != exclude_id)
    return [
        notify(session, person.id, kind, title, body, link)
        for person in session.scalars(stmt)
    ]


def notify_everyone(
    session: Session, kind: str, title: str, body: str | None = None,
    link: str | None = None, region: str | None = None,
) -> list[Notification]:
    stmt = select(Employee).where(Employee.status == "active")
    if region:
        stmt = stmt.where(Employee.region == region)
    return [
        notify(session, person.id, kind, title, body, link)
        for person in session.scalars(stmt)
    ]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def unread_count(session: Session, employee_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.employee_id == employee_id, Notification.read_at.is_(None)
        )
    ) or 0


def inbox(session: Session, employee_id: int, limit: int = 40) -> list[Notification]:
    return list(session.scalars(
        select(Notification)
        .where(Notification.employee_id == employee_id)
        .order_by(Notification.id.desc())
        .limit(limit)
    ))


def mark_read(session: Session, employee_id: int, notification_id: int) -> None:
    row = session.get(Notification, notification_id)
    if row is not None and row.employee_id == employee_id and row.read_at is None:
        row.read_at = dt.datetime.now(dt.timezone.utc)


def mark_all_read(session: Session, employee_id: int) -> int:
    rows = list(session.scalars(
        select(Notification).where(
            Notification.employee_id == employee_id, Notification.read_at.is_(None)
        )
    ))
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        row.read_at = now
    return len(rows)


# ---------------------------------------------------------------------------
# The specific events
# ---------------------------------------------------------------------------
def _dates(request: LeaveRequest) -> str:
    if request.start_date == request.end_date:
        return request.start_date.strftime("%d %b")
    return f"{request.start_date:%d %b} – {request.end_date:%d %b}"


def notify_request_submitted(
    session: Session, request: LeaveRequest, approver: Employee | None
) -> None:
    if approver is None:
        return
    employee = session.get(Employee, request.employee_id)
    notify(
        session, approver.id, KIND_APPROVAL_NEEDED,
        f"{employee.name} requested {request.leave_type_id} leave",
        f"{_dates(request)} · {Decimal(request.duration_days):g} days. Awaiting your decision.",
        "/approvals",
    )


def notify_request_forwarded(
    session: Session, request: LeaveRequest, sender: Employee,
    recipient: Employee | None, note: str | None,
) -> None:
    employee = session.get(Employee, request.employee_id)
    if recipient is not None:
        notify(
            session, recipient.id, KIND_APPROVAL_NEEDED,
            f"{sender.name} forwarded {employee.name}'s leave request to you",
            (note or f"{_dates(request)} · {Decimal(request.duration_days):g} days.")
            + " Awaiting your decision.",
            "/approvals",
        )
    notify(
        session, request.employee_id, KIND_REQUEST_DECIDED,
        f"Your {request.leave_type_id} request moved to the next approver",
        f"{sender.name} reviewed it and sent it to "
        f"{recipient.name if recipient else 'the next approver'}.",
        "/requests",
    )


def notify_request_decided(
    session: Session, request: LeaveRequest, actor: Employee | None, decision: str,
    reason: str | None = None,
) -> None:
    verb = "approved" if decision == "approved" else "declined"
    body = f"{_dates(request)} · {Decimal(request.duration_days):g} days."
    if reason:
        body += f" “{reason}”"
    if decision == "approved":
        body += " The days have been deducted from your balance."
    notify(
        session, request.employee_id, KIND_REQUEST_DECIDED,
        f"Your {request.leave_type_id} request was {verb}"
        + (f" by {actor.name}" if actor else ""),
        body, "/requests",
    )


def notify_policy_published(
    session: Session, region: str, policy_year: int, actor: Employee,
    summary: str,
) -> None:
    """Tell a region its leave policy for next year is live.

    People plan leave around these numbers, so a change that lands silently is
    a change that generates disputes in six months.
    """
    notify_everyone(
        session, KIND_POLICY,
        f"Leave policy for {policy_year} published",
        f"{actor.name} published the {policy_year} leave policy for {region}. {summary}",
        "/", region=region,
    )
