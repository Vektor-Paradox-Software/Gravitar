"""create customers table with middle_name as NOT NULL, model says nullable

Revision ID: 0001
Revises:
Create Date: 2026-08-03 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("middle_name", sa.String(length=50), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("customers")
