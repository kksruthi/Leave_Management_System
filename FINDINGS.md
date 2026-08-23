# Review findings 5–41 — what changed and why

All 37 findings are closed. **342 tests pass.** Two new migrations (`0003`,
`0004`), five new modules, four new tables.

Findings 5–8 were closed in the previous pass (see the README's *Review fixes*
section). This document covers 9–41, plus the annual policy-versioning
requirement, plus the reasoning on what else should be dynamic.

---

## The one thing to read first

Three findings pointed at the same underlying mistake, and it is worth naming
because it will come up again in Module 7:

> **A rule enforced only in the outermost layer is a rule enforced nowhere.**

Finding 10 said "the API/UI must prevent employees accessing another
employee's data". Finding 16 said "controls are needed around who can write to
the ledger". Finding 29 said the system "records who acted but does not verify
they were authorized".

In each case the rule existed in someone's head, or in a UI, while the
underlying library stayed open. But these modules *are* a library: Module 7's
pipeline, the CLI, an accrual job and a future HTTP layer all call them
directly. The next caller in never knows about a rule the outer layer was
holding.

So each of those three is now enforced next to the data:

| Rule | Where it lives now |
|---|---|
| Who may read a balance | `app/authorization.py`, called inside `get_dashboard` / `get_live_balance` |
| Who may approve a tier | `app/authorization.py`, called inside `record_approval_decision` |
| Who may rewrite the ledger | **A Postgres trigger.** Nobody. Not even `psql`. |

---

## Module 4 — Dashboard

| # | Finding | Fix | Behaviour change |
|---|---|---|---|
| 9 | Carry-over expiry stored but not enforced | `expires_on` on each ledger row; every balance query filters it. `run_year_end_carryover()` is the job that produces those rows — the setting was previously inert because nothing ever created a carry-over credit | **Yes** — lapsed days stop counting |
| 10 | Authorization pushed to the API | `viewer=` on `get_dashboard` / `get_live_balance`, enforced in-library. `viewer=None` = trusted internal caller, and is greppable | No (opt-in) |
| 11 | Negative balance undefined | `org_policies.allow_negative_balance`, default `false`: a shortfall falls to unpaid rather than borrowing against unaccrued days | No |
| 12 | `as_of_date` not applied to pending requests | Pending requests are filtered by `submitted_at <= as_of_date`, exactly as the balance is | **Yes** |
| 13 | Missing employee reads as zero | `EmployeeNotFoundError`. `strict=False` restores the old behaviour explicitly | **Yes** |
| 14 | Carry-over not separated from current year | `bucket` column; `get_balance_buckets()`; `current_year_balance` / `carryover_balance` on the payload | No (additive) |
| 15 | Queries inefficient at scale | Covering index `ix_leave_ledger_balance_expiry`; `get_balances()` answers every type in one grouped query | No |
| 16 | Ledger writes uncontrolled | `trg_leave_ledger_append_only` — UPDATE and DELETE raise, for every caller | **Yes** |

**On 16.** An audit trail that can be silently rewritten is not an audit trail.
Corrections are made by appending a reversing entry, which leaves both the
error and the fix visible. This bit me while building: the test suite could no
longer clean up its own committed rows, and `cli reset-ledger` broke. That is
the constraint working. `reset-ledger` now requires `--i-understand` and
temporarily disables the trigger — wiping an audit trail should be an
obviously privileged act, not something an ordinary `DELETE` does by accident.

---

## Module 5 — Requests & Classification

| # | Finding | Fix | Behaviour change |
|---|---|---|---|
| 17 | Public holidays not excluded | The **`holidays` PyPI package**, per region | **Yes** — durations shrink |
| 18 | Pending requests can double-spend a balance | `committed_days()` / `available_balance()`; pending requests reserve their days | **Yes** |
| 19 | Classification goes stale before approval | The split is stored on the request; `reclassify_request()` reports drift without overwriting it | No (additive) |
| 20 | Overlapping requests not prevented | App-level check **and** a partial GiST `EXCLUDE` constraint | **Yes** |
| 21 | Policy change mid-request not split | `policy_spans()` splits the request and classifies each span against its own policy | No (same answer when nothing changes) |
| 22 | Backdated leave only warned | Rejected unless `allow_backdated`, or an explicit `override_reason` | **Yes** |
| 23 | Minimum notice only warned | Enforced per `enforcement` | **Yes** |
| 24 | Max consecutive only warned | Enforced per `enforcement` | **Yes** |
| 25 | Unknown policy → unpaid | `PolicyMissingError`. `strict_policy=False` restores the old path | **Yes** |
| 26 | Substitution order hardcoded | `substitution_rules` table, region-aware | No |
| 27 | No half-day leave | `start_half_day` / `end_half_day`; durations in 0.5 steps | No (additive) |
| 28 | Notice measured from the system clock | Measured from `submitted_at` | **Yes** |

