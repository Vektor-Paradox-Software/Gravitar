"""SPEC-18 T6 scoring tests. One hand-crafted fixture per §5 edge case.

Keep these small and deterministic — each test either verifies a
specific scoring rule or a specific gate decision. If a new edge
case comes up at run time, add a fixture here first.
"""

from __future__ import annotations

from benchmarks.oracle.scope import (
    DEDUCTED_GRAVITAR_CATEGORIES,
    DEDUCTED_ORACLE_KINDS,
    apply_scope_deductions,
    deduction_histogram,
)
from benchmarks.oracle.score import decide_gate, score_unit


def _finding(
    category: str = "missing_column",
    table: str = "widgets",
    column: str = "deleted_at",
    severity: str = "error",
    kind: str = "fail",
) -> dict[str, object]:
    return {
        "category": category,
        "table": table,
        "column": column,
        "severity": severity,
        "kind": kind,
    }


def _diff(
    category: str = "missing_column",
    table: str = "widgets",
    column: str = "deleted_at",
) -> dict[str, object]:
    return {"category": category, "table": table, "column": column}


# --- Scope deductions ---


def test_apply_scope_deductions_removes_oracle_indexes() -> None:
    oracle_diffs = [
        {"kind": "add_index", "table": "widgets", "column": None},
        _diff(),
    ]
    kept_o, kept_g, deducts = apply_scope_deductions(oracle_diffs, [])

    assert len(kept_o) == 1
    assert kept_o[0]["column"] == "deleted_at"
    assert len(deducts) == 1
    assert deducts[0].side == "oracle"
    assert deducts[0].kind == "add_index"


def test_apply_scope_deductions_removes_gravitar_unknown_type() -> None:
    gravitar_findings = [
        _finding(category="unknown_type", column="data"),
        _finding(),
    ]
    _, kept_g, deducts = apply_scope_deductions([], gravitar_findings)

    assert len(kept_g) == 1
    assert kept_g[0]["column"] == "deleted_at"
    assert any(d.side == "gravitar" and d.kind == "unknown_type" for d in deducts)


def test_deduction_taxonomy_matches_spec_section_3_2() -> None:
    """Guard: SPEC-18 §3.2 lists these categories as "deducted from
    both sides before scoring". If someone narrows the lists without
    editing the spec, this test flags it."""
    assert "add_index" in DEDUCTED_ORACLE_KINDS
    assert "modify_server_default" in DEDUCTED_ORACLE_KINDS
    assert "add_view" in DEDUCTED_ORACLE_KINDS
    assert "unknown_type" in DEDUCTED_GRAVITAR_CATEGORIES


def test_deduction_histogram_counts_per_kind() -> None:
    _, _, deducts = apply_scope_deductions(
        [
            {"kind": "add_index", "table": "a", "column": None},
            {"kind": "add_index", "table": "b", "column": None},
            {"kind": "modify_server_default", "table": "c", "column": "x"},
        ],
        [_finding(category="unknown_type", column="y")],
    )
    hist = deduction_histogram(deducts)

    assert hist["add_index"] == 2
    assert hist["modify_server_default"] == 1
    assert hist["unknown_type"] == 1


# --- Scoring: TP/FP/FN basics ---


def test_tp_when_gravitar_matches_oracle() -> None:
    score = score_unit([_diff()], [_finding()])

    assert score.true_positives == 1
    assert score.false_positives.total == 0
    assert score.false_negatives.total == 0


def test_fp_counted_at_finding_tier() -> None:
    # Oracle sees nothing; Gravitar flags two things at different tiers.
    score = score_unit(
        [],
        [
            _finding(column="a", severity="error", kind="fail"),
            _finding(column="b", severity="warning", kind="fail"),
            _finding(column="c", severity="error", kind="review"),
        ],
    )

    assert score.false_positives.error == 1
    assert score.false_positives.warning == 1
    assert score.false_positives.review == 1


def test_fn_counted_at_derived_tier() -> None:
    # Oracle has three different categories; Gravitar misses all.
    score = score_unit(
        [
            _diff(category="missing_column", column="a"),
            _diff(category="orphaned_column", column="b"),
            _diff(category="type_mismatch", column="c"),
        ],
        [],
    )
    # missing_column + type_mismatch → error tier; orphaned_column → warning.
    assert score.false_negatives.error == 2
    assert score.false_negatives.warning == 1


# --- Scoring: tainted-table rules (SPEC §5) ---


def test_fp_on_tainted_table_demotes_from_error_to_warning() -> None:
    """SPEC-18 §5: ``fp_tainted`` is reported but excluded from the
    error-tier FP count."""
    score = score_unit(
        [],
        [_finding(table="widgets", severity="error", kind="fail")],
        tainted_tables={"widgets"},
    )

    assert score.fp_tainted == 1
    assert score.false_positives.error == 0  # demoted
    assert score.false_positives.warning == 1


