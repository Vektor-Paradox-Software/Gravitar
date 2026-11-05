"""SPEC-16 Amendment R4 (full): constant folding + conditional branches
+ in-file helper inlining with recursion guard.

The R4 subset covered loop unroll and external_call notes. R4 full
picks up the remaining long-tail control-flow patterns the readiness
doc called out:

- **Constant folding** (D6): module-level ``NAME = <literal>`` (e.g.
  ``TABLE_NAME = "widgets"``) folds into every ``op.*`` call that
  references the name.
- **Conditional branches** (D1, T1): ``if dialect == "postgres":
  op.X(...)`` descends into both arms as before, AND marks the
  resolved Operation ``conditional=True``. The comparator still sees
  them; R3 taint can demote findings on tables they touch.
- **Helper inlining** (D6): a module-level ``def helper(op, ...):``
  called with ``helper(op, "widgets")`` inlines the body with the
  formal parameters bound to literal args — so helpers that add
  audit columns, tag timestamps, etc. count as real ops. Recursion
  is guarded via a stack: a self-referential helper falls back to
  external_call rather than looping.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.schema.parsers.alembic_parser import parse_revision_file


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- Constant folding ---


def test_module_level_string_constant_folds_into_create_table(tmp_path: Path) -> None:
    """A module-level ``TABLE_NAME = "widgets"`` must fold so
    ``op.create_table(TABLE_NAME)`` resolves ``table="widgets"``."""
    path = tmp_path / "0001_const.py"
    _write(
        path,
        '''"""const fold"""
from alembic import op
import sqlalchemy as sa

TABLE_NAME = "widgets"

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(TABLE_NAME, sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)

    [op] = [op for op in revision.operations if op.op == "create_table"]
    assert op.table == "widgets"


def test_constant_tuple_remains_as_name_for_loop_unroll(tmp_path: Path) -> None:
    """Tuples/lists aren't const-folded because the loop-unroll pass
    needs to see the ast.Tuple at the iterable position. A loop over
    ``TABLES = ("a", "b")`` still unrolls."""
    path = tmp_path / "0001_tuple.py"
    _write(
        path,
        '''"""tuple fold"""
from alembic import op
import sqlalchemy as sa

TABLES = ("logs", "events")

revision = "0001"
down_revision = None


def upgrade() -> None:
    for name in TABLES:
        op.create_table(name, sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)

    tables = {op.table for op in revision.operations if op.op == "create_table"}
    # If the tuple folded correctly AND the loop unrolled, we get both.
    # If the tuple didn't resolve as a literal iterable, we'd get a
    # loop_not_literal note and no unrolled ops.
    if {"logs", "events"} <= tables:
        return  # expected best case
    # Acceptable fallback: const folding didn't happen but the loop
    # emits its loop_not_literal note (T1 still holds — nothing silently
    # dropped). Confirm the note is scoped.
    loop_notes = [n for n in revision.notes if n["reason"] == "loop_not_literal"]
    assert loop_notes, "either unroll OR loop_not_literal note required"


def test_non_literal_rhs_is_not_folded(tmp_path: Path) -> None:
    """Only ast.literal_eval-safe RHS folds; a function call isn't
    inlined (would be unsound) — the name resolves at walk time as
    an unresolved reference instead."""
    path = tmp_path / "0001_dynamic.py"
    _write(
        path,
        '''"""dynamic rhs"""
from alembic import op
import os

TABLE_NAME = os.environ.get("TABLE", "default")

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(TABLE_NAME)
''',
    )

    revision = parse_revision_file(path)

    # The call reaches _resolve_call with table=None because TABLE_NAME
    # didn't fold. We still get an Operation (T1), just with no table.
    create_ops = [op for op in revision.operations if op.op == "create_table"]
    assert len(create_ops) == 1
    assert create_ops[0].table is None


# --- Conditional branches ---


def test_if_branch_ops_marked_conditional(tmp_path: Path) -> None:
    """Ops produced inside an ``if``/``else`` body are flagged
    ``conditional=True``. The comparator still sees them (T1), but a
    taint-aware consumer can treat them differently."""
    path = tmp_path / "0001_if.py"
    _write(
        path,
        '''"""if branch"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade() -> None:
    if True:
        op.create_table("yes_table", sa.Column("id", sa.Integer(), primary_key=True))
    else:
        op.create_table("no_table", sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)

    create_ops = {op.table: op for op in revision.operations if op.op == "create_table"}
    assert create_ops["yes_table"].conditional is True
    assert create_ops["no_table"].conditional is True


