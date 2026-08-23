"""Working-day calendar — weekends, public holidays and half days.

Closes findings 17 (public holidays not excluded) and 27 (no half-day leave).

## Where the holiday data comes from

The `holidays` PyPI package, not a hand-maintained table. It already knows
that Pongal is a Tamil Nadu holiday and Thanksgiving a US one, that Indian
Independence Day is 15 August, and how the movable feasts fall in each year.
Re-deriving that by hand would be a permanent source of quiet bugs, and every
year someone would forget to top the table up.

Region strings map onto the library's country + subdivision codes:

    "India-TamilNadu"  ->  holidays.India(subdiv="TN")
    "USA-Texas"        ->  holidays.UnitedStates(subdiv="TX")

Adding a region means adding one row to `REGION_CALENDARS` — still "insert
data, don't touch logic", the same claim the policy table makes.

## What the database is still for

`holiday_overrides` covers what a library cannot know: a company shutdown
between Christmas and New Year, a founding-day holiday, or a statutory day
this particular employer does not observe (`is_working_day = True` forces it
back to a working day).

## Half days

A request carries `start_half_day` / `end_half_day` flags. Each one that
falls on a working day subtracts 0.5 from the duration, so a Monday-morning
start through Friday is 4.5 days. Both flags on a single-day request means a
half day, not a whole one.
"""

from __future__ import annotations

import datetime as dt
import functools
from decimal import Decimal

import holidays as holidays_pkg
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HolidayOverride

__all__ = [
    "REGION_CALENDARS",
    "HALF_DAY",
    "is_weekend",
    "is_public_holiday",
    "holiday_name",
    "is_working_day",
    "working_days_between",
    "working_days_in",
    "holidays_in_range",
    "UnknownRegionError",
]

HALF_DAY = Decimal("0.5")

# region -> (holidays-package country class name, subdivision code)
REGION_CALENDARS: dict[str, tuple[str, str | None]] = {
    "India-TamilNadu": ("India", "TN"),
    "India-Karnataka": ("India", "KA"),
    "India-Maharashtra": ("India", "MH"),
    "USA-Texas": ("UnitedStates", "TX"),
    "USA-California": ("UnitedStates", "CA"),
}


class UnknownRegionError(LookupError):
    """No holiday calendar is configured for this region."""


@functools.lru_cache(maxsize=64)
def _calendar(region: str, year: int):
    """The statutory calendar for one region and year.

    Cached because the library rebuilds the year's rules on each construction
    and a duration calculation asks about the same year repeatedly. This is a
    cache of *immutable public holiday facts*, not of anyone's balance — the
    live-balance no-cache rule in Module 4 is untouched.
    """
    if region not in REGION_CALENDARS:
        raise UnknownRegionError(
            f"No holiday calendar configured for region {region!r}. "
            f"Add it to REGION_CALENDARS in app/holidays.py. "
            f"Known regions: {', '.join(sorted(REGION_CALENDARS))}."
        )
    country, subdiv = REGION_CALENDARS[region]
    return getattr(holidays_pkg, country)(subdiv=subdiv, years=[year])


def is_weekend(day: dt.date) -> bool:
    """Saturday or Sunday.

    Note this assumes a Mon-Fri week everywhere. Regions with a different
    working week (a Sunday-Thursday week, or alternate-Saturday working)
    would need a per-region pattern; none of the seeded regions do.
    """
    return day.weekday() >= 5


def is_public_holiday(day: dt.date, region: str, session: Session | None = None) -> bool:
    return holiday_name(day, region, session) is not None


def holiday_name(day: dt.date, region: str, session: Session | None = None) -> str | None:
    """The holiday's name, or None if it is an ordinary day.

    Company overrides win over the statutory calendar in both directions.
    """
    if session is not None:
        override = session.scalar(
            select(HolidayOverride).where(
                HolidayOverride.region == region,
                HolidayOverride.holiday_date == day,
            )
        )
        if override is not None:
            return None if override.is_working_day else override.name

    return _calendar(region, day.year).get(day)


def is_working_day(day: dt.date, region: str, session: Session | None = None) -> bool:
    if is_weekend(day):
        return False
    return holiday_name(day, region, session) is None


