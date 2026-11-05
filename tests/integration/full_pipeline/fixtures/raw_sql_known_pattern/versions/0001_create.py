"""create widgets, then a raw-SQL ADD COLUMN matching a known M8 pattern

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), nullable=False, primary_key=True))
    op.execute("ALTER TABLE widgets ADD COLUMN flag BOOLEAN")


def downgrade() -> None:
    op.drop_table("widgets")
