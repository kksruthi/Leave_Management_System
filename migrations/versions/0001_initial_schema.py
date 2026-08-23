"""initial schema — Module 1 data layer

Creates the seven tables the Leave Engine is built on, plus the two
write-time controls that are the whole of "policy conflict checking" in
this system:

  1. ck_org_policies_compliance_note  — compliance_note must be non-empty
  2. ex_org_policies_no_overlap       — no two org_policies rows may cover
     the same (region, leave_type_id) with overlapping tenure brackets AND
     overlapping effective date windows

Revision ID: 0001_initial_schema
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = '0001_initial_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Needed so a GiST exclusion constraint can mix scalar equality (=) on
    # region/leave_type_id with range overlap (&&) in the same index.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table('approval_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('condition_field', sa.String(length=32), nullable=False),
    sa.Column('operator', sa.String(length=4), nullable=False),
    sa.Column('value', sa.String(length=64), nullable=False),
    sa.Column('adds_tier', sa.String(length=16), nullable=False),
    sa.Column('tier_order', sa.Integer(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.CheckConstraint("adds_tier IN ('manager', 'hr_admin', 'director')", name='ck_approval_rules_adds_tier'),
    sa.CheckConstraint("operator IN ('>', '>=', '<', '<=', '==', '!=')", name='ck_approval_rules_operator'),
    sa.CheckConstraint('tier_order >= 1', name='ck_approval_rules_tier_order'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('condition_field', 'operator', 'value', 'adds_tier', name='uq_approval_rules_condition')
    )
    op.create_table('employee',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('join_date', sa.Date(), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('manager_id', sa.Integer(), nullable=True),
    sa.CheckConstraint('manager_id IS NULL OR manager_id <> id', name='ck_employee_not_own_manager'),
    sa.ForeignKeyConstraint(['manager_id'], ['employee.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_index('ix_employee_region', 'employee', ['region'], unique=False)
    op.create_table('org_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('legal_entity', sa.String(length=128), nullable=False),
    sa.Column('leave_type_id', sa.String(length=16), nullable=False),
    sa.Column('tenure_min_years', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('tenure_max_years', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('entitlement_days_per_year', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('is_paid', sa.Boolean(), nullable=False),
    sa.Column('accrual_method', sa.String(length=16), nullable=False),
    sa.Column('carryover_max_days', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('carryover_expiry', sa.String(length=5), nullable=True),
    sa.Column('max_consecutive_days', sa.Integer(), nullable=True),
    sa.Column('min_notice_days', sa.Integer(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.Column('compliance_note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("accrual_method IN ('monthly', 'annual_lump', 'none')", name='ck_org_policies_accrual_method'),
    sa.CheckConstraint("carryover_expiry IS NULL OR carryover_expiry ~ '^[0-1][0-9]-[0-3][0-9]$'", name='ck_org_policies_carryover_expiry_format'),
    sa.CheckConstraint('carryover_max_days >= 0', name='ck_org_policies_carryover_nonneg'),
    sa.CheckConstraint('effective_to IS NULL OR effective_to >= effective_from', name='ck_org_policies_effective_range'),
    sa.CheckConstraint('entitlement_days_per_year >= 0', name='ck_org_policies_entitlement_nonneg'),
    sa.CheckConstraint('length(btrim(compliance_note)) > 0', name='ck_org_policies_compliance_note'),
    sa.CheckConstraint('min_notice_days >= 0', name='ck_org_policies_notice_nonneg'),
    sa.CheckConstraint('tenure_max_years IS NULL OR tenure_max_years > tenure_min_years', name='ck_org_policies_tenure_range'),
    sa.CheckConstraint('tenure_min_years >= 0', name='ck_org_policies_tenure_min_nonneg'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_org_policies_lookup', 'org_policies', ['region', 'leave_type_id', 'tenure_min_years'], unique=False)
    op.create_table('employee_exceptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('leave_type_id', sa.String(length=16), nullable=False),
    sa.Column('entitlement_days_per_year', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('approved_by', sa.Integer(), nullable=True),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.CheckConstraint('effective_to IS NULL OR effective_to >= effective_from', name='ck_employee_exceptions_effective_range'),
    sa.CheckConstraint('entitlement_days_per_year >= 0', name='ck_employee_exceptions_entitlement'),
    sa.CheckConstraint('length(btrim(reason)) > 0', name='ck_employee_exceptions_reason'),
    sa.ForeignKeyConstraint(['approved_by'], ['employee.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_employee_exceptions_lookup', 'employee_exceptions', ['employee_id', 'leave_type_id'], unique=False)
    op.create_table('leave_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('leave_type_id', sa.String(length=16), nullable=False),
    sa.Column('amount', sa.Numeric(precision=8, scale=3), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('policy_snapshot_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount <> 0', name='ck_leave_ledger_amount_nonzero'),
    sa.CheckConstraint('length(btrim(reason)) > 0', name='ck_leave_ledger_reason'),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['policy_snapshot_id'], ['org_policies.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_leave_ledger_balance', 'leave_ledger', ['employee_id', 'leave_type_id', 'effective_date'], unique=False)
    op.create_table('leave_request',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('leave_type_id', sa.String(length=16), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('duration_days', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name='ck_leave_request_status'),
    sa.CheckConstraint('duration_days > 0', name='ck_leave_request_duration_positive'),
    sa.CheckConstraint('end_date >= start_date', name='ck_leave_request_date_order'),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_leave_request_employee', 'leave_request', ['employee_id', 'start_date'], unique=False)
    op.create_table('approval_steps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.Integer(), nullable=False),
    sa.Column('tier', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('routing_reason', sa.Text(), nullable=True),
    sa.Column('acted_by', sa.Integer(), nullable=True),
    sa.Column('acted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("role IN ('manager', 'hr_admin', 'director')", name='ck_approval_steps_role'),
    sa.CheckConstraint("status IN ('pending', 'active', 'approved', 'rejected')", name='ck_approval_steps_status'),
    sa.CheckConstraint('tier >= 1', name='ck_approval_steps_tier'),
    sa.ForeignKeyConstraint(['acted_by'], ['employee.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['request_id'], ['leave_request.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('request_id', 'tier', name='uq_approval_steps_request_tier')
    )

    # ------------------------------------------------------------------
    # The overlapping-tenure-range guard.
    #
    # Two policy rows collide only if ALL of these are true at once:
    #   same region, same leave type,
    #   their tenure brackets overlap   -> numrange, [min, max) half-open so
    #                                      0-1yr and 1-3yr sit flush, not overlapping
    #   their effective windows overlap -> daterange, [from, to] inclusive
    #
    # NULL tenure_max_years / effective_to become unbounded ends automatically,
    # so "5+ years" and "no end date" are handled without special-casing.
    # ------------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE org_policies
        ADD CONSTRAINT ex_org_policies_no_overlap
        EXCLUDE USING gist (
            region WITH =,
            leave_type_id WITH =,
            numrange(tenure_min_years, tenure_max_years, '[)') WITH &&,
            daterange(effective_from, effective_to, '[]') WITH &&
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE org_policies DROP CONSTRAINT IF EXISTS ex_org_policies_no_overlap")
    op.drop_table('approval_steps')
    op.drop_index('ix_leave_request_employee', table_name='leave_request')
    op.drop_table('leave_request')
    op.drop_index('ix_leave_ledger_balance', table_name='leave_ledger')
    op.drop_table('leave_ledger')
    op.drop_index('ix_employee_exceptions_lookup', table_name='employee_exceptions')
    op.drop_table('employee_exceptions')
    op.drop_index('ix_org_policies_lookup', table_name='org_policies')
    op.drop_table('org_policies')
    op.drop_index('ix_employee_region', table_name='employee')
    op.drop_table('employee')
    op.drop_table('approval_rules')
