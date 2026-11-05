"""SPEC-18 T0 tests: harness skeleton.

Everything here can run without Docker/Postgres — T0 is the config +
CLI + empty-summary layer. T1 (`pg.py` + container lifecycle) adds the
real oracle call and needs its own test suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.oracle.cli import main
from benchmarks.oracle.config import ProjectUnit, load_oracle_units
from benchmarks.oracle.run_report import empty_summary, write_summary

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_manifest(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- config.load_oracle_units ---


def test_load_oracle_units_returns_empty_when_section_missing(tmp_path: Path) -> None:
    """A manifest with no ``oracle_units:`` key is valid — the simpler
    projects: harness can still be configured without SPEC-18."""
    manifest = tmp_path / "manifest.yml"
    _write_manifest(manifest, "projects: []\n")

    assert load_oracle_units(manifest) == []


def test_load_oracle_units_parses_single_entry(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - project: mlflow\n"
        "    unit: db-migrations\n"
        "    tree_path: mlflow/store/db_migrations\n"
        "    metadata_import: 'mlflow.store.db:Base.metadata'\n"
        "    env_var: MLFLOW_DATABASE_URL\n"
        "    dialects: [postgres]\n"
        "    python: '3.11'\n"
        "    lockfile: locks/mlflow/0ba577fe/requirements.txt\n",
    )

    units = load_oracle_units(manifest)

    assert len(units) == 1
    assert units[0].project == "mlflow"
    assert units[0].unit == "db-migrations"
    assert units[0].dialects == ("postgres",)


def test_load_oracle_units_rejects_missing_required_keys(tmp_path: Path) -> None:
    """SPEC-18 §3.1 ProjectUnit has required fields; a half-formed entry
    must stop the run with the missing keys named, not fall through."""
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - project: p\n"
        "    unit: u\n",  # missing tree_path, metadata_import, env_var
    )

    with pytest.raises(ValueError, match="missing required key"):
        load_oracle_units(manifest)


def test_load_oracle_units_sorts_deterministically(tmp_path: Path) -> None:
    """SPEC-18 §2.8 deterministic ordering: units sort by (project, unit)
    regardless of their order in the manifest."""
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - { project: z, unit: a, tree_path: ., metadata_import: 'x:m', env_var: X_URL }\n"
        "  - { project: a, unit: b, tree_path: ., metadata_import: 'x:m', env_var: X_URL }\n"
        "  - { project: a, unit: a, tree_path: ., metadata_import: 'x:m', env_var: X_URL }\n",
    )

    units = load_oracle_units(manifest)

    assert [(u.project, u.unit) for u in units] == [("a", "a"), ("a", "b"), ("z", "a")]


def test_load_oracle_units_rejects_dialects_not_list_of_strings(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - project: p\n"
        "    unit: u\n"
        "    tree_path: .\n"
        "    metadata_import: 'x:m'\n"
        "    env_var: X\n"
        "    dialects: 'postgres'\n",  # should be a list
    )

    with pytest.raises(ValueError, match="dialects"):
        load_oracle_units(manifest)


# --- run_report.empty_summary ---


def _unit(project: str = "p", unit: str = "u") -> ProjectUnit:
    return ProjectUnit(
        project=project,
        unit=unit,
        tree_path=".",
        metadata_import="x:m",
        env_var="X",
        dialects=("postgres",),
        python="3.11",
        lockfile="",
    )


def test_empty_summary_shape_has_section_8_keys() -> None:
    summary = empty_summary([_unit()], run_id="r1")

    # Section 8 top-level keys.
    assert set(summary.keys()) >= {
        "run_id",
        "gravitar_commit",
        "gravitar_reader",
        "corpus_commit",
        "postgres",
        "units",
        "roll_up",
        "gate_decision",
    }
    assert summary["run_id"] == "r1"


def test_empty_summary_marks_every_unit_unrunnable_in_t0() -> None:
    """SPEC-18 T0 scope: the oracle isn't implemented yet, so every
    unit must be flagged ``unrunnable`` with a clear reason — never
    silently empty."""
    summary = empty_summary([_unit(), _unit(project="other")], run_id="r2")

    for row in summary["units"]:
        assert row["unrunnable"] == {"reason": "T0_skeleton_only"}


def test_write_summary_produces_sorted_keys(tmp_path: Path) -> None:
    """SPEC-18 §10 reproducibility: two writes of the same input must
    be byte-identical. Sort order enforces that at the write layer."""
    summary = empty_summary([_unit()], run_id="r3")

    write_summary(summary, tmp_path / "s1.json")
    write_summary(summary, tmp_path / "s2.json")

    assert (tmp_path / "s1.json").read_bytes() == (tmp_path / "s2.json").read_bytes()
    data = json.loads((tmp_path / "s1.json").read_text())
    # Spot-check that keys at each level are sorted.
    assert list(data.keys()) == sorted(data.keys())


# --- CLI ---


def test_cli_list_enumerates_units(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - project: a\n"
        "    unit: u1\n"
        "    tree_path: .\n"
        "    metadata_import: 'x:m'\n"
        "    env_var: X\n",
    )

    exit_code = main(["--config", str(manifest), "list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "a/u1" in captured.out
    assert "postgres" in captured.out  # default dialect


def test_cli_run_writes_summary(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - project: a\n"
        "    unit: u1\n"
        "    tree_path: .\n"
        "    metadata_import: 'x:m'\n"
        "    env_var: X\n",
    )

    exit_code = main(
        [
            "--config",
            str(manifest),
            "run",
            "--out",
            str(tmp_path / "reports"),
            "--run-id",
            "t0-pytest",
        ]
    )

    assert exit_code == 0
    summary_path = tmp_path / "reports" / "t0-pytest" / "summary.json"
    assert summary_path.exists()
    data = json.loads(summary_path.read_text())
    assert data["units"][0]["project"] == "a"
    assert data["units"][0]["unrunnable"]["reason"] == "T0_skeleton_only"


def test_cli_run_is_byte_deterministic(tmp_path: Path) -> None:
    """SPEC-18 §10: two runs with the same inputs produce byte-identical
    summary.json. Deterministic time via explicit --run-id."""
    manifest = tmp_path / "manifest.yml"
    _write_manifest(
        manifest,
        "oracle_units:\n"
        "  - project: a\n"
        "    unit: u1\n"
        "    tree_path: .\n"
        "    metadata_import: 'x:m'\n"
        "    env_var: X\n",
    )

    main(
        [
            "--config",
            str(manifest),
            "run",
            "--out",
            str(tmp_path / "r1"),
            "--run-id",
            "fixed",
        ]
    )
    main(
        [
            "--config",
            str(manifest),
            "run",
            "--out",
            str(tmp_path / "r2"),
            "--run-id",
            "fixed",
        ]
    )

    first = (tmp_path / "r1" / "fixed" / "summary.json").read_bytes()
    second = (tmp_path / "r2" / "fixed" / "summary.json").read_bytes()
    assert first == second


def test_cli_run_as_module_from_shell() -> None:
    """SPEC-18 §16: `python -m benchmarks.oracle list` is the canonical
    invocation. Exercise it end-to-end through subprocess to confirm
    the __main__.py wiring works."""
    result = subprocess.run(
        [sys.executable, "-m", "benchmarks.oracle", "list"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
