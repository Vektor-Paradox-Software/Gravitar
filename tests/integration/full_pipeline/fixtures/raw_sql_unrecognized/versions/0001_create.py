"""create widgets, then a raw-SQL statement no M8 pattern recognizes

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), nullable=False, primary_key=True))
    op.execute("UPDATE widgets SET id = id")


def downgrade() -> None:
    op.drop_table("widgets")
