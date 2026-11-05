"""SPEC-16 Amendment R14 (remainder): baseline/ratchet mode + --show-all.

A baseline is a snapshot of the finding ids present at a known-good
point in time. Each later `gravitar check` compares against it:

- findings present in the baseline are "unchanged" (do not fail CI)
- findings not in the baseline are "new" (do fail CI)
- `--show-all` keeps baselined findings in the output; without it they
  are filtered (default behavior for CI noise control)
- `--no-baseline` ignores the stored baseline for one run

The pattern lets a drifty project onboard Gravitar without turning its
CI red on day one; teams accept the current state as the floor and
ratchet from there.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gravitar.baseline import (
    baseline_path_for,
    clear_baseline,
    load_baseline,
    read_baseline_metadata,
    write_baseline,
)
from gravitar.cli import main
from gravitar.errors import EXIT_DRIFT, EXIT_SUCCESS

FIXTURES = Path(__file__).parent / "fixtures"


# --- baseline.py unit tests ---


def test_load_baseline_returns_none_when_missing(tmp_path: Path) -> None:
    """A fresh repo has no baseline; load_baseline must return None, not
    raise or create a file."""
    assert load_baseline(tmp_path / ".gravitar" / "baseline.json") is None
    assert not (tmp_path / ".gravitar").exists()  # never created lazily


def test_write_baseline_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / ".gravitar" / "baseline.json"
    write_baseline(path, ["id_a", "id_b", "id_a"])  # duplicate dropped

    loaded = load_baseline(path)
    assert loaded == {"id_a", "id_b"}


def test_write_baseline_is_sorted_and_atomic(tmp_path: Path) -> None:
    """Writes must be byte-identical for the same input (sorted output)
    so a `git diff` on the baseline shows only genuine changes."""
    path = tmp_path / ".gravitar" / "baseline.json"
    write_baseline(path, ["z", "a", "m"])
    first = path.read_text(encoding="utf-8")

    # Same content, different input order
    write_baseline(path, ["m", "z", "a"])
    second = path.read_text(encoding="utf-8")

    # created_at timestamps differ by second resolution; strip that
    # for the comparison but confirm finding_ids ordering is stable.
    assert json.loads(first)["finding_ids"] == json.loads(second)["finding_ids"]
    assert json.loads(first)["finding_ids"] == ["a", "m", "z"]


def test_clear_baseline_removes_file(tmp_path: Path) -> None:
    path = tmp_path / ".gravitar" / "baseline.json"
    write_baseline(path, ["id_x"])
    assert path.exists()

    assert clear_baseline(path) is True
    assert not path.exists()
    # Second clear is a no-op, not an error.
    assert clear_baseline(path) is False


def test_malformed_baseline_raises(tmp_path: Path) -> None:
    """A corrupt baseline must be loud: silently behaving as if no
    baseline existed would turn a baselined finding into a new one
    overnight and break CI without the user understanding why."""
    path = tmp_path / ".gravitar" / "baseline.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    try:
        load_baseline(path)
    except ValueError as e:
        assert "malformed" in str(e).lower()
    else:  # pragma: no cover - defensive
        raise AssertionError("expected ValueError")


def test_read_baseline_metadata_includes_created_at(tmp_path: Path) -> None:
    path = tmp_path / ".gravitar" / "baseline.json"
    write_baseline(path, ["id_1"])

    metadata = read_baseline_metadata(path)
    assert metadata is not None
    assert metadata["version"] == 1
    assert metadata["finding_ids"] == ["id_1"]
    assert "created_at" in metadata


# --- CLI: `baseline set/clear/show` ---


def test_baseline_set_writes_file_with_current_finding_ids() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Point at a fixture with known drift
        result = runner.invoke(
            main,
            ["baseline", "set", str(FIXTURES / "missing_column")],
        )

        assert result.exit_code == EXIT_SUCCESS, result.output
        assert "finding(s)" in result.output
        baseline_file = baseline_path_for(FIXTURES / "missing_column")
        try:
            assert baseline_file.exists()
            ids = load_baseline(baseline_file)
            assert ids is not None
            assert len(ids) >= 1
        finally:
            if baseline_file.exists():
                baseline_file.unlink()
            parent = baseline_file.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()


def test_baseline_show_prints_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    baseline_file = baseline_path_for(tmp_path)
    write_baseline(baseline_file, ["id_1", "id_2"])

    result = runner.invoke(main, ["baseline", "show", str(tmp_path)])

    assert result.exit_code == EXIT_SUCCESS
    assert "findings: 2" in result.output
    assert "created_at:" in result.output


def test_baseline_show_reports_absent(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["baseline", "show", str(tmp_path)])
    assert result.exit_code == EXIT_SUCCESS
    assert "no baseline" in result.output


def test_baseline_clear_removes_file(tmp_path: Path) -> None:
    runner = CliRunner()
    baseline_file = baseline_path_for(tmp_path)
    write_baseline(baseline_file, ["id_1"])
    assert baseline_file.exists()

    result = runner.invoke(main, ["baseline", "clear", str(tmp_path)])

    assert result.exit_code == EXIT_SUCCESS
    assert not baseline_file.exists()
    assert "removed" in result.output


# --- CLI: `check` with baseline ---


def test_check_exits_zero_when_all_findings_are_baselined() -> None:
    """The headline ratchet behavior: run `baseline set`, then re-run
    `check` — exit code is 0 even though the drift is still there."""
    runner = CliRunner()
    fixture = FIXTURES / "missing_column"
    baseline_file = baseline_path_for(fixture)
    try:
        set_result = runner.invoke(main, ["baseline", "set", str(fixture)])
        assert set_result.exit_code == EXIT_SUCCESS

        check_result = runner.invoke(main, ["check", str(fixture), "--no-color"])

        assert check_result.exit_code == EXIT_SUCCESS, check_result.output
    finally:
        if baseline_file.exists():
            baseline_file.unlink()
        parent = baseline_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def test_check_default_hides_baselined_findings() -> None:
    """Without --show-all, baselined findings do not appear in output —
    CI logs stay focused on new drift."""
    runner = CliRunner()
    fixture = FIXTURES / "missing_column"
    baseline_file = baseline_path_for(fixture)
    try:
        runner.invoke(main, ["baseline", "set", str(fixture)])

        result = runner.invoke(main, ["check", str(fixture), "--format", "json", "--no-color"])

        data = json.loads(result.output)
        assert data["findings"] == []
        assert data.get("baseline_active") is True
    finally:
        if baseline_file.exists():
            baseline_file.unlink()
        parent = baseline_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def test_check_show_all_includes_baselined_findings() -> None:
    """--show-all surfaces baselined findings for human review while
    still exiting 0 (exit code is unaffected by --show-all)."""
    runner = CliRunner()
    fixture = FIXTURES / "missing_column"
    baseline_file = baseline_path_for(fixture)
    try:
        runner.invoke(main, ["baseline", "set", str(fixture)])

        result = runner.invoke(
            main,
            ["check", str(fixture), "--format", "json", "--show-all", "--no-color"],
        )

        assert result.exit_code == EXIT_SUCCESS, result.output
        data = json.loads(result.output)
        assert len(data["findings"]) >= 1
        assert all(f.get("baselined") is True for f in data["findings"])
    finally:
        if baseline_file.exists():
            baseline_file.unlink()
        parent = baseline_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def test_check_no_baseline_flag_ignores_stored_baseline() -> None:
    """--no-baseline makes the run behave as if no baseline file
    existed: every drift finding is 'new' and the exit code reflects
    that, even though baseline.json is present."""
    runner = CliRunner()
    fixture = FIXTURES / "missing_column"
    baseline_file = baseline_path_for(fixture)
    try:
        runner.invoke(main, ["baseline", "set", str(fixture)])

        result = runner.invoke(main, ["check", str(fixture), "--no-baseline", "--no-color"])

        assert result.exit_code == EXIT_DRIFT, result.output
    finally:
        if baseline_file.exists():
            baseline_file.unlink()
        parent = baseline_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


def test_check_new_drift_fails_even_with_baseline() -> None:
    """A finding introduced AFTER the baseline must still fail CI —
    that's the point of ratcheting (floor doesn't drop, drift only
    ever gets paid down)."""
    runner = CliRunner()
    with runner.isolated_filesystem() as tmp_dir:
        tmp = Path(tmp_dir)
        # Minimal setup: models declaring one table, no migration.
        (tmp / "models").mkdir()
        (tmp / "models" / "__init__.py").write_text(
            "from sqlalchemy import Column, Integer\n"
            "from sqlalchemy.orm import declarative_base\n"
            "Base = declarative_base()\n"
            "class One(Base):\n"
            "    __tablename__ = 'one'\n"
            "    id = Column(Integer, primary_key=True)\n"
        )
        (tmp / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
        (tmp / "alembic" / "versions").mkdir(parents=True)
        (tmp / "alembic" / "versions" / "0001.py").write_text(
            '"""noop"""\nrevision = "0001"\ndown_revision = None\n'
            "def upgrade() -> None:\n    pass\n"
        )

        # Snapshot the current drift ('one' is missing_table).
        runner.invoke(main, ["baseline", "set", str(tmp)])
        clean = runner.invoke(main, ["check", str(tmp), "--no-color"])
        assert clean.exit_code == EXIT_SUCCESS

        # Add a NEW drifting table — baseline doesn't cover it, must fail.
        (tmp / "models" / "__init__.py").write_text(
            "from sqlalchemy import Column, Integer\n"
            "from sqlalchemy.orm import declarative_base\n"
            "Base = declarative_base()\n"
            "class One(Base):\n"
            "    __tablename__ = 'one'\n"
            "    id = Column(Integer, primary_key=True)\n"
            "class Two(Base):\n"  # NEW
            "    __tablename__ = 'two'\n"
            "    id = Column(Integer, primary_key=True)\n"
        )

        result = runner.invoke(main, ["check", str(tmp), "--no-color"])

        assert result.exit_code == EXIT_DRIFT, result.output


def test_check_malformed_baseline_file_errors_loudly(tmp_path: Path) -> None:
    """A corrupt baseline must not silently convert baselined findings
    into 'new' ones — the run must stop and tell the user."""
    runner = CliRunner()
    fixture = FIXTURES / "missing_column"
    baseline_file = baseline_path_for(fixture)
    baseline_file.parent.mkdir(parents=True, exist_ok=True)
    baseline_file.write_text("{broken", encoding="utf-8")
    try:
        result = runner.invoke(main, ["check", str(fixture), "--no-color"])

        assert result.exit_code != EXIT_SUCCESS
        assert (
            "malformed" in result.output.lower()
            or "malformed" in result.stderr_bytes.decode(errors="replace").lower()
        )
    finally:
        if baseline_file.exists():
            baseline_file.unlink()
        parent = baseline_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


# --- SARIF baselineState ---


def test_sarif_marks_baselined_results_as_unchanged() -> None:
    """SARIF's native `baselineState` field is what GitHub Code Scanning
    uses to style pre-existing findings differently from new ones."""
    runner = CliRunner()
    fixture = FIXTURES / "missing_column"
    baseline_file = baseline_path_for(fixture)
    try:
        runner.invoke(main, ["baseline", "set", str(fixture)])

        result = runner.invoke(
            main,
            [
                "check",
                str(fixture),
                "--format",
                "sarif",
                "--show-all",
                "--no-color",
            ],
        )

        log = json.loads(result.output)
        results = log["runs"][0]["results"]
        assert len(results) >= 1
        assert all(r["baselineState"] == "unchanged" for r in results)
        assert log["runs"][0]["properties"]["baselineActive"] is True
    finally:
        if baseline_file.exists():
            baseline_file.unlink()
        parent = baseline_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
