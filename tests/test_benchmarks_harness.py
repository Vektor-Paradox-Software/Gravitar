"""SPEC-16 R0: tests for the benchmark harness.

Only the harness pieces are tested here — not the real-world corpus
(that lives under ``benchmarks/corpus/`` and would be covered by a
real integration run of ``python benchmarks/run.py``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from benchmarks.classify import classify, untagged
from benchmarks.oracle import load_oracle
from benchmarks.oracle.static import load_static_oracle
from benchmarks.report import render_report, write_result_json
from benchmarks.schema import (
    ClassifiedFinding,
    OracleFinding,
    Project,
    ProjectResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARKS = REPO_ROOT / "benchmarks"


# --- Static oracle loader ---


def test_load_static_oracle_parses_findings(tmp_path: Path) -> None:
    path = tmp_path / "oracle.yml"
    path.write_text(
        "findings:\n"
        "  - category: missing_column\n"
        "    table: widgets\n"
        "    column: deleted_at\n"
        "  - category: missing_table\n"
        "    table: unrelated\n"
        "    column: null\n",
        encoding="utf-8",
    )

    findings = load_static_oracle(path)

    assert len(findings) == 2
    assert findings[0].category == "missing_column"
    assert findings[0].table == "widgets"
    assert findings[0].column == "deleted_at"


def test_load_static_oracle_rejects_malformed(tmp_path: Path) -> None:
    """A malformed oracle must stop the harness — never silently
    produce an empty list (would turn a project into a false clean)."""
    import pytest

    path = tmp_path / "oracle.yml"
    path.write_text("findings: not-a-list\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a list"):
        load_static_oracle(path)


# --- Classify ---


def _env_with_findings(*findings: dict) -> dict:
    """Minimal Gravitar JSON envelope shape."""
    return {
        "findings": list(findings),
        "coverage": {},
    }


def test_classify_tp_when_oracle_and_gravitar_match() -> None:
    project = Project(name="t", project_path=Path("/dev/null"))
    envelope = _env_with_findings(
        {
            "category": "missing_column",
            "table": "widgets",
            "column": "deleted_at",
            "severity": "error",
            "finding_id": "abc",
        }
    )
    oracles = [OracleFinding("missing_column", "widgets", "deleted_at")]

    classified = classify(project, envelope, oracles, {})

    assert len(classified) == 1
    assert classified[0].outcome == "tp"
    assert classified[0].finding_id == "abc"
    assert classified[0].cause is None


def test_classify_fp_error_when_gravitar_finds_what_oracle_doesnt() -> None:
    project = Project(name="t", project_path=Path("/dev/null"))
    envelope = _env_with_findings(
        {
            "category": "missing_column",
            "table": "widgets",
            "column": "spurious",
            "severity": "error",
        }
    )

    classified = classify(project, envelope, [], {})

    assert len(classified) == 1
    assert classified[0].outcome == "fp_error"
    assert classified[0].cause is None  # not auto-attributable; needs triage


def test_classify_fp_warning_for_warning_severity() -> None:
    project = Project(name="t", project_path=Path("/dev/null"))
    envelope = _env_with_findings(
        {
            "category": "unknown_type",
            "table": "widgets",
            "column": "data",
            "severity": "warning",
        }
    )

    classified = classify(project, envelope, [], {})

    assert classified[0].outcome == "fp_warning"


def test_classify_fn_when_oracle_has_what_gravitar_misses() -> None:
    project = Project(name="t", project_path=Path("/dev/null"))
    envelope = _env_with_findings()
    oracles = [OracleFinding("missing_column", "widgets", "missed_col")]

    classified = classify(project, envelope, oracles, {})

    assert len(classified) == 1
    assert classified[0].outcome == "fn"
    assert classified[0].table == "widgets"
    assert classified[0].column == "missed_col"


def test_classify_auto_attributes_tainted_table_fp() -> None:
    """A finding whose table is in coverage.tainted_tables gets
    ``cause=unresolved_taint`` automatically — R3 taint explains it."""
    project = Project(name="t", project_path=Path("/dev/null"))
    envelope = {
        "findings": [
            {
                "category": "type_mismatch",
                "table": "widgets",
                "column": "x",
                "severity": "error",
            }
        ],
        "coverage": {"tainted_tables": ["widgets"]},
    }

    classified = classify(project, envelope, [], {})

    assert classified[0].outcome == "fp_error"
    assert classified[0].cause == "unresolved_taint"


def test_classify_applies_triage_entry() -> None:
    project = Project(name="pname", project_path=Path("/dev/null"))
    envelope = _env_with_findings(
        {
            "category": "type_mismatch",
            "table": "widgets",
            "column": "data",
            "severity": "error",
        }
    )
    triage = {
        "pname|fp_error|type_mismatch|widgets|data": {
            "cause": "custom_type",
            "note": "JsonBlob needs R13 pin",
        }
    }

    classified = classify(project, envelope, [], triage)

    assert classified[0].cause == "custom_type"
    assert "R13 pin" in (classified[0].note or "")


def test_untagged_lists_only_fp_fn_without_cause() -> None:
    classified = [
        ClassifiedFinding("tp", "missing_column", "a", "b", cause=None),
        ClassifiedFinding("fp_error", "missing_column", "a", "c", cause=None),
        ClassifiedFinding("fn", "missing_column", "a", "d", cause="batch_op"),
    ]

    result = untagged(classified)

    assert len(result) == 1
    assert result[0].outcome == "fp_error"


def test_classify_oracle_scope_types_false_excludes_type_mismatch() -> None:
    """SPEC §Type-comparison scope: on SQLite-reflected projects,
    type_mismatch results are untrustworthy and must not count
    against the FP budget."""
    project = Project(name="t", project_path=Path("/dev/null"), oracle_scope_types=False)
    envelope = _env_with_findings(
        {
            "category": "type_mismatch",
            "table": "widgets",
            "column": "data",
            "severity": "error",
        }
    )

    classified = classify(project, envelope, [], {})

    # The result is recorded but as a non-counting pass — the oracle
    # couldn't check this category, so it never contributes to FP.
    assert classified[0].outcome == "tp"
    assert "oracle_scope.types=false" in (classified[0].note or "")


# --- Report ---


def test_render_report_includes_totals_and_project_section() -> None:
    project = Project(name="my_proj", project_path=Path("/dev/null"))
    result = ProjectResult(
        project=project,
        status="ran",
        classified=[
            ClassifiedFinding("tp", "missing_column", "widgets", "deleted_at", cause=None),
            ClassifiedFinding(
                "fp_error",
                "type_mismatch",
                "widgets",
                "price",
                cause="custom_type",
                note="JsonBlob",
            ),
        ],
    )

    report = render_report([result])

    assert "Gravitar benchmark report" in report
    assert "my_proj" in report
    assert "TP 1" in report
    assert "FP(err) 1" in report
    assert "custom_type" in report


def test_render_report_marks_untagged_items_in_table() -> None:
    project = Project(name="my_proj", project_path=Path("/dev/null"))
    result = ProjectResult(
        project=project,
        status="ran",
        classified=[
            ClassifiedFinding("fp_error", "missing_column", "a", "b", cause=None),
        ],
    )

    report = render_report([result])

    assert "**UNTAGGED**" in report


def test_render_report_lists_other_items_separately() -> None:
    project = Project(name="my_proj", project_path=Path("/dev/null"))
    result = ProjectResult(
        project=project,
        status="ran",
        classified=[
            ClassifiedFinding(
                "fp_error",
                "missing_column",
                "a",
                "b",
                cause="other",
                note="genuinely new cause",
            ),
        ],
    )

    report = render_report([result])

    assert "other`-tagged items" in report
    assert "genuinely new cause" in report


def test_write_result_json_round_trips(tmp_path: Path) -> None:
    import json

    project = Project(name="x", project_path=Path("/dev/null"))
    result = ProjectResult(
        project=project,
        status="ran",
        classified=[ClassifiedFinding("tp", "missing_column", "a", "b", cause=None)],
    )

    write_result_json(result, tmp_path / "x.json")

    data = json.loads((tmp_path / "x.json").read_text())
    assert data["project"] == "x"
    assert data["tp"] == 1
    assert data["classified"][0]["outcome"] == "tp"


# --- Oracle dispatch ---


def test_load_oracle_postgres_raises_not_implemented() -> None:
    """SPEC §oracle_runner: v1 doesn't ship the postgres runner. The
    stub must raise NotImplementedError, never a silent empty oracle."""
    import pytest

    project = Project(
        name="p",
        project_path=Path("/dev/null"),
        oracle_runner="postgres",
    )
    with pytest.raises(NotImplementedError, match="not implemented in v1"):
        load_oracle(project)


# --- End-to-end: run.py against the self-test corpus ---


def test_run_py_against_self_test_corpus_exits_zero(tmp_path: Path) -> None:
    """The committed self-test corpus must pass the harness end-to-end.
    This is also the acceptance check for SPEC-16 R0's deliverable:
    `python benchmarks/run.py` reproduces a passing report."""
    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "run.py"),
            "--out",
            str(tmp_path),
            "--label",
            "pytest",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"run.py exited {result.returncode}\nstderr: {result.stderr}\nstdout: {result.stdout}"
    )
    report = (tmp_path / "pytest" / "REPORT.md").read_text(encoding="utf-8")
    assert "TP 1" in report
    assert "**UNTAGGED**" not in report


def test_run_py_exits_nonzero_when_untagged_items_present(tmp_path: Path) -> None:
    """SPEC §Attribution: untagged FP/FN must fail the run. Simulate
    this with a modified manifest that points at a corpus with an
    extra oracle finding Gravitar won't match (FN)."""
    # Build a one-off manifest in tmp_path referring to the real
    # corpus but with an inflated oracle.
    inflated_oracle = tmp_path / "inflated.yml"
    inflated_oracle.write_text(
        "findings:\n"
        "  - category: missing_column\n"
        "    table: widgets\n"
        "    column: deleted_at\n"
        "  - category: missing_column\n"
        "    table: widgets\n"
        "    column: never_declared\n",  # Gravitar will not find this → FN
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yml"
    corpus = (BENCHMARKS / "corpus" / "dirty_missing_column").resolve()
    manifest.write_text(
        "projects:\n"
        f"  - name: inflated\n"
        f'    project_path: "{corpus.as_posix()}"\n'
        f'    oracle: "{inflated_oracle.as_posix()}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "run.py"),
            "--manifest",
            str(manifest),
            "--out",
            str(tmp_path / "results"),
            "--label",
            "fail-case",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, (
        f"expected non-zero exit for untagged FN; got 0\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "untagged" in result.stderr.lower()


def test_run_py_allow_untagged_lets_it_pass(tmp_path: Path) -> None:
    """``--allow-untagged`` is the escape hatch for authoring the
    first-ever triage pass. Same setup as the fail test, but with
    the flag — must exit 0 and still write the report."""
    inflated_oracle = tmp_path / "inflated.yml"
    inflated_oracle.write_text(
        "findings:\n"
        "  - category: missing_column\n"
        "    table: widgets\n"
        "    column: deleted_at\n"
        "  - category: missing_column\n"
        "    table: widgets\n"
        "    column: never_declared\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yml"
    corpus = (BENCHMARKS / "corpus" / "dirty_missing_column").resolve()
    manifest.write_text(
        "projects:\n"
        f"  - name: inflated\n"
        f'    project_path: "{corpus.as_posix()}"\n'
        f'    oracle: "{inflated_oracle.as_posix()}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "run.py"),
            "--manifest",
            str(manifest),
            "--out",
            str(tmp_path / "results"),
            "--label",
            "fail-case",
            "--allow-untagged",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
