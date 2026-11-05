"""create wallets table with a plain numeric column

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
        "wallets",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("balance", sa.Numeric(12, 2), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("wallets")
