"""drop legacy column via a dedicated drop_column op (model still declares it)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.drop_column("widgets", "legacy")


def downgrade() -> None:
    op.add_column("widgets", sa.Column("legacy", sa.String(length=20), nullable=True))
