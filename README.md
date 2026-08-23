# Dynamic PTO & Leave Management Engine

| Module | What it is |
|---|---|---|
| **1 — Data layer**  | Schema, migrations, seed data, write-time validation |
| **2 — Policy engine**| `resolve_policy()` — the one lookup everything else calls |
| **3 — Accrual** | Monthly / annual-lump ledger jobs + manual triggers |
| **4 — Dashboard** | `get_live_balance()` + the full employee payload |
| **5 — Classification** | Request submission, paid/unpaid split, substitution order |
| **6 — Approval** | Chain resolution, authorization, delegation, SLA, outbox |
| 7 — Integration | End-to-end pipeline + ledger deduction + payroll |
| **Review fixes 5–8** | Pro-ration, part-time, termination, explicit rounding |
| **Review fixes 9–41** | See [FINDINGS.md](FINDINGS.md) |
| **Policy lifecycle** | HR's annual roll-forward (`app/policy_admin.py`) |
| **Dashboard + auth** | JWT login, role-based React UI — see [DASHBOARD.md](DASHBOARD.md) |

`pytest` runs all built modules — **356 tests**. It provisions its own
`leave_engine_test` database, so it never competes with demo data.

**Read [FINDINGS.md](FINDINGS.md)** for the finding-by-finding changes, the
reasoning on what is dynamic vs hardcoded, and the list of behaviour changes.

```bash
# the web app
pip install -r requirements.txt && cd web && npm install && npm run build && cd ..
alembic upgrade head && python -m app.seed && python seed_users.py && python seed_demo.py
python run_api.py        # -> http://127.0.0.1:8000   (login: priya@northbridge.example / leave1234)

# the CLI
python -m app.cli policy-year --region India-TamilNadu --from-year 2025 \
                              --set CL:entitlement_days_per_year:9
python -m app.cli carryover  --run-date 2026-03-31
python -m app.cli outbox
```

---

# Module 1 — Data Layer

Schema, migrations, seed data, and insert-time validation. Nothing else.

No `resolve_policy()`, no accrual jobs, no balance math, no approval-chain
resolution, no dashboards. If you find business logic in here, it's in the
wrong module.

---

## Stack

| Choice | What | Why |
|---|---|---|
| Database | **PostgreSQL 16** | The overlapping-tenure-range rule is a range-overlap problem. Postgres solves it natively with a GiST `EXCLUDE` constraint over `numrange`/`daterange`, so the rule is enforced by the storage engine rather than by application code that can be bypassed. `NUMERIC` also gives exact decimal accrual amounts (1.25 days/month), which `FLOAT` would not. |
| ORM / models | **SQLAlchemy 2.0** (typed `Mapped[]` declarative) | Models double as the schema source of truth for autogenerate, and give Module 2 a ready query layer. |
| Migrations | **Alembic** | Versioned, reversible. `0001_initial_schema` creates everything. |
| Validation | **Both layers** (per the design decision) | DB constraints are the hard floor; `app/validation.py` runs the same checks in Python first so callers get readable errors. |

---

## Quick start

```bash
# 1. dependencies
pip install -r requirements.txt

# 2. a database  (any Postgres 16 works; this is just a convenience)
./scripts/dev_db.sh up

# 3. point at it
cp .env.example .env        # edit DATABASE_URL if yours differs

# 4. schema + data
alembic upgrade head
python -m app.seed

# 5. prove it
pytest
```

`DATABASE_URL` defaults to
`postgresql+psycopg2://leave:leave@localhost:5432/leave_engine`.
Set the env var to override; `.env` is loaded automatically.

Other commands:

```bash
python -m app.seed --reset        # wipe seeded tables, then re-seed
python -m app.seed --strict-spec  # seed only the rows listed in the brief
alembic downgrade base            # drop everything
```

`schema.sql` is a `pg_dump --schema-only` snapshot of the migrated database —
useful for review, but **the migration is authoritative**, not that file.

---

## The seven tables

```
org_policies ─────────────┐  the single lookup table the whole engine reads
                          │  (region + tenure bracket + leave type → entitlement)
employee ─────────────────┤  region + join_date; tenure is derived, never stored
employee_exceptions ──────┤  optional per-person override, wins over org_policies
leave_request ────────────┤  what someone asked for
approval_steps ───────────┤  the materialised approval chain for one request
approval_rules ───────────┤  routing as rows: which tiers get added, and when
leave_ledger ─────────────┘  append-only; balance = SUM(amount), never cached
```

A few schema decisions worth knowing before Module 2:

- **Tenure brackets are half-open `[min, max)`.** `0–1yr` and `1–3yr` sit flush
  without overlapping. `tenure_max_years IS NULL` means "and above".
