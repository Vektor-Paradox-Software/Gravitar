"""End-to-end `gravitar check` tests against the Django fixture suite
(tests/fixtures_django/), mirroring tests/fixtures/'s Alembic suite:
same drift categories, same expected-output shape, different source DSL.
Parity between the two fixture suites is itself a test of the abstraction's
generality.

Run via subprocess, not CliRunner: Django's app registry is a process-global
singleton (`django.setup()` can't be reconfigured with a different app list
within one process — see django_models.py), and each fixture is its own
"blog" app. A fresh interpreter per fixture sidesteps that entirely, and
matches how a real user actually invokes the CLI anyway.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures_django"


def _run_check(fixture_name: str) -> tuple[int, dict]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gravitar",
            "check",
            str(FIXTURES / fixture_name),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, json.loads(result.stdout)


def test_clean_django_fixture_reports_no_drift() -> None:
    code, payload = _run_check("clean")

    assert code == 0
    assert payload["status"] == "clean"
    assert payload["findings"] == []


def test_missing_table_django_fixture() -> None:
    code, payload = _run_check("missing_table")

    assert code == 1
    assert payload["findings"][0]["category"] == "missing_table"
    assert payload["findings"][0]["table"] == "blog_comment"


def test_missing_column_django_fixture() -> None:
    code, payload = _run_check("missing_column")

    assert code == 1
    finding = payload["findings"][0]
    assert finding["category"] == "missing_column"
    assert finding["table"] == "blog_post"
    assert finding["column"] == "subtitle"


def test_type_mismatch_django_fixture() -> None:
    code, payload = _run_check("type_mismatch")

    assert code == 1
    finding = payload["findings"][0]
    assert finding["category"] == "type_mismatch"
    assert finding["model_value"] == "varchar(200)"
    assert finding["migration_value"] == "varchar(100)"


def test_nullability_mismatch_django_fixture() -> None:
    code, payload = _run_check("nullability_mismatch")

    assert code == 1
    finding = payload["findings"][0]
    assert finding["category"] == "nullability_mismatch"
    assert finding["model_value"] is True
    assert finding["migration_value"] is False


def test_orphaned_column_django_fixture() -> None:
    code, payload = _run_check("orphaned_column")

    assert code == 1
    finding = payload["findings"][0]
    assert finding["category"] == "orphaned_column"
    assert finding["column"] == "legacy_flag"


def test_unknown_type_django_fixture() -> None:
    code, payload = _run_check("unknown_type")

    # SPEC-16 Amendment Decision #1: unknown_type exits per its severity tier
    # (warning → exit 0), no longer forced to exit 1.
    assert code == 0
    finding = payload["findings"][0]
    assert finding["category"] == "unknown_type"
    assert finding["severity"] == "warning"
    assert finding["model_value"] == "unknown:DurationField"


# --- Every Django finding carries the same shape as the Alembic path ---


@pytest.mark.parametrize(
    "fixture_name",
    ["missing_table", "missing_column", "type_mismatch", "nullability_mismatch", "orphaned_column"],
)
def test_django_findings_have_the_same_shape_as_alembic_findings(fixture_name: str) -> None:
    _, payload = _run_check(fixture_name)

    finding = payload["findings"][0]
    assert 0.0 <= finding["confidence"] <= 1.0
    assert finding["evidence"]
    assert finding["source"]["file"]
    assert "finding_id" in finding
    assert payload["schema_version"] == 2
