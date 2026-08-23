"""authentication and directory fields

Adds what a login and a role-based UI need, and nothing more:

  * password_hash        — PBKDF2-SHA256, salted, per user
  * department           — HR filters and reports by it
  * last_login_at        — basic account hygiene
  * must_change_password — seeded demo accounts start true

Revision ID: 0005_auth
Revises: 0004_policy_lifecycle
"""
from alembic import op
import sqlalchemy as sa

revision = '0005_auth'
down_revision = '0004_policy_lifecycle'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employee", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column("employee", sa.Column("department", sa.String(64), nullable=True))
    op.add_column("employee", sa.Column(
        "last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("employee", sa.Column(
        "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_employee_department", "employee", ["department"])


def downgrade() -> None:
    op.drop_index("ix_employee_department", table_name="employee")
    op.drop_column("employee", "must_change_password")
    op.drop_column("employee", "last_login_at")
    op.drop_column("employee", "department")
    op.drop_column("employee", "password_hash")