- **`leave_ledger.amount` is signed** — positive for accruals and grants,
  negative for deductions. There is no separate credit/debit column, and no
  stored balance field anywhere. Live balance is always a `SUM`.
- **`leave_ledger.policy_snapshot_id`** FKs to the exact `org_policies` row that
  produced the entry, with `ON DELETE RESTRICT`. That's what makes "why did I
  get 1.5 days that month?" answerable years later — and it means a policy row
  that has been used cannot be deleted, only closed off with `effective_to`.
- **Leave types are plain string codes** (`EL`, `CL`, `SL`, `Unpaid`), not a
  lookup table, keeping the table count at the specified seven. Adding a type
  stays a data change.
- **`created_at` and audit columns** (`acted_by`, `acted_at`, `is_active`) were
  added beyond the brief's column list — cheap now, painful to retrofit.

---

## The two write-time controls

These are the *only* validation in the entire system. There is no runtime
compliance engine anywhere; compliance is a data-authoring discipline.

### 1. `compliance_note` must be non-empty

Every policy row has to carry a human-written statement of what law or internal
minimum the number satisfies. A process control, not an engine.

- DB: `CHECK (length(btrim(compliance_note)) > 0)` plus `NOT NULL`
- App: `PolicyValidationError` with a message explaining what to write

### 2. No overlapping tenure ranges

Two `org_policies` rows collide only if **all four** are true: same region, same
leave type, overlapping tenure brackets, *and* overlapping effective-date
windows. That last condition is what lets you supersede a policy — close the old
row's `effective_to`, then insert the replacement.

- DB:
  ```sql
  EXCLUDE USING gist (
      region WITH =,
      leave_type_id WITH =,
      numrange(tenure_min_years, tenure_max_years, '[)') WITH &&,
      daterange(effective_from, effective_to, '[]')      WITH &&
  )
  ```
  (needs the `btree_gist` extension, created by the migration). `NULL` upper
  bounds become unbounded ranges automatically — "5+ years" and "no end date"
  need no special-casing.

- App: `find_conflicting_policy()` runs the same logic in Python and reports
  *which* existing row conflicts, with both brackets and both date windows in
  the message.

Why both? The app layer is for humans; the DB constraint is the guarantee. A
direct `psql` insert, a future admin UI, a data-migration script — none of them
can write an overlapping row.

### Using the validated path

```python
from app.db import SessionLocal
from app.models import OrgPolicy
from app.validation import add_policy, PolicyValidationError

with SessionLocal() as s:
    try:
        add_policy(s, OrgPolicy(region="India-Karnataka", ...))
        s.commit()
    except PolicyValidationError as e:
        print(e)   # readable: names the conflicting row and how to fix it
```

`update_policy()` does the same for edits, excluding the row being edited from
its own overlap check.

---

## Seed data

**Policies** — the eleven rows from the brief, exactly as specified:
India-TamilNadu EL 15/18/24/30 across the four tenure bands, CL 7, SL 10;
USA-Texas EL 10/15/20/25, SL 8. All `effective_from 2020-01-01`, open-ended,
with realistic carryover / notice / max-consecutive values and a written
compliance note each.

Plus **two `Unpaid` rows** (one per region) that are *not* in the brief's list.
They're needed so Module 2's `resolvePolicy()` has something to find for the
final fallback in the substitution order. Run `--strict-spec` to omit them.

**Approval rules** — the four routing rules. Manager-as-tier-1 is stored as a
row (`condition_field="always"`) rather than hardcoded, so the entire chain
stays inspectable as data.

**Employees** — Priya (India-TamilNadu) and Raj (USA-Texas), both joined
`2025-04-01`, matching the worked example in the design doc. Two managers are
seeded alongside them so `manager_id` is populated and Module 2's tier-1
routing resolves to a real person.

Seeding is idempotent — running it twice is a no-op.

---

## Acceptance checks

`pytest` — 20 tests, all four brief checks covered:

| Check | Tests |
|---|---|
| Both regions seed cleanly | region set, both EL tenure ladders verified value-by-value, approval rules present |
| Overlapping tenure range is rejected | app layer **and** raw-SQL bypass; open-ended bracket overlap; plus the negative cases — flush-adjacent brackets, same bracket in a new region, and a superseding policy with a non-overlapping effective window all succeed |
| Empty `compliance_note` is rejected | app layer (empty, whitespace, newline) **and** raw-SQL bypass, **and** `NULL` |
| Priya and Raj ready for Module 2 | names, regions, join dates, and manager linkage |

The tests run against a migrated + seeded database and roll back after each
case, so they leave the seed intact.

