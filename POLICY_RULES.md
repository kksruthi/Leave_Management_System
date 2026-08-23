# Policy and accrual rules

Every number in this document is a **row in `org_policies`**, not a constant in
code. That is the point of the system: entitlement is `f(region, leave type,
tenure, date)`, resolved at run time, and HR changes it without a deployment.

What follows is the state of the seeded configuration, plus the engine rules
that act on it.

---

## 1. The regions

| | India-TamilNadu | USA-Texas |
|---|---|---|
| Legal entity | NorthBridge Technologies India Pvt Ltd | NorthBridge Systems USA Inc. |
| Leave year | **1 Apr → 31 Mar** | **1 Jan → 31 Dec** |
| Leave types offered | EL, CL, SL, Unpaid | EL, SL, Unpaid |
| Public holidays | `holidays.India(subdiv="TN")` | `holidays.UnitedStates(subdiv="TX")` |
| Weekend | Sat–Sun | Sat–Sun |

Texas has no Casual Leave, and no code anywhere says "except Texas". CL exists
as a *leave type*; Texas simply has no CL *policy row*. That is the whole
mechanism.

---

## 2. Entitlement by leave type

### Earned Leave (EL) — accrues monthly, scales with tenure

| Tenure | India days/yr | Texas days/yr |
|---|---|---|
| 0 – 1 yr | 15 | 10 |
| 1 – 3 yr | 18 | 15 |
| 3 – 5 yr | 24 | 20 |
| 5 yr + | 30 | 25 |

* **Accrual method:** `monthly` — `annual ÷ 12`, credited on the last day of
  each month.
* **Notice:** 7 days (India) / 14 days (Texas).
* **Max consecutive:** 15–20 days (India), 10–15 days (Texas), by bracket.
* **Tenure is evaluated on the date of the leave**, not on the date of the
  request. Someone who crosses their 3-year mark mid-request draws on both
  brackets — see §6.

### Casual Leave (CL) — India only

| | |
|---|---|
| Entitlement | 7 days/year |
| Accrual | `annual_lump`, granted at the start of the leave year |
| Notice | 1 day |
| Max consecutive | 3 days |
| Carry-over | none |

### Sick Leave (SL)

| | India | Texas |
|---|---|---|
| Entitlement | 10 days/yr | 8 days/yr |
| Accrual | `annual_lump` | `annual_lump` |
| Notice | 0 days (backdating allowed) | 0 days |
| Max consecutive | 5 days | 5 days |
| Carry-over | none | none |

### Unpaid Leave

Entitlement 0, `accrual_method = none`, `is_paid = false`. It is the terminal
fallback of every substitution chain: it needs no balance, so a request can
always be *expressed*, even when it cannot be *paid*.

---

## 3. Accrual rules

**Monthly (`monthly`).** `annual ÷ 12`, credited on the last day of the month,
pro-rated for a partial month at the start (joiner) or end (leaver). The first
and last months are `days_employed_in_month ÷ days_in_month`.

**Annual lump (`annual_lump`).** The whole entitlement on the first day of the
leave year, pro-rated for a mid-year joiner by
`days_remaining_in_year ÷ days_in_year`.

The credit is **dated to the leave year it covers**, not to the day the job
ran, and clamped to the employee's join date. Running the job on 1 January for
an India employee credits 1 April — the start of the year those days belong
to — so CL and SL read 7 and 10 whenever the job happens to be run. It is also
idempotent **per leave year**: running it on 1 April and again on 18 August
grants once.

**Part-time.** `employment_fraction` multiplies the entitlement. A 0.5 employee
on the 18-day bracket earns 9. One column, not a parallel set of policy rows.

**Rounding.** 3 decimal places, `ROUND_HALF_UP`, on `Decimal` throughout. Never
floats, and never Python's `round()` — that is banker's rounding and would
round 2.5 to 2.

**Termination.** Accrual stops at `exit_date`; the final partial cycle is
pro-rated.

---

## 4. What "balance" means

A spendable balance is **scoped to the current leave year**, not summed over
the whole ledger. The ledger is append-only and goes back years, so summing
all of it answers "how much has this person ever been credited" — which is
how a 30-day entitlement produced a displayed balance of 50.

    Current year   credits and deductions dated inside this leave year
    Carry-over     what the year-end job explicitly moved forward,
                   while still inside its expiry window
    Available      current year + valid carry-over − pending requests

Last year's unused days survive **only** through carry-over, with its cap and
its expiry. Once carry-over expires it drops out of Available immediately.

---

## 5. Carry-over — 18-day cap, 3-month expiry

At the end of each leave year, unused eligible balance carries into the next
year subject to **three** rules:

1. **Cap.** The lower of the policy's own `carryover_max_days` and an
   organisation-wide ceiling of **18 days**. India's 3–5 yr bracket allows 24
   and the 5 yr+ bracket allows 30; both are cut to 18. Texas caps at 5 or 10,
   which are stricter and therefore win.
2. **Separate bucket.** Carried days land in `bucket = 'carryover'`. They are
   **not** mixed into the new year's entitlement, and the dashboard shows the
   two separately.
