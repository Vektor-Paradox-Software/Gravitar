"""Shared test helpers: running the model+migration analysis pipeline against a
fixture directory under tests/fixtures/.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from gravitar.schema import Coverage, DriftFinding, Unresolved, sort_findings
from gravitar.schema.compare import compare_schemas
from gravitar.schema.migrations import build_migration_result, parse_versions_dir
from gravitar.schema.models import load_model_schema

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class AnalysisResult:
    findings: list[DriftFinding]
    tables_analyzed: int
    columns_analyzed: int
    notes: list[Unresolved]
    coverage: Coverage


def analyze_fixture(fixture_name: str, monkeypatch: pytest.MonkeyPatch) -> AnalysisResult:
    fixture_dir = FIXTURES / fixture_name
    monkeypatch.syspath_prepend(str(fixture_dir))
    sys.modules.pop("models", None)

    model_schema = load_model_schema(["models"])
    revisions = parse_versions_dir(fixture_dir / "versions")
    result = build_migration_result(revisions)

    findings = sort_findings(compare_schemas(model_schema, result.schema) + result.findings)
    tables_analyzed = len(model_schema)
    columns_analyzed = sum(len(table["columns"]) for table in model_schema.values())
    return AnalysisResult(
        findings=findings,
        tables_analyzed=tables_analyzed,
        columns_analyzed=columns_analyzed,
        notes=result.notes,
        coverage=result.coverage,
    )
