"""a revision whose down_revision references a migration file that doesn't exist
(e.g. deleted, renamed, or never merged)

Revision ID: 0001
Revises: 0000_never_existed
Create Date: 2026-08-03 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = "0000_never_existed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orphan_start",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("orphan_start")
