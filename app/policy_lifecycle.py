"""The yearly policy term — publish, expire, remind.

`app/policy_admin.py` already knows how to open next year's policy set without
destroying last year's history. What it did not have was a **term**: rolled
rows were left with `effective_to = NULL`, meaning "in force forever". The
review asked for the real-world rule instead:

    A policy is valid for one year. In a given year the new policy applies and
    the previous year's does not. HR decides, every year, whether to publish a
    new one or carry the existing numbers forward.

## Why an explicit end date, and what it costs

Giving a policy row a hard `effective_to` is what makes "the previous year's
policy won't apply" true in the database rather than merely true in a
paragraph. `resolve_policy()` filters on the effective window, so once
2026-03-31 passes, the 2025 row genuinely stops resolving.

The cost is real and worth stating: if HR never publishes 2026, then on
2026-04-01 `resolve_policy()` returns `None` and leave stops working. That is
not a bug I have hidden — it is the consequence the rule demands, and the
alternative (silently extending an expired policy) is how organisations end up
accruing against numbers nobody approved.

So the module makes the cliff impossible to walk off unnoticed:

  * `policy_year_status()` reports, per region, when the term ends and how
    many days are left.
  * `remind_expiring_policies()` writes an in-app notification to every HR
    admin and director once a region enters the reminder window (90 days by
    default), and again inside 30 and 7 days. It is idempotent per band, so a
    daily job does not produce a daily nag.
  * `publish_policy_year()` is the single call HR's screen makes. It rolls the
    set forward — with changes or without — stamps the one-year term, and
    tells the region.

## Carry forward is still a decision

Publishing with no changes writes a NEW row with the same numbers. "Nobody
looked at 2026" and "somebody looked at 2026 and decided it should match 2025"
are different facts, and only the second one is defensible in a dispute.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee, OrgPolicy
from app.notifications import KIND_POLICY, notify_policy_published, notify_role
from app.policy_admin import (
    PolicyAdminError,
    leave_year_bounds,
    policy_set,
    preview_roll_forward,
    roll_forward_year,
)

log = logging.getLogger("leave_engine.policy_lifecycle")

__all__ = [
    "publish_policy_year",
    "policy_year_status",
    "remind_expiring_policies",
    "PolicyYearStatus",
    "REMINDER_BANDS",
]

# Days-remaining thresholds that each trigger one reminder. Descending, so the
# first band a region falls into is the most urgent one not yet sent.
REMINDER_BANDS = (90, 30, 7)


@dataclass
class PolicyYearStatus:
    """Where a region stands in its policy cycle."""

    region: str
    policy_year: int | None
    term_start: dt.date | None
    term_end: dt.date | None
    days_remaining: int | None
    is_expired: bool
    next_year_open: bool
    leave_type_ids: list[str] = field(default_factory=list)
    #: The year HR would roll FROM if they published today. For a termed
    #: policy that is `policy_year`; for the open-ended baseline there is no
    #: stated year, so it is the leave year currently running.
    suggested_from_year: int | None = None
    #: The region's leave-year end ("03-31"), needed to establish a term on a
    #: baseline that never had one.
    leave_year_end: str | None = None

    #: True when rows are in force but have no stated year or end date — the
    #: original seeded baseline, which predates the yearly cycle.
    open_ended: bool = False

    @property
    def state(self) -> str:
        # "Unconfigured" means NOTHING is in force. A set of rows that is in
        # force but carries no stated `policy_year` is a termed baseline, not
        # an absent policy — reporting it as unconfigured put a red "No policy
        # configured for this region" banner directly above a live term with
        # 224 days left on it.
        if not self.leave_type_ids:
            return "unconfigured"
        if self.open_ended:
            return "open_ended"
        if self.is_expired:
            return "expired"
        if self.next_year_open:
            return "renewed"
        if self.days_remaining is not None and self.days_remaining <= REMINDER_BANDS[0]:
            return "renewal_due"
        return "active"

    @property
    def headline(self) -> str:
        if self.state == "unconfigured":
            return "No policy configured for this region."
        if self.open_ended:
            return (
                f"{len(self.leave_type_ids)} policies are in force with no end "
                "date — the baseline set, never put on a yearly cycle. Publish "
                "a policy year to start one."
            )
        if self.is_expired:
            which = f"The {self.policy_year} policy" if self.policy_year else "The policy"
            return (
                f"{which} expired on {self.term_end:%d %b %Y}. Leave cannot be "
                "calculated until a new year is published."
            )
        # The year may be unnamed on a baseline set, so every sentence below
        # has to read without it.
        named = f"The {self.policy_year} policy" if self.policy_year else "The current policy"
        nxt = f"{self.policy_year + 1}" if self.policy_year else "the next year"
        if self.next_year_open:
            return f"{nxt} is already published. Nothing to do."
        if self.days_remaining is not None and self.days_remaining <= REMINDER_BANDS[0]:
            return (
                f"{named} ends in {self.days_remaining} days "
                f"({self.term_end:%d %b %Y}). Publish {nxt} to keep leave running."
            )
        return (
            f"{named} runs to {self.term_end:%d %b %Y} "
            f"({self.days_remaining} days left)."
        )

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "policy_year": self.policy_year,
            "term_start": self.term_start.isoformat() if self.term_start else None,
            "term_end": self.term_end.isoformat() if self.term_end else None,
            "days_remaining": self.days_remaining,
            "is_expired": self.is_expired,
            "next_year_open": self.next_year_open,
            "state": self.state,
            "open_ended": self.open_ended,
            "headline": self.headline,
            "suggested_from_year": self.suggested_from_year,
            "leave_year_end": self.leave_year_end,
            "leave_type_ids": self.leave_type_ids,
        }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _current_rows(session: Session, region: str, as_of: dt.date) -> list[OrgPolicy]:
    return list(session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == region,
            OrgPolicy.effective_from <= as_of,
            (OrgPolicy.effective_to.is_(None)) | (OrgPolicy.effective_to >= as_of),
        ).order_by(OrgPolicy.leave_type_id)
    ))


def policy_year_status(
    session: Session, region: str, as_of: dt.date | None = None
) -> PolicyYearStatus:
    as_of = as_of or dt.date.today()
    rows = _current_rows(session, region, as_of)

    if not rows:
        # Nothing in force today. Was there something that has now lapsed?
        last = session.scalars(
            select(OrgPolicy).where(OrgPolicy.region == region)
            .order_by(OrgPolicy.effective_from.desc()).limit(1)
        ).first()
        if last is None:
            return PolicyYearStatus(region, None, None, None, None, False, False)
        return PolicyYearStatus(
            region=region,
            policy_year=last.policy_year,
            term_start=last.effective_from,
            term_end=last.effective_to,
            days_remaining=0,
            is_expired=True,
            next_year_open=False,
            leave_type_ids=[],
        )

    term_start = max(r.effective_from for r in rows)
    ends = [r.effective_to for r in rows if r.effective_to is not None]
    # The soonest end date is when cover starts breaking, even if other rows
    # run longer — a region without an EL policy is not a working region.
    term_end = min(ends) if len(ends) == len(rows) else None
    year = next((r.policy_year for r in rows if r.policy_year is not None), None)
    year_end = _region_leave_year_end(region, rows)

    next_open = session.scalars(
        select(OrgPolicy).where(
            OrgPolicy.region == region, OrgPolicy.effective_from > term_start
        ).limit(1)
    ).first() is not None

    return PolicyYearStatus(
        region=region,
        policy_year=year,
        term_start=term_start,
        term_end=term_end,
        days_remaining=(term_end - as_of).days if term_end else None,
        is_expired=False,
        next_year_open=next_open,
        leave_type_ids=sorted({r.leave_type_id for r in rows}),
        open_ended=(term_end is None and year is None),
        suggested_from_year=year if year is not None else _running_year(year_end, as_of),
        leave_year_end=year_end,
    )


def _region_leave_year_end(region: str, rows: list[OrgPolicy]) -> str | None:
    """The region's leave-year end, from the policy rows or the seeded default."""
    stated = next((r.leave_year_end for r in rows if r.leave_year_end), None)
    if stated:
        return stated
    from app.seed import REGION_LEAVE_YEAR_END

    return REGION_LEAVE_YEAR_END.get(region)


