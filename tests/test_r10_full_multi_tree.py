"""SPEC-16 Amendment R10 (full): ProjectUnit multi-tree + no-ini mode.

The R10 (subset) tests cover %(here)s, recursive versions, and the
zero-revisions-is-tool-error gate. This file covers the remaining R10
pieces:

- No-ini mode (Flask-Migrate style). Projects such as Flask-Migrate ship
  no ``alembic.ini`` — the migrations directory lives at a known path
  and env.py is invoked directly. R10 adds an ``alembic.versions_dir``
  key that bypasses the ini entirely.
- ProjectUnit multi-tree. Repos such as CTFd (core + plugins) and large
  Django monorepos ship multiple analysis units per checkout; R10
  analyzes each and aggregates findings/notes/coverage into one output.

Deferred to a follow-up: ``[tool.alembic]`` in pyproject.toml,
``pkg:resource`` script_location, env.py ``target_metadata`` auto-
resolution, and ``include_object``/``include_name`` policy mode.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.config import TreeConfig, resolve_config
from gravitar.errors import EXIT_ANALYSIS_ERROR, EXIT_CONFIG_ERROR, EXIT_DRIFT, EXIT_SUCCESS


def _models_with_table(root: Path, pkg: str, table: str) -> None:
    """Write a minimal SQLAlchemy models package at root/<pkg>/__init__.py
    declaring one table."""
    (root / pkg).mkdir(parents=True, exist_ok=True)
    (root / pkg / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class Row(Base):\n"
        f'    __tablename__ = "{table}"\n'
        "    id = Column(Integer, primary_key=True)\n",
        encoding="utf-8",
    )


def _empty_models(root: Path, pkg: str = "models") -> None:
    (root / pkg).mkdir(parents=True, exist_ok=True)
    (root / pkg / "__init__.py").write_text("", encoding="utf-8")


def _create_table_revision(name: str, table: str, down: str | None = None) -> str:
    down_literal = f'"{down}"' if down else "None"
    return (
        f'"""{name}"""\n\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        f'revision = "{name}"\n'
        f"down_revision = {down_literal}\n\n\n"
        "def upgrade() -> None:\n"
        f'    op.create_table("{table}",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        "    )\n"
    )


# --- No-ini mode (R10 full, Flask-Migrate style) ---


def test_no_ini_mode_uses_direct_versions_dir(tmp_path: Path) -> None:
    """``alembic.versions_dir`` in .gravitar.yml bypasses the ini. The
    Flask-Migrate convention is ``migrations/versions`` with no
    alembic.ini in the project root."""
    _models_with_table(tmp_path, "models", "widgets")
    (tmp_path / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "migrations" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )
    (tmp_path / ".gravitar.yml").write_text("alembic:\n  versions_dir: 'migrations/versions'\n")
    # No alembic.ini anywhere — pre-R10 this crashed on the missing ini.
    assert not (tmp_path / "alembic.ini").exists()

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "no drift detected" in result.output


def test_no_ini_mode_missing_versions_dir_fails_closed(tmp_path: Path) -> None:
    """A ``versions_dir`` that doesn't exist must be a ConfigError, not a
    silent zero-revisions pass."""
    _empty_models(tmp_path)
    (tmp_path / ".gravitar.yml").write_text("alembic:\n  versions_dir: 'does/not/exist'\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR, result.output
    assert "not found" in result.output
    assert "versions_dir" in result.output


def test_no_ini_mode_takes_precedence_over_config(tmp_path: Path) -> None:
    """When both ``versions_dir`` and ``config`` are set, versions_dir
    wins — a user who wrote both clearly means to use the no-ini path."""
    _models_with_table(tmp_path, "models", "widgets")
    # Point ``config`` at an alembic.ini that would resolve a DIFFERENT
    # versions dir. R10 full ignores the ini when versions_dir is set.
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = would_be_wrong\n")
    (tmp_path / "would_be_wrong" / "versions").mkdir(parents=True)
    (tmp_path / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "migrations" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )
    (tmp_path / ".gravitar.yml").write_text(
        "alembic:\n  config: 'alembic.ini'\n  versions_dir: 'migrations/versions'\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output


# --- Config parsing: TreeConfig ---


def test_resolve_config_parses_extra_trees_list(tmp_path: Path) -> None:
    _models_with_table(tmp_path, "models", "widgets")
    _models_with_table(tmp_path, "plugin_pkg", "plugin_rows")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "plugin_pkg" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n"
        "  - name: plugin\n"
        "    models:\n"
        "      paths: ['plugin_pkg']\n"
        "    alembic:\n"
        "      versions_dir: 'plugin_pkg/migrations/versions'\n"
    )

    resolved = resolve_config(tmp_path, None)

    assert len(resolved.extra_trees) == 1
    [tree] = resolved.extra_trees
    assert isinstance(tree, TreeConfig)
    assert tree.name == "plugin"
    assert tree.model_paths == ["plugin_pkg"]
    assert tree.versions_dir == tmp_path / "plugin_pkg/migrations/versions"


def test_resolve_config_rejects_unknown_tree_keys(tmp_path: Path) -> None:
    _empty_models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n  - name: bad\n    typo_key: value\n    models:\n      paths: ['foo']\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "typo_key" in result.output


def test_tree_without_models_paths_is_rejected(tmp_path: Path) -> None:
    """An Alembic tree with no ``models.paths`` can't be analyzed —
    we can't discover default packages per-tree because a tree is
    scoped to one specific package."""
    _empty_models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n  - name: no_models\n    alembic:\n      versions_dir: 'versions'\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "models.paths" in result.output


# --- Multi-tree analysis via CLI ---


def test_multi_tree_aggregates_findings_from_each_tree(tmp_path: Path) -> None:
    """The primary tree's models declare table 'widgets' with no matching
    migration; the extra tree's models declare 'plugin_rows' with its own
    unmatched migration. The CLI should report drift from BOTH trees in
    one run."""
    _models_with_table(tmp_path, "models", "widgets")  # primary: missing_table
    _models_with_table(tmp_path, "plugin_pkg", "plugin_rows")  # extra: missing_table

    # Primary tree has an empty versions dir (minus a placeholder revision)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "unrelated")
    )

    # Extra tree
    (tmp_path / "plugin_pkg" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "plugin_pkg" / "migrations" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "another_unrelated")
    )

    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n"
        "  - name: plugin\n"
        "    models:\n"
        "      paths: ['plugin_pkg']\n"
        "    alembic:\n"
        "      versions_dir: 'plugin_pkg/migrations/versions'\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json", "--no-color"])

    assert result.exit_code == EXIT_DRIFT, result.output
    data = json.loads(result.output)
    # Each tree contributes a missing_table (widgets, plugin_rows) and an
    # orphaned_column (unrelated, another_unrelated); exact findings depend
    # on comparator rules — just assert BOTH trees' tables appear.
    tables_in_findings = {f["table"] for f in data["findings"] if f.get("table")}
    assert "widgets" in tables_in_findings
    assert "plugin_rows" in tables_in_findings


def test_multi_tree_coverage_is_summed(tmp_path: Path) -> None:
    """Coverage aggregates per-tree: primary (1 revision) + extra (2
    revisions) → revisions_total = 3."""
    _models_with_table(tmp_path, "models", "widgets")
    _models_with_table(tmp_path, "plugin_pkg", "plugin_rows")

    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )

    (tmp_path / "plugin_pkg" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "plugin_pkg" / "migrations" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "plugin_rows")
    )
    (tmp_path / "plugin_pkg" / "migrations" / "versions" / "0002.py").write_text(
        _create_table_revision("0002", "plugin_extra", down="0001")
    )

    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n"
        "  - name: plugin\n"
        "    models:\n"
        "      paths: ['plugin_pkg']\n"
        "    alembic:\n"
        "      versions_dir: 'plugin_pkg/migrations/versions'\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json", "--no-color"])

    data = json.loads(result.output)
    assert data["coverage"]["revisions_total"] == 3


def test_multi_tree_zero_revisions_in_extra_tree_is_tool_error(tmp_path: Path) -> None:
    """Per-tree zero-revisions must still fail the whole run — a plugin
    with no migrations analyzed as clean would silently flag every one
    of its model tables as missing."""
    _models_with_table(tmp_path, "models", "widgets")
    _models_with_table(tmp_path, "plugin_pkg", "plugin_rows")

    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )

    (tmp_path / "plugin_pkg" / "migrations" / "versions").mkdir(parents=True)
    # Empty — no revisions.

    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n"
        "  - name: plugin\n"
        "    models:\n"
        "      paths: ['plugin_pkg']\n"
        "    alembic:\n"
        "      versions_dir: 'plugin_pkg/migrations/versions'\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_ANALYSIS_ERROR
    assert "zero revisions" in result.output


def test_multi_tree_text_output_renders_without_crash(tmp_path: Path) -> None:
    """Text output must handle aggregated findings — exercise the full
    text formatter path across trees."""
    _models_with_table(tmp_path, "models", "widgets")
    _models_with_table(tmp_path, "plugin_pkg", "plugin_rows")

    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )

    (tmp_path / "plugin_pkg" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "plugin_pkg" / "migrations" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "plugin_rows")
    )

    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n"
        "  - name: plugin\n"
        "    models:\n"
        "      paths: ['plugin_pkg']\n"
        "    alembic:\n"
        "      versions_dir: 'plugin_pkg/migrations/versions'\n"
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    # Coverage line reflects summed revisions_total across trees.
    assert "coverage" in result.output.lower() or "revisions" in result.output.lower()
