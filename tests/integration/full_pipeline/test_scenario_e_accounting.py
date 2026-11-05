"""Scenario E (SPEC-12 Amendment v1.1 §3): accounting identity.

The transparency invariant T1 ("No silent skips") is testable:
    ``nodes_seen == applied + inert_count + noted``
for every revision Gravitar parses, through either DSL.

A "node" is a top-level migration operation — an ``op.*``/``batch_op.*``
call in Alembic's ``upgrade()`` body, or an element of Django's
``Migration.operations`` list. Each must land in exactly one bucket:
applied (an ``Operation`` the schema builder will replay), inert
(correctly understood but not representable in Gravitar's Schema —
indexes, constraints, bulk inserts), or noted (an ``Unresolved`` note
flagging what the parser couldn't fully resolve).

Pre-E, native ``op.*`` calls not in the handler table passed through
silently — a T1 gap my earlier SPEC-16 commits left open. The
prerequisite commit added a known-inert list and an ``unrecognized_op``
note for anything else, so this invariant can now actually hold.
"""

from __future__ import annotations

import ast
from pathlib import Path

from gravitar.schema.parsers.alembic_parser import parse_revision_file as alembic_parse
from gravitar.schema.parsers.django_parser import parse_migration_file as django_parse


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- Alembic side ---


def _count_alembic_nodes(path: Path) -> int:
    """Count every ast.Expr-of-call in the ``upgrade()`` body whose
    receiver is ``op`` or any name used as a batch binding — i.e. every
    thing the walker should classify. Walks the same tree the parser
    walks, but counts rather than resolves, so the two can be compared
    without a circular check."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    upgrade_fn = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )
    if upgrade_fn is None:
        return 0

    batch_bindings: set[str] = set()
    count = 0
    for node in ast.walk(upgrade_fn):
        if isinstance(node, ast.With):
            for item in node.items:
                expr = item.context_expr
                if (
                    isinstance(expr, ast.Call)
                    and isinstance(expr.func, ast.Attribute)
                    and isinstance(expr.func.value, ast.Name)
                    and expr.func.value.id == "op"
                    and expr.func.attr == "batch_alter_table"
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    batch_bindings.add(item.optional_vars.id)
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
        ):
            receiver = node.value.func.value.id
            if receiver == "op" or receiver in batch_bindings:
                count += 1
    return count


def test_alembic_accounting_identity_holds_for_a_mixed_revision(tmp_path: Path) -> None:
    """Mixed shape: 2 handled ops, 1 inert, 1 unrecognized → notes."""
    path = tmp_path / "0001_mixed.py"
    _write(
        path,
        '''"""mixed"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))  # applied
    op.add_column("widgets", sa.Column("name", sa.String(50)))                   # applied
    op.create_index("ix_widgets_name", "widgets", ["name"])                       # inert
    op.not_a_real_op("whatever")                                                   # noted
''',
    )

    revision = alembic_parse(path)
    nodes_seen = _count_alembic_nodes(path)
    unrecognized_notes = [n for n in revision.notes if n["reason"] == "unrecognized_op"]

    assert nodes_seen == 4
    assert len(revision.operations) == 2
    assert revision.inert_count == 1
    assert len(unrecognized_notes) == 1
    assert nodes_seen == len(revision.operations) + revision.inert_count + len(unrecognized_notes)


def test_alembic_accounting_holds_inside_batch_blocks(tmp_path: Path) -> None:
    """Batch ops count too — a batch_op.add_column is a node; a
    batch_op.create_index is a node (inert); a batch_op.not_a_thing is
    a node (noted)."""
    path = tmp_path / "0002_batch.py"
    _write(
        path,
        '''"""batch mixed"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))  # applied
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(50)))                      # applied
        batch_op.create_index("ix_name", ["name"])                                 # inert
        batch_op.weird_method()                                                     # noted
''',
    )

    revision = alembic_parse(path)
    nodes_seen = _count_alembic_nodes(path)
    unrecognized = [n for n in revision.notes if n["reason"] == "unrecognized_op"]

    assert nodes_seen == 4
    assert len(revision.operations) == 2
    assert revision.inert_count == 1
    assert len(unrecognized) == 1
    assert nodes_seen == len(revision.operations) + revision.inert_count + len(unrecognized)


def test_alembic_fully_resolved_revision_has_zero_notes_and_zero_inert(tmp_path: Path) -> None:
    """Baseline: a revision that uses only handled ops has
    ``inert_count == 0`` and ``notes == 0``, and every node is applied."""
    path = tmp_path / "0003_clean.py"
    _write(
        path,
        '''"""clean"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = None


def upgrade() -> None:
    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))
    op.add_column("t", sa.Column("name", sa.String(50)))
''',
    )

    revision = alembic_parse(path)
    nodes_seen = _count_alembic_nodes(path)

    assert nodes_seen == 2
    assert len(revision.operations) == 2
    assert revision.inert_count == 0
    assert revision.notes == []


# --- Django side ---


def test_django_accounting_identity_holds_for_a_mixed_migration(tmp_path: Path) -> None:
    """Django: 1 handled (CreateModel), 1 inert (AddIndex), 1 noted (RunSQL).
    nodes_seen = len(Migration.operations)."""
    path = tmp_path / "0001_mixed.py"
    _write(
        path,
        """from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Post",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.AddIndex(
            model_name="post", index=models.Index(fields=["id"], name="ix_id")
        ),
        migrations.RunSQL(sql="UPDATE blog_post SET id = id"),
    ]
""",
    )

    revision = django_parse(path, app_label="blog")

    assert len(revision.operations) == 1  # CreateModel
    assert revision.inert_count == 1  # AddIndex
    unsupported = [n for n in revision.notes if n["reason"] == "django_op_unsupported"]
    assert len(unsupported) == 1  # RunSQL

    nodes_seen = 3
    assert nodes_seen == len(revision.operations) + revision.inert_count + len(unsupported)


def test_django_all_inert_migration_has_only_inert_count(tmp_path: Path) -> None:
    path = tmp_path / "0001_indexes.py"
    _write(
        path,
        """from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.AddIndex(model_name="post", index=models.Index(fields=["a"], name="ix_a")),
        migrations.AddIndex(model_name="post", index=models.Index(fields=["b"], name="ix_b")),
    ]
""",
    )

    revision = django_parse(path, app_label="blog")

    assert revision.operations == []
    assert revision.inert_count == 2
    assert revision.notes == []
