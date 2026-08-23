"""Relocating an employee between regions.

Entitlement is `f(region, leave type, tenure, date)`, so moving someone from
Tamil Nadu to Texas changes what they are owed. This module works out what, if
anything, has to be written to the ledger — and, just as importantly, what
must NOT be.

## The rule: days already earned are never taken back

The first version of this compared "earned so far under the old region" with
"earned so far under the new region" and wrote the difference:

    India EL earned so far  = 11.392
    Texas EL earned so far  =  9.493
    adjustment              = -1.899      # WRONG

That re-prices the WHOLE year to date at the new region's rate, which means
an employee who worked five months in Chennai has days removed retroactively
because they later moved to Texas. They earned those days under the policy
that was in force while they earned them. Taking them away is not a
reconciliation, it is a clawback.

The correct split is at the transfer date:

    everything before it   priced by the OLD region  — already earned, kept
    everything after it    priced by the NEW region  — from now on

## Which types actually need an adjustment

**Monthly types (EL): none.** Nothing was granted in advance. The accrual job
resolves the policy fresh on every run, so from the transfer date it simply
starts crediting the new region's monthly rate by itself. Writing anything
here would double-count. This is the case the old formula got wrong.

**Annual-lump types (CL, SL): the remaining slice only.** The whole year's
entitlement was credited up front at the old region's rate. The part of the
year still to come should be worth the new region's rate, so the adjustment
is the difference applied to the REMAINING fraction of the leave year:

    (new_annual - old_annual) x remaining_days / days_in_leave_year

For a move on 1 July with an India leave year (Apr–Mar), roughly three
quarters of the year remains, and only that part is re-priced.

**Types the new region does not offer** (Casual Leave in Texas): the balance
stays and is still spendable, but nothing further accrues. Deleting it would
confiscate days already granted.

A relocation can never push a balance below zero. If the arithmetic would,
the reduction is capped at whatever is actually there and the cap is stated
on the ledger row.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.models import Employee
from app.policy_engine import resolve_policy

log = logging.getLogger("leave_engine.region_transfer")

__all__ = [
    "reconcile_region_transfer",
    "relocate_employee",
    "preview_region_transfer",
    "TransferAdjustment",
    "LEAVE_TYPES",
]

LEAVE_TYPES = ["EL", "CL", "SL"]
_DP = Decimal("0.001")


@dataclass(frozen=True)
class TransferAdjustment:
    leave_type_id: str
    old_entitlement: Decimal | None
    new_entitlement: Decimal | None
    adjustment: Decimal
    reason: str
    #: Why this leave type was treated the way it was — shown to HR so the
    #: absence of an adjustment is as legible as the presence of one.
    basis: str


def _round(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_DP, rounding=ROUND_HALF_UP)


def _leave_year(employee: Employee, region: str, on_date: dt.date) -> tuple[dt.date, dt.date]:
    """The leave year containing `on_date`, on `region`'s calendar."""
    from app.seed import REGION_LEAVE_YEAR_END

    stated = REGION_LEAVE_YEAR_END.get(region) or "12-31"
    month, day = (int(part) for part in stated.split("-"))
    end_this_year = dt.date(on_date.year, month, day)
    end = end_this_year if on_date <= end_this_year else dt.date(
        on_date.year + 1, month, day
    )
    start = dt.date(end.year - 1, month, day) + dt.timedelta(days=1)
    return start, end


def _remaining_fraction(transfer_date: dt.date, start: dt.date, end: dt.date) -> Decimal:
    """How much of the leave year is still ahead of the transfer."""
    total = Decimal((end - start).days + 1)
    remaining = Decimal((end - transfer_date).days)
    if remaining <= 0:
        return Decimal("0")
    return remaining / total


