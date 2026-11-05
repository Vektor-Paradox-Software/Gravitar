"""create widgets table with a legacy column

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("legacy", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("widgets")
