"""SPEC-16 Amendment R7 + T4/T5: coverage reporting and exit codes.

T4: every ``gravitar check`` run prints a coverage line, in both text and
JSON output. The coverage object exists on every envelope — the earlier
foundation commit introduced it; this file asserts the T4/T5 behavior
specifically.

A clean run with incomplete coverage must not be indistinguishable
from a fully-covered clean run. ``--strict-coverage`` is the opt-in
gate for CI pipelines that want "no notes" to fail — needed to keep
green checks from meaning "I saw nothing".

The coverage count accuracy (fully_resolved / partial / skipped) is
already covered by test_r11_fault_isolation.py and
tests/integration/full_pipeline/test_full_stack.py — this file focuses
on the exit code and the coverage line being visible on every run.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.errors import EXIT_INCOMPLETE, EXIT_SUCCESS

FIXTURES = Path(__file__).parent / "fixtures"


# --- T4: coverage line is always present ---


def test_coverage_line_appears_on_clean_run() -> None:
    """A clean fully-resolved run still prints coverage so the output can't
    be mistaken for "I didn't look"."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS
    assert "coverage:" in result.output
    assert "3/3 revisions fully resolved" in result.output


def test_coverage_object_appears_in_json_output() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--format", "json"])

    payload = json.loads(result.output)
    assert payload["coverage"] == {
        "revisions_total": 3,
        "revisions_fully_resolved": 3,
        "revisions_partial": 0,
        "revisions_skipped": 0,
    }


# --- T5: --strict-coverage flag ---


def test_strict_coverage_fails_a_run_with_parse_error_notes(tmp_path: Path) -> None:
    """A broken migration file (R11 fault isolation) now produces a
    parse_error note. Default behavior is to keep going (and the exit code
    reflects actual findings only). --strict-coverage flips that: any note
    makes the exit incomplete."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001_broken.py").write_text("def upgrade(:\n    pass\n")

    without_strict = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])
    with_strict = CliRunner().invoke(
        main, ["check", str(tmp_path), "--no-color", "--strict-coverage"]
    )

    assert without_strict.exit_code == EXIT_SUCCESS  # R11: run continues
    assert with_strict.exit_code == EXIT_INCOMPLETE  # T5: incomplete coverage fails CI


def test_strict_coverage_leaves_clean_fully_resolved_runs_alone() -> None:
    """--strict-coverage is specifically about coverage gaps, not about
    requiring zero findings. A clean fully-resolved run still exits 0."""
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "clean"), "--no-color", "--strict-coverage"]
    )

    assert result.exit_code == EXIT_SUCCESS


def test_strict_coverage_still_lets_drift_exit_as_drift(tmp_path: Path) -> None:
    """--strict-coverage doesn't override drift. If a run has errors AND
    incomplete coverage, drift (EXIT_DRIFT=1) takes priority — the user
    cares more about real drift than about an incidental parse_error on
    an unrelated file."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
        "    id = Column(Integer, primary_key=True)\n"
    )
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    # No migration for widgets → missing_table error. Plus a broken file
    # that produces a parse_error note.
    (tmp_path / "versions" / "0001_broken.py").write_text("def upgrade(:\n    pass\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color", "--strict-coverage"])

    # Drift beats incomplete — a user with real drift should see "1" not "11".
    assert result.exit_code == 1