def holidays_in_range(
    start: dt.date, end: dt.date, region: str, session: Session | None = None
) -> list[tuple[dt.date, str]]:
    """Every non-weekend public holiday in an inclusive range, for explanations."""
    out = []
    cursor = start
    while cursor <= end:
        if not is_weekend(cursor):
            name = holiday_name(cursor, region, session)
            if name:
                out.append((cursor, name))
        cursor += dt.timedelta(days=1)
    return out


def working_days_in(
    start: dt.date, end: dt.date, region: str, session: Session | None = None
) -> int:
    """Whole working days in an inclusive range: no weekends, no holidays."""
    count = 0
    cursor = start
    while cursor <= end:
        if is_working_day(cursor, region, session):
            count += 1
        cursor += dt.timedelta(days=1)
    return count


def working_days_between(
    start: dt.date,
    end: dt.date,
    region: str,
    session: Session | None = None,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
) -> Decimal:
    """Chargeable leave days for a request, as a Decimal in 0.5 steps.

    The convention, stated explicitly:

      * Both endpoints count. Mon 1st to Fri 5th is 5 days.
      * Weekends never count.
      * Public holidays never count, per the region's calendar.
      * A half-day flag subtracts 0.5, but only if that endpoint is actually
        a working day — flagging a half day on a Saturday changes nothing.
      * A single day flagged at both ends is 0.5, not 0.
      * A range with no working days is 0, which callers reject.
    """
    if end < start:
        raise ValueError(f"end ({end}) must be on or after start ({start}).")

    whole = Decimal(working_days_in(start, end, region, session))
    if whole == 0:
        return Decimal("0")

    if start == end:
        # One day: either half of it or all of it.
        return HALF_DAY if (start_half_day or end_half_day) else whole

    if start_half_day and is_working_day(start, region, session):
        whole -= HALF_DAY
    if end_half_day and is_working_day(end, region, session):
        whole -= HALF_DAY

    return whole


# ---------------------------------------------------------------------------
# Day-by-day explanation
# ---------------------------------------------------------------------------
DAY_KIND_LEAVE = "leave"
DAY_KIND_HALF = "half_day"
DAY_KIND_WEEKEND = "weekend"
DAY_KIND_HOLIDAY = "holiday"


def explain_days(
    start: dt.date,
    end: dt.date,
    region: str,
    session: Session | None = None,
    *,
    start_half_day: bool = False,
    end_half_day: bool = False,
) -> list[dict]:
    """One row per calendar day, saying whether it is charged and why.

    This is what turns "3 days" into something an employee can check:

        Apr 20  Mon  Leave
        Apr 21  Tue  Leave
        Apr 22  Wed  Public holiday — Ramzan
        Apr 23  Thu  Leave
        Apr 24  Fri  Leave
        Leave requested: 4 days

    A number the employee cannot verify is a number they will dispute. Showing
    the working turns a support ticket into a glance.
    """
    if end < start:
        raise ValueError(f"end ({end}) must be on or after start ({start}).")

    rows: list[dict] = []
    cursor = start
    while cursor <= end:
        weekend = is_weekend(cursor)
        name = None if weekend else holiday_name(cursor, region, session)

        if weekend:
            kind, charged, label = DAY_KIND_WEEKEND, Decimal("0"), "Weekend"
        elif name:
            kind, charged, label = DAY_KIND_HOLIDAY, Decimal("0"), f"Public holiday — {name}"
        else:
            is_half = (
                (start_half_day and cursor == start) or (end_half_day and cursor == end)
            )
            if start == end and (start_half_day or end_half_day):
                is_half = True
            if is_half:
                kind, charged, label = DAY_KIND_HALF, HALF_DAY, "Half day"
            else:
                kind, charged, label = DAY_KIND_LEAVE, Decimal("1"), "Leave"

        rows.append({
            "date": cursor,
            "weekday": cursor.strftime("%a"),
            "kind": kind,
            "label": label,
            "holiday_name": name,
            "charged_days": charged,
        })
        cursor += dt.timedelta(days=1)
    return rows


__all__ += ["explain_days", "DAY_KIND_LEAVE", "DAY_KIND_HALF",
            "DAY_KIND_WEEKEND", "DAY_KIND_HOLIDAY"]