---

---

# Module 2 — Policy Engine

`app/policy_engine.py`. One function answers the only question the system ever
asks. Every downstream module calls it; none of them read `org_policies`
directly or re-implement policy logic.

```bash
python -m app.policy_engine     # the Priya/Raj proof point, side by side
```

```
Employee Region            As of        Tenure   EL days/yr  Per month  Source
------------------------------------------------------------------------------
Priya    India-TamilNadu   2025-06-01   0 yr     15.00       1.250      org_policy
Priya    India-TamilNadu   2026-04-01   1 yr     18.00       1.500      org_policy
Raj      USA-Texas         2025-06-01   0 yr     10.00       0.833      org_policy
Raj      USA-Texas         2026-04-01   1 yr     15.00       1.250      org_policy
```

## API

```python
from app.db import SessionLocal
from app.policy_engine import resolve_policy, years_between

with SessionLocal() as s:
    p = resolve_policy(s, priya, "EL", "2026-04-01")

    p.entitlement_days_per_year   # Decimal("18.00")
    p.monthly_accrual             # Decimal("1.500")  -> Module 3
    p.is_paid, p.accrual_method   # True, "monthly"   -> Module 5
    p.min_notice_days             # 7                 -> request validation
    p.policy_snapshot_id          # FK for leave_ledger
    p.source                      # "org_policy" | "exception"
    p.explain()                   # human-readable audit line
```

- `employee` may be an `Employee` instance **or** an id.
- `as_of_date` may be a `date`, an ISO string, or omitted (defaults to today).
- The result is a **frozen dataclass** — callers can't mutate a resolved policy.
- It's a **pure read**: no writes, no session state, no HTTP or scheduler
  coupling. Import it from anywhere.

## Precedence — exactly two levels

```
1. employee_exceptions   active row for this person + leave type  -> wins outright
2. org_policies          region + tenure bracket + effective date
```

No third statutory layer, no compliance cross-check at read time — that was
deliberately removed from the design. Compliance lives in Module 1's
`compliance_note`, at authoring time.

## Tenure semantics

`years_between()` uses **full elapsed years, anniversary-based** — not
365-day arithmetic, so it reads the way a service date does:

| Joined | As of | Tenure |
|---|---|---|
| 2025-04-01 | 2026-03-31 | 0 |
| 2025-04-01 | 2026-04-01 | 1 |
| 2024-02-29 | 2025-02-28 | 1 (leap-day joiners hit their anniversary on 28 Feb) |
| 2025-04-01 | 2020-01-01 | 0 (future join date clamps to 0, never negative) |

Combined with Module 1's half-open `[min, max)` brackets, this is what makes
accrual "dynamic" with no anniversary-detection code anywhere: the same job
re-resolves tenure on every run and simply gets a different answer once someone
crosses a boundary.

## Two judgment calls

**Not-found returns `None`, not an exception.** Per the brief — a gap in the
brackets, an unknown leave type, a date outside every effective window, or an
unseeded region all return `None` and let the caller decide. `resolve_policy_or_raise()`
is there for callers that treat it as a bug.

**An exception inherits operational fields from the regional row.**
`employee_exceptions` carries an entitlement number and nothing else — no
`is_paid`, no `accrual_method`, no carryover rules. The exception's *number*
wins outright, exactly as specified; the mechanics it cannot express are read
from the regional row, so Module 3 still knows *how* to accrue those days.
`policy_snapshot_id` keeps pointing at the regional row, so the ledger stays
auditable. If you'd rather exceptions be fully self-describing, the fix is to
add those columns to `employee_exceptions` in a Module 1 migration — the
inheritance is a workaround for a gap in that table's shape, and it's isolated
to one branch of `resolve_policy()`.

## Tests — 36

| Group | Covers |
|---|---|
| Worked example | All four Priya/Raj cases, exact values; the full 8-point tenure ladder from day one to 15 years; monthly accrual to 3dp against the design doc |
| Exception precedence | Beats a matching regional bracket; inherits mechanics; expires correctly; scoped to one person and one leave type |
| Not-found | Unknown leave type, leave type absent in region, date before all windows, unknown employee id, strict-variant raises |
| Tenure | 10 parametrised cases including leap-day joiners and future join dates |
| Library contract | Side-effect-free, immutable result, accepts id or object, accepts str or date, no global session binding |

---

# Module 3 — Accrual Engine

`app/accrual.py`. Turns the policy number into dated rows in `leave_ledger`.