def _running_year(leave_year_end: str | None, as_of: dt.date) -> int | None:
    """Which leave year `as_of` currently falls in, by that region's calendar.

    With a 03-31 year end, 2026-02-14 is still leave year 2025 — the year that
    began on 2025-04-01. Getting this wrong would have HR publish a year that
    has already started.
    """
    if not leave_year_end:
        return None
    month, day = (int(p) for p in leave_year_end.split("-"))
    if (month, day) == (12, 31):
        return as_of.year
    return as_of.year if as_of > dt.date(as_of.year, month, day) else as_of.year - 1


def all_region_status(
    session: Session, as_of: dt.date | None = None
) -> list[PolicyYearStatus]:
    regions = sorted(session.scalars(select(OrgPolicy.region).distinct()))
    return [policy_year_status(session, r, as_of) for r in regions]


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def publish_policy_year(
    session: Session,
    region: str,
    from_year: int,
    changes: dict | None = None,
    *,
    actor: Employee,
    change_reason: str,
    default_leave_year_end: str | None = None,
    term_years: int = 1,
    announce: bool = True,
    commit: bool = True,
) -> list[OrgPolicy]:
    """Open, term-limit and announce next year's policy set.

    This is `roll_forward_year()` plus the two things the review asked for:
    a one-year expiry on the rows it creates, and a notification to the people
    the change affects.
    """
    created = roll_forward_year(
        session, region, from_year, changes,
        actor=actor, change_reason=change_reason,
        default_leave_year_end=default_leave_year_end,
        commit=False,
    )
    if not created:
        raise PolicyAdminError(
            f"No policy rows exist for {region!r} in {from_year} to carry forward."
        )

    new_year = created[0].policy_year
    # The term ends where the leave year ends — the same boundary accrual and
    # carry-over already use, so a policy never straddles two leave years.
    year_end = created[0].leave_year_end or default_leave_year_end
    _, term_end = leave_year_bounds(year_end, new_year + (term_years - 1))
    for row in created:
        row.effective_to = term_end

    session.flush()

    if announce:
        notify_policy_published(
            session, region, new_year, actor, _summary(created, changes),
        )
        notify_role(
            session, "hr_admin", KIND_POLICY,
            f"{region} {new_year} policy published",
            f"{len(created)} policy rows, in force until {term_end:%d %b %Y}.",
            "/hr/policies", exclude_id=actor.id,
        )

    log.info(
        "published %s policy year %s (%d rows) in force to %s",
        region, new_year, len(created), term_end,
    )
    if commit:
        session.commit()
    return created


