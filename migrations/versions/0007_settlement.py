"""store the per-source classification breakdown on the request

Settlement writes one ledger row per SOURCE leave type. A 6-day EL request
covered by 3 EL + 3 CL must produce two deductions, so the breakdown has to
survive from submission to approval — a paid/unpaid total is not enough.

Revision ID: 0007_settlement
Revises: 0006_dynamic_admin
"""
from alembic import op
import sqlalchemy as sa

revision = '0007_settlement'
down_revision = '0006_dynamic_admin'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leave_request",
                  sa.Column("classification_sources", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("leave_request", "classification_sources")