def preview_region_transfer(
    session: Session,
    employee: Employee,
    old_region: str,
    new_region: str,
    transfer_date: dt.date,
) -> list[TransferAdjustment]:
    """Work out the adjustments without writing anything.

    `employee.region` must already be the NEW region; the old one is passed in
    so the outgoing policy can be reconstructed without mutating the row.
    """
    stand_in = Employee(
        id=employee.id,
        name=employee.name,
        join_date=employee.join_date,
        region=old_region,
        employment_fraction=employee.employment_fraction,
    )

    year_start, year_end = _leave_year(employee, old_region, transfer_date)
    remaining = _remaining_fraction(transfer_date, year_start, year_end)

    out: list[TransferAdjustment] = []
    for leave_type in LEAVE_TYPES:
        old = resolve_policy(session, stand_in, leave_type, transfer_date)
        new = resolve_policy(session, employee, leave_type, transfer_date)

        if old is None and new is None:
            continue

        if new is None:
            out.append(TransferAdjustment(
                leave_type, old.entitlement_days_per_year, None, Decimal("0"),
                f"{new_region} does not offer {leave_type}.",
                f"Balance kept and still spendable; nothing further will accrue. "
                f"Days already granted under {old_region} are not confiscated.",
            ))
            continue

        if old is None:
            starts = (
                "the next monthly accrual"
                if new.accrual_method == "monthly"
                else "the next time the annual grant runs, pro-rated for the "
                     "part of the leave year they are here for"
            )
            out.append(TransferAdjustment(
                leave_type, None, new.entitlement_days_per_year, Decimal("0"),
                f"{leave_type} is newly available in {new_region}.",
                f"They start earning it at {starts}. Nothing is back-dated — "
                f"{old_region} did not offer this type, so there is no earlier "
                "period to credit.",
            ))
            continue

        old_rate = Decimal(old.entitlement_days_per_year)
        new_rate = Decimal(new.entitlement_days_per_year)

        if new.accrual_method == "monthly":
            out.append(TransferAdjustment(
                leave_type, old_rate, new_rate, Decimal("0"),
                f"{leave_type} accrues monthly — no adjustment needed.",
                f"Nothing was granted in advance, so the next accrual simply "
                f"credits {new_rate:g}/12 instead of {old_rate:g}/12. Days "
                f"already earned under {old_region} are kept.",
            ))
            continue

        # Annual lump: the whole year was credited up front at the old rate.
        # Only the part of the year still to come is re-priced.
        adjustment = _round((new_rate - old_rate) * remaining)
        pct = int(remaining * 100)
        if adjustment == 0:
            basis = (
                f"Both regions grant {old_rate:g} days a year, so the lump "
                "already credited is still correct."
            )
        else:
            direction = "more" if adjustment > 0 else "less"
            basis = (
                f"The {old_rate:g}-day lump was credited for the whole leave "
                f"year at {old_region}'s rate. {pct}% of that year is still "
                f"ahead, and it is worth {new_rate:g} a year in {new_region} — "
                f"{abs(adjustment)} days {direction} over the remaining period. "
                f"Days covering the period already served are untouched."
            )
        out.append(TransferAdjustment(
            leave_type, old_rate, new_rate, adjustment,
            f"{leave_type}: {old_rate:g}/yr → {new_rate:g}/yr from "
            f"{transfer_date.isoformat()}.",
            basis,
        ))

    return out


def reconcile_region_transfer(
    session: Session,
    employee_id: int,
    old_region: str,
    new_region: str,
    transfer_date: dt.date,
    *,
    commit: bool = True,
) -> list[TransferAdjustment]:
    """Write the adjustments a relocation calls for. Assumes `employee.region`
    has ALREADY been set to `new_region`."""
    from app.dashboard import get_live_balance
    from app.models import LeaveLedger

    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ValueError(f"No employee with id={employee_id}")

    planned = preview_region_transfer(
        session, employee, old_region, new_region, transfer_date
    )
    written: list[TransferAdjustment] = []

    for item in planned:
        if item.adjustment == 0:
            written.append(item)
            continue

        amount = item.adjustment
        # A relocation must never leave someone owing days.
        if amount < 0:
            balance = get_live_balance(
                session, employee.id, item.leave_type_id, transfer_date
            )
            if balance + amount < 0:
                capped = -balance
                log.info(
                    "capping %s relocation adjustment for %s: %s -> %s "
                    "(balance %s)",
                    item.leave_type_id, employee.name, amount, capped, balance,
                )
                amount = capped
            if amount == 0:
                written.append(item)
                continue

        policy = resolve_policy(
            session, employee, item.leave_type_id, transfer_date
        )
        session.add(LeaveLedger(
            employee_id=employee.id,
            leave_type_id=item.leave_type_id,
            amount=amount,
            reason=(
                f"Relocation {old_region} → {new_region} on "
                f"{transfer_date.isoformat()}: {item.basis}"
            ),
            effective_date=transfer_date,
            policy_snapshot_id=policy.policy_snapshot_id if policy else None,
        ))
        written.append(
            item if amount == item.adjustment
            else TransferAdjustment(
                item.leave_type_id, item.old_entitlement, item.new_entitlement,
                amount, item.reason,
                item.basis + " Capped so the balance cannot go negative.",
            )
        )

    session.flush()
    if commit:
        session.commit()
    return written


def relocate_employee(
    session: Session,
    employee: Employee,
    new_region: str,
    transfer_date: dt.date | None = None,
    *,
    commit: bool = True,
) -> dict:
    """Move someone to another region and reconcile it. The HR entry point.

    Sets the region FIRST — the reconciler resolves the incoming policy from
    the live row and reconstructs the outgoing one from the region it is
    given.
    """
    transfer_date = transfer_date or dt.date.today()
    old_region = employee.region
    if new_region == old_region:
        raise ValueError(f"{employee.name} is already in {old_region}.")

    employee.region = new_region
    session.flush()

    adjustments = reconcile_region_transfer(
        session, employee.id, old_region, new_region, transfer_date,
        commit=commit,
    )

    old_start, old_end = _leave_year(employee, old_region, transfer_date)
    new_start, new_end = _leave_year(employee, new_region, transfer_date)
    return {
        "employee_id": employee.id,
        "name": employee.name,
        "from_region": old_region,
        "to_region": new_region,
        "transfer_date": transfer_date.isoformat(),
        "leave_year_before": f"{old_start.isoformat()} → {old_end.isoformat()}",
        "leave_year_after": f"{new_start.isoformat()} → {new_end.isoformat()}",
        "adjustments": [
            {
                "leave_type_id": a.leave_type_id,
                "old_entitlement": str(a.old_entitlement) if a.old_entitlement is not None else None,
                "new_entitlement": str(a.new_entitlement) if a.new_entitlement is not None else None,
                "adjustment": str(a.adjustment),
                "reason": a.reason,
                "basis": a.basis,
            }
            for a in adjustments
        ],
    }
