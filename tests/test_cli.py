import json
import shutil
from pathlib import Path

from click.testing import CliRunner

from gravitar import __version__
from gravitar.cli import main
from gravitar.errors import (
    EXIT_ANALYSIS_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_DRIFT,
    EXIT_SUCCESS,
    EXIT_WARNINGS_ONLY,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_version_prints_version_and_exits_zero() -> None:
    result = CliRunner().invoke(main, ["version"])

    assert result.exit_code == EXIT_SUCCESS
    assert __version__ in result.output


def test_init_writes_default_config(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init"])

        assert result.exit_code == EXIT_SUCCESS
        assert Path(".gravitar.yml").exists()
        assert "models:" in Path(".gravitar.yml").read_text()


def test_init_refuses_to_overwrite_existing_config(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path(".gravitar.yml").write_text("custom: true\n")
        result = runner.invoke(main, ["init"])

        assert result.exit_code == EXIT_CONFIG_ERROR
        assert Path(".gravitar.yml").read_text() == "custom: true\n"


def test_check_empty_directory_is_config_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR


def test_check_clean_fixture_exits_zero_and_reports_no_drift() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS
    assert "no drift detected" in result.output


def test_check_missing_column_fixture_exits_one_and_reports_it() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "missing_column"), "--no-color"])

    assert result.exit_code == EXIT_DRIFT
    assert "missing_column" in result.output
    assert "widgets.deleted_at" in result.output


def test_check_json_format_is_valid_and_matches_status() -> None:
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "missing_table"), "--format", "json"]
    )

    assert result.exit_code == EXIT_DRIFT
    payload = json.loads(result.output)
    assert payload["status"] == "drift"
    assert payload["findings"][0]["category"] == "missing_table"


def test_check_json_format_clean_fixture() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean"), "--format", "json"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["status"] == "clean"
    assert payload["findings"] == []


def test_check_quiet_suppresses_summary_line() -> None:
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "missing_column"), "--quiet", "--no-color"]
    )

    assert "summary:" not in result.output


def test_check_no_color_has_no_ansi_codes() -> None:
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "missing_column"), "--no-color"])

    assert "\x1b[" not in result.output


def test_check_model_import_error_exits_analysis_error(tmp_path: Path) -> None:
    """An exception during import other than ImportError (Section 5 gives this its
    full treatment in Milestone 5) must still fail closed, not crash."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_ANALYSIS_ERROR
    assert "boom" in result.output


def test_check_invalid_migration_syntax_surfaces_as_a_parse_error_note(tmp_path: Path) -> None:
    """Per-file isolation: one unparseable file no longer aborts the whole
    run — it becomes a parse_error note and every other file still parses.

    Earlier behavior was to exit EXIT_ANALYSIS_ERROR with the file:line in
    the error message; the current contract is "no project aborts the run."
    """
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001_broken.py").write_text("def upgrade(:\n    pass\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    # Run completed — exit code is "clean" today (R7 will revisit per T5).
    assert result.exit_code == EXIT_SUCCESS
    # The failure is still visible — surfaced as a parse_error note, not hidden.
    assert "0001_broken.py" in result.output
    assert "parse_error" in result.output
    # Coverage reports the file as skipped (T4).
    assert "1 skipped" in result.output


def test_check_unknown_config_key_exits_config_error(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / ".gravitar.yml").write_text("modles:\n  paths: []\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "modles" in result.output


def test_check_unknown_type_exits_per_severity_tier() -> None:
    """``unknown_type`` no longer forces exit 1. It exits per its severity
    tier (warning). 'Could not read' isn't 'drift' — failing CI on it turns
    unreadable input into a red build without surfacing anything the user
    can act on.
    """
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "unknown_type"), "--format", "json"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["findings"][0]["category"] == "unknown_type"
    assert payload["findings"][0]["severity"] == "warning"
    assert payload["summary"]["errors"] == 0
    assert payload["summary"]["warnings"] == 1


def test_check_unknown_type_with_warnings_as_nonzero_exits_ten() -> None:
    """Opt-in behavior: a user who wants `unknown_type` to fail CI can set
    `--warnings-as-nonzero`, same as for any other warning. R3 moves
    unknown_type into the ordinary warning gate — this flag is the
    escape hatch."""
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "unknown_type"), "--warnings-as-nonzero"]
    )

    assert result.exit_code == EXIT_WARNINGS_ONLY


def test_check_inert_dml_in_raw_sql_fixture_produces_no_finding() -> None:
    """SPEC-16 amendment R12: ``op.execute("UPDATE ...")`` is DML — classified
    as inert, no finding emitted. Pre-R12 this fixture produced a broken_chain
    warning with ``any table may be affected`` noise."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "raw_sql"), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS
    assert "no drift detected" in result.output
    # Pre-R12 the "raw SQL" warning line appeared here; R12 drops it.
    assert "any table may be affected" not in result.output