**On 17 — why the library and not a table.** `holidays` already knows Pongal is
a Tamil Nadu holiday, that Thanksgiving is a US one, and how the movable feasts
fall each year. A hand-maintained table would be a standing source of bugs and
someone would forget to top it up every December. The `holiday_overrides`
table covers only what a library cannot know: a company shutdown, a founding
day, or a statutory day this employer does not observe.

```
TN, 11–15 Aug 2025:  4 days   (15 Aug is Independence Day)
TX, 24–28 Nov 2025:  3 days   (Thanksgiving + the Friday after)
TN, 24–28 Nov 2025:  5 days   (neither is an Indian holiday)
```

**On 22–24 — the override.** Hard-blocking without an escape hatch means a
genuine emergency needs a DBA. `override_reason` downgrades a block to a
warning, is stored on the request, and is surfaced to approvers — so the
exception is visible rather than invisible.

---

## Module 6 — Approval Engine

Built on your uploaded `approval.py`; the rule-evaluation and routing-reason
design is kept intact.

| # | Finding | Fix |
|---|---|---|
| 29 | Approver authorization not enforced | `require_approve_step()`. Tier 1 must be the requester's **actual manager**, not merely someone with the manager role. Nobody approves their own leave, whatever their role |
| 30 | Concurrent approvals race | `SELECT … FOR UPDATE` on the step before its status is read |
| 31 | Steps store only a role | `assigned_approver_id`, resolved at chain creation; `pending_steps_for()` gives an approver their queue |
| 32 | No decision reasons | `decision_reason` on every decision |
| 33 | Manager fallback hardcoded | The tier comes from the rules table. The fallback remains as a safety net but now **logs a warning** and says so in `routing_reason`, so a broken config is visible instead of silently papered over |
| 34 | Tier conflicts not prevented | Validated at rule creation, re-checked at resolve time, and `uq(request_id, role)` at the database |
| 35 | Bad rules fail at runtime | `validate_approval_rule()` — a typo is caught when the rule is authored, not in front of an employee waiting on their leave |
| 36 | No delegation | `approval_delegations`; the audit trail records both who clicked and whose authority they used |
| 37 | No escalation | `escalate_overdue_steps()` reassigns to `escalate_to_role`. **Never auto-approves** — see below |
| 38 | No SLA | `approval_rules.sla_hours` → `due_at` on each step |
| 39 | Hook fires before commit | Transactional outbox |
| 40 | Events are process-local | Transactional outbox |
| 41 | Duplicate chains | Guard in `create_approval_steps` + `uq(request_id, tier)` + `uq(request_id, role)` |

**On 37 — escalation never auto-approves.** It moves the step to a different
person and emits an event. Whether an unattended request should be *granted* is
a policy judgement the system is not entitled to make on HR's behalf. What it
can do is stop the request sitting in a dead inbox forever, which was the
finding.

**On 39/40 — why an outbox.** The old hook had two defects. It ran listeners
*before* the caller committed, so a rollback could leave payroll notified about
an approval that never happened. And the callback list lived in one Python
process's memory, so a second worker never saw the event and a restart lost
every registration. Events are now rows written in the same transaction as the
approval: they commit together or not at all. A relay publishes them
at-least-once. The relay is deliberately **not** implemented — what it
publishes to is a deployment decision, and guessing would be worse than
leaving a clean seam.

---

## New: annual policy versioning (`app/policy_admin.py`)

You said policies change yearly and HR must be able to update them — or
explicitly continue unchanged — without a developer.

```python
plan = preview_roll_forward(session, "India-TamilNadu", 2025,
                            changes={"CL": {"entitlement_days_per_year": 9}})
print(plan.describe())      # the diff HR signs off

roll_forward_year(session, "India-TamilNadu", 2025, changes={...},
                  actor=hr_admin, change_reason="FY2026 annual review")
```

```
India-TamilNadu: leave year 2025 -> 2026
  closing 2026-03-31, opening 2026-04-01–2027-03-31
  7 policy rows, 1 changed
    CL 0.00+yr: entitlement_days_per_year 7.00 -> 9
    EL 0.00-1.00yr: carried forward unchanged
    ...
```

**A policy row is never edited once used, and never deleted.** Rolling a year
forward closes the outgoing row (`effective_to`), inserts a successor, and
links them (`supersedes_id`). So a ledger entry written in 2025 still points at
the 2025 row with the 2025 number, and *"why did I get 1.25 days that month?"*
stays answerable after HR raises the 2026 entitlement. Module 1's exclusion
constraint guarantees the windows cannot overlap.

