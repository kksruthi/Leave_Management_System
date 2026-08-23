# Dashboard — role-based UI with JWT auth

React + Vite frontend on a FastAPI backend, over the existing engine.
**356 tests pass.**

```bash
# 1. backend
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://leave:leave@localhost:5432/leave_engine"
export JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
alembic upgrade head
python -m app.seed && python seed_users.py && python seed_demo.py

# 2. frontend
cd web && npm install && npm run build && cd ..

# 3. run  ->  http://127.0.0.1:8000
python run_api.py
```

For frontend development with hot reload, run `python run_api.py` and
`cd web && npm run dev` side by side and use `http://localhost:5173` — Vite
proxies `/api` to the backend.

**Demo logins** — password `leave1234` for all:

| Role | Email |
|---|---|
| Employee | `priya@northbridge.example` |
| Manager | `anitha.rajan@northbridge.example` |
| HR | `fatima.khan@northbridge.example` |
| Director | `nathan.cole@northbridge.example` |

---

## The approval routing you specified

> *"Only the manager can send the request to HR… and when a manager applies for
> leave, HR should approve."*

The manager's tier-1 decision is what activates HR's tier — nobody else can
put a request in front of HR. So the button does not say "Approve" when
another tier follows; it says what actually happens:

```
Approve & send to HR     ← a further tier exists; this forwards it
Approve                  ← final tier; this grants the leave
```

`decision_options()` supplies that label, with the matching sentence above the
buttons: *"Approving does not grant this leave — it forwards the request to HR
for the next decision."* A manager who thinks they granted leave they only
forwarded is a support ticket waiting to happen.

For a requester's **own** leave, the tier-1 approver walks the role ladder:

| Requester | Tier 1 goes to | Why |
|---|---|---|
| Employee | Their manager | Normal line management |
| **Manager** | **HR** | No manager above them; they cannot approve themselves |
| HR admin | Director | Same rule, one level up |
| Director | Self-approved, and labelled as such | Nobody outranks them |

The director case is the awkward one. Leaving the step assigned to nobody
means their leave pends forever; inventing a peer approver gives someone
authority they do not have. So the chain is marked approved with the reason
*"Self-approved: requester is at the top of the approval chain"* — an
auditable fact rather than a silent gap. Four tests pin each rung.

---

## Permissions, not role checks

As you asked — explicit permissions, with roles as a shorthand for a set:

```python
employee: view_own_leave, create_leave_request, cancel_own_request
manager:  + view_team_leave, approve_team_leave, reject_team_leave
hr_admin: + view_all_leave, manage_employees, manage_policies,
            manage_holidays, manage_exceptions, view_audit,
            correct_ledger, view_reports
director: same as hr_admin (its distinct power is approval authority)
```

Two rules make this worth having:

**A permission is necessary but not sufficient.** `approve_team_leave` says a
manager may approve; it does not say *whose* requests. Row-level scoping stays
in `app/authorization.py`, which knows the reporting line. Both checks must
pass, or any manager could approve anyone's leave. The same split applies to
viewing: `/api/hr/employees/{id}` is gated on `view_team_leave` so a manager
can open their own report, and `can_view_employee` then refuses anyone outside
their line — that combination is what a test caught me getting wrong.

