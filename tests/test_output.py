import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from conftest import FIXTURES, analyze_fixture

from gravitar import __version__
from gravitar.output.json import SCHEMA_VERSION, format_json, format_json_error
from gravitar.output.text import format_text
from gravitar.schema import DriftFinding, Evidence, SourceLocation

SCHEMA_FILE = Path(__file__).parent / "schemas" / "gravitar-output-v2.json"

FIXTURE_NAMES = [
    "clean",
    "missing_table",
    "missing_column",
    "type_mismatch",
    "nullability_mismatch",
    "orphaned_column",
    "broken_chain",
    "unknown_type",
    "raw_sql",
    "raw_sql_matched_pattern",
    "raw_sql_orphaned_reference",
]


def _finding(**overrides: Any) -> DriftFinding:
    base: DriftFinding = DriftFinding(
        category="missing_column",
        severity="error",
        table="users",
        column="deleted_at",
        model_value={"type": "timestamp", "nullable": True},
        migration_value=None,
        message="model declares column, no migration adds it",
        suggestion='add a migration: alembic revision --autogenerate -m "add users.deleted_at"',
        confidence=1.0,
        source=SourceLocation(file="models/__init__.py", line=10, revision_id=None),
        evidence=[Evidence(signal="ast_fully_resolved", weight=1.0, detail="test fixture")],
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# --- format_text ---


def test_text_clean_message() -> None:
    output = format_text([], tables_analyzed=5, columns_analyzed=20, no_color=True)

    assert output == "gravitar: no drift detected (5 tables, 20 columns analyzed)\n"


def test_text_missing_column_finding() -> None:
    output = format_text([_finding()], tables_analyzed=1, columns_analyzed=1, no_color=True)

    assert "gravitar: drift detected — 1 findings" in output
    assert "  error  missing_column  users.deleted_at" in output
    assert "         model declares column, no migration adds it" in output
    assert (
        '         → add a migration: alembic revision --autogenerate -m "add users.deleted_at"'
        in output
    )
    assert "summary: 1 errors, 0 warnings, 0 review across 1 tables" in output


def test_text_type_mismatch_shows_both_values() -> None:
    finding = _finding(
        category="type_mismatch", model_value="numeric(10,2)", migration_value="integer"
    )
    output = format_text([finding], tables_analyzed=1, columns_analyzed=1, no_color=True)

    assert "         model: numeric(10,2)" in output
    assert "         migration: integer" in output


def test_text_nullability_mismatch_shows_both_values() -> None:
    finding = _finding(category="nullability_mismatch", model_value=True, migration_value=False)
    output = format_text([finding], tables_analyzed=1, columns_analyzed=1, no_color=True)

    assert "         model: nullable=True" in output
    assert "         migration: nullable=False" in output


def test_text_table_level_finding_has_no_column_suffix() -> None:
    finding = _finding(
        category="missing_table",
        table="products",
        column=None,
        model_value=None,
        message="model declares table, no migration creates it",
    )
    output = format_text([finding], tables_analyzed=1, columns_analyzed=0, no_color=True)

    assert "  error  missing_table  products" in output
    assert "products." not in output


def test_text_quiet_suppresses_summary() -> None:
    output = format_text(
        [_finding()], tables_analyzed=1, columns_analyzed=1, quiet=True, no_color=True
    )

    assert "summary:" not in output


def test_text_multiple_findings_separated_by_blank_line() -> None:
    output = format_text(
        [_finding(), _finding(table="orders", column="total")],
        tables_analyzed=2,
        columns_analyzed=2,
        no_color=True,
    )

    assert "\n\n  error  missing_column  orders.total" in output


def test_text_color_codes_present_when_enabled() -> None:
    output = format_text([_finding()], tables_analyzed=1, columns_analyzed=1, no_color=False)

    assert "\x1b[31m" in output


def test_text_no_color_has_no_ansi_codes() -> None:
    output = format_text([_finding()], tables_analyzed=1, columns_analyzed=1, no_color=True)

    assert "\x1b[" not in output


# --- format_json ---


def test_json_error_status() -> None:
    envelope = json.loads(format_json_error("alembic.ini not found"))

    assert envelope["status"] == "error"
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["error"] == "alembic.ini not found"
    assert envelope["findings"] == []
    assert envelope["summary"] == {
        "tables_analyzed": 0,
        "columns_analyzed": 0,
        "errors": 0,
        "warnings": 0,
        "reviews": 0,
    }
    assert envelope["coverage"] == {
        "revisions_total": 0,
        "revisions_fully_resolved": 0,
        "revisions_partial": 0,
        "revisions_skipped": 0,
    }
    assert envelope["notes"] == []


def test_json_error_validates_against_schema(json_schema: dict[str, Any]) -> None:
    envelope = json.loads(format_json_error("something broke"))

    jsonschema.validate(envelope, json_schema)


def test_json_clean_status() -> None:
    envelope = json.loads(format_json([], tables_analyzed=5, columns_analyzed=20))

    assert envelope["status"] == "clean"
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["gravitar_version"] == __version__
    assert envelope["summary"] == {
        "tables_analyzed": 5,
        "columns_analyzed": 20,
        "errors": 0,
        "warnings": 0,
        "reviews": 0,
    }
    assert envelope["findings"] == []
    assert envelope["notes"] == []


def test_json_drift_status_and_findings() -> None:
    envelope = json.loads(format_json([_finding()], tables_analyzed=1, columns_analyzed=1))

    assert envelope["status"] == "drift"
    assert envelope["summary"]["errors"] == 1
    assert envelope["findings"][0]["category"] == "missing_column"
    assert envelope["findings"][0]["model_value"] == {"type": "timestamp", "nullable": True}


# --- JSON schema validation (tests/schemas/gravitar-output-v2.json) ---


@pytest.fixture(scope="module")
def json_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_json_output_matches_schema(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch, json_schema: dict[str, Any]
) -> None:
    result = analyze_fixture(fixture_name, monkeypatch)
    envelope = json.loads(
        format_json(
            result.findings,
            result.tables_analyzed,
            result.columns_analyzed,
            notes=result.notes,
            coverage=result.coverage,
        )
    )

    jsonschema.validate(envelope, json_schema)


# --- golden text output (byte-for-byte) ---


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_text_output_matches_golden_file(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = analyze_fixture(fixture_name, monkeypatch)
    output = format_text(
        result.findings,
        result.tables_analyzed,
        result.columns_analyzed,
        notes=result.notes,
        coverage=result.coverage,
        no_color=True,
    )

    golden_path = FIXTURES / fixture_name / "expected_output.txt"
    assert output == golden_path.read_text(encoding="utf-8")