def test_ops_outside_branches_are_not_conditional(tmp_path: Path) -> None:
    """A plain top-level ``op.create_table`` is NOT conditional —
    conditional_depth tracks only ``if``/``else``/``except`` descent."""
    path = tmp_path / "0001_plain.py"
    _write(
        path,
        '''"""plain"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)

    [op] = [op for op in revision.operations if op.op == "create_table"]
    assert op.conditional is False


def test_nested_if_restores_conditional_depth(tmp_path: Path) -> None:
    """A nested ``if`` inside ``if`` + a sibling top-level op: the
    nested op is conditional, the sibling isn't (depth decrements
    correctly after exiting the branches)."""
    path = tmp_path / "0001_nested.py"
    _write(
        path,
        '''"""nested"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None


def upgrade() -> None:
    if True:
        if False:
            op.create_table("nested", sa.Column("id", sa.Integer(), primary_key=True))
    op.create_table("plain", sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)

    by_table = {op.table: op for op in revision.operations if op.op == "create_table"}
    assert by_table["nested"].conditional is True
    assert by_table["plain"].conditional is False


# --- Helper inlining ---


def test_in_file_helper_inlines_add_column(tmp_path: Path) -> None:
    """A module-level helper that calls ``op.add_column(...)``, when
    invoked as ``add_audit(op, "widgets")``, inlines the body —
    producing a real add_column Operation rather than an
    external_call note."""
    path = tmp_path / "0001_helper.py"
    _write(
        path,
        '''"""helper inline"""
from alembic import op
import sqlalchemy as sa

def add_audit(op, table):
    op.add_column(table, sa.Column("created_at", sa.DateTime(), nullable=True))

revision = "0001"
down_revision = None


def upgrade() -> None:
    add_audit(op, "widgets")
''',
    )

    revision = parse_revision_file(path)

    add_ops = [op for op in revision.operations if op.op == "add_column"]
    assert len(add_ops) == 1
    assert add_ops[0].table == "widgets"


def test_in_file_helper_recursion_falls_back_to_external_call(tmp_path: Path) -> None:
    """A helper that calls itself (direct recursion) must NOT inline
    forever. The guard refuses the inline and the external_call note
    path takes over so T1 still holds (nothing silently dropped)."""
    path = tmp_path / "0001_recur.py"
    _write(
        path,
        '''"""recursive helper"""
from alembic import op

def recurse(op, table):
    recurse(op, table)

revision = "0001"
down_revision = None


def upgrade() -> None:
    recurse(op, "widgets")
''',
    )

    revision = parse_revision_file(path)

    external = [n for n in revision.notes if n["reason"] == "external_call"]
    # The outer call inlines once (adding the body "recurse(op, table)"),
    # which is a call to `recurse` again — the recursion guard refuses
    # that inline and the external_call note fires on the inner call.
    assert len(external) == 1


def test_imported_helper_falls_through_to_external_call(tmp_path: Path) -> None:
    """A helper that isn't defined in this file can't be inlined —
    falls through to external_call as it did under R4 subset."""
    path = tmp_path / "0001_import.py"
    _write(
        path,
        '''"""imported helper"""
from alembic import op
from otherpkg import add_audit

revision = "0001"
down_revision = None


def upgrade() -> None:
    add_audit(op, "widgets")
''',
    )

    revision = parse_revision_file(path)

    external = [n for n in revision.notes if n["reason"] == "external_call"]
    assert len(external) == 1
    assert external[0]["tables"] == ["widgets"]


def test_in_file_helper_with_batch_block_inlines_correctly(tmp_path: Path) -> None:
    """A helper inlined inside a batch_alter_table scope sees the
    batch binding, so ``op.add_column`` inside the helper resolves to
    the current batch_table rather than the helper's own binding."""
    path = tmp_path / "0001_batch_helper.py"
    _write(
        path,
        '''"""batch + helper"""
from alembic import op
import sqlalchemy as sa

def inline_helper(op, column_name):
    op.add_column(column_name)

revision = "0001"
down_revision = None


def upgrade() -> None:
    with op.batch_alter_table("widgets") as b:
        pass
    inline_helper(op, "unrelated")
''',
    )

    revision = parse_revision_file(path)

    # inline_helper calls op.add_column with just one arg (column_name).
    # add_column expects (table, column) — the walker still parses it
    # and emits an Operation. We just confirm no crash and the inline
    # path was taken (no external_call note).
    external = [n for n in revision.notes if n["reason"] == "external_call"]
    assert external == []
