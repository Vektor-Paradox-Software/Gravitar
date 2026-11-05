"""SPEC-16 Amendment R14 (subset): SARIF 2.1.0 output.

Baseline/ratchet mode and ``--show-all`` are deferred — this file
covers the straight SARIF emission path only. The amendment's
``rank`` = confidence × 100 and ``kind: review`` for demoted findings
are the load-bearing bits for GitHub Code Scanning integration, so the
tests pin those explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.errors import EXIT_DRIFT, EXIT_SUCCESS
from gravitar.output.sarif import SARIF_VERSION, format_sarif
from gravitar.schema import (
    Coverage,
    DriftFinding,
    Evidence,
    SourceLocation,
    make_unresolved,
)

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
        suggestion='add a migration: alembic revision --autogenerate -m "add widgets.deleted_at"',
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


# --- Envelope shape ---


def test_sarif_log_declares_version_2_1_0_and_schema() -> None:
    log = json.loads(format_sarif([], tables_analyzed=0, columns_analyzed=0))

    assert log["version"] == SARIF_VERSION
    assert log["$schema"].endswith("sarif-schema-2.1.0.json")
    assert len(log["runs"]) == 1


def test_sarif_tool_driver_names_gravitar() -> None:
    log = json.loads(format_sarif([], tables_analyzed=0, columns_analyzed=0))

    driver = log["runs"][0]["tool"]["driver"]
    assert driver["name"] == "gravitar"
    assert driver["version"]
    rule_ids = {rule["id"] for rule in driver["rules"]}
    assert "missing_column" in rule_ids
    assert "orphaned_reference" in rule_ids
    assert "broken_chain" in rule_ids


# --- Finding → SARIF result mapping ---


def test_fail_error_finding_maps_to_level_error_kind_fail() -> None:
    log = json.loads(format_sarif([_finding()], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    assert result["level"] == "error"
    assert result["kind"] == "fail"
    assert result["ruleId"] == "missing_column"


def test_fail_warning_finding_maps_to_level_warning_kind_fail() -> None:
    finding = _finding(severity="warning", category="unknown_type")
    log = json.loads(format_sarif([finding], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    assert result["level"] == "warning"
    assert result["kind"] == "fail"


def test_review_demoted_finding_maps_to_level_note_kind_review() -> None:
    """R3 demotion → SARIF review tier. Dashboards use this to show
    "warrants human review" rather than "must fix"."""
    demoted = _finding(kind="review", demoted_from="error", explaining_notes=["abc123"])
    log = json.loads(format_sarif([demoted], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    assert result["level"] == "note"
    assert result["kind"] == "review"


def test_confidence_maps_to_rank_times_100() -> None:
    """Amendment C.R14: ``rank`` = confidence × 100. The readiness doc's
    "``rank`` 0-100 confidence" wording."""
    finding = _finding(confidence=0.75)
    log = json.loads(format_sarif([finding], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    assert result["rank"] == 75.0


def test_finding_id_appears_as_partial_fingerprint() -> None:
    """SARIF partialFingerprints are what dashboards use to track a
    finding across runs — these must match the finding_id Gravitar
    exposes in its own JSON output, so baseline/ratchet (deferred)
    will have stable anchors."""
    log = json.loads(format_sarif([_finding()], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    assert "findingId" in result["partialFingerprints"]
    # Matches what feedback.ids.finding_id would produce for this finding.
    from gravitar.feedback.ids import finding_id

    assert result["partialFingerprints"]["findingId"] == finding_id(_finding())


def test_source_location_is_in_physical_location() -> None:
    log = json.loads(format_sarif([_finding()], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    [location] = result["locations"]
    assert location["physicalLocation"]["artifactLocation"]["uri"] == "models/__init__.py"
    assert location["physicalLocation"]["region"]["startLine"] == 10


def test_evidence_appears_in_message_text() -> None:
    """A dashboard reviewer should be able to see the reasoning
    (signal/detail pairs) inline, without expanding into a separate
    properties blob."""
    log = json.loads(format_sarif([_finding()], tables_analyzed=1, columns_analyzed=1))

    [result] = log["runs"][0]["results"]
    assert "ast_fully_resolved" in result["message"]["text"]
    assert "Suggestion:" in result["message"]["text"]


# --- Notes → invocations.toolExecutionNotifications ---


def test_notes_appear_as_tool_execution_notifications() -> None:
    note = make_unresolved(
        "unrecognized_op",
        "op.weird_thing",
        SourceLocation(file="versions/0001.py", line=42, revision_id="0001"),
    )
    log = json.loads(format_sarif([], tables_analyzed=0, columns_analyzed=0, notes=[note]))

    [invocation] = log["runs"][0]["invocations"]
    [notification] = invocation["toolExecutionNotifications"]
    assert notification["level"] == "warning"
    assert "op.weird_thing" in notification["message"]["text"]
    assert notification["properties"]["reason"] == "unrecognized_op"


def test_parse_error_note_is_notification_level_error() -> None:
    """``parse_error`` notes (R11) are more serious than ordinary
    unresolved ops — a file Gravitar couldn't parse at all. SARIF
    ``error`` level reflects that."""
    note = make_unresolved(
        "parse_error",
        "invalid syntax at line 1",
        SourceLocation(file="versions/0002.py", line=1, revision_id=None),
    )
    log = json.loads(format_sarif([], tables_analyzed=0, columns_analyzed=0, notes=[note]))

    [notification] = log["runs"][0]["invocations"][0]["toolExecutionNotifications"]
    assert notification["level"] == "error"


# --- Coverage → run.properties ---


def test_coverage_appears_in_run_properties() -> None:
    coverage = Coverage(
        revisions_total=10,
        revisions_fully_resolved=8,
        revisions_partial=1,
        revisions_skipped=1,
    )
    log = json.loads(format_sarif([], tables_analyzed=5, columns_analyzed=20, coverage=coverage))

    props = log["runs"][0]["properties"]
    assert props["tablesAnalyzed"] == 5
    assert props["columnsAnalyzed"] == 20
    assert props["coverage"] == dict(coverage)


# --- End-to-end via CLI ---


def test_cli_format_sarif_against_a_real_fixture() -> None:
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "missing_column"), "--format", "sarif"]
    )

    assert result.exit_code == EXIT_DRIFT
    log = json.loads(result.output)
    assert log["version"] == SARIF_VERSION
    assert len(log["runs"][0]["results"]) == 1
    assert log["runs"][0]["results"][0]["ruleId"] == "missing_column"


def test_cli_format_sarif_on_clean_fixture_still_emits_envelope() -> None:
    """SARIF output must be a well-formed log even when there are no
    findings — Code Scanning uploads expect an envelope either way."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--format", "sarif"])

    assert result.exit_code == EXIT_SUCCESS
    log = json.loads(result.output)
    assert log["version"] == SARIF_VERSION
    assert log["runs"][0]["results"] == []
