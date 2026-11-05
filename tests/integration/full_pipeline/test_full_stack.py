"""Scenario D: full stack, one migration.

Pre-R12 (M12 original): the ``full_stack_mixed`` fixture exercised a
fully-resolved op, a pattern-matched raw SQL op, and a genuinely unresolvable
op in one migration file, and asserted three findings at three distinct
confidence levels.

Post-R12 (SPEC-16 amendment): the recognized ADD COLUMN raw-SQL op is now
applied to schema state (no finding), DML is inert (no finding), and
dynamic SQL becomes a *note*, not a finding. The fixture's pipeline now
produces: a missing_column finding (model declares ``extra`` but no op
adds it), an orphaned_column finding (raw-SQL ADD COLUMN added
``widgets.flag`` to the migration side but the model doesn't declare
it), and a ``dynamic_sql`` note for the ``op.execute(variable)`` the
walker couldn't see into.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_helpers import copy_fixture, run_check


def test_full_stack_fixture_produces_the_expected_findings_and_notes(tmp_path: Path) -> None:
    repo = copy_fixture("full_stack_mixed", tmp_path)

    payload = run_check(repo)

    # Model declares `extra` but no op adds it to the migration side.
    missing = [f for f in payload["findings"] if f["category"] == "missing_column"]
    assert len(missing) == 1
    assert missing[0]["column"] == "extra"

    # Raw-SQL ADD COLUMN brought `flag` into the migration-side schema;
    # the model doesn't declare it -> orphaned_column.
    orphaned = [f for f in payload["findings"] if f["category"] == "orphaned_column"]
    assert len(orphaned) == 1
    assert orphaned[0]["column"] == "flag"


def test_full_stack_fixture_emits_a_dynamic_sql_note(tmp_path: Path) -> None:
    """R12: ``op.execute(statement)`` — a non-literal argument — becomes a
    ``dynamic_sql`` note, not a 0.3-confidence broken_chain warning
    (which was false noise)."""
    repo = copy_fixture("full_stack_mixed", tmp_path)

    payload = run_check(repo)

    dynamic = [n for n in payload["notes"] if n["reason"] == "dynamic_sql"]
    assert len(dynamic) == 1


def test_full_stack_fixture_reports_coverage(tmp_path: Path) -> None:
    """T4: the coverage line is present. The revision has one note scoped
    to it (dynamic_sql), so it counts as "partial"."""
    repo = copy_fixture("full_stack_mixed", tmp_path)

    payload = run_check(repo)

    assert payload["coverage"]["revisions_total"] == 1
    assert payload["coverage"]["revisions_partial"] == 1
    assert payload["coverage"]["revisions_fully_resolved"] == 0


# --- SPEC-12 Amendment v1.1 §3 Scenario D extensions (G6) ---


def test_full_stack_fixture_findings_carry_tier_and_demotion_fields(
    tmp_path: Path,
) -> None:
    """SPEC-12 Amendment G6: Scenario D's observable contract now
    includes the tier (kind) + demotion reason per finding, not just
    the finding category/confidence. Pre-amendment the test asserted
    three confidence levels only, which couldn't catch an R3 regression
    that silently stopped tagging demotions."""
    repo = copy_fixture("full_stack_mixed", tmp_path)

    payload = run_check(repo)

    assert payload["findings"], "fixture must still produce findings for this test to mean anything"
    for finding in payload["findings"]:
        # Every finding carries the tier contract: kind is 'fail' | 'review',
        # demoted_from is 'error' | 'warning' | None, explaining_notes is a list.
        assert finding["kind"] in ("fail", "review")
        assert finding["demoted_from"] in (None, "error", "warning")
        assert isinstance(finding["explaining_notes"], list)


def test_full_stack_fixture_summary_counts_match_findings(tmp_path: Path) -> None:
    """SPEC-12 Amendment G6: the summary's per-tier counts must match
    the per-finding tiers. Pre-amendment the summary could silently
    diverge if demotion/counting logic changed without a per-finding
    check."""
    repo = copy_fixture("full_stack_mixed", tmp_path)

    payload = run_check(repo)

    errors = sum(1 for f in payload["findings"] if f["kind"] == "fail" and f["severity"] == "error")
    warnings = sum(
        1 for f in payload["findings"] if f["kind"] == "fail" and f["severity"] == "warning"
    )
    reviews = sum(1 for f in payload["findings"] if f["kind"] == "review")

    summary = payload["summary"]
    assert summary["errors"] == errors
    assert summary["warnings"] == warnings
    assert summary["reviews"] == reviews


def test_full_stack_fixture_note_reasons_are_declared_set(tmp_path: Path) -> None:
    """SPEC-12 Amendment §3 parity assertion: the *set* of note reasons
    must be stable. If a future change swaps 'dynamic_sql' for a
    'raw_sql_unparseable' note, that would be visible drift the test
    should catch, not a silent reshuffle."""
    repo = copy_fixture("full_stack_mixed", tmp_path)

    payload = run_check(repo)

    note_reasons = {n["reason"] for n in payload["notes"]}
    # The fixture has exactly one dynamic_sql note (one op.execute with a
    # non-literal argument). If more reasons appear here without the
    # fixture changing, something upstream started emitting them.
    assert note_reasons == {"dynamic_sql"}, note_reasons