def test_fn_on_tainted_table_not_scored_at_warning_tier() -> None:
    """SPEC-18 §5: a miss on a tainted table at warning/review tier
    is counted as a taint-explained miss (R3 explains it), NOT as a
    warning-tier FN."""
    score = score_unit(
        [_diff(category="orphaned_column", table="widgets", column="x")],
        [],
        tainted_tables={"widgets"},
    )

    assert score.taint_explained_misses == 1
    assert score.false_negatives.warning == 0


def test_fn_on_tainted_table_still_scored_at_error_tier() -> None:
    """SPEC-18 §5 is explicit: error-tier misses STILL count even
    when the table is tainted. Taint demotes but never masks error."""
    score = score_unit(
        [_diff(category="missing_column", table="widgets", column="x")],
        [],
        tainted_tables={"widgets"},
    )

    assert score.false_negatives.error == 1
    assert score.taint_explained_misses == 0


def test_tp_on_tainted_table_counted_as_tp_tainted() -> None:
    score = score_unit(
        [_diff(table="widgets")],
        [_finding(table="widgets")],
        tainted_tables={"widgets"},
    )

    assert score.true_positives == 1
    assert score.tp_tainted == 1


# --- Scoring: agreement (§5 formula) ---


def test_agreement_overall_jaccard() -> None:
    """3 of 4 in intersection, union of 5 → Jaccard = 3/5."""
    oracle = [
        _diff(category="missing_column", column="a"),
        _diff(category="missing_column", column="b"),
        _diff(category="missing_column", column="c"),
        _diff(category="missing_column", column="only_in_oracle"),
    ]
    gravitar = [
        _finding(column="a"),
        _finding(column="b"),
        _finding(column="c"),
        _finding(column="only_in_gravitar"),
    ]

    score = score_unit(oracle, gravitar)

    assert score.agreement_overall == 0.6  # 3/5


def test_agreement_by_category_split() -> None:
    """Different categories agree independently; the by_category map
    carries each one's own Jaccard."""
    oracle = [
        _diff(category="missing_column", column="a"),
        _diff(category="type_mismatch", column="t"),
    ]
    gravitar = [
        _finding(category="missing_column", column="a"),
        _finding(category="type_mismatch", column="DIFFERENT"),
    ]

    score = score_unit(oracle, gravitar)

    assert score.agreement_by_category["missing_column"] == 1.0
    assert score.agreement_by_category["type_mismatch"] == 0.0


def test_agreement_empty_sets_is_one() -> None:
    """Both sides saw nothing — agreement is trivially 1.0, not NaN."""
    score = score_unit([], [])

    assert score.agreement_overall == 1.0


# --- Gate decision (SPEC-16 v1.1 §B) ---


def test_gate_passes_on_clean_scores() -> None:
    s = score_unit([_diff()], [_finding()])

    gate = decide_gate([s])

    assert gate.passed is True
    assert gate.first_failure is None


def test_gate_fails_on_too_many_error_fps_pre_1_0() -> None:
    """Pre-1.0 cap is 1 total error-FP; 2 trips the gate."""
    s = score_unit(
        [],
        [
            _finding(column="a"),
            _finding(column="b"),
        ],
    )

    gate = decide_gate([s], pre_1_0=True)

    assert gate.passed is False
    assert "error_fp_total" in (gate.first_failure or "")


def test_gate_fails_on_warning_fp_rate_over_cap() -> None:
    """Fabricate 1 TP + 3 warning-FPs → rate = 3/1 = 3.0, over the
    0.10 cap. (The denominator is oracle size, so warning-tier FPs
    can push the rate above 1.)"""
    s = score_unit(
        [_diff()],
        [
            _finding(column="deleted_at"),  # TP
            _finding(column="spurious1", severity="warning"),
            _finding(column="spurious2", severity="warning"),
            _finding(column="spurious3", severity="warning"),
        ],
    )

    gate = decide_gate([s], pre_1_0=True)

    assert gate.passed is False
    assert "warning_fp_rate" in (gate.first_failure or "")


def test_gate_tightens_at_ga() -> None:
    """At GA the error-FP cap is 0; a single error-FP fails."""
    s = score_unit([], [_finding()])

    gate = decide_gate([s], pre_1_0=False)

    assert gate.passed is False


# --- Deterministic ordering (SPEC §2.8) ---


def test_score_as_dict_sorts_categories() -> None:
    """SPEC-18 §2.8 deterministic ordering: by_category is sorted
    alphabetically in output so a diff between runs shows only
    genuine changes, not key reorderings."""
    oracle = [
        _diff(category="z_cat", column="x"),
        _diff(category="a_cat", column="y"),
    ]
    gravitar = [
        _finding(category="z_cat", column="x"),
        _finding(category="a_cat", column="y"),
    ]

    out = score_unit(oracle, gravitar).as_dict()
    cats = list(out["agreement"]["by_category"])
    assert cats == sorted(cats)
