"""leave-type catalogue, notifications, forwarding, split reason columns

Five changes, each closing a review point or enabling an admin feature:

  * `leave_types`      — leave types were string codes with no catalogue, so
                         adding "Bereavement Leave" meant a code change. Now
                         HR adds a row.
  * `notifications`    — approvers, requesters and policy changes need to
                         reach people.
  * reason columns     — `override_reason` was carrying THREE distinct
                         concepts (employee comment, policy override
                         justification, cancellation reason). A private
                         comment could make the system believe a policy
                         override existed. Split into three columns.
  * `forwarded` status — approvers now route requests onward explicitly, so a
                         step can end in "forwarded" rather than only
                         approved/rejected.
  * `forwarded_to_*`   — records who sent it on, and to whom.

Revision ID: 0006_dynamic_admin
Revises: 0005_auth
"""
from alembic import op
import sqlalchemy as sa

revision = '0006_dynamic_admin'
down_revision = '0005_auth'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # leave_types — the catalogue HR can extend
    # ------------------------------------------------------------------
    op.create_table(
        "leave_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color_token", sa.String(16), nullable=False, server_default="series-1"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # A type flagged as the unpaid fallback never draws on a balance.
        sa.Column("is_unpaid_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("length(btrim(code)) > 0", name="ck_leave_types_code"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_leave_types_name"),
        sa.ForeignKeyConstraint(["created_by_id"], ["employee.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_leave_types_code"),
    )
    # Backfill from whatever the policies already reference, so nothing breaks.
    op.execute("""
        INSERT INTO leave_types (code, name, description, color_token, display_order,
                                 is_unpaid_fallback)
        SELECT DISTINCT leave_type_id,
               CASE leave_type_id
                   WHEN 'EL' THEN 'Earned Leave'
                   WHEN 'CL' THEN 'Casual Leave'
                   WHEN 'SL' THEN 'Sick Leave'
                   WHEN 'Unpaid' THEN 'Unpaid Leave'
                   ELSE leave_type_id END,
               CASE leave_type_id
                   WHEN 'EL' THEN 'Accrues monthly and scales with tenure. The main annual leave allowance.'
                   WHEN 'CL' THEN 'Granted as a lump sum each leave year for short, unplanned absences.'
                   WHEN 'SL' THEN 'Granted as a lump sum each leave year for illness.'
                   WHEN 'Unpaid' THEN 'Loss of pay. No balance required; the final fallback.'
                   ELSE NULL END,
               CASE leave_type_id
                   WHEN 'EL' THEN 'series-1' WHEN 'CL' THEN 'series-2'
                   WHEN 'SL' THEN 'series-3' ELSE 'series-4' END,
               CASE leave_type_id
                   WHEN 'EL' THEN 10 WHEN 'CL' THEN 20
                   WHEN 'SL' THEN 30 ELSE 90 END,
               leave_type_id = 'Unpaid'
        FROM org_policies
        ON CONFLICT (code) DO NOTHING
    """)

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("link", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_inbox", "notifications",
                    ["employee_id", "read_at", "id"])

    # ------------------------------------------------------------------
    # leave_request — three reasons, three columns
    # ------------------------------------------------------------------
    op.add_column("leave_request", sa.Column("employee_reason", sa.Text(), nullable=True))
    op.add_column("leave_request", sa.Column("cancellation_reason", sa.Text(), nullable=True))
    # Existing rows: the column held employee comments far more often than
    # genuine overrides, so move it to employee_reason and leave override NULL.
    op.execute("UPDATE leave_request SET employee_reason = override_reason "
               "WHERE override_reason IS NOT NULL")
    op.execute("UPDATE leave_request SET override_reason = NULL")

    # ------------------------------------------------------------------
    # approval_steps — explicit forwarding
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE approval_steps DROP CONSTRAINT IF EXISTS ck_approval_steps_status")
    op.create_check_constraint(
        "ck_approval_steps_status", "approval_steps",
        "status IN ('pending', 'active', 'approved', 'rejected', 'forwarded')")
    op.add_column("approval_steps", sa.Column("forwarded_to_role", sa.String(16), nullable=True))
    op.add_column("approval_steps", sa.Column("forwarded_to_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_approval_steps_forwarded_to", "approval_steps", "employee",
                          ["forwarded_to_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_approval_steps_forwarded_to", "approval_steps", type_="foreignkey")
    op.drop_column("approval_steps", "forwarded_to_id")
    op.drop_column("approval_steps", "forwarded_to_role")
    op.execute("ALTER TABLE approval_steps DROP CONSTRAINT IF EXISTS ck_approval_steps_status")
    op.create_check_constraint(
        "ck_approval_steps_status", "approval_steps",
        "status IN ('pending', 'active', 'approved', 'rejected')")

    op.execute("UPDATE leave_request SET override_reason = employee_reason "
               "WHERE employee_reason IS NOT NULL")
    op.drop_column("leave_request", "cancellation_reason")
    op.drop_column("leave_request", "employee_reason")

    op.drop_index("ix_notifications_inbox", table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("leave_types")