def test_check_inert_dml_with_warnings_as_nonzero_flag_still_exits_zero() -> None:
    """Inert DML emits no warning, so --warnings-as-nonzero changes nothing."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "raw_sql"), "--warnings-as-nonzero"])

    assert result.exit_code == EXIT_SUCCESS


def _minimal_revision(contents: str = "") -> str:
    return (
        '"""minimal revision"""\n\n'
        'revision = "0001"\n'
        "down_revision = None\n\n\n"
        "def upgrade() -> None:\n"
        f"    {contents or 'pass'}\n"
    )


def test_check_exclude_tables_via_gravitar_yml(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n\n"
        "class Temp(Base):\n"
        '    __tablename__ = "tmp_cache"\n'
        "    id = Column(Integer, primary_key=True)\n"
    )
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    # SPEC-16 R10: a non-empty versions dir is required (zero revisions is
    # a tool error). A minimal no-op revision keeps this test's intent
    # (exclude tmp_* so no drift reported) while satisfying the gate.
    (tmp_path / "versions" / "0001_empty.py").write_text(_minimal_revision())
    (tmp_path / ".gravitar.yml").write_text('exclude:\n  tables:\n    - "tmp_*"\n')

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS
    assert "no drift detected" in result.output


def test_check_disabled_check_via_gravitar_yml(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
        "    id = Column(Integer, primary_key=True)\n"
    )
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001_empty.py").write_text(_minimal_revision())
    (tmp_path / ".gravitar.yml").write_text("checks:\n  missing_table: false\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS
    assert "no drift detected" in result.output


def test_check_output_format_defaults_from_gravitar_yml() -> None:
    """output.format in .gravitar.yml should apply when --format isn't passed."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "clean_with_json_default")])

    assert result.exit_code == EXIT_SUCCESS
    json.loads(result.output)  # would raise if this were text output


def test_check_format_flag_overrides_gravitar_yml_default() -> None:
    result = CliRunner().invoke(
        main, ["check", str(FIXTURES / "clean_with_json_default"), "--format", "text"]
    )

    assert result.exit_code == EXIT_SUCCESS
    assert "no drift detected" in result.output


