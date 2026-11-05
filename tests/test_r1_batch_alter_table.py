"""SPEC-16 R1: batch_alter_table support.

Pre-R1, the parser's flat ``ast.walk`` matched calls on the literal
receiver ``op`` only, so every ``batch_op.add_column(...)`` inside a
``with op.batch_alter_table("widgets") as batch_op:`` was invisible —
readiness doc D1 pins this as the dominant source of the pre-amendment
113 false error findings (526 batch blocks across the corpus).

R1 adds a batch-aware statement walker: a ``with`` on
``op.batch_alter_table(T)`` binds the batch variable to ``T`` for its
body, and ``batch_op.add_column``/``alter_column``/``drop_column``
calls are translated to the equivalent op.* with ``table=T``.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.schema.migrations import build_migration_result
from gravitar.schema.parsers.alembic_parser import parse_versions_dir


def _write(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(body, encoding="utf-8")


def test_batch_add_column_is_applied_to_schema_state(tmp_path: Path) -> None:
    """The headline R1 case: a batch_op.add_column inside a with block
    must mutate the schema state. Pre-R1 it was silently dropped."""
    _write(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    _write(
        tmp_path,
        "0002_batch_add.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
""",
    )

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert "deleted_at" in result.schema["widgets"]["columns"]
    assert result.schema["widgets"]["columns"]["deleted_at"]["nullable"] is True


def test_batch_drop_column_is_applied(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("legacy", sa.String(length=50), nullable=True),
    )
""",
    )
    _write(
        tmp_path,
        "0002_batch_drop.py",
        """
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.drop_column("legacy")
""",
    )

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert "legacy" not in result.schema["widgets"]["columns"]


def test_batch_alter_column_applies_type_and_nullability(tmp_path: Path) -> None:
    """``batch_op.alter_column(col, type_=..., nullable=...)`` — note the
    column is args[0] here (unlike native ``op.alter_column`` where it's
    args[1] after the table). R1's resolver has to know that."""
    _write(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
    )
""",
    )
    _write(
        tmp_path,
        "0002_batch_alter.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.alter_column("name", type_=sa.String(length=200), nullable=True)
""",
    )

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert result.schema["widgets"]["columns"]["name"]["type"] == "varchar(200)"
    assert result.schema["widgets"]["columns"]["name"]["nullable"] is True


def test_batch_variable_binding_name_is_arbitrary(tmp_path: Path) -> None:
    """R1 spec: ``batch_op`` is a convention, not a requirement. ``bop``,
    ``b``, anything readable should work."""
    _write(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    _write(
        tmp_path,
        "0002_bop.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("widgets") as bop:
        bop.add_column(sa.Column("extra", sa.Integer(), nullable=True))
""",
    )

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert "extra" in result.schema["widgets"]["columns"]


def test_batch_add_drop_alter_in_one_block(tmp_path: Path) -> None:
    """SPEC-16 R1 acceptance: one batch block with multiple method calls
    must apply all of them, in order."""
    _write(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("legacy", sa.String(length=20), nullable=True),
    )
""",
    )
    _write(
        tmp_path,
        "0002_batch_mixed.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.add_column(sa.Column("flag", sa.Boolean(), nullable=True))
        batch_op.alter_column("name", type_=sa.String(length=200))
        batch_op.drop_column("legacy")
""",
    )

    result = build_migration_result(parse_versions_dir(tmp_path))

    columns = result.schema["widgets"]["columns"]
    assert "flag" in columns
    assert columns["name"]["type"] == "varchar(200)"
    assert "legacy" not in columns


def test_batch_inert_methods_do_not_affect_schema(tmp_path: Path) -> None:
    """``batch_op.create_index`` et al. are on the known-inert list — they
    don't change column existence/type/nullability. Gravitar doesn't
    model indexes, so these are silent (correctly understood)."""
    _write(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "widgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=50)),
    )
""",
    )
    _write(
        tmp_path,
        "0002_batch_index.py",
        """
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.create_index("ix_widgets_name", ["name"])
""",
    )

    result = build_migration_result(parse_versions_dir(tmp_path))

    # Only one op (create_table) actually represented in schema.
    assert set(result.schema["widgets"]["columns"]) == {"id", "name"}
