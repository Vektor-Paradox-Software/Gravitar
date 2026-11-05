"""create orders table

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-22 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Numeric(10, 2), nullable=False),
    )
    op.alter_column("orders", "total_cents", existing_type=sa.Numeric(10, 2), nullable=False)


def downgrade() -> None:
    op.drop_table("orders")