Four deliberate choices:

- **"Continue unchanged" still writes new rows.** That records that a human
  looked at 2026 and decided it should match 2025 — a different fact from
  nobody having looked.
- **`change_reason` is required**, even when nothing changes.
- **Only `hr_admin` or `director` may run it.** An employee editing their own
  entitlement is the obvious attack.
- **Identity fields cannot change.** Region, leave type and tenure bracket
  define *which* policy a row is; changing them would create a different
  policy, not a new version of this one.

---

## What should be dynamic — and what should not

You asked me to reason about this rather than just list changes. My test is:
**will a non-engineer need to change it on a timescale shorter than a release,
and is it a business decision rather than a correctness rule?**

### Now dynamic (data, editable by HR)

| Thing | Table / column | Why it must be |
|---|---|---|
| Entitlements per year | `org_policies` + roll-forward | Changes annually, by decision, per region |
| Rounding precision | `rounding_dp` | Payroll systems differ; a 2dp region shouldn't need a deploy |
| Pro-ration convention | `proration_method` | `daily` vs `monthly` is a handbook choice, not a truth |
| Leave-year end | `leave_year_end` | April in India, January in the US |
| Notice / max-consecutive strictness | `enforcement` | Legal in one region, advisory in another |
| Backdating and overdraft | `allow_backdated`, `allow_negative_balance` | Culture and contract, not code |
| Approval routing | `approval_rules` | Thresholds move with company size |
| Approval SLAs and escalation | `sla_hours`, `escalate_to_role` | Ops tuning, changes often |
| Substitution order | `substitution_rules` | Differs per region; Texas has no CL |
| Company holidays | `holiday_overrides` | Statutory days come from the library; company days are per employer |
| Delegation | `approval_delegations` | Changes every time someone takes leave |
| Part-time patterns | `employee.employment_fraction` | Per person, changes mid-career |

### Deliberately still code

- **The substitution *algorithm*** (draw from each type in order until covered).
  The order is data; the walk is a correctness rule.
- **Sum conservation** (`paid + unpaid == requested`). Making this configurable
  would mean configuring your way into a wrong answer.
- **The append-only ledger.** A per-region "allow edits" flag would defeat the
  purpose.
- **Tenure-bracket resolution** (half-open `[min, max)`). The brackets are data;
  the matching rule is invariant.
- **The two-level policy precedence** (exception → regional). The design
  explicitly removed a third layer; re-adding configurable depth would bring
  back the conflict-resolution problem that was deleted on purpose.

### Recommended next, not built

1. **A `leave_types` catalogue table.** Types are currently string codes
   (`EL`, `CL`, `SL`, `Unpaid`) with a `CHECK`-free contract. A table would let
   HR add "Bereavement Leave" without a migration. It touches every module, so
   it is a deliberate follow-up rather than something to slip into this pass.
   **This is the biggest remaining hardcoded thing.**
2. **Working-week patterns per region.** `is_weekend()` assumes Mon–Fri
   everywhere. A Sunday–Thursday region would need a per-region pattern; none
   of the seeded regions do, so it is a documented assumption.
3. **A policy-approval workflow.** Roll-forward requires `hr_admin`, but a
   second-person sign-off on a policy change is a natural extension of the
   `approval_rules` machinery already here.
4. **Encashment on termination.** The engine reports a leaver's remaining
   balance but writes no closing entry, because whether unused leave is paid
   out or lapses differs between Tamil Nadu and Texas. Payroll's call.

---

## Behaviour changes that will surprise you

These change existing numbers or reject things that previously succeeded.
Listed so nothing is a surprise in a demo:

1. **Durations shrink.** A request spanning a public holiday is now shorter.
2. **Backdated and short-notice requests are rejected**, where they used to be
   recorded with a warning.
3. **An unknown leave type is rejected** instead of becoming unpaid leave.
4. **Missing employees raise** instead of returning a zero balance.
5. **Pending requests reserve balance**, so a second request may classify as
   partly unpaid where it previously read as fully paid.
6. **The dashboard's pending list is bounded by `as_of_date`**, so a
   historical view shows fewer requests.
7. **Ledger rows cannot be updated or deleted**, including from `psql`.
8. **Overlapping requests are refused.**
9. **Raj's SL grant is 6.027, not 8.000** (from the previous pass — he is a
   mid-year joiner under the US calendar leave year).

Every one is pinned by a test that names the finding and states the old
behaviour, so none of them can drift back silently.
