"""SPEC-16 Amendment R14 (remainder): GitHub Actions annotations.

GitHub's log parser recognizes `workflow commands`_ (``::error``,
``::warning``, ``::notice``) and converts them into inline annotations
on PR diffs. Emitting them as one of Gravitar's output formats lets a
CI user see findings inline without a separate SARIF upload step.

Mapping (tier → verb):
- kind=fail, severity=error   → ::error
- kind=fail, severity=warning → ::warning
- kind=review                 → ::notice
- baselined                   → ::notice

.. _workflow commands:
   https://docs.github.com/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.errors import EXIT_DRIFT, EXIT_SUCCESS
from gravitar.output.github import format_github
from gravitar.schema import DriftFinding, Evidence, SourceLocation, make_unresolved

FIXTURES = Path(__file__).parent / "fixtures"


def _finding(**overrides: Any) -> DriftFinding:
    base: DriftFinding = DriftFinding(
        category="missing_column",
        severity="error",
        table="widgets",
        column="deleted_at",
        model_value={"type": "timestamp", "nullable": True},
        migration_value=None,
        message="model declares column, no migration adds it",
        suggestion="add a migration",
        confidence=0.95,
        source=SourceLocation(file="models/__init__.py", line=10, revision_id=None),
        evidence=[Evidence(signal="ast_fully_resolved", weight=1.0, detail="fully resolved")],
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# --- Verb mapping ---


def test_error_tier_emits_error_command() -> None:
    output = format_github([_finding(severity="error")], 1, 1)
    assert output.startswith("::error ")


def test_warning_tier_emits_warning_command() -> None:
    output = format_github([_finding(severity="warning", category="unknown_type")], 1, 1)
    assert output.startswith("::warning ")


def test_review_kind_emits_notice_command() -> None:
    """R3-demoted findings are 'warrants human review' — they shouldn't
    fail CI; ``::notice`` is GitHub's lowest-severity annotation."""
    output = format_github(
        [_finding(kind="review", demoted_from="error", explaining_notes=["abc"])],
        1,
        1,
    )
    assert output.startswith("::notice ")


def test_baselined_finding_renders_as_notice_regardless_of_tier() -> None:
    """A baselined error-tier finding must NOT render as ``::error`` —
    GitHub would then mark the step as failing even though the finding
    is pre-existing drift the team has accepted."""
    finding = _finding(severity="error")
    from gravitar.feedback.ids import finding_id

    output = format_github([finding], 1, 1, baselined_ids=frozenset([finding_id(finding)]))
    assert output.startswith("::notice ")
    assert "(baselined)" in output


# --- Line format ---


def test_annotation_includes_file_and_line() -> None:
    output = format_github([_finding()], 1, 1).strip()
    assert "file=models/__init__.py" in output
    assert "line=10" in output


def test_annotation_includes_title_with_category_and_location() -> None:
    output = format_github([_finding()], 1, 1).strip()
    assert "title=missing_column" in output
    assert "widgets.deleted_at" in output


def test_annotation_message_includes_suggestion_and_id() -> None:
    """The message body is the only reader-visible text in GitHub's
    diff hover — must carry the drift message, the suggestion (so the
    reviewer can act), and the finding id (so they can confirm/dismiss
    via feedback)."""
    output = format_github([_finding()], 1, 1).strip()
    assert "model declares column, no migration adds it" in output
    assert "add a migration" in output
    assert "[id:" in output


def test_newlines_in_message_are_escaped() -> None:
    """A workflow command is one line; a literal newline in the message
    would break the parser. Must be %0A-escaped."""
    output = format_github([_finding(message="line one\nline two")], 1, 1).strip()
    assert "\nline two" not in output  # not a literal newline
    assert "%0A" in output


def test_percent_in_message_is_escaped_first() -> None:
    """Escape order matters: % must be escaped BEFORE other sequences,
    or the %0A from a later escape would itself be re-escaped."""
    output = format_github([_finding(message="50% off")], 1, 1).strip()
    assert "50%25 off" in output


# --- Notes ---


def test_notes_render_as_notice_annotations() -> None:
    """An ``Unresolved`` note is informational — ``::notice`` so the
    reviewer sees it inline without failing the build on it alone
    (exit code is set by the finding tiers, not the notes themselves)."""
    note = make_unresolved(
        "unrecognized_op",
        "op.weird_thing",
        SourceLocation(file="versions/0001.py", line=42, revision_id="0001"),
    )
    output = format_github([], 1, 1, notes=[note]).strip()
    assert output.startswith("::notice ")
    assert "op.weird_thing" in output


# --- Multi-finding output ---


def test_each_finding_is_its_own_line() -> None:
    """One workflow command per line — GitHub parses line-by-line."""
    output = format_github([_finding(table="a"), _finding(table="b"), _finding(table="c")], 1, 1)
    lines = [line for line in output.splitlines() if line]
    assert len(lines) == 3
    assert all(line.startswith("::") for line in lines)


def test_empty_findings_returns_empty_string() -> None:
    """A clean run produces no annotations. GitHub Actions treats the
    absence of annotations as success; emitting a benign line like
    'no drift' would create a spurious notice on every PR."""
    assert format_github([], 1, 1) == ""


# --- CLI end-to-end ---


def test_cli_format_github_against_fixture() -> None:
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "missing_column"), "--format", "github"]
    )

    assert result.exit_code == EXIT_DRIFT
    assert result.output.startswith("::error ") or result.output.startswith("::warning ")
    assert "file=" in result.output
    assert "[id:" in result.output


def test_cli_format_github_on_clean_fixture_emits_nothing() -> None:
    """A clean fixture should produce ZERO workflow commands — no
    spurious annotations on green PRs."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--format", "github"])

    assert result.exit_code == EXIT_SUCCESS
    assert result.output == ""