def test_check_config_error_as_json(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json"])

    assert result.exit_code == EXIT_CONFIG_ERROR
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["schema_version"] == 2
    assert payload["error"]
    assert payload["findings"] == []


def test_check_invalid_migration_syntax_surfaces_in_json(tmp_path: Path) -> None:
    """R11 again: the file isn't dropped, it's a note. JSON output exposes
    the note at top level so a CI consumer sees it without parsing text."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "0001_broken.py").write_text("def upgrade(:\n    pass\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json"])

    assert result.exit_code == EXIT_SUCCESS
    payload = json.loads(result.output)
    assert payload["status"] == "clean"
    assert len(payload["notes"]) == 1
    assert payload["notes"][0]["reason"] == "parse_error"
    assert "0001_broken.py" in payload["notes"][0]["source"]["file"]
    assert payload["coverage"]["revisions_skipped"] == 1


# --- gravitar feedback ---
#
# CliRunner.invoke() re-invokes `main` in-process against the exact repo path
# given, so — unlike the fixture-directory tests above — these copy a fixture
# into tmp_path first: `feedback confirm`/`dismiss` create `.gravitar/
# feedback.sqlite` under the repo root, and tests/fixtures/ is a tracked
# directory that must never get a stray file written into it by a test run.


def _copy_fixture(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


def _first_finding_id(repo: Path) -> str:
    result = CliRunner().invoke(main, ["check", str(repo), "--format", "json"])
    payload = json.loads(result.output)
    id_: str = payload["findings"][0]["finding_id"]
    return id_


def test_feedback_confirm_records_outcome_and_exits_zero(tmp_path: Path) -> None:
    repo = _copy_fixture("missing_column", tmp_path)
    finding_id = _first_finding_id(repo)

    result = CliRunner().invoke(main, ["feedback", "confirm", finding_id, str(repo)])

    assert result.exit_code == EXIT_SUCCESS
    assert finding_id in result.output
    assert (repo / ".gravitar" / "feedback.sqlite").exists()


def test_feedback_dismiss_records_outcome_and_exits_zero(tmp_path: Path) -> None:
    repo = _copy_fixture("missing_column", tmp_path)
    finding_id = _first_finding_id(repo)

    result = CliRunner().invoke(main, ["feedback", "dismiss", finding_id, str(repo)])

    assert result.exit_code == EXIT_SUCCESS
    assert (repo / ".gravitar" / "feedback.sqlite").exists()


def test_feedback_confirm_config_error_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["feedback", "confirm", "abc123", str(tmp_path)])

    assert result.exit_code == EXIT_CONFIG_ERROR


def test_feedback_confirm_analysis_error_fails(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()

    result = CliRunner().invoke(main, ["feedback", "confirm", "abc123", str(tmp_path)])

    assert result.exit_code == EXIT_ANALYSIS_ERROR
    assert "boom" in result.output


def test_feedback_confirm_unknown_id_fails(tmp_path: Path) -> None:
    repo = _copy_fixture("missing_column", tmp_path)

    result = CliRunner().invoke(main, ["feedback", "confirm", "not-a-real-id", str(repo)])

    assert result.exit_code == EXIT_ANALYSIS_ERROR
    assert "no finding with id" in result.output


def test_feedback_stats_with_no_feedback_recorded(tmp_path: Path) -> None:
    repo = _copy_fixture("missing_column", tmp_path)

    result = CliRunner().invoke(main, ["feedback", "stats", str(repo)])

    assert result.exit_code == EXIT_SUCCESS
    assert "no feedback recorded" in result.output


def test_feedback_stats_shows_pattern_reliability_after_confirming(tmp_path: Path) -> None:
    repo = _copy_fixture("missing_column", tmp_path)
    finding_id = _first_finding_id(repo)
    CliRunner().invoke(main, ["feedback", "confirm", finding_id, str(repo)])

    result = CliRunner().invoke(main, ["feedback", "stats", str(repo)])

    assert result.exit_code == EXIT_SUCCESS
    assert "ast:missing_column" in result.output
    assert "1 confirmed, 0 dismissed" in result.output


def test_feedback_confirm_then_check_raises_confidence_for_that_pattern(tmp_path: Path) -> None:
    repo = _copy_fixture("missing_column", tmp_path)
    finding_id = _first_finding_id(repo)
    for _ in range(5):
        CliRunner().invoke(main, ["feedback", "confirm", finding_id, str(repo)])

    result = CliRunner().invoke(main, ["check", str(repo), "--format", "json"])
    payload = json.loads(result.output)

    assert payload["findings"][0]["confidence"] > 0.5


# --- --live-db-url ---
#
# Commit checkpoint: absent, output is byte-for-byte unchanged from
# pre-Milestone-11 behavior (already proven by every fixture test above, none
# of which pass the flag) — these add the flag's own behavior on top.


def test_live_db_url_absent_matches_the_pre_milestone_11_golden_file() -> None:
    """The exact same golden file test_output.py checks — proof this milestone
    changed nothing about default (no-flag) text output."""
    result = CliRunner().invoke(main, ["check", str(FIXTURES / "unknown_type"), "--no-color"])

    golden = (FIXTURES / "unknown_type" / "expected_output.txt").read_text(encoding="utf-8")
    assert result.output == golden


def test_live_db_url_invalid_fails_soft(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "check",
            str(FIXTURES / "unknown_type"),
            "--format",
            "json",
            "--live-db-url",
            "not-a-valid-url",
        ],
    )
    baseline = CliRunner().invoke(
        main, ["check", str(FIXTURES / "unknown_type"), "--format", "json"]
    )

    assert result.exit_code == baseline.exit_code
    assert json.loads(result.output) == json.loads(baseline.output)


def test_live_db_url_corroborates_a_low_confidence_finding(tmp_path: Path) -> None:
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE wallets (id INTEGER PRIMARY KEY, balance NUMERIC(12, 2) NOT NULL)")
        )
    engine.dispose()

    result = CliRunner().invoke(
        main,
        [
            "check",
            str(FIXTURES / "unknown_type"),
            "--format",
            "json",
            "--live-db-url",
            f"sqlite:///{db_path}",
        ],
    )

    payload = json.loads(result.output)
    finding = payload["findings"][0]
    assert finding["live_verified"] is True
    assert finding["confidence"] > 0.5
    assert any(e["signal"] == "live_db_corroboration" for e in finding["evidence"])


def test_live_db_url_verified_finding_shown_in_text_output(tmp_path: Path) -> None:
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE wallets (id INTEGER PRIMARY KEY, balance NUMERIC(12, 2) NOT NULL)")
        )
    engine.dispose()

    result = CliRunner().invoke(
        main,
        [
            "check",
            str(FIXTURES / "unknown_type"),
            "--no-color",
            "--live-db-url",
            f"sqlite:///{db_path}",
        ],
    )

    assert "live-verified against the database" in result.output
