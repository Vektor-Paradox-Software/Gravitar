"""SPEC-12 Amendment v1.1 G9 + G10: corpus-quirk fixtures and raw-SQL id
uniqueness.

The original SPEC-12 fixtures were all root-level, single-tree, with an
``alembic.ini`` at repo root and ``versions/`` beside it. None mirrored
the shapes the real-world benchmark corpus actually ships with — the
exact shapes R10 added support for (``%(here)s`` in script_location,
nested ``versions/`` subdirs, multi-tree layouts, ini outside the repo
root, Alembic 1.11+'s annotated-``revision``, syntax errors, and
booby-trapped migrations proving the parser never imports them).

This file builds those shapes dynamically in ``tmp_path`` and runs the
pipeline end-to-end against each. Also covers G10: multiple raw-SQL
findings in one revision get distinct finding_ids.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_helpers import run_check


def _models(root: Path, pkg: str = "models", table: str = "widgets") -> None:
    """Minimal SQLAlchemy models package declaring one table."""
    (root / pkg).mkdir(parents=True, exist_ok=True)
    (root / pkg / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class Row(Base):\n"
        f'    __tablename__ = "{table}"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(50), nullable=False)\n",
        encoding="utf-8",
    )


def _create_table_revision(name: str, table: str, down: str | None = None) -> str:
    down_literal = f'"{down}"' if down else "None"
    return (
        f'"""{name}"""\n\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        f'revision = "{name}"\n'
        f"down_revision = {down_literal}\n\n"
        "def upgrade() -> None:\n"
        f'    op.create_table("{table}",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("name", sa.String(length=50), nullable=False),\n'
        "    )\n"
    )


# --- G9: corpus-quirk layouts ---


def test_percent_here_in_script_location(tmp_path: Path) -> None:
    """R10 (subset): default `alembic init` on 1.20+ writes
    ``script_location = %(here)s/alembic`` — the ini-dir interpolation
    must resolve correctly."""
    _models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )

    payload = run_check(tmp_path)

    assert payload["status"] == "clean"


def test_nested_versions_subdirectory(tmp_path: Path) -> None:
    """R10 (subset): Prefect's ``versions/postgresql/*.py`` + ``sqlite/*.py``
    layout must be discovered via rglob, not top-level glob only."""
    _models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions" / "postgresql").mkdir(parents=True)
    (tmp_path / "versions" / "postgresql" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )

    payload = run_check(tmp_path)

    assert payload["status"] == "clean"


def test_ini_outside_project_root(tmp_path: Path) -> None:
    """R10 (full): the project at ``repo/`` references
    ``../backend/alembic.ini``; script_location is relative to the ini
    dir, so the versions dir lives alongside the ini, not the repo."""
    repo = tmp_path / "repo"
    backend = tmp_path / "backend"
    _models(repo)
    backend.mkdir()
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (backend / "alembic" / "versions").mkdir(parents=True)
    (backend / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )
    (repo / ".gravitar.yml").write_text(
        "alembic:\n  config: '../backend/alembic.ini'\n",
        encoding="utf-8",
    )

    payload = run_check(repo)

    assert payload["status"] == "clean", payload


def test_multi_tree_core_plus_plugin(tmp_path: Path) -> None:
    """R10 (full): the CTFd-style layout — core at repo root, plugin
    at ``plugins/<name>/``, both with their own models + migrations.
    One run must analyze both trees and aggregate findings/notes."""
    _models(tmp_path, pkg="models", table="widgets")
    _models(tmp_path, pkg="plugin_mod", table="plugin_rows")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "widgets")
    )
    (tmp_path / "plugin_mod" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "plugin_mod" / "migrations" / "versions" / "0001.py").write_text(
        _create_table_revision("0001", "plugin_rows")
    )
    (tmp_path / ".gravitar.yml").write_text(
        "trees:\n"
        "  - name: plugin\n"
        "    models:\n"
        "      paths: ['plugin_mod']\n"
        "    alembic:\n"
        "      versions_dir: 'plugin_mod/migrations/versions'\n",
        encoding="utf-8",
    )

    payload = run_check(tmp_path)

    assert payload["status"] == "clean", payload
    assert payload["coverage"]["revisions_total"] == 2


def test_annotated_revision_identifier(tmp_path: Path) -> None:
    """R9: Alembic 1.11+'s default template writes
    ``revision: str = "..."`` (an AnnAssign) rather than a plain
    Assign. Both forms must parse identically."""
    _models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001.py").write_text(
        '"""annotated"""\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        'revision: str = "0001"\n'
        "down_revision: str | None = None\n\n"
        "def upgrade() -> None:\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("name", sa.String(length=50), nullable=False),\n'
        "    )\n"
    )

    payload = run_check(tmp_path)

    assert payload["status"] == "clean", payload


def test_file_with_syntax_error_isolated(tmp_path: Path) -> None:
    """R11: a SyntaxError in one file must become a ``parse_error``
    note on a placeholder Revision — the rest of the run proceeds.
    Pre-R11, one bad file would abort the whole analysis."""
    _models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001.py").write_text(_create_table_revision("0001", "widgets"))
    (tmp_path / "versions" / "0002_broken.py").write_text("def upgrade(:\n    pass\n")

    payload = run_check(tmp_path)

    parse_errors = [n for n in payload["notes"] if n["reason"] == "parse_error"]
    assert len(parse_errors) == 1
    # Good file still produced its clean create_table — overall exit
    # is clean (the parse_error is a note, not a drift finding).
    assert payload["coverage"]["revisions_total"] >= 1


def test_booby_trapped_migration_is_never_imported(tmp_path: Path) -> None:
    """Alembic migrations are PARSED, never IMPORTED. A migration that
    would raise on import must still parse cleanly for its op calls. If
    this test ever fails because the import side-effect fired, the parser
    broke its contract."""
    _models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    sentinel = tmp_path / "sentinel.txt"
    (tmp_path / "versions" / "0001_boobytrap.py").write_text(
        '"""booby-trapped"""\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "from pathlib import Path\n\n"
        # Side effect at import time: writes a file. If the parser
        # imports this module, the sentinel will exist afterwards.
        f'Path(r"{sentinel}").write_text("TRIPPED")\n\n'
        'revision = "0001"\n'
        "down_revision = None\n\n"
        "def upgrade() -> None:\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("name", sa.String(length=50), nullable=False),\n'
        "    )\n"
    )

    payload = run_check(tmp_path)

    assert not sentinel.exists(), "parser imported a migration — contract violation"
    # And the op was still parsed from AST.
    assert payload["status"] == "clean", payload


# --- G10: raw-SQL finding_id uniqueness ---


def test_multiple_raw_sql_findings_have_distinct_finding_ids(tmp_path: Path) -> None:
    """SPEC-12 Amendment G10: when one revision emits several raw-SQL
    findings, each must get a distinct ``finding_id``. The G4 fix made
    pattern_id include a resolution class; this test guards the
    orthogonal property that per-row IDs don't collapse — Scenario C's
    dismiss loop depends on it."""
    _models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    # One revision with THREE unrecognized op.execute calls at different
    # lines — each should produce its own raw-SQL finding/note with a
    # unique id anchored to source.line.
    (tmp_path / "versions" / "0001_multi_sql.py").write_text(
        '"""multi raw sql"""\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        'revision = "0001"\n'
        "down_revision = None\n\n"
        "def upgrade() -> None:\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("name", sa.String(length=50), nullable=False),\n'
        "    )\n"
        "    stmt_a = \"UPDATE widgets SET name = 'a'\"\n"
        "    op.execute(stmt_a)\n"
        "    stmt_b = \"UPDATE widgets SET name = 'b'\"\n"
        "    op.execute(stmt_b)\n"
        "    stmt_c = \"UPDATE widgets SET name = 'c'\"\n"
        "    op.execute(stmt_c)\n"
    )

    payload = run_check(tmp_path)

    # Each dynamic_sql note / raw-SQL finding carries a stable id anchored
    # to source line; collect them and check for duplicates.
    dynamic_notes = [n for n in payload["notes"] if n["reason"] == "dynamic_sql"]
    note_ids = [n.get("id") for n in dynamic_notes]
    assert len(note_ids) == len(set(note_ids)), (
        f"finding_id collision across raw-SQL notes: {note_ids}"
    )
    assert len(note_ids) == 3, dynamic_notes
