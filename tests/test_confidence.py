"""Confidence scoring tests."""

import pytest
from conftest import FIXTURES, analyze_fixture
from hypothesis import given
from hypothesis import strategies as st

from gravitar.analysis.confidence import (
    SIGNATURE_MATCH_BONUS,
    UNRESOLVED_NODE_PENALTY,
    base_confidence,
    score_finding,
)

# --- Fixture-based tests: one per base signal, exact expected score/evidence ---


def test_ast_fully_resolved_scores_at_base() -> None:
    confidence, evidence = score_finding("ast_fully_resolved", "fully resolved")

    assert confidence == 1.0
    assert evidence == [{"signal": "ast_fully_resolved", "weight": 1.0, "detail": "fully resolved"}]


def test_unknown_type_scores_at_base() -> None:
    confidence, evidence = score_finding("unknown_type", "type not normalized")

    assert confidence == 0.5
    assert evidence == [{"signal": "unknown_type", "weight": 0.5, "detail": "type not normalized"}]


def test_raw_sql_unresolved_scores_at_base() -> None:
    confidence, evidence = score_finding("raw_sql_unresolved", "op.execute() not classified")

    assert confidence == 0.3
    assert evidence == [
        {"signal": "raw_sql_unresolved", "weight": 0.3, "detail": "op.execute() not classified"}
    ]


def test_unknown_signal_raises() -> None:
    with pytest.raises(KeyError):
        score_finding("not_a_real_signal", "detail")


# --- Golden evidence-list regression: unresolved nodes and matched signatures each
# append their own evidence entry, in input order, penalty/bonus before base is unused
# (base is always evidence[0]) ---


def test_unresolved_nodes_append_penalty_evidence_in_order() -> None:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "fully resolved", unresolved_nodes=["node_a", "node_b"]
    )

    assert confidence == pytest.approx(1.0 - 2 * UNRESOLVED_NODE_PENALTY)
    assert evidence == [
        {"signal": "ast_fully_resolved", "weight": 1.0, "detail": "fully resolved"},
        {"signal": "unresolved_node", "weight": -UNRESOLVED_NODE_PENALTY, "detail": "node_a"},
        {"signal": "unresolved_node", "weight": -UNRESOLVED_NODE_PENALTY, "detail": "node_b"},
    ]


def test_matched_signatures_append_bonus_evidence_in_order() -> None:
    confidence, evidence = score_finding(
        "raw_sql_unresolved", "op.execute() not classified", matched_signatures=["sql:add_column"]
    )

    assert confidence == pytest.approx(0.3 + SIGNATURE_MATCH_BONUS)
    assert evidence == [
        {"signal": "raw_sql_unresolved", "weight": 0.3, "detail": "op.execute() not classified"},
        {
            "signal": "sql_pattern_match",
            "weight": SIGNATURE_MATCH_BONUS,
            "detail": "sql:add_column",
        },
    ]


# --- Clamping ---


def test_score_never_exceeds_one() -> None:
    confidence, _ = score_finding(
        "ast_fully_resolved", "fully resolved", matched_signatures=["a", "b", "c", "d", "e"]
    )

    assert confidence == 1.0


def test_score_never_drops_below_zero() -> None:
    confidence, _ = score_finding(
        "raw_sql_unresolved", "unresolved", unresolved_nodes=list(range(10))
    )

    assert confidence == 0.0


# --- Property tests: monotonicity and range ---


@given(st.integers(min_value=0, max_value=20))
def test_score_is_monotonically_non_increasing_in_unresolved_node_count(count: int) -> None:
    fewer, _ = score_finding("ast_fully_resolved", "x", unresolved_nodes=list(range(count)))
    more, _ = score_finding("ast_fully_resolved", "x", unresolved_nodes=list(range(count + 1)))

    assert more <= fewer


