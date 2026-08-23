"""annual policy lifecycle + approval SLA configuration

Two things the review pass surfaced indirectly:

  * HR needs to review policies EVERY YEAR — raise a number, or explicitly
    decide nothing changes — without a developer. `org_policies` was already
    versioned by `effective_from`/`effective_to`, but nothing drove that
    lifecycle, recorded who changed what, or linked a new row to the one it
    replaced. See app/policy_admin.py.

  * Approval SLAs and escalation targets (findings 37, 38) belong in
    `approval_rules` alongside the routing they qualify, not in code.

Revision ID: 0004_policy_lifecycle
Revises: 0003_review_findings
"""
from alembic import op
import sqlalchemy as sa

revision = '0004_policy_lifecycle'
down_revision = '0003_review_findings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_policies", sa.Column("policy_year", sa.Integer(), nullable=True))
    op.add_column("org_policies", sa.Column("supersedes_id", sa.Integer(), nullable=True))
    op.add_column("org_policies", sa.Column("created_by_id", sa.Integer(), nullable=True))
    op.add_column("org_policies", sa.Column("change_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_org_policies_supersedes", "org_policies", "org_policies",
        ["supersedes_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(
        "fk_org_policies_created_by", "org_policies", "employee",
        ["created_by_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_org_policies_year", "org_policies", ["region", "policy_year"])

    op.add_column("approval_rules", sa.Column("sla_hours", sa.Integer(), nullable=True))
    op.add_column("approval_rules", sa.Column("escalate_to_role", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_approval_rules_sla_hours", "approval_rules", "sla_hours IS NULL OR sla_hours > 0")
    op.create_check_constraint(
        "ck_approval_rules_escalate_to_role", "approval_rules",
        "escalate_to_role IS NULL OR escalate_to_role IN ('manager', 'hr_admin', 'director')")


def downgrade() -> None:
    op.drop_constraint("ck_approval_rules_escalate_to_role", "approval_rules", type_="check")
    op.drop_constraint("ck_approval_rules_sla_hours", "approval_rules", type_="check")
    op.drop_column("approval_rules", "escalate_to_role")
    op.drop_column("approval_rules", "sla_hours")

    op.drop_index("ix_org_policies_year", table_name="org_policies")
    op.drop_constraint("fk_org_policies_created_by", "org_policies", type_="foreignkey")
    op.drop_constraint("fk_org_policies_supersedes", "org_policies", type_="foreignkey")
    op.drop_column("org_policies", "change_reason")
    op.drop_column("org_policies", "created_by_id")
    op.drop_column("org_policies", "supersedes_id")
    op.drop_column("org_policies", "policy_year")
