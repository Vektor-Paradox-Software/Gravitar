"""fully-resolved + pattern-matched raw SQL + genuinely unresolvable raw SQL,
all in one migration file (Scenario D)

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    # Fully resolved: model also declares "extra", which this doesn't add.
    op.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
    )
    # Pattern-matched raw SQL (M8's sql:add_column).
    op.execute("ALTER TABLE widgets ADD COLUMN flag BOOLEAN")
    # Genuinely unresolvable: a non-literal argument the AST walker can't see
    # into at all, not merely an unrecognized-but-visible SQL string.
    statement = "UPDATE widgets SET flag = true"
    op.execute(statement)


def downgrade() -> None:
    op.drop_table("widgets")
