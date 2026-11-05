"""create widgets and gadgets, neither with the model's extra column

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
    )
    op.create_table(
        "gadgets",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("title", sa.String(length=50), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("widgets")
    op.drop_table("gadgets")
