"""review findings 9-41 — enforcement, calendars, approvals, integrity

Closes the second review pass across Modules 4, 5 and 6. Every column here
has a default that preserves existing behaviour, EXCEPT the deliberate
behaviour changes called out in the README (hard enforcement of notice and
max-consecutive limits, and rejection of backdated leave).

The four DB-level guarantees, which is the point of doing this in the schema
rather than in application code:

  * `ex_leave_request_no_overlap`  — an employee cannot hold two open
    requests covering the same day (finding 20).
  * `trg_leave_ledger_append_only` — the ledger cannot be UPDATEd or DELETEd
    by anyone, including a psql prompt (finding 16).
  * `uq_approval_steps_request_tier` (already present) plus
    `uq_approval_steps_request_role` — a request cannot get two chains or two
    steps for the same role (findings 34, 41).
  * `ck_leave_request_split_conserves` — a stored paid/unpaid split must sum
    to the duration.

Revision ID: 0003_review_findings
Revises: 0002_proration_parttime_rounding
"""
from alembic import op
import sqlalchemy as sa

revision = '0003_review_findings'
down_revision = '0002_proration_parttime_rounding'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ======================================================================
    # org_policies — enforcement is policy data (findings 11, 22, 23, 24)
    # ======================================================================
    op.add_column("org_policies", sa.Column(
        "enforcement", sa.String(8), nullable=False, server_default="block"))
    op.add_column("org_policies", sa.Column(
        "allow_backdated", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("org_policies", sa.Column(
        "allow_negative_balance", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint(
        "ck_org_policies_enforcement", "org_policies", "enforcement IN ('block', 'warn')")

    # ======================================================================
    # employee — directory role drives authorization (findings 10, 29)
    # ======================================================================
    op.add_column("employee", sa.Column(
        "role", sa.String(16), nullable=False, server_default="employee"))
    op.create_check_constraint(
        "ck_employee_role", "employee",
        "role IN ('employee', 'manager', 'hr_admin', 'director')")
    # Anyone who already manages someone is a manager.
    op.execute("""
        UPDATE employee SET role = 'manager'
        WHERE id IN (SELECT DISTINCT manager_id FROM employee WHERE manager_id IS NOT NULL)
    """)

    # ======================================================================
    # leave_request — half days, submission time, split snapshot (19, 27, 28)
    # ======================================================================
    op.add_column("leave_request", sa.Column(
        "start_half_day", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("leave_request", sa.Column(
        "end_half_day", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("leave_request", sa.Column(
        "submitted_at", sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()")))
    op.add_column("leave_request", sa.Column("paid_days", sa.Numeric(6, 2), nullable=True))
    op.add_column("leave_request", sa.Column("unpaid_days", sa.Numeric(6, 2), nullable=True))
    op.add_column("leave_request", sa.Column(
        "classified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leave_request", sa.Column("override_reason", sa.Text(), nullable=True))

    # Existing rows predate the submitted_at column; backfill from created_at
    # so notice-period arithmetic on historical requests stays sane.
    op.execute("UPDATE leave_request SET submitted_at = created_at")

    op.execute("""
        ALTER TABLE leave_request DROP CONSTRAINT IF EXISTS ck_leave_request_status
    """)
    op.create_check_constraint(
        "ck_leave_request_status", "leave_request",
        "status IN ('pending', 'approved', 'rejected', 'cancelled')")
    op.create_check_constraint(
        "ck_leave_request_split_conserves", "leave_request",
        "paid_days IS NULL OR unpaid_days IS NULL "
        "OR paid_days + unpaid_days = duration_days")
    op.create_index("ix_leave_request_open", "leave_request", ["employee_id", "status"])

    # --- finding 20: no two OPEN requests may cover the same day ----------
    # Only pending and approved requests reserve time; a rejected or cancelled
    # one releases its dates, which is why the constraint is partial.
    op.execute("""
        ALTER TABLE leave_request
        ADD CONSTRAINT ex_leave_request_no_overlap
        EXCLUDE USING gist (
            employee_id WITH =,
            daterange(start_date, end_date, '[]') WITH &&
        )
        WHERE (status IN ('pending', 'approved'))
    """)

    # ======================================================================
    # leave_ledger — carry-over buckets and expiry (findings 9, 14)
    # ======================================================================
    op.add_column("leave_ledger", sa.Column(
        "bucket", sa.String(16), nullable=False, server_default="current"))
    op.add_column("leave_ledger", sa.Column("expires_on", sa.Date(), nullable=True))
    op.create_check_constraint(
        "ck_leave_ledger_bucket", "leave_ledger", "bucket IN ('current', 'carryover')")
    op.create_index(
        "ix_leave_ledger_balance_expiry", "leave_ledger",
        ["employee_id", "leave_type_id", "effective_date", "expires_on"])

    # --- finding 16: the ledger is append-only, enforced by the database ---
    # An audit trail that can be silently rewritten is not an audit trail.
    # This blocks UPDATE and DELETE for every caller, including psql. A
    # correction is made by appending a reversing entry, which leaves both
    # the error and the fix visible.
    op.execute("""
        CREATE OR REPLACE FUNCTION leave_ledger_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'leave_ledger is append-only: % on row id=% is not permitted. '
                'Append a reversing entry instead.',
                TG_OP, OLD.id
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_leave_ledger_append_only
        BEFORE UPDATE OR DELETE ON leave_ledger
        FOR EACH ROW EXECUTE FUNCTION leave_ledger_append_only()
    """)

    # ======================================================================
    # approval_steps — ownership, reasons, SLA (findings 31, 32, 34, 37, 38, 41)
    # ======================================================================
    op.add_column("approval_steps", sa.Column("assigned_approver_id", sa.Integer(), nullable=True))
    op.add_column("approval_steps", sa.Column("decision_reason", sa.Text(), nullable=True))
    op.add_column("approval_steps", sa.Column(
        "activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approval_steps", sa.Column(
        "due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approval_steps", sa.Column(
        "escalated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approval_steps", sa.Column("acted_on_behalf_of", sa.Integer(), nullable=True))

    op.create_foreign_key(
        "fk_approval_steps_assigned_approver", "approval_steps", "employee",
        ["assigned_approver_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(
        "fk_approval_steps_on_behalf_of", "approval_steps", "employee",
        ["acted_on_behalf_of"], ["id"], ondelete="SET NULL")

    # findings 34 + 41: one step per role per request, so a chain cannot be
    # created twice and two rules cannot both add the same role at
    # different tiers. uq(request_id, tier) already existed; this closes the
    # other axis.
    op.create_unique_constraint(
        "uq_approval_steps_request_role", "approval_steps", ["request_id", "role"])

    # ======================================================================
    # New tables
    # ======================================================================
    op.create_table(
        "holiday_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_working_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_holiday_overrides_name"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("region", "holiday_date", name="uq_holiday_overrides_region_date"),
    )
    op.create_index("ix_holiday_overrides_lookup", "holiday_overrides", ["region", "holiday_date"])

    op.create_table(
        "substitution_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("leave_type_id", sa.String(16), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fallback_leave_type_id", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("position >= 0", name="ck_substitution_rules_position"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("leave_type_id", "region", "position",
                            name="uq_substitution_rules_position"),
    )
    op.create_index("ix_substitution_rules_lookup", "substitution_rules",
                    ["leave_type_id", "region", "position"])

    op.create_table(
        "approval_delegations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("delegator_id", sa.Integer(), nullable=False),
        sa.Column("delegate_id", sa.Integer(), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("to_date >= from_date", name="ck_approval_delegations_dates"),
        sa.CheckConstraint("delegator_id <> delegate_id", name="ck_approval_delegations_distinct"),
        sa.ForeignKeyConstraint(["delegator_id"], ["employee.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delegate_id"], ["employee.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_delegations_lookup", "approval_delegations",
                    ["delegator_id", "from_date", "to_date"])

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_unpublished", "outbox", ["published_at", "id"])
    op.create_index("ix_outbox_aggregate", "outbox", ["aggregate_type", "aggregate_id"])


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("approval_delegations")
    op.drop_table("substitution_rules")
    op.drop_table("holiday_overrides")

    op.drop_constraint("uq_approval_steps_request_role", "approval_steps", type_="unique")
    op.drop_constraint("fk_approval_steps_on_behalf_of", "approval_steps", type_="foreignkey")
    op.drop_constraint("fk_approval_steps_assigned_approver", "approval_steps", type_="foreignkey")
    for column in ("acted_on_behalf_of", "escalated_at", "due_at", "activated_at",
                   "decision_reason", "assigned_approver_id"):
        op.drop_column("approval_steps", column)

    op.execute("DROP TRIGGER IF EXISTS trg_leave_ledger_append_only ON leave_ledger")
    op.execute("DROP FUNCTION IF EXISTS leave_ledger_append_only()")
    op.drop_index("ix_leave_ledger_balance_expiry", table_name="leave_ledger")
    op.drop_constraint("ck_leave_ledger_bucket", "leave_ledger", type_="check")
    op.drop_column("leave_ledger", "expires_on")
    op.drop_column("leave_ledger", "bucket")

    op.execute("ALTER TABLE leave_request DROP CONSTRAINT IF EXISTS ex_leave_request_no_overlap")
    op.drop_index("ix_leave_request_open", table_name="leave_request")
    op.drop_constraint("ck_leave_request_split_conserves", "leave_request", type_="check")
    op.execute("ALTER TABLE leave_request DROP CONSTRAINT IF EXISTS ck_leave_request_status")
    op.create_check_constraint(
        "ck_leave_request_status", "leave_request",
        "status IN ('pending', 'approved', 'rejected')")
    for column in ("override_reason", "classified_at", "unpaid_days", "paid_days",
                   "submitted_at", "end_half_day", "start_half_day"):
        op.drop_column("leave_request", column)

    op.drop_constraint("ck_employee_role", "employee", type_="check")
    op.drop_column("employee", "role")

    op.drop_constraint("ck_org_policies_enforcement", "org_policies", type_="check")
    op.drop_column("org_policies", "allow_negative_balance")
    op.drop_column("org_policies", "allow_backdated")
    op.drop_column("org_policies", "enforcement")