```bash
python -m app.cli monthly --run-date 2025-06-01   # one month, everyone
python -m app.cli annual  --run-date 2025-04-01   # CL/SL lump sums
python -m app.cli simulate --start 2025-04-01 --months 15
python -m app.cli ledger --employee Raj
python -m app.cli reset-ledger                    # clear demo runs
```

## The demo that makes the point

`simulate` runs consecutive monthly accruals and prints what each one wrote:

```
Run date                     Priya                   Raj
--------------------------------------------------------
2025-04-01        1.250 (15.00/yr)      0.833 (10.00/yr)
       ...                     ...                   ...
2026-03-01        1.250 (15.00/yr)    0.837 (10.00/yr) *
2026-04-01        1.500 (18.00/yr)      1.250 (15.00/yr)
2026-05-01        1.500 (18.00/yr)      1.250 (15.00/yr)

* = year-end true-up folded in
```

The step-up on 2026-04-01 is the whole design in one screen. **There is no
anniversary check anywhere in `accrual.py`** — no bracket comparison, no
"has the employee levelled up" branch, nothing. The job calls
`resolve_policy(..., as_of_date=run_date)` fresh each run and writes down
whatever it says. The test
`test_accrual_does_not_reimplement_policy_logic` asserts this structurally:
`accrual.py` may not contain `select(OrgPolicy)` or the string
`tenure_min_years`.

## API

```python
from app.accrual import run_monthly_accrual, run_annual_grant

result = run_monthly_accrual(session, "2026-04-01")   # commits by default
result.written      # [LedgerEntry(...), ...]  each with ledger_id + policy_snapshot_id
result.skipped      # [SkippedEntry(employee, leave_type, why), ...]
result.total_days
result.summary()    # "monthly accrual for 2026-04-01: 4 entries (+6.666 days), 0 skipped"
```

Pass `commit=False` to run inside a caller-controlled transaction — that's how
the tests keep the ledger clean.

## Three behaviours worth knowing

**Rounding: 3 decimals + a year-end true-up.** `10/12 = 0.8333…`. Storing
`0.83` would quietly cost every US employee 0.04 days a year, forever. The
ledger stores `0.833`, and the final run of each anniversary cycle writes a
correction entry so twelve runs sum to exactly `10.000`. Priya's `15/12 = 1.25`
divides evenly, so she never gets a true-up row at all — no noise where there's
no drift. The true-up only fires on **complete** cycles (all 12 months
present); a partial cycle is genuine under-accrual, not rounding, and silently
topping it up would be wrong. Disable with `true_up=False`.

**Re-runs are idempotent.** A second run for the same
`(employee, leave type, date, reason)` detects the existing row and skips it.
A double-click on the demo trigger, or a re-run after a partial failure, can't
inflate anyone's balance.

**A missing policy skips, it never aborts the batch.** `resolve_policy()`
returning `None` — a gap in the brackets, a leave type not offered in that
region — logs a warning naming the employee and leave type, records a
`SkippedEntry`, and moves on. One broken region must not stop accrual for
everyone else. Raj legitimately has no CL row, so every run skips that pair.

Also: employees whose `join_date` is after `run_date` don't accrue. `employee`
has no status column in Module 1, so "active" means "has already joined" —
worth revisiting when leavers and suspensions enter the schema.

## Pro-ration

Not implemented — explicitly STRETCH in the build phases. A mid-cycle joiner
currently receives the **full** annual lump, not a pro-rated share. The hook
is marked in `run_annual_grant()`, and `cycle_bounds()` already computes the
anniversary window the calculation would need.

## Tests — 35

| Group | Covers |
|---|---|
| Worked example | All four Priya/Raj amounts asserted on the actual ledger rows; both in one batch |
| Annual lump | Full entitlement for CL/SL; region-correct (no CL in Texas); jobs don't poach each other's accrual methods; `Unpaid` never accrues |
| Null policy | Punches a real gap in the tenure brackets, then asserts the batch survives, other employees and other leave types still accrue, and the warning names who was skipped |
| `policy_snapshot_id` | Matches what the engine resolved; changes when the bracket changes; no orphaned FKs; a past accrual is explainable from the row alone |
| Idempotency | Re-runs don't double-credit, different dates aren't false duplicates |
| Rounding / true-up | 12 runs sum to exactly 10.000 for Raj and 15.000 for Priya; no true-up row when division is exact; none on incomplete cycles; drift visible when disabled |
| Dynamism | 24 consecutive runs across the bracket boundary; exception holders accrue their exception amount; an HR policy edit changes the next run with no deploy |

---

# Module 4 — Live Balance & Dashboard

`app/dashboard.py`. What the employee actually sees. **Read-only** — Module 3
owns every ledger write.

