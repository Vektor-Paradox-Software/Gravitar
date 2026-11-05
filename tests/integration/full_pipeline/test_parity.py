"""Scenario A: cross-parser parity — the load-bearing test of this
whole milestone.

    Alembic source   Django source
Fully resolved             X               X   <- this file
Raw SQL, known pattern     X               N/A  <- test_classification.py
Raw SQL, unrecognized      X               N/A  <- test_classification.py
Unresolvable dynamic       X               X   <- test_full_stack.py (Alembic)
                                                    / already covered by every
                                                    "fully resolved" case above
                                                    on the Django side, since
                                                    Django parsing has no
                                                    "unresolvable" failure mode
                                                    of its own (Section 4.2:
                                                    direct object inspection,
                                                    not AST — nothing to fail
                                                    to resolve)
Post-feedback rerun        X               X   <- test_feedback_loop.py

The two raw-SQL rows are N/A on the Django side by construction, not by
oversight: Django migrations are declarative operation objects, never
imperative code with embedded SQL strings. There is no Django equivalent
to `op.execute()` for Gravitar to classify — `RunSQL` is the nearest analog
and is explicitly out of scope for Milestone 10. This is a genuine,
permanent scope boundary of the Django parser, documented here rather than
silently skipped.

The single most important thing this file proves is not "the pipeline runs
without crashing" — it's whether the same logical schema change, expressed
once as an Alembic migration and once as an equivalent Django migration,
produces structurally equivalent findings through both paths. That's the
empirical test of whether the canonical representation is actually
DSL-agnostic, or only looked that way in each DSL's own isolated fixtures.
If this fails: stop and report the specific structural mismatch. Do not
narrow the tolerance or adjust the fixtures to force a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pipeline_helpers import copy_fixture, run_check

# Confidence band width for Scenario A. Set strictly tighter than one
# ``UNRESOLVED_NODE_PENALTY`` (0.15) so a one-unresolved-node gap between
# the Alembic and Django paths DOES break parity — SPEC-12 Amendment v1.1
# G5 flagged the old 0.15 tolerance as "exactly equals one node", a band
# that can't distinguish "same" from "one node apart". The amendment's
# own specified value is 0.05.
TOLERANCE = 0.05

PARITY_FIXTURES = ["parity_add_column", "parity_drop_column", "parity_alter_column_type"]


@dataclass
class ParityAssertion:
    alembic_findings: list[dict[str, Any]]
    django_findings: list[dict[str, Any]]
    tolerance: float = TOLERANCE


def assert_parity(a: ParityAssertion) -> None:
    """Structural equivalence per Scenario A: same count, and each pair
    (matched by sort order — both sides sort identically since sort_findings
    only orders by category/table/column, and these fixtures produce exactly
    one finding) agrees on category, severity, and column, with confidence
    within `tolerance`.

    Table name is deliberately *not* compared: Django's `<app_label>_<model>`
    naming convention differs from Alembic's raw table names by design
    (django_parser.py's `table_name`) — that's a correct DSL difference, not
    a parity gap, so asserting equality there would be testing the wrong
    thing.
    """
    assert len(a.alembic_findings) == len(a.django_findings), (
        f"finding count differs: alembic={len(a.alembic_findings)} django={len(a.django_findings)}"
    )

    for alembic_finding, django_finding in zip(a.alembic_findings, a.django_findings, strict=True):
        assert alembic_finding["category"] == django_finding["category"]
        assert alembic_finding["severity"] == django_finding["severity"]
        assert alembic_finding["column"] == django_finding["column"]
        assert abs(alembic_finding["confidence"] - django_finding["confidence"]) <= a.tolerance, (
            f"confidence outside tolerance ({a.tolerance}): "
            f"alembic={alembic_finding['confidence']} django={django_finding['confidence']}"
        )


@pytest.mark.parametrize("fixture_name", PARITY_FIXTURES)
def test_alembic_and_django_produce_structurally_equivalent_findings(
    fixture_name: str, tmp_path: Path
) -> None:
    alembic_repo = copy_fixture(f"{fixture_name}/alembic", tmp_path)
    django_repo = copy_fixture(f"{fixture_name}/django", tmp_path)

    alembic_payload = run_check(alembic_repo)
    django_payload = run_check(django_repo)

    assert_parity(
        ParityAssertion(
            alembic_findings=alembic_payload["findings"],
            django_findings=django_payload["findings"],
        )
    )


@pytest.mark.parametrize("fixture_name", PARITY_FIXTURES)
def test_parity_fixtures_actually_exercise_the_intended_category(
    fixture_name: str, tmp_path: Path
) -> None:
    """Guards against a fixture silently rotting into "clean" (which would
    make assert_parity's count check trivially pass with zero findings on
    both sides — a false positive for parity)."""
    expected_category = {
        "parity_add_column": "orphaned_column",
        "parity_drop_column": "missing_column",
        "parity_alter_column_type": "type_mismatch",
    }[fixture_name]

    alembic_repo = copy_fixture(f"{fixture_name}/alembic", tmp_path)
    payload = run_check(alembic_repo)

    assert len(payload["findings"]) == 1
    assert payload["findings"][0]["category"] == expected_category


# --- Section 7 + SPEC-12 Amendment v1.1 G5: tolerance test is behavioral ---


def test_assert_parity_detects_a_one_unresolved_node_difference() -> None:
    """SPEC-12 Amendment v1.1 G5. The old regression test was
    ``assert TOLERANCE == 0.15`` — tautological, and would pass even if
    parity were silently broken. The behavioral replacement: seed exactly
    one ``UNRESOLVED_NODE_PENALTY`` of confidence difference between the
    Alembic and Django paths and assert ``assert_parity`` fails on it.

    With TOLERANCE < UNRESOLVED_NODE_PENALTY (0.05 < 0.15), the gap must
    land outside the band. Someone later bumping TOLERANCE back to 0.15
    to paper over a real parity break will see this test fail — the old
    one wouldn't.
    """
    from gravitar.analysis.confidence import UNRESOLVED_NODE_PENALTY

    assert TOLERANCE < UNRESOLVED_NODE_PENALTY, (
        "regression: TOLERANCE must be strictly less than one UNRESOLVED_NODE_PENALTY "
        f"so a one-node difference breaks parity; got TOLERANCE={TOLERANCE}, "
        f"UNRESOLVED_NODE_PENALTY={UNRESOLVED_NODE_PENALTY}"
    )

    base = _parity_finding_at_confidence(1.0)
    one_node_lower = _parity_finding_at_confidence(1.0 - UNRESOLVED_NODE_PENALTY)

    try:
        assert_parity(ParityAssertion(alembic_findings=[base], django_findings=[one_node_lower]))
    except AssertionError:
        return
    raise AssertionError(
        "assert_parity did not detect a one-unresolved-node confidence gap — tolerance "
        "is too wide to distinguish same from one-node-apart"
    )


def test_assert_parity_accepts_exactly_equal_confidence() -> None:
    """Sanity: assert_parity does NOT fire on true parity. Guards the
    seeded-mismatch test above from being a false-positive-only check."""
    f = _parity_finding_at_confidence(1.0)
    assert_parity(ParityAssertion(alembic_findings=[f], django_findings=[f]))


def test_assert_parity_accepts_within_tolerance_differences() -> None:
    """A sub-tolerance confidence drift (e.g. 0.03 < TOLERANCE=0.05) must
    still pass, so the band is doing real work rather than being a strict
    equality check in disguise."""
    base = _parity_finding_at_confidence(1.0)
    small_drift = _parity_finding_at_confidence(1.0 - TOLERANCE / 2)

    assert_parity(ParityAssertion(alembic_findings=[base], django_findings=[small_drift]))


def _parity_finding_at_confidence(confidence: float) -> dict[str, Any]:
    """Minimal finding dict shaped like what ``run_check`` returns, for
    direct ``assert_parity`` wiring without going through subprocess."""
    return {
        "category": "missing_column",
        "severity": "error",
        "column": "legacy",
        "table": "widgets",
        "confidence": confidence,
    }
