"""Regression test for the baseline-enabled JSON envelope contract.

Pins the exact field-presence rules for the baseline mode of the JSON
output format:

- Without a baseline (or with ``--no-baseline``), the envelope must
  not contain ``baseline_active`` and no finding carries a
  ``baselined`` field. The envelope shape is identical to a run
  that never heard of baselines.
- With a baseline active, the envelope carries ``baseline_active:
  true`` and every finding carries ``baselined: bool``.

Guards against two silent-drift regressions this file exists to
block:

1. A refactor to ``src/gravitar/output/json.py`` that always-emits
   ``baseline_active`` even when no baseline is set, breaking the
   backward-compatible envelope shape of pre-baseline consumers.
2. A refactor that drops ``baselined`` from findings when a
   baseline IS active, breaking ratcheting consumers that key off
   that field to classify each finding as new vs. known.

The test runs ``gravitar check`` end-to-end through the Click
runner (not an in-process call) so it catches a wrapper-layer
regression in addition to the formatter's own contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gravitar.baseline import baseline_path_for
from gravitar.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _invoke_check(repo_path: Path, extra_args: list[str] | None = None) -> dict:
    """Run `gravitar check --format json` and parse the envelope."""
    args = ["check", str(repo_path), "--format", "json", "--no-color"]
    if extra_args:
        args.extend(extra_args)
    result = CliRunner().invoke(main, args)
    return json.loads(result.output)


def _clear_fixture_baseline(fixture_path: Path) -> None:
    """Remove any committed/leftover baseline from the fixture (safety
    net for a test author who forgot to clean up)."""
    baseline_file = baseline_path_for(fixture_path)
    if baseline_file.exists():
        baseline_file.unlink()
    parent = baseline_file.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


def test_envelope_without_baseline_omits_baseline_marker_and_finding_flag() -> None:
    """Pre-baseline consumers rely on the envelope NOT having
    ``baseline_active`` at the top level and NOT having a
    ``baselined`` key on each finding. If a change in json.py ever
    always-emits those fields, this test catches it before it ships."""
    fixture = FIXTURES / "missing_column"
    _clear_fixture_baseline(fixture)

    envelope = _invoke_check(fixture)

    assert envelope["schema_version"] == 2
    assert envelope["status"] == "drift"
    assert "baseline_active" not in envelope, (
        "envelope must not include 'baseline_active' when no baseline is "
        f"configured; got: {envelope.get('baseline_active')!r}"
    )
    assert envelope["findings"], "fixture must produce drift for this test to be meaningful"
    for finding in envelope["findings"]:
        assert "baselined" not in finding, (
            f"no finding should carry a 'baselined' key without an active baseline; got {finding}"
        )


def test_envelope_with_no_baseline_flag_omits_fields_even_if_baseline_file_exists() -> None:
    """``--no-baseline`` must behave exactly like the no-baseline-file
    case: no top-level ``baseline_active``, no per-finding ``baselined``."""
    fixture = FIXTURES / "missing_column"
    try:
        # Set a baseline so there's something to IGNORE.
        set_result = CliRunner().invoke(main, ["baseline", "set", str(fixture)])
        assert set_result.exit_code == 0

        envelope = _invoke_check(fixture, ["--no-baseline"])

        assert "baseline_active" not in envelope
        for finding in envelope["findings"]:
            assert "baselined" not in finding
    finally:
        _clear_fixture_baseline(fixture)


def test_envelope_with_active_baseline_includes_marker_and_tags_findings() -> None:
    """With a baseline set, the envelope carries ``baseline_active:
    true`` and every surfaced finding carries ``baselined: bool`` so
    consumers can classify each one as new vs. known."""
    fixture = FIXTURES / "missing_column"
    try:
        set_result = CliRunner().invoke(main, ["baseline", "set", str(fixture)])
        assert set_result.exit_code == 0

        # --show-all so baselined findings are surfaced (default hides them).
        envelope = _invoke_check(fixture, ["--show-all"])

        assert envelope.get("baseline_active") is True
        assert envelope["findings"], "--show-all should surface baselined findings"
        for finding in envelope["findings"]:
            assert "baselined" in finding, (
                f"active baseline must tag every finding with 'baselined'; got {finding}"
            )
            assert isinstance(finding["baselined"], bool)
    finally:
        _clear_fixture_baseline(fixture)


def test_summary_counts_reflect_only_new_drift_when_baseline_hides_findings() -> None:
    """Without ``--show-all``, baselined findings are filtered from
    ``findings`` AND the summary's error/warning/review counts drop
    to zero — a ratcheting CI needs the summary to see 'nothing new
    today' when every finding is baselined."""
    fixture = FIXTURES / "missing_column"
    try:
        set_result = CliRunner().invoke(main, ["baseline", "set", str(fixture)])
        assert set_result.exit_code == 0

        envelope = _invoke_check(fixture)  # default: --no-show-all

        assert envelope.get("baseline_active") is True
        assert envelope["findings"] == [], (
            "default run with full-coverage baseline should hide all findings"
        )
        assert envelope["summary"]["errors"] == 0
        assert envelope["summary"]["warnings"] == 0
        assert envelope["summary"]["reviews"] == 0
    finally:
        _clear_fixture_baseline(fixture)
