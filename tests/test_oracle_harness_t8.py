"""SPEC-18 T8 tests: hard-seed mutations.

Covers the scanner (finds targetable ops), the planner (deterministic
shape + target selection), and each shape's applier (the rewrite
produces the structure the walker is supposed to recover).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.oracle.mutate import seeded_rng
from benchmarks.oracle.mutate_hard import (
    HARD_SHAPES,
    HardApplyError,
    HardMutationPlan,
    MigrationOp,
    apply_hard_mutation,
    plan_hard_mutation,
    scan_migration_ops,
)


def _write_migration(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "0001.py"
    path.write_text(body, encoding="utf-8")
    return path


# --- Scanner ---


def test_scan_finds_create_table_op(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        "def upgrade():\n"
        '    op.create_table("widgets", sa.Column("id", sa.Integer()))\n',
    )

    ops = scan_migration_ops(path)

    assert ops == [MigrationOp(kind="create_table", table="widgets", line=5, end_line=5)]


def test_scan_only_literal_table_names(tmp_path: Path) -> None:
    """A non-literal first arg (``op.create_table(T, ...)``) isn't
    rewritable — the hard-seed layer can't know what table it names.
    Scanner skips it."""
    path = _write_migration(
        tmp_path,
        'from alembic import op\ndef upgrade():\n    T = "widgets"\n    op.create_table(T)\n',
    )

    assert scan_migration_ops(path) == []


def test_scan_only_top_level_upgrade_body(tmp_path: Path) -> None:
    """Ops inside an inner function / helper aren't scanned — the
    hard-seed scanner only targets what the planner can reliably
    rewrite in-place."""
    path = _write_migration(
        tmp_path,
        "from alembic import op\n"
        "def _helper():\n"
        '    op.create_table("inner")\n'
        "def upgrade():\n"
        "    _helper()\n"
        '    op.create_table("outer")\n',
    )

    ops = scan_migration_ops(path)

    assert [o.table for o in ops] == ["outer"]


# --- Planner ---


def test_plan_hard_mutation_returns_none_without_targets(tmp_path: Path) -> None:
    path = _write_migration(tmp_path, "def upgrade():\n    pass\n")
    rng = seeded_rng("proj", "unit", 0, bucket="hard")

    assert plan_hard_mutation(rng, scan_migration_ops(path), path) is None


def test_plan_hard_mutation_is_deterministic(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        'from alembic import op\ndef upgrade():\n    op.create_table("widgets")\n',
    )
    ops = scan_migration_ops(path)

    r1 = plan_hard_mutation(seeded_rng("p", "u", 0, bucket="hard"), ops, path)
    r2 = plan_hard_mutation(seeded_rng("p", "u", 0, bucket="hard"), ops, path)
    assert r1 == r2


def test_plan_hard_mutation_covers_every_shape(tmp_path: Path) -> None:
    """Fairness check: across enough trials, every HARD_SHAPES
    member should appear at least once."""
    path = _write_migration(
        tmp_path,
        'from alembic import op\ndef upgrade():\n    op.add_column("widgets", 1)\n',
    )
    ops = scan_migration_ops(path)
    seen = set()
    for i in range(200):
        plan = plan_hard_mutation(seeded_rng("p", "u", i, bucket="hard"), ops, path)
        if plan is not None:
            seen.add(plan.shape)
    assert seen == set(HARD_SHAPES)


# --- Appliers ---


def _plan(shape: str, path: Path, table: str = "widgets", line: int = 5) -> HardMutationPlan:
    return HardMutationPlan(
        shape=shape,  # type: ignore[arg-type]
        migration_file=path,
        original_line=line,
        original_table=table,
    )


def test_apply_batch_shape_wraps_add_column(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        'revision = "0001"\n'
        "def upgrade():\n"
        '    op.add_column("widgets", sa.Column("name", sa.String(50)))\n',
    )
    apply_hard_mutation(_plan("batch", path, line=5))
    out = path.read_text(encoding="utf-8")

    assert "batch_alter_table('widgets')" in out
    assert "b.add_column(" in out


def test_apply_loop_shape_wraps_create_table(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "from alembic import op\n"
        'revision = "0001"\n'
        "def upgrade():\n"
        '    op.create_table("widgets")\n',
    )
    apply_hard_mutation(_plan("loop", path, line=4))
    out = path.read_text(encoding="utf-8")

    assert "for name in ('widgets',):" in out
    assert "op.create_table(name)" in out


def test_apply_helper_shape_inserts_helper_def(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        'revision = "0001"\n'
        "def upgrade():\n"
        '    op.add_column("widgets", sa.Column("x", sa.Integer()))\n',
    )
    apply_hard_mutation(_plan("helper", path, line=5))
    out = path.read_text(encoding="utf-8")

    assert "_hard_seed_helper(op)" in out
    assert "def _hard_seed_helper(op):" in out
    # The helper body references `sa.Column` — the migration imports
    # it already; test fixture confirms the pattern looks right.
    assert 'sa.Column("_hard_seed"' in out


def test_apply_run_sql_shape_replaces_with_execute(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        'revision = "0001"\n'
        "def upgrade():\n"
        '    op.add_column("widgets", sa.Column("x", sa.Integer()))\n',
    )
    apply_hard_mutation(_plan("run_sql", path, line=5))
    out = path.read_text(encoding="utf-8")

    assert 'op.execute("ALTER TABLE widgets ADD COLUMN _hard_seed INTEGER")' in out


def test_apply_errors_cleanly_on_bad_line(tmp_path: Path) -> None:
    path = _write_migration(tmp_path, "def upgrade():\n    pass\n")
    with pytest.raises(HardApplyError, match="out of range"):
        apply_hard_mutation(_plan("batch", path, line=99))


# --- Integration: plan + apply produces a valid Python file ---


def test_plan_and_apply_produces_parseable_output(tmp_path: Path) -> None:
    """A hard-seed rewrite must still produce a syntactically valid
    migration — downstream the walker will ast.parse it. If this
    test fails, the rewriter is producing broken code."""
    import ast

    path = _write_migration(
        tmp_path,
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        'revision = "0001"\n'
        "down_revision = None\n"
        "def upgrade():\n"
        '    op.add_column("widgets", sa.Column("x", sa.Integer()))\n',
    )
    for shape in HARD_SHAPES:
        tmp_path.joinpath(f"test-{shape}.py").write_text(path.read_text(encoding="utf-8"))
        shape_path = tmp_path / f"test-{shape}.py"
        apply_hard_mutation(_plan(shape, shape_path, line=6))
        ast.parse(shape_path.read_text(encoding="utf-8"))
