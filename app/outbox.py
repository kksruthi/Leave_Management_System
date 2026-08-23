"""Transactional outbox — reliable events without a message broker.

Closes findings 39 (the final-approval hook can act before the transaction
commits) and 40 (the event mechanism is process-local and in-memory).

## What was wrong

Module 6 fired registered Python callbacks inside `record_approval_decision`,
before the caller committed. Two defects, both real:

  1. **Ordering.** A listener that wrote to the ledger, called payroll or sent
     an email ran while the approval was still uncommitted. If the surrounding
     transaction then rolled back — a constraint violation, a deploy, a lost
     connection — the approval vanished but its side effects did not. Payroll
     had been told about a leave that never happened.

  2. **Reach.** The callback list lived in one Python process's memory. A
     second web worker, a cron process, or anything not in that interpreter
     never saw the event. Restarting the process lost every registration.

## What replaces it

Events are INSERTed into `outbox` in the same transaction as the state change.
They commit together or not at all — problem 1 is gone by construction. A
separate relay then reads unpublished rows and delivers them, so problem 2
becomes a deployment concern with a durable queue behind it rather than a
process-local list.

The relay is deliberately NOT implemented here. What it publishes to — Kafka,
SQS, a webhook, a Celery task — is a deployment decision, and guessing would
be worse than leaving a clean seam. `claim_unpublished()` and
`mark_published()` are the interface it needs; `run_relay()` shows the loop.

## Delivery semantics

**At-least-once, not exactly-once.** A relay can publish and then crash before
marking the row, so consumers must be idempotent. `aggregate_type` +
`aggregate_id` are on every event to make that easy: a consumer that has
already processed `leave_request 42` can drop the duplicate.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutboxEvent

log = logging.getLogger("leave_engine.outbox")

__all__ = [
    "emit",
    "claim_unpublished",
    "mark_published",
    "mark_failed",
    "run_relay",
    "pending_count",
    "TOPIC_REQUEST_APPROVED",
    "TOPIC_REQUEST_REJECTED",
    "TOPIC_STEP_ESCALATED",
    "TOPIC_LEAVE_DEDUCTED",
]

TOPIC_REQUEST_APPROVED = "leave_request.approved"
TOPIC_REQUEST_REJECTED = "leave_request.rejected"
TOPIC_STEP_ESCALATED = "approval_step.escalated"
TOPIC_LEAVE_DEDUCTED = "leave_ledger.deducted"


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def emit(
    session: Session,
    topic: str,
    aggregate_type: str,
    aggregate_id: int,
    payload: dict,
) -> OutboxEvent:
    """Record an event in the caller's transaction.

    Deliberately does NOT commit. The whole point is that this row commits
    with the state change it describes — committing here would reintroduce
    the ordering bug it exists to fix.
    """
    event = OutboxEvent(
        topic=topic,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=json.dumps(payload, default=_json_default),
    )
    session.add(event)
    session.flush()
    log.info("outbox: %s for %s %s", topic, aggregate_type, aggregate_id)
    return event


def pending_count(session: Session) -> int:
    return len(list(session.scalars(
        select(OutboxEvent.id).where(OutboxEvent.published_at.is_(None))
    )))


def claim_unpublished(session: Session, limit: int = 100) -> list[OutboxEvent]:
    """Lock and return unpublished events, oldest first.

    `FOR UPDATE SKIP LOCKED` lets several relay instances run concurrently
    without handing the same event to two of them.
    """
    return list(session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ))


def mark_published(session: Session, event: OutboxEvent) -> None:
    event.published_at = dt.datetime.now(dt.timezone.utc)
    event.attempts += 1


def mark_failed(session: Session, event: OutboxEvent, error: str) -> None:
    """Leave it unpublished so the next pass retries it."""
    event.attempts += 1
    event.last_error = error[:2000]


def run_relay(
    session: Session,
    publish: Callable[[str, dict], None],
    *,
    limit: int = 100,
    commit: bool = True,
) -> tuple[int, int]:
    """One pass of the relay. Returns (published, failed).

    `publish(topic, payload)` is whatever the deployment uses. A real
    deployment runs this on a short timer, or after each transaction, in its
    own process.
    """
    published = failed = 0
    for event in claim_unpublished(session, limit):
        try:
            publish(event.topic, json.loads(event.payload))
        except Exception as exc:  # noqa: BLE001 - relay must not die on one bad event
            log.warning("outbox: publish failed for event %s: %s", event.id, exc)
            mark_failed(session, event, str(exc))
            failed += 1
        else:
            mark_published(session, event)
            published += 1

    if commit:
        session.commit()
    return published, failed


def events_for(
    session: Session, aggregate_type: str, aggregate_id: int
) -> Iterable[OutboxEvent]:
    return session.scalars(
        select(OutboxEvent)
        .where(
            OutboxEvent.aggregate_type == aggregate_type,
            OutboxEvent.aggregate_id == aggregate_id,
        )
        .order_by(OutboxEvent.id)
    )
