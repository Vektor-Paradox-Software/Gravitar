"""Scenario F (SPEC-12 Amendment v1.1 §3): tier and exit contract.

Consolidates the behavioral invariants my SPEC-16 Amendment commits
establish, as a single integration pass over the exit-code and tier
behavior. The individual mechanisms are tested in isolation elsewhere
(test_r3_demotion, test_r7_coverage, test_r10_discovery); this file
proves they compose correctly end-to-end:

- Clean + covered → exit 0.
- Drift → exit 1 (``kind="fail"`` error findings).
- Incomplete + ``--strict-coverage`` → exit 11.
- Zero revisions found → exit 3 (tool error).
- Coverage line is always printed in text output.
- No error-severity ``kind="fail"`` finding on a table in the taint
  set (R3 demotion drops it to review).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.errors import EXIT_ANALYSIS_ERROR, EXIT_DRIFT, EXIT_INCOMPLETE, EXIT_SUCCESS

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _minimal_revision(contents: str = "") -> str:
    return (
        '"""minimal revision"""\n\n'
        'revision = "0001"\n'
        "down_revision = None\n\n\n"
        "def upgrade() -> None:\n"
        f"    {contents or 'pass'}\n"
    )


# --- Exit codes ---


def test_clean_and_covered_exits_zero() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS
    assert "no drift detected" in result.output


def test_drift_exits_one() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "missing_column"), "--no-color"])

    assert result.exit_code == EXIT_DRIFT


def test_incomplete_with_strict_coverage_exits_eleven(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001_broken.py").write_text("def upgrade(:\n    pass\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color", "--strict-coverage"])

    assert result.exit_code == EXIT_INCOMPLETE


def test_zero_revisions_exits_three(tmp_path: Path) -> None:
    """R10's gate: a successfully-resolved versions dir with zero
    revisions is a tool error (would otherwise silently report every
    model table as missing)."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()  # empty

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_ANALYSIS_ERROR


# --- Coverage line is always present ---


def test_coverage_line_present_on_clean_runs() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--no-color"])

    assert "coverage:" in result.output


def test_coverage_line_present_on_drift_runs() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "missing_column"), "--no-color"])

    assert "coverage:" in result.output


def test_coverage_present_in_json_envelope() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--format", "json"])

    payload = json.loads(result.output)
    assert "coverage" in payload
    assert set(payload["coverage"].keys()) == {
        "revisions_total",
        "revisions_fully_resolved",
        "revisions_partial",
        "revisions_skipped",
    }


# --- No error-severity finding on a tainted table ---


def test_no_error_finding_on_a_tainted_table(tmp_path: Path) -> None:
    """R3 (Decision #2): a finding whose table has an in-scope note is
    demoted to review. The ``kind="fail"`` + ``severity="error"``
    combination must not survive on a tainted table — only
    ``kind="review"`` does.

    Setup: model declares a column the migration doesn't add
    (missing_column, would be error at kind=fail), AND the migration
    contains an ``op.not_a_real_op("widgets")`` which generates an
    ``unrecognized_op`` note scoped to the widgets table. R3's taint
    machinery should demote the missing_column finding to review.
    """
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(50), nullable=False)\n"
    )
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    # Write a note-scoped-to-widgets by first manually injecting an
    # Unresolved. For a minimum-friction test, use op.not_a_real_op —
    # the note that produces is currently global (tables=None), so it
    # wouldn't demote. Instead, use a batch block, which emits
    # unrecognized_op scoped to the batch table.
    _write(
        tmp_path / "versions" / "0001_init.py",
        _minimal_revision(
            'op.create_table("widgets", '
            '__import__("sqlalchemy").Column("id", '
            '__import__("sqlalchemy").Integer(), primary_key=True))'
        ),
    )
    _write(
        tmp_path / "versions" / "0002_batch.py",
        '"""batch with unrecognized method"""\n\n'
        'revision = "0002"\n'
        'down_revision = "0001"\n\n\n'
        "def upgrade() -> None:\n"
        "    from alembic import op\n"
        '    with op.batch_alter_table("widgets") as batch_op:\n'
        "        batch_op.weird_method_that_will_note()\n",
    )

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json"])
    payload = json.loads(result.output)

    # The missing_column finding for widgets.name must NOT be
    # error+fail (which exits 1). It should be demoted to review.
    widgets_findings = [f for f in payload["findings"] if f["table"] == "widgets"]
    for finding in widgets_findings:
        assert not (finding["kind"] == "fail" and finding["severity"] == "error"), (
            f"T3 violation: finding on tainted 'widgets' kept kind=fail severity=error: {finding}"
        )
    # And at least one of those findings should actually be in review tier,
    # proving demotion fired rather than being vacuously true.
    assert any(f["kind"] == "review" for f in widgets_findings), (
        "expected at least one widgets finding to be demoted to review"
    )
    # Exit code reflects the demotion: no raw errors (demoted to review),
    # exit code 0 if nothing else is error-tier.
    assert result.exit_code != EXIT_DRIFT