```bash
python -m app.cli dashboard --employee Priya --as-of 2026-04-15
python -m app.cli dashboard --employee Raj --as-of 2026-03-20 --json
```

```
Priya — India-TamilNadu
joined 2025-04-01 · tenure 1 yr · as of 2026-04-15
==========================================================================

EL
  Current balance      16.500 days
  Annual entitlement   18.00 days/year (paid, monthly)
  Next accrual         +1.500 on 2026-05-01
  Notice required      7 days
  History (13 entries)
    … 8 earlier entries (use --full-history)
    2026-03-01     1.250  running   15.000   monthly accrual
    2026-04-01     1.500  running   16.500   monthly accrual
```

## API

```python
from app.dashboard import get_live_balance, get_dashboard, get_balances

get_live_balance(session, priya.id, "EL", "2025-09-15")   # Decimal("5.000")
get_balances(session, priya.id, "2025-09-15")             # {"EL": ..., "CL": ...}

dash = get_dashboard(session, priya, "2026-04-15")        # all leave types, one call
dash.leave_type("EL").balance                 # Decimal("16.500")
dash.leave_type("EL").entitlement_days_per_year
dash.leave_type("EL").next_accrual.amount     # Decimal("1.500")
dash.leave_type("EL").history                 # [LedgerLine(..., running_total=...)]
dash.pending_requests                         # [] until Modules 5/6 write rows
dash.to_dict()                                # JSON-ready payload
```

`to_dict()` serialises Decimals as **strings** by default — `"1.250"` survives
a JSON round trip, `1.25` as a float does not. Pass `decimals_as_str=False` if
the consumer prefers numbers.

## The no-cache rule, enforced structurally

`get_live_balance()` is a `SUM` computed on every call. There is no balance
column, no memo, no snapshot table, no rollup job. That's a correctness
requirement: the moment a balance is stored anywhere else, there are two
truths and they diverge.

`test_module_declares_no_cache` parses `dashboard.py`'s **AST** and fails if
`functools` is ever imported or any function grows a `cache` decorator. (It
reads the AST rather than the raw text because the module docstring
legitimately contains the word `lru_cache` while explaining why there isn't
one — a substring search flagged its own documentation.)

If this ever gets slow, the answer is `ix_leave_ledger_balance`, which Module 1
already ships — not a cached number.

## Next-accrual projection shows a bracket crossing *before* it happens

For monthly types, the projection re-resolves policy at the **future** accrual
date, not today's. So Raj's dashboard on 2026-03-20 already reads:

```json
"next_accrual": {
  "date": "2026-04-01",
  "amount": "1.250",
  "note": "Tenure bracket changes on 2026-04-01: entitlement rises from 10 to 15 days/year."
}
```

Same mechanism as Module 3's job — re-resolve at the date you care about —
so there is still no anniversary logic anywhere. Annual-lump types project the
next leave-year start via `cycle_bounds()`. `Unpaid` (`accrual_method: none`)
correctly projects nothing.

## Two edge cases worth knowing

**A balance survives its policy being retired.** If HR closes off a bracket,
`resolve_policy()` returns `None` — but the employee still holds days they
earned. The payload shows `policy_found: false`, keeps the balance and the
full history, and carries a "Contact HR" note. Hiding earned days because a
config row expired would be the wrong answer.

Note the test for this **expires** the bracket rather than deleting it: Module
1's `ON DELETE RESTRICT` makes deleting a policy that has already produced
accruals impossible. `test_used_policy_rows_cannot_be_deleted` asserts that
from this side of the boundary.

**Deductions need no new code.** Ledger amounts are signed, so a negative row
from Module 6 reduces the balance through the same `SUM`. Already tested here.

## Tests — 42

| Group | Covers |
|---|---|
| Worked example | 4 × 1.25 → exactly 5.000 on 2025-09-15; keeps growing to 6.500 across the bracket crossing; point-in-time balances at every intermediate date |
| Future exclusion | Future-dated entries excluded; boundary date inclusive; empty history is `Decimal("0")`, not `None` |
| Freshness | Three identical repeat calls; a new row visible on the very next call; a deduction lands immediately; no cache between dashboard loads; AST guard |
| Entitlement | 15 → 18 across the crossing; parametrised against `resolve_policy()` at five dates; an HR edit shows on the next load |
| Payload | All leave types in one call; region-correct type lists; history running totals; policy drill-down; next-accrual projection incl. year-end rollover; pending-requests hook populated from real `leave_request`/`approval_steps` rows; JSON round trip |

Ledger rows in these tests come from calling **Module 3's real accrual job**,
not hand-written fixtures, so the integration is exercised rather than mocked.

---

# Module 5 — Request & Classification

