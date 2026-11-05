"""SPEC-16 Amendment R4 (subset): loop unroll + external_call note.

Pre-R4, the walker descended into loop bodies but didn't resolve the
loop variable — a migration like ``for t in ("logs", "events"):
op.create_table(t, ...)`` produced ``Operation(table=None)`` for each
op (readiness D6). And a helper call like ``add_audit_columns(op,
"widgets")`` was invisible: the walker only matched calls whose
receiver was literally ``op``, so plain-function calls that received
op as an argument were silently dropped (readiness: Airflow has 13 of
these).

R4's full statement walker (constant folding of module-level ``NAME =
<literal>``, in-file helper inlining with recursion guard, conditional
branch marking) stays deferred. The subset here covers the two
highest-value patterns:

- Loop unroll over a literal tuple/list: ``for t in ("a", "b"):``
  unrolls one iteration per element, substituting the loop variable
  with each literal value before resolving ops in the body.
- Non-literal loop iterable: a single ``loop_not_literal`` note
  scoped to any literal tables mentioned inside the body (so the
  comparator can taint-demote findings on those tables).
- External call detection: any plain Python call that receives ``op``
  as an argument emits an ``external_call`` note scoped to the
  literal table arguments present, else global.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.schema.parsers.alembic_parser import parse_revision_file


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- Loop unroll over literal tuple/list ---


def test_for_loop_over_literal_tuple_unrolls(tmp_path: Path) -> None:
    """D6 headline: a loop over a literal tuple should produce one
    create_table per element with the loop variable substituted."""
    path = tmp_path / "0001_unroll.py"
    _write(
        path,
        '''"""unroll"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    for name in ("logs", "events"):
        op.create_table(name, sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)

    create_tables = [op for op in revision.operations if op.op == "create_table"]
    assert len(create_tables) == 2
    assert {op.table for op in create_tables} == {"logs", "events"}


def test_for_loop_over_literal_list_unrolls(tmp_path: Path) -> None:
    """List literal (not just tuple) also unrolls."""
    path = tmp_path / "0001_list_unroll.py"
    _write(
        path,
        '''"""list unroll"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    for t in ["a_log", "b_log"]:
        op.create_table(t, sa.Column("id", sa.Integer(), primary_key=True))
''',
    )

    revision = parse_revision_file(path)
    assert {op.table for op in revision.operations if op.op == "create_table"} == {"a_log", "b_log"}


def test_unrolled_loop_body_handles_multiple_ops(tmp_path: Path) -> None:
    """Each iteration unrolls all ops in the body."""
    path = tmp_path / "0001_multi.py"
    _write(
        path,
        '''"""multi ops in loop"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    for t in ("a", "b"):
        op.create_table(t, sa.Column("id", sa.Integer(), primary_key=True))
        op.add_column(t, sa.Column("name", sa.String(50)))
''',
    )

    revision = parse_revision_file(path)
    create = [op for op in revision.operations if op.op == "create_table"]
    add = [op for op in revision.operations if op.op == "add_column"]
    assert {op.table for op in create} == {"a", "b"}
    assert {op.table for op in add} == {"a", "b"}


# --- Non-literal loop iterable → loop_not_literal note ---


def test_for_loop_over_non_literal_emits_one_note(tmp_path: Path) -> None:
    path = tmp_path / "0001_dynamic_loop.py"
    _write(
        path,
        '''"""dynamic iterable"""
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    import os
    for name in os.environ.get("TABLES", "").split(","):
        op.create_table(name)
        op.create_table(name)
''',
    )

    revision = parse_revision_file(path)

    loop_notes = [n for n in revision.notes if n["reason"] == "loop_not_literal"]
    assert len(loop_notes) == 1  # one note for the whole loop, not per op


def test_for_loop_over_non_literal_with_literal_tables_scopes_the_note(
    tmp_path: Path,
) -> None:
    """When the loop body ALSO names a literal table, the note is scoped
    to it so R3 taint can demote findings on that specific table rather
    than globally."""
    path = tmp_path / "0001_mixed_loop.py"
    _write(
        path,
        '''"""mixed"""
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    import os
    for col in os.environ.get("COLS", "").split(","):
        op.add_column("widgets", col)
''',
    )

    revision = parse_revision_file(path)

    loop_notes = [n for n in revision.notes if n["reason"] == "loop_not_literal"]
    assert len(loop_notes) == 1
    assert loop_notes[0]["tables"] == ["widgets"]


# --- external_call detection ---


def test_external_call_passing_op_emits_a_note(tmp_path: Path) -> None:
    """D6 (helpers): with R4 full, an in-file helper whose first param
    is ``op`` gets *inlined*; external_call only fires when the helper
    is NOT in-file (imported from elsewhere) or when inlining is
    refused (recursion, etc.).

    This test uses an imported helper — one Gravitar can't follow — so
    external_call still fires as scoped to the literal table arg.
    """
    path = tmp_path / "0001_helper.py"
    _write(
        path,
        '''"""helper call"""
from alembic import op
from mymodule.helpers import add_audit_columns

revision = "0001"
down_revision = None


def upgrade() -> None:
    add_audit_columns(op, "widgets")
''',
    )

    revision = parse_revision_file(path)

    external = [n for n in revision.notes if n["reason"] == "external_call"]
    assert len(external) == 1
    assert external[0]["tables"] == ["widgets"]
    assert "add_audit_columns" in external[0]["detail"]


def test_external_call_without_table_arg_is_global(tmp_path: Path) -> None:
    """A helper called with just ``op`` and no literal table args scopes
    the note globally (``tables=None``) — R3 Decision #2 means it
    reduces confidence but doesn't demote specific findings.

    Imported helper (so R4 full inlining doesn't apply — the walker
    can't see the body)."""
    path = tmp_path / "0001_global_helper.py"
    _write(
        path,
        '''"""global helper"""
from alembic import op
from thirdparty import do_stuff

revision = "0001"
down_revision = None


def upgrade() -> None:
    do_stuff(op)
''',
    )

    revision = parse_revision_file(path)

    external = [n for n in revision.notes if n["reason"] == "external_call"]
    assert len(external) == 1
    assert external[0]["tables"] is None


def test_plain_call_without_op_argument_is_not_noted(tmp_path: Path) -> None:
    """A function call that doesn't involve op (e.g. ``print(...)``)
    isn't a migration op — the walker must NOT flag it."""
    path = tmp_path / "0001_noise.py"
    _write(
        path,
        '''"""noise"""
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    print("about to run")
    op.create_table("widgets")
''',
    )

    revision = parse_revision_file(path)

    external = [n for n in revision.notes if n["reason"] == "external_call"]
    assert external == []


def test_op_as_keyword_argument_also_detected(tmp_path: Path) -> None:
    """``helper(table="widgets", op=op)`` — op passed as a kwarg still
    counts as an external_call."""
    path = tmp_path / "0001_kwarg.py"
    _write(
        path,
        '''"""kwarg helper"""
from alembic import op

def helper(table, op):
    pass

revision = "0001"
down_revision = None


def upgrade() -> None:
    helper(table="widgets", op=op)
''',
    )

    revision = parse_revision_file(path)

    external = [n for n in revision.notes if n["reason"] == "external_call"]
    assert len(external) == 1
