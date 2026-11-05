"""End-to-end (Python-API level) pipeline tests: load_model_schema + build_migration_schema
+ compare_schemas against each drift-category fixture in tests/fixtures/.

CLI-level end-to-end tests (invoking `gravitar check`, asserting exit codes and stdout)
live in test_cli.py now that the CLI is wired to this same pipeline.
"""

import pytest
from conftest import analyze_fixture


def test_clean_fixture_has_no_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert analyze_fixture("clean", monkeypatch).findings == []


def test_missing_table_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = analyze_fixture("missing_table", monkeypatch).findings

    assert len(findings) == 1
    assert findings[0]["category"] == "missing_table"
    assert findings[0]["table"] == "products"


def test_missing_column_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = analyze_fixture("missing_column", monkeypatch).findings

    assert len(findings) == 1
    assert findings[0]["category"] == "missing_column"
    assert findings[0]["table"] == "widgets"
    assert findings[0]["column"] == "deleted_at"


def test_type_mismatch_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = analyze_fixture("type_mismatch", monkeypatch).findings

    assert len(findings) == 1
    assert findings[0]["category"] == "type_mismatch"
    assert findings[0]["table"] == "invoices"
    assert findings[0]["column"] == "total_cents"
    assert findings[0]["model_value"] == "numeric(10,2)"
    assert findings[0]["migration_value"] == "integer"


def test_nullability_mismatch_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = analyze_fixture("nullability_mismatch", monkeypatch).findings

    assert len(findings) == 1
    assert findings[0]["category"] == "nullability_mismatch"
    assert findings[0]["table"] == "customers"
    assert findings[0]["column"] == "middle_name"


def test_orphaned_column_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = analyze_fixture("orphaned_column", monkeypatch).findings

    assert len(findings) == 1
    assert findings[0]["category"] == "orphaned_column"
    assert findings[0]["table"] == "gadgets"
    assert findings[0]["column"] == "internal_flag"


def test_broken_chain_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    findings = analyze_fixture("broken_chain", monkeypatch).findings

    assert len(findings) == 1
    assert findings[0]["category"] == "broken_chain"
    assert findings[0]["table"] is None
