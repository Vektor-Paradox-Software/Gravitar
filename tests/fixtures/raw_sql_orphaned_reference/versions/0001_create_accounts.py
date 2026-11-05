"""create accounts table, then a raw-SQL ADD COLUMN against a table that doesn't exist

Revision ID: 0001
Revises:
Create Date: 2026-08-17 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
    )
    op.execute("ALTER TABLE ghost_table ADD COLUMN nickname VARCHAR(50)")


def downgrade() -> None:
    op.drop_table("accounts")