`app/classification.py`. Records the request, then decides how many days are
paid. **Writes exactly one kind of row: `leave_request`.** The ledger is not
touched — the split is only *computed* here; deduction happens after the
approval chain completes (Module 6/7). A test parses the AST and fails if this
module ever constructs a `LeaveLedger`.

```bash
python -m app.cli request --employee Priya --leave-type EL \
                          --start 2025-12-01 --end 2025-12-19 --dry-run
```

```
Request #22 — Priya · EL
  2025-12-01 → 2025-12-19  (15 working days)
  Status               pending
  Paid                 15 days
  Unpaid               0 days
  EL balance was short; covered from the substitution order. (11.25 from EL, 3.75 from CL)

  Substitution walk
    EL        11.250  (balance 11.250, requested)
    CL         3.750  (balance 7.000, fallback)
```

## API

```python
from app.classification import submit_leave_request, classify_paid_unpaid, substitution_order

result = submit_leave_request(session, priya, "EL", "2026-05-04", "2026-05-08")
result.request_id                    # what Module 6 needs
result.classification.paid_days      # Decimal
result.classification.draws          # [Draw(leave_type_id, days, balance_before, ...)]
result.warnings                      # advisory, non-blocking

classify_paid_unpaid(session, priya, "EL", 6, "2025-06-01")   # pure computation
```

## The substitution order — read this bit carefully

The design says the fallback order is **Casual → Earned → Unpaid**, but the
pseudocode resets `remaining = requested_days` *before* iterating. So the chain
has to **start with the requested type itself**, or the worked example (EL
balance 3, request 6, CL balance 7 → 3 from EL then 3 from CL) could never draw
those first 3 days from EL.

```python
substitution_order("EL")     # ["EL", "CL", "Unpaid"]
substitution_order("CL")     # ["CL", "EL", "Unpaid"]
substitution_order("SL")     # ["SL", "CL", "EL", "Unpaid"]
substitution_order("Unpaid") # ["Unpaid"]   — never substitutes
```

Duplicates are dropped, `Unpaid` is always last and always absorbs the
remainder. Each fallback is checked against **its own** live balance, and a
fallback only counts as paid if *its* policy says so. Types absent in the
employee's region are stepped over — Raj's chain skips CL entirely, since
Texas has no CL row.

Hardcoded rather than table-driven, as the brief allows. Making it configurable
later means replacing one function.

## How days are counted

`business_days_between()` — **inclusive of both endpoints, Mon–Fri**. Mon 1st
to Fri 5th is 5 days; Fri to Mon is 2; a weekend-only range is 0 and is
rejected at submission.

**Public holidays are not excluded.** There is no holiday calendar in Module 1's
schema, and Tamil Nadu and Texas have entirely different ones. A request
spanning Diwali or Thanksgiving currently over-counts by those days.
`test_holidays_are_not_excluded_yet` pins that behaviour so it can't change
silently. The fix is a `holidays(region, date)` table filtered inside this one
function. Half-days aren't modelled either.

## Sum conservation is enforced by the type

`paid_days + unpaid_days == requested_days` is checked in
`Classification.__post_init__`, so a broken split cannot even be constructed —
no caller can observe one. Tested across nine balance/request permutations.

## Two judgment calls

**No policy → unpaid, not an error.** If `resolve_policy()` returns `None`,
every day is classified unpaid with a "Contact HR" reason. Claiming days are
paid when no policy supports it would be the worse failure.

**Notice and max-consecutive limits warn, they don't block.** A 20-day EL
request exceeding the 15-day maximum is still recorded as `pending`, with a
warning attached for the approver. Whether those should hard-block is a policy
decision rather than a data one, and blocking isn't specified for this module —
Module 6's approver UI is the natural place to surface them.

## Tests — 49

