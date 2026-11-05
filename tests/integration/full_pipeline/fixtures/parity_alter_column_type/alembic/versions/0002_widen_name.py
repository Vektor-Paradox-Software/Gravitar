"""widen name via a dedicated alter_column op (model still declares varchar(50))

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.alter_column("widgets", "name", type_=sa.String(length=100))


def downgrade() -> None:
    op.alter_column("widgets", "name", type_=sa.String(length=50))
