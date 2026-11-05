"""SPEC-16 Amendment R10 (subset): discovery improvements.

Three scenarios from the readiness doc that pre-R10 caused silent or loud
failures on the pinned corpus:

- S6: default ``alembic init`` output on 1.20 writes
  ``script_location = %(here)s/alembic`` — pre-R10 the ``%(here)s`` string
  wasn't interpolated and the resulting path crashed. R10 interpolates it
  to the directory containing ``alembic.ini``.
- S9: Prefect ships ``versions/postgresql/*.py`` and ``versions/sqlite/*.py``
  side-by-side. Pre-R10 ``glob("*.py")`` only matched the top level and
  silently found zero revisions, making every model table look like
  ``missing_table``. R10 recurses.
- Gate requirement: "No project aborts the run; zero revisions parsed is
  a tool error." A successfully-resolved versions dir that contains zero
  revisions used to produce a clean pass with every model table flagged
  as missing. R10 flips this into EXIT_ANALYSIS_ERROR with a message.

The bigger ProjectUnit / env.py / Flask-Migrate / [tool.alembic] /
pkg:resource / include_object pieces of R10 stay deferred — each is a
session's worth on its own.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.errors import EXIT_ANALYSIS_ERROR, EXIT_SUCCESS


def _empty_models(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    (tmp_path / "models" / "__init__.py").write_text("")


def _revision(name: str) -> str:
    return (
        f'"""{name}"""\n\n'
        f'revision = "{name}"\n'
        "down_revision = None\n\n\n"
        "def upgrade() -> None:\n"
        "    pass\n"
    )


# --- R10 (a): %(here)s interpolation ---


def test_percent_here_interpolation_in_script_location(tmp_path: Path) -> None:
    """A relative ``%(here)s/alembic`` layout — the default ``alembic init``
    output on 1.20+ — must resolve to ``<ini_dir>/alembic``. Pre-R10 this
    crashed because %(here)s was a literal string in the resolved path."""
    _empty_models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(_revision("0001"))

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "no drift detected" in result.output


def test_non_root_alembic_ini_resolves_relative_script_location(tmp_path: Path) -> None:
    """``alembic.ini`` lives under ``backend/`` and names
    ``script_location = alembic`` — resolving the versions dir must be
    relative to the *ini's* directory, not to the repo root."""
    _empty_models(tmp_path)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "backend" / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "backend" / "alembic" / "versions" / "0001.py").write_text(_revision("0001"))
    (tmp_path / ".gravitar.yml").write_text("alembic:\n  config: 'backend/alembic.ini'\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "no drift detected" in result.output


# --- R10 (b): recursive version dir walk ---


def test_recursive_versions_dir_sibling_dialect_subdirs(tmp_path: Path) -> None:
    """Prefect-shaped layout: dialect-specific subdirs of ``versions/``.
    Pre-R10 top-level glob found zero and every model table was flagged
    missing; R10 recurses so both subdirs' files are parsed."""
    _empty_models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions" / "postgresql").mkdir(parents=True)
    (tmp_path / "versions" / "sqlite").mkdir(parents=True)
    (tmp_path / "versions" / "postgresql" / "0001_pg.py").write_text(_revision("0001_pg"))
    (tmp_path / "versions" / "sqlite" / "0001_sq.py").write_text(_revision("0001_sq"))

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    # Both files surface in the coverage count: 2 revisions.
    assert "2/2 revisions fully resolved" in result.output


# --- R10 (c): zero-revisions-is-a-tool-error ---


def test_zero_revisions_is_a_tool_error(tmp_path: Path) -> None:
    """An empty versions dir resolves successfully but yields no revisions
    — pre-R10 this produced a clean pass with every model table flagged
    missing. R10 makes it an explicit tool error so the user is told
    rather than silently misled."""
    _empty_models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_ANALYSIS_ERROR
    assert "zero revisions found" in result.output


def test_zero_revisions_is_a_tool_error_in_json_too(tmp_path: Path) -> None:
    _empty_models(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json"])

    import json

    assert result.exit_code == EXIT_ANALYSIS_ERROR
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert "zero revisions found" in payload["error"]