| Group | Covers |
|---|---|
| The four brief cases | 10/6 → 6 paid; 3+7/6 → 6 paid via CL (asserting the *fallback's* balance was read); 0+0/4 → 4 unpaid; direct Unpaid short-circuits even with 20 days of EL banked |
| Sum conservation | 9 parametrised permutations, plus the constructor-level guard |
| Substitution | Chain shape for every type; SL walking all four legs; partial coverage; region-absent types skipped; negative balances not treated as available |
| Liveness | Balance change visible on the next call; `as_of_date` respected so future accruals aren't spendable |
| Boundaries | Writes no ledger rows (AST guard + row counts); creates no `approval_steps` |
| Submission | Pending row fully populated; id returned; classified against the *start date*'s policy; backwards range, weekend-only range and unknown employee rejected; warnings don't block |
| Day counting | 7 parametrised ranges; inverted range raises; holiday limitation pinned |
| Integration | A submitted request appears in Module 4's `pending_requests` |

## What Module 6 gets from this

- `result.request_id` — the `leave_request` row to resolve an approval chain for.
- `duration_days` and `leave_type_id` on that row are exactly the fields
  `approval_rules` conditions match on (`duration_days > 5`,
  `leave_type == "Unpaid"`).
- `result.classification` is the split to write to the ledger **on final
  approval** — as a negative `leave_ledger` amount per `Draw`, which is why
  `draws` records the per-type breakdown rather than just a total.
- `result.warnings` are ready to surface on the approver's screen.

---

# Review fixes — pro-ration, part-time, termination, rounding

Four gaps found in review of Modules 1–3. All four are now closed, in
migration `0002` plus `app/proration.py`. Every one is **policy data**, not a
constant in code.

```bash
python -m app.cli proration --employee Meera --leave-type CL --as-of 2025-11-01
```

```
Meera — India-TamilNadu
joined 2025-10-15
==========================================================================
  Leave type           CL
  Accrual year         2025-04-01 → 2026-03-31  (365 days, fixed org year)
  Eligible service     2025-10-15 → 2026-03-31  (168 days)
  Method / rounding    daily · 3dp
  Full-year figure     7.00 days
  Pro-rated to         3.222 days
```

## The finding behind the finding

Pro-ration could not work at all under the original design, and not because
of missing arithmetic. Accrual cycles ran **anniversary to anniversary**, so
every employee's leave year started on their own join date — which makes a
mid-year joiner a contradiction in terms. Nobody was ever partial.

So the fix starts with a new column, `org_policies.leave_year_end` ("MM-DD"):

| Set to | Cycle | Used for |
|---|---|---|
| `"03-31"` (India), `"12-31"` (US) | Fixed organisational leave year | Annual-lump types (CL, SL) |
| `NULL` | Anniversary-aligned, as before | Monthly types (EL) |

Monthly types stay anniversary-aligned deliberately: partial service there is
already handled month by month, so pro-rating the annual figure as well would
double-count it.

**This changes an existing number.** Raj joined 2025-04-01 in Texas, which
runs a calendar leave year — so he is a mid-year joiner and his SL grant is
now `6.027` days (8 × 275/365), not a flat `8.000`. That over-grant is
precisely the bug. `test_annual_grant_respects_region` documents the change.

## 1. Pro-ration for joiners *and* leavers

`prorate_entitlement()` splits the cycle into **segments** at every date the
answer could change, and weights each by its share of the year:

```
prorated = Σ over segments of ( entitlement_in_segment × segment_days / cycle_days )
```

A naive `entitlement × eligible_days / cycle_days` is wrong whenever the
entitlement itself moves mid-cycle — and it does, two ways:

- **Policy change.** HR raises CL from 7 to 13 on 1 October. The year is worth
  `7 × 183/365 + 13 × 182/365 = 9.992`, not 7 and not 13.
- **Tenure change.** A calendar-year cycle straddles an April anniversary:
  `15 × 90/365 + 18 × 275/365 = 17.260`.

Segmenting handles both without special-casing either, and a cycle with no
changes collapses to one segment and the naive formula. Both cases are tested
with those exact numbers.

Partial **months** are pro-rated too: a joiner starting on the 15th of a
31-day month earns 17/31 of that month's accrual (`1.25 × 17/31 = 0.685`).

`proration_method` on each policy row picks the convention — `daily`
(default), `monthly` (whole calendar months / 12, the common HR shorthand),
or `none` (a partial year earns the full entitlement).

## 2. Part-time entitlement

`employee.employment_fraction` — `1.000` full-time, `0.500` half-time.
`resolve_policy()` scales the entitlement by it, so **every** caller gets the
part-time figure without multiplying anything:

```
EL: 7.500 days/yr (15.00 full-time × 0.500 FTE, India-TamilNadu, 0-year tenure)
```

One column rather than a parallel set of part-time policy rows, so HR cannot
author a new region and forget its part-time variant. It composes with
pro-ration for free — half-time for half a year gives a quarter of the
full-time year, with no code that knows about the combination.
`full_time_entitlement_days_per_year` is kept alongside so a payslip can show
the arithmetic instead of an unexplained 7.5. A regression test pins that
`fraction = 1.000` is an exact no-op for everyone already in the system.

## 3. Termination and final accrual

`employee.status` + `employee.exit_date`. Accrual stops at the exit date, the
final part-month is pro-rated, and the final cycle's entitlement is pro-rated
to actual service. DB constraints: an exit date cannot precede a join date,
and a `terminated` employee must have one (otherwise they would accrue
forever, which is the original bug).

Two subtleties worth knowing:

- **`status` is not used as an accrual filter.** It is a *current* flag while
  accrual is *point-in-time* — a back-dated re-run of last March must still
  pay someone who has since resigned. `exit_date` bounds service; the CHECK
  guarantees it exists.
- **Eligibility is "any day in the run month"**, not "in service on the run
  date". Otherwise a joiner starting on the 15th misses the month entirely,
  because the job runs on the 1st.

**Not built:** final settlement / encashment. The engine reports the remaining
balance but does not write a closing entry, because whether unused leave is
paid out or lapses is a payroll policy decision (Module 7's territory), and it
varies by region — Tamil Nadu and Texas differ here.

## 4. Rounding, stated explicitly

`org_policies.rounding_dp` (0–3, default 3). The rule, in one line:

> **ROUND_HALF_UP to the policy's `rounding_dp`, with a year-end true-up so a
> complete cycle sums to the entitlement exactly.**

It used to be a constant in `accrual.py`. It is now data, so a region whose
payroll insists on 2dp says so in a row rather than a code change — and the
true-up absorbs the larger residual automatically (at 2dp Raj's 12 × 0.83 =
9.96, trued up by 0.040 to land on 10.000; at 3dp it's 0.004). Both are
tested. `ROUND_HALF_UP` is explicit because it is *not* Python's default —
`round()` uses banker's rounding, which would send 0.015 to 0.01.

The true-up now targets the **pro-rated** entitlement and counts **eligible**
months rather than a flat 12, so a joiner or leaver is trued up against what
they actually earned. It still won't fire on a cycle the system merely hasn't
finished running — that gap is real under-accrual, not rounding.

## New seed data

| Name | Case |
|---|---|
| Meera | Joined 2025-10-15 — mid-year joiner |
| Kavya | 0.5 FTE — part-time |
| Tom | Joined 2025-04-01, exited 2026-01-31 — leaver |

Seeding is now idempotent **per person**, so these land in an existing
database without a full `--reset`.

## Tests — 46 new

Covering: eligible-period clipping at both ends; joiner and leaver annual
grants; partial first and last months; part-time at every tenure bracket and
the full-time no-op; mid-cycle policy change and mid-cycle tenure change with
exact expected blends; all three pro-ration methods; `ROUND_HALF_UP` at 0/2/3
dp; the 2dp true-up; and every new DB constraint.

## Demo data — run the simulation

```bash
python -m app.seed          # policies, approval rules, base employees
python seed_users.py        # 29 logins across employee / manager / HR / director
python simulate_demo.py     # plays two leave years forward through the engine
```

`simulate_demo.py` is what makes the dynamic parts visible. It resets the
ledger and rebuilds it by running the real engine forward through time:
accrual month by month, leave requested and routed and approved, year-end
carry-over capped and dated, a mid-year region transfer, a tenure bracket
crossing, and a policy year published. Every number on the dashboard after
running it was produced by the same code paths the API calls — nothing is
poked into a table.

Re-run it whenever the demo data drifts. It prints a narrated trace of each
step.

### Approval copilot

When a request reaches a manager, the card now leads with a coverage verdict —
**Safe to approve**, **Worth a check** or **Thin cover** — over a bar showing
how much of the team would be away at the busiest point. "Why?" expands the
working: who overlaps, on which day, approved or still pending, plus notice,
unpaid days and how much leave this person has taken against the team median.

It is rules-based on purpose (`app/copilot.py`). Every sentence is generated
from a query, so a manager told "thin cover" can ask why, get "3 of your 4
people are off on Mon 07 Dec", and check it. It is advice about cover only —
it never blocks a decision and it is not a judgement on the request.

Thresholds live in `TEAM_COVERAGE_THRESHOLDS` and `MIN_COMFORTABLE_REMAINING`
because the right numbers are an organisational choice.


### The Simulation screen

Sign in as HR and open **Administration → Simulation**. Pick anyone, then run:

| Button | What it proves |
|---|---|
| Run one month's accrual | Accrual is a job, not a stored number |
| Show the tenure brackets | Entitlement changes itself as people stay |
| Transfer to the other region | Policy is a function of region, resolved live |
| Run year end (carry-over) | Carry-over is capped, bucketed and perishable |
| Publish next policy year | Policy is versioned, never overwritten |

Each button calls the same function a scheduled job would; the page adds a
before/after snapshot either side so the change has somewhere to appear, and
the ledger timeline below shows the row it wrote. Some actions take effect on
a future date — next month's accrual, a year end — so the "after" column is
read on that date and the page says so.

