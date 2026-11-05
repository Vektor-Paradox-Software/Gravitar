"""add extra column via a dedicated add_column op (not declared by the model)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("widgets", sa.Column("extra", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("widgets", "extra")
