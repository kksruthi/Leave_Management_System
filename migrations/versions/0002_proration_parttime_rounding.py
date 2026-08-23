"""pro-ration, part-time and explicit rounding

Closes four gaps found in review of Modules 1-3:

  1. No pro-ration for employees who join partway through an accrual year
     -> org_policies.proration_method, plus employee.exit_date so the same
        machinery covers leavers.
  2. Part-time entitlement undefined
     -> employee.employment_fraction (1.000 = full-time). Entitlement is
        scaled at resolve time, so one policy row serves every schedule.
  3. Termination / final accrual undefined
     -> employee.status + employee.exit_date. Accrual stops at exit and the
        final partial cycle is pro-rated.
  4. Rounding rules not explicitly defined
     -> org_policies.rounding_dp. The convention was previously a constant in
        accrual.py; it is now policy data, per region, visible to HR.

All four columns have defaults that preserve existing behaviour exactly:
every current employee becomes 1.000 FTE / active / no exit date, and every
current policy row becomes rounding_dp=3 / proration_method='daily'.

Revision ID: 0002_proration_parttime_rounding
Revises: 0001_initial_schema
"""
from alembic import op
import sqlalchemy as sa

revision = '0002_proration_parttime_rounding'
down_revision = '0001_initial_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # employee: working pattern and end of service
    # ------------------------------------------------------------------
    op.add_column(
        "employee",
        sa.Column(
            "employment_fraction",
            sa.Numeric(4, 3),
            nullable=False,
            server_default="1.000",
        ),
    )
    op.add_column(
        "employee",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column("employee", sa.Column("exit_date", sa.Date(), nullable=True))

    op.create_check_constraint(
        "ck_employee_employment_fraction",
        "employee",
        "employment_fraction > 0 AND employment_fraction <= 1",
    )
    op.create_check_constraint(
        "ck_employee_status", "employee", "status IN ('active', 'terminated')"
    )
    op.create_check_constraint(
        "ck_employee_exit_after_join", "employee", "exit_date IS NULL OR exit_date >= join_date"
    )
    op.create_check_constraint(
        "ck_employee_terminated_needs_exit_date",
        "employee",
        "status <> 'terminated' OR exit_date IS NOT NULL",
    )
    op.create_index("ix_employee_status", "employee", ["status"])

    # ------------------------------------------------------------------
    # org_policies: rounding and pro-ration as data, not code
    # ------------------------------------------------------------------
    op.add_column(
        "org_policies",
        sa.Column("rounding_dp", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "org_policies",
        sa.Column(
            "proration_method", sa.String(16), nullable=False, server_default="daily"
        ),
    )
    op.create_check_constraint(
        "ck_org_policies_rounding_dp", "org_policies", "rounding_dp BETWEEN 0 AND 3"
    )
    op.create_check_constraint(
        "ck_org_policies_proration_method",
        "org_policies",
        "proration_method IN ('daily', 'monthly', 'none')",
    )

    # The organisation's leave year end, "MM-DD". NULL keeps the previous
    # anniversary-aligned behaviour, so this column is a no-op until seeded.
    op.add_column("org_policies", sa.Column("leave_year_end", sa.String(5), nullable=True))
    op.create_check_constraint(
        "ck_org_policies_leave_year_end_format",
        "org_policies",
        "leave_year_end IS NULL OR leave_year_end ~ '^[0-1][0-9]-[0-3][0-9]$'",
    )

    # Annual-lump types get a fixed regional leave year; monthly types stay
    # anniversary-aligned, since partial service is handled month by month.
    op.execute("""
        UPDATE org_policies SET leave_year_end = '03-31'
        WHERE region LIKE 'India-%' AND accrual_method = 'annual_lump'
    """)
    op.execute("""
        UPDATE org_policies SET leave_year_end = '12-31'
        WHERE region LIKE 'USA-%' AND accrual_method = 'annual_lump'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE org_policies DROP CONSTRAINT IF EXISTS ck_org_policies_leave_year_end_format")
    op.drop_column("org_policies", "leave_year_end")
    op.drop_constraint("ck_org_policies_proration_method", "org_policies", type_="check")
    op.drop_constraint("ck_org_policies_rounding_dp", "org_policies", type_="check")
    op.drop_column("org_policies", "proration_method")
    op.drop_column("org_policies", "rounding_dp")

    op.drop_index("ix_employee_status", table_name="employee")
    op.drop_constraint("ck_employee_terminated_needs_exit_date", "employee", type_="check")
    op.drop_constraint("ck_employee_exit_after_join", "employee", type_="check")
    op.drop_constraint("ck_employee_status", "employee", type_="check")
    op.drop_constraint("ck_employee_employment_fraction", "employee", type_="check")
    op.drop_column("employee", "exit_date")
    op.drop_column("employee", "status")
    op.drop_column("employee", "employment_fraction")