def _summary(created: list[OrgPolicy], changes: dict | None) -> str:
    if not changes:
        return "The existing entitlements carry forward unchanged."
    parts = [
        f"{row.leave_type_id} {Decimal(str(row.entitlement_days_per_year)):g} days"
        for row in created
    ]
    return "New entitlements: " + ", ".join(parts) + "."


def preview_publish(
    session: Session, region: str, from_year: int, changes: dict | None = None,
    *, default_leave_year_end: str | None = None,
) -> dict:
    """What publishing would do, with the resulting term, before committing."""
    plan = preview_roll_forward(
        session, region, from_year, changes,
        default_leave_year_end=default_leave_year_end,
    )
    return {
        "region": region,
        "from_year": from_year,
        "to_year": plan.to_year,
        "term_start": plan.new_year_start.isoformat(),
        "term_end": plan.new_year_end.isoformat(),
        "changed_count": plan.changed_count,
        "warnings": plan.warnings,
        "changes": [
            {
                "policy_id": c.policy_id,
                "leave_type_id": c.leave_type_id,
                "tenure": c.tenure_label,
                "changed": {k: [str(o), str(n)] for k, (o, n) in c.changed.items()},
                "summary": c.describe(),
            }
            for c in plan.changes
        ],
    }


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
def _band_for(days_remaining: int) -> int | None:
    for band in sorted(REMINDER_BANDS):
        if days_remaining <= band:
            return band
    return None


def remind_expiring_policies(
    session: Session, as_of: dt.date | None = None, commit: bool = False
) -> list[str]:
    """Nudge HR before a region's policy term runs out. Idempotent per band.

    Run this from the same scheduled job that escalates overdue approvals.
    Sending at 90, 30 and 7 days rather than daily is the difference between a
    reminder and noise that gets filtered.
    """
    as_of = as_of or dt.date.today()
    sent: list[str] = []

    for status in all_region_status(session, as_of):
        if status.next_year_open or status.days_remaining is None:
            continue
        if status.is_expired:
            title = f"ACTION REQUIRED — {status.region} leave policy has expired"
            marker = f"[{status.region}:expired]"
        else:
            band = _band_for(status.days_remaining)
            if band is None:
                continue
            title = (
                f"{status.region} leave policy expires in "
                f"{status.days_remaining} days"
            )
            marker = f"[{status.region}:{status.policy_year}:{band}]"

        if _already_sent(session, marker):
            continue

        for role in ("hr_admin", "director"):
            notify_role(
                session, role, KIND_POLICY, title,
                f"{status.headline} {marker}", "/hr/policies",
            )
        sent.append(marker)

    if commit:
        session.commit()
    return sent


def _already_sent(session: Session, marker: str) -> bool:
    from app.models import Notification

    return session.scalars(
        select(Notification).where(
            Notification.kind == KIND_POLICY,
            Notification.body.like(f"%{marker}%"),
        ).limit(1)
    ).first() is not None