3. **Expiry three months in.** Carried days are stamped with an expiry of
   three months after the new leave year starts.

   > Carried over on **1 Apr 2026** → expires **30 Jun 2026**.
   > (Texas: carried 1 Jan 2026 → expires 31 Mar 2026.)

**Anything above the cap is forfeited explicitly**, as a negative ledger row
reading `carry-over forfeited`. The loss appears in the history rather than as
a silent gap between two balances.

### Consumption order — perishable first

When leave is taken, the system **spends the expiring carry-over before the
new year's accrual**. A 5-day request against 5 carried days and 20 accrued
days produces one deduction of 5 from `carryover` and nothing from `current`.

This is not a preference. Deducting from `current` first would let carry-over
lapse in June while an unexpiring balance sat untouched — the employee losing
days to nothing but the order the code happened to deduct in.

Worked example (Nikhil, 5 yr+ India bracket, 47.5 days on 31 Mar 2026):

```
2026-03-31   -18.000  current    carry-over moved to next year
2026-03-31   -29.500  current    carry-over forfeited
2026-04-01   +18.000  carryover  carry-over brought forward   expires 2026-06-30
```

Buckets on 2 Apr: `carryover 18.000`, `current 0.000`.
Buckets on 1 Jul: `carryover 0`, `current 7.500` — the carried days have
expired, and only the April–June accrual remains.

---

## 6. Relocation between regions

HR relocates someone from **Employees → the person → Relocate**. Preview
first, then apply; the ledger is append-only, so "undo" means a reversing
entry rather than a delete.

Everything region-dependent follows them: the entitlement bracket, the leave
year (and therefore which accruals are in scope and when carry-over expires),
the carry-over cap, the public-holiday calendar, and which HR admin their
requests route to.

### Days already earned are never taken back

The rule is a split at the transfer date — everything before it stays priced
by the old region, everything after by the new one.

| Accrual method | What happens | Why |
|---|---|---|
| **monthly** (EL) | **no adjustment** | Nothing was granted in advance. The next accrual simply credits the new rate. |
| **annual lump** (CL, SL) | only the **remaining** slice of the leave year is re-priced | The whole year was credited up front at the old rate; the part still to come should be worth the new rate. |
| **not offered** in the new region (CL in Texas) | balance **kept**, nothing further accrues | Deleting it would confiscate days already granted. |

The formula for a lump is `(new_annual − old_annual) × remaining_days ÷
days_in_leave_year`, and a relocation can never push a balance below zero —
a reduction is capped at what is actually there, and the cap is stated on the
ledger row.

> **What this replaced.** The first version compared *earned so far* under
> each region — `9.493 − 11.392 = −1.899` — which re-prices the whole year to
> date and retroactively removes days somebody earned in Chennai because they
> later moved to Texas. That is a clawback, not a reconciliation.
> `test_days_already_earned_are_never_taken_back` pins the fix.

Worked example, Ravi moving Tamil Nadu → Texas on 1 July 2026:

```
EL  24/yr → 20/yr    no change    monthly; next accrual uses the new rate
CL   7/yr → not offered  no change    balance kept, stops growing
SL  10/yr →  8/yr    −1.496       74% of the leave year re-priced
leave year  2026-04-01→2027-03-31  becomes  2026-01-01→2026-12-31
```

---

## 7. Multi-bracket and multi-policy requests

A request that spans a tenure boundary or a policy version change is split
into **spans**, and each span is classified against the policy in force for
it. The balance is shared across spans — a 3-day span and a 7-day span
against a 5-day balance yield 5 paid and 5 unpaid, not 8 paid.

---

## 8. Classification and substitution

Requested days are covered in order:

1. the requested leave type, up to its available balance;
2. the substitution chain for that type (`substitution_rules`);
3. Unpaid, which always absorbs the remainder.

The employee sees the resulting split *before* submitting, day by day, with
holidays and weekends named — not a bare "3 days".

---

## 9. Approval routing

Routing is **manual**, by design:

* Submission creates **tier 1 only** — the requester's line manager.
* **Approve** grants the leave and deducts the balance. Final.
* **Forward** explicitly does *not* grant it. A manager may forward to HR;
  HR may forward to a director. One step at a time, never backwards.
* The rules table still runs, as a **recommendation** shown to the approver
  with its reason ("over 5 days"). Overriding it is a recorded judgement call.
* A manager's own leave goes to HR; HR's goes to a director; a director with
  nobody above them self-approves.
* Role tiers resolve to someone **in the requester's own region** where one
  exists — a Chennai request does not land on Texas HR.

---

## 10. Policy lifecycle

A published policy year has a **one-year term**. HR either publishes the next
year with changes, or publishes it unchanged — and "HR looked at 2027 and
decided it should match 2026" is recorded as a different fact from "nobody
looked".

Rows are never edited once used and never deleted. Rolling a year forward
closes the outgoing row's `effective_to`, inserts a successor, and links them
with `supersedes_id`, so a 2025 ledger entry still resolves to the 2025
number forever.

Reminders go to every HR admin and director at **90, 30 and 7 days** before a
region's term ends, and again if it lapses.
