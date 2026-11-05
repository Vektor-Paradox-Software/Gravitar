"""finding_id / pattern_id tests."""

from typing import Any

from gravitar.feedback.ids import finding_id, pattern_id
from gravitar.schema import DriftFinding, Evidence, SourceLocation


def _finding(**overrides: Any) -> DriftFinding:
    base: DriftFinding = DriftFinding(
        category="missing_column",
        severity="error",
        table="users",
        column="deleted_at",
        model_value=None,
        migration_value=None,
        message="",
        suggestion=None,
        confidence=1.0,
        source=SourceLocation(file="models/__init__.py", line=10, revision_id="0001"),
        evidence=[Evidence(signal="ast_fully_resolved", weight=1.0, detail="")],
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# --- finding_id: stable, and distinct for distinct findings ---


def test_finding_id_is_deterministic() -> None:
    assert finding_id(_finding()) == finding_id(_finding())


def test_finding_id_differs_by_table() -> None:
    assert finding_id(_finding(table="users")) != finding_id(_finding(table="orders"))


def test_finding_id_differs_by_column() -> None:
    assert finding_id(_finding(column="a")) != finding_id(_finding(column="b"))


def test_finding_id_differs_by_category() -> None:
    assert finding_id(_finding(category="missing_column")) != finding_id(
        _finding(category="type_mismatch")
    )


def test_finding_id_differs_by_revision_id() -> None:
    a = _finding(source=SourceLocation(file="x.py", line=None, revision_id="0001"))
    b = _finding(source=SourceLocation(file="x.py", line=None, revision_id="0002"))
    assert finding_id(a) != finding_id(b)


def test_finding_id_handles_none_table_and_column() -> None:
    # broken_chain findings have table=column=None; must not raise.
    finding_id(_finding(table=None, column=None))


def test_finding_id_handles_none_revision_id() -> None:
    # compare.py-derived findings never have a revision_id; must not raise.
    finding_id(_finding(source=SourceLocation(file="x.py", line=None, revision_id=None)))


# --- pattern_id: matched raw-SQL signature wins; category fallback otherwise ---


def test_pattern_id_falls_back_to_ast_prefixed_category_with_resolution_class() -> None:
    """SPEC-12 Amendment v1.1 G4: pattern_id now carries ``:resolved`` or
    ``:partial`` suffix so dismissals of noisy taint-explained findings
    don't bleed into the reliability track for fully-resolved findings of
    the same category."""
    # Default: no review/demotion → :resolved
    assert pattern_id(_finding(category="missing_column")) == "ast:missing_column:resolved"
    # kind="review" (demoted by R3) → :partial
    assert (
        pattern_id(_finding(category="missing_column", kind="review", demoted_from="error"))
        == "ast:missing_column:partial"
    )


def test_pattern_id_separates_partial_from_resolved_reliability_tracks() -> None:
    """G4: a dismissed partial-resolution ``type_mismatch`` must not share
    a reliability track with a fully-resolved real ``type_mismatch``, or
    R3 demotion compounds the hazard T7 warns about."""
    resolved = _finding(category="type_mismatch")
    partial = _finding(category="type_mismatch", kind="review", demoted_from="error")

    assert pattern_id(resolved) != pattern_id(partial)


def test_pattern_id_uses_matched_sql_signature_when_present() -> None:
    finding = _finding(
        category="broken_chain",
        evidence=[
            Evidence(signal="raw_sql_unresolved", weight=0.3, detail="x"),
            Evidence(signal="sql_pattern_match", weight=0.2, detail="sql:add_column"),
        ],
    )
    assert pattern_id(finding) == "sql:add_column"


# --- Section 5: distinct pattern_ids must never collapse into the same bucket ---


def test_pattern_ids_for_distinct_categories_are_distinct() -> None:
    ids = {
        pattern_id(_finding(category=category))
        for category in (
            "missing_table",
            "missing_column",
            "type_mismatch",
            "nullability_mismatch",
            "orphaned_column",
            "orphaned_reference",
            "broken_chain",
            "unknown_type",
        )
    }
    assert len(ids) == 8


def test_pattern_id_for_distinct_matched_sql_signatures_are_distinct() -> None:
    ids = {
        pattern_id(
            _finding(evidence=[Evidence(signal="sql_pattern_match", weight=0.2, detail=sig)])
        )
        for sig in (
            "sql:add_column",
            "sql:drop_column",
            "sql:alter_column_type",
            "sql:create_index",
            "sql:create_unique_index",
        )
    }
    assert len(ids) == 5