@given(
    st.sampled_from(["ast_fully_resolved", "unknown_type", "raw_sql_unresolved"]),
    st.lists(st.integers(), max_size=10),
    st.lists(st.text(max_size=10), max_size=10),
)
def test_score_always_stays_in_unit_range(
    signal: str, unresolved_nodes: list[int], matched_signatures: list[str]
) -> None:
    confidence, _ = score_finding(
        signal, "x", unresolved_nodes=unresolved_nodes, matched_signatures=matched_signatures
    )

    assert 0.0 <= confidence <= 1.0


# --- base_confidence lookup ---


def test_base_confidence_matches_score_finding_with_no_adjustments() -> None:
    for signal in ("ast_fully_resolved", "unknown_type", "raw_sql_unresolved"):
        confidence, _ = score_finding(signal, "x")
        assert confidence == base_confidence(signal)


# --- End-to-end: every finding produced by the real pipeline carries a score in
# [0, 1] and a non-empty evidence trail; the v1 fixtures' fully-resolved findings
# score at 1.0, and the not-yet-classified raw_sql fixture's broken_chain finding
# scores low (Milestone 8 raises it via corroboration_bonus). ---


@pytest.mark.parametrize(
    "fixture_name",
    [p.name for p in FIXTURES.iterdir() if (p / "alembic.ini").exists()],
)
def test_every_finding_has_confidence_and_evidence(
    fixture_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = analyze_fixture(fixture_name, monkeypatch)

    for finding in result.findings:
        assert 0.0 <= finding["confidence"] <= 1.0
        assert finding["evidence"]
        assert finding["source"]["file"]


def test_raw_sql_fixture_is_inert_under_r12(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-R12: the ``op.execute("UPDATE ...")`` here produced a 0.3-confidence
    broken_chain warning (noise). Post-R12 the DML is correctly classified as
    inert — no finding, no note. The old test is kept as a baseline so we
    notice if the classification ever regresses to generating noise."""
    result = analyze_fixture("raw_sql", monkeypatch)

    assert result.findings == []
    # And no dynamic_sql note either — the statement WAS fully understood,
    # just inert for schema purposes.
    assert not any(n["reason"] == "dynamic_sql" for n in result.notes)


def test_missing_column_fixture_scores_at_full_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    result = analyze_fixture("missing_column", monkeypatch)

    assert result.findings
    assert all(f["confidence"] == 1.0 for f in result.findings)


def test_unknown_type_fixture_scores_at_half_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    result = analyze_fixture("unknown_type", monkeypatch)

    unknown_type_findings = [f for f in result.findings if f["category"] == "unknown_type"]
    assert unknown_type_findings
    assert all(f["confidence"] == 0.5 for f in unknown_type_findings)


# --- A recognized raw-SQL pattern scores meaningfully higher than an
# unrecognized one, with a full evidence trail. ---


def test_matched_create_index_still_scores_above_unmatched_dml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-R12: matched CREATE INDEX = 0.5 confidence, DML = 0.3. Post-R12:
    DML is inert (no finding at all); CREATE INDEX is matched but Gravitar's
    Schema has no index concept, so the warning finding is still emitted
    (correct — the user should still see "I noticed this but I don't model
    indexes"). The signature classifier's confidence contribution is
    unchanged for the matched case, so this still records at 0.5.
    """
    matched = analyze_fixture("raw_sql_matched_pattern", monkeypatch)

    matched_findings = [
        f for f in matched.findings if f["category"] == "broken_chain" and "raw SQL" in f["message"]
    ]
    assert len(matched_findings) == 1
    assert matched_findings[0]["confidence"] == pytest.approx(0.5)
    signals = [e["signal"] for e in matched_findings[0]["evidence"]]
    assert "sql_pattern_match" in signals


def test_orphaned_reference_fixture_produces_distinct_low_confidence_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = analyze_fixture("raw_sql_orphaned_reference", monkeypatch)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["category"] == "orphaned_reference"
    assert finding["confidence"] == 0.2
    assert finding["table"] == "ghost_table"
    assert finding["column"] == "nickname"
    assert finding["evidence"][0]["signal"] == "orphaned_reference"