**A typo'd permission raises rather than passing.** `has_permission(role,
"mange_policies")` throws `KeyError` instead of quietly returning `False` and
locking everyone out of a screen nobody notices is missing.

The navigation is built from the permission list the server sends, so the menu
can never offer something the API will refuse. Client-side route guards are
cosmetic — hand-typing a URL gets a 403 from the API, not data.

---

## The calculation, shown

You were emphatic about this, and it is the part most likely to earn trust:

```
Nov 30  Mon   Leave                              1 d
Dec 01  Tue   Leave                              1 d
Dec 02  Wed   Public holiday — Christmas Day     —
Dec 03  Thu   Leave                              1 d
Dec 04  Fri   Leave                              1 d
Dec 05  Sat   Weekend                            —
Dec 06  Sun   Weekend                            —
────────────────────────────────────────────────────
Leave requested                              4 days
```

Every row is labelled with *why* it does or does not count, and the holiday is
named. `explain_days()` produces it; the same breakdown appears on the
approver's card, so both sides are looking at identical arithmetic.

The form previews live as dates change, and shows the split, the balance
before and after, and any blocking rule with the reason. When a rule blocks,
an "request an exception" box appears — the override is recorded on the
request and surfaced to approvers, so an exception is visible rather than
invisible.

---

## Privacy

| Field | Employee | Manager | HR |
|---|---|---|---|
| Own balances and history | ✓ | ✓ | ✓ |
| Team member's dates, type, duration | — | ✓ | ✓ |
| Team member's **reason/comment** | — | **✗** | ✓ |
| Entitlement exceptions | — | ✗ | ✓ |
| Policy administration | — | ✗ | ✓ |
| Audit trail | — | ✗ | ✓ |

A manager approving leave needs to know who is away and for how long. *"IVF
treatment"* is not theirs to read. The approval card shows the leave type and
a neutral marker — *"The employee attached a private note. It is visible to
them and HR, not to approvers"* — so the manager knows a note exists without
seeing it. The team calendar shows type and dates only.

Managers explicitly **cannot** change policies, entitlements, balances,
regions, FTE, or ledger rows: none of those permissions are in the manager
set, and the ledger is append-only at the database level regardless of role.

---

## Colour

Leave-type colours come from the data-viz palette and were run through its
validator rather than eyeballed:

| | Light | Dark |
|---|---|---|
| EL | `#2a78d6` | `#3987e5` |
| CL | `#eb6834` | `#d95926` |
| SL | `#1baf7a` | `#199e70` |
| Unpaid | `#4a3aa7` | `#9085e9` |

Both modes pass the CVD-separation, chroma and lightness checks. Light-mode
aqua (SL) sits below 3:1 against the surface, which triggers the palette's
**relief rule** — so every calendar bar carries a visible type label and
pending leave is hatched as well as tinted. Identity never rests on hue alone,
which also makes the calendar readable in print and for colour-blind viewers.
Status badges use the reserved status palette and always carry their word.

---

## Security notes

- **PBKDF2-SHA256**, 240k iterations, per-user salt, constant-time compare.
  Argon2id would be the better production default; PBKDF2 avoids a native
  dependency here, and the stored format carries its iteration count so the
  cost can be raised with a re-hash on next login.
- **The role in the JWT is a UI hint, never an authorisation input.** Every
  request re-reads the employee row, so a token issued before someone left HR
  does not keep HR's powers until it expires.
- **Login failures are indistinguishable.** Unknown address, wrong password
  and inactive account all return the same message, and the unknown-address
  path burns a matching hash so response timing does not leak either.
- `JWT_SECRET` **must** be set in production. The development fallback is
  random per process, which invalidates every token on restart — deliberately
  noisy, so the default cannot ship by accident.
- Tokens live in `localStorage`, which is standard for a token-based SPA but
  is XSS-readable. An httpOnly refresh-cookie split is the upgrade path if
  this ever faces the public internet.

---

## Tests — 39 new

Auth (hashing, salting, tokens, the enumeration-safe login), permissions
(cumulative roles, typo'd permission raises, employees and managers blocked
from administration), API access (every HR and team endpoint refused to an
employee, row-level scoping for managers), the full routing ladder
(employee→manager→HR→director→self), and the employee flow (day-by-day
preview, blockers reported rather than raised, rejection requires a reason,
approve labels say where the request goes next).

`pytest` now provisions its own `leave_engine_test` database automatically, so
the suite no longer competes with demo data — which it was doing, and losing.

---

## Not built

- **Ledger deduction on final approval** — that is Module 7. Approving a
  request today completes the chain and emits the outbox event; no balance
  moves yet, which is why the HR "leave consumed" tile reads 0.
- Employee create/edit forms (HR can view and search; editing is API-only).
- Reports beyond the overview tiles.
- Password reset flow — `must_change_password` is set on seeded accounts but
  not yet enforced at login.
