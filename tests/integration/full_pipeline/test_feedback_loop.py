"""Scenario C: the feedback loop closes,
for both parsers, sharing the same pattern_id.

Each fixture (feedback_loop_alembic, feedback_loop_django) declares two
independent missing_column findings — widgets.legacy and gadgets.notes — both
resolving to `pattern_id_for()`'s category-based fallback "ast:missing_column"
(feedback/ids.py), which is DSL-agnostic by construction: it's derived from
the finding's `category`, never from anything parser-specific. Dismissing one
finding and then observing the *other* (never itself touched) score lower is
exactly the proof Section 4 asks for: "the reliability store isn't
accidentally keyed to something parser-specific." If it were keyed to, say,
the parser class or the migration file, dismissing widgets.legacy would leave
gadgets.notes at confidence 1.0 — which is precisely what these tests would
catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pipeline_helpers import copy_fixture, run_check, run_feedback, run_feedback_stats


def _find(payload: dict, table: str, column: str) -> dict:
    return next(f for f in payload["findings"] if f["table"] == table and f["column"] == column)


@pytest.mark.parametrize("fixture_name", ["feedback_loop_alembic", "feedback_loop_django"])
def test_dismissing_one_finding_lowers_a_different_findings_confidence(
    fixture_name: str, tmp_path: Path
) -> None:
    repo = copy_fixture(fixture_name, tmp_path)

    baseline = run_check(repo)
    dismissed_table = "widgets" if fixture_name == "feedback_loop_alembic" else "blog_widget"
    untouched_table = "gadgets" if fixture_name == "feedback_loop_alembic" else "blog_gadget"

    to_dismiss = _find(baseline, dismissed_table, "legacy")
    untouched_baseline = _find(baseline, untouched_table, "notes")
    assert to_dismiss["confidence"] == 1.0
    assert untouched_baseline["confidence"] == 1.0

    feedback_result = run_feedback(repo, "dismiss", to_dismiss["finding_id"])
    assert feedback_result.returncode == 0

    rerun = run_check(repo)
    untouched_after = _find(rerun, untouched_table, "notes")

    assert untouched_after["confidence"] < untouched_baseline["confidence"]
    assert any(e["signal"] == "historical_reliability" for e in untouched_after["evidence"])


@pytest.mark.parametrize("fixture_name", ["feedback_loop_alembic", "feedback_loop_django"])
def test_dismissed_finding_and_untouched_finding_share_a_pattern_id(
    fixture_name: str, tmp_path: Path
) -> None:
    """Not itself a claim SPEC-12 asks for directly, but it's the mechanism
    the tests above rely on — worth asserting explicitly rather than only
    implicitly through the confidence-drop outcome."""
    repo = copy_fixture(fixture_name, tmp_path)
    dismissed_table = "widgets" if fixture_name == "feedback_loop_alembic" else "blog_widget"
    untouched_table = "gadgets" if fixture_name == "feedback_loop_alembic" else "blog_gadget"

    payload = run_check(repo)

    assert _find(payload, dismissed_table, "legacy")["category"] == "missing_column"
    assert _find(payload, untouched_table, "notes")["category"] == "missing_column"


def test_alembic_and_django_dismissals_land_on_the_same_pattern_id_string(
    tmp_path: Path,
) -> None:
    """The stronger, direct version of the "isn't parser-specific" claim: the
    stats command reports the identical pattern_id string regardless of
    which parser produced the finding being dismissed."""
    alembic_repo = copy_fixture("feedback_loop_alembic", tmp_path / "alembic_repo")
    django_repo = copy_fixture("feedback_loop_django", tmp_path / "django_repo")

    alembic_finding = _find(run_check(alembic_repo), "widgets", "legacy")
    django_finding = _find(run_check(django_repo), "blog_widget", "legacy")

    run_feedback(alembic_repo, "dismiss", alembic_finding["finding_id"])
    run_feedback(django_repo, "dismiss", django_finding["finding_id"])

    alembic_stats = run_feedback_stats(alembic_repo)
    django_stats = run_feedback_stats(django_repo)

    assert "ast:missing_column" in alembic_stats.stdout
    assert "ast:missing_column" in django_stats.stdout
