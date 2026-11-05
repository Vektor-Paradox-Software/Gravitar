"""apply_reliability integration tests: confirming/dismissing a finding
measurably changes future confidence scores for the same pattern.
"""

from pathlib import Path
from typing import Any

from gravitar.feedback.recalibrate import apply_reliability
from gravitar.feedback.store import record_outcome
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
        source=SourceLocation(file="models/__init__.py", line=10, revision_id=None),
        evidence=[Evidence(signal="ast_fully_resolved", weight=1.0, detail="")],
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


def test_finding_is_unchanged_with_no_feedback_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    finding = _finding()

    [result] = apply_reliability([finding], db_path)

    assert result["confidence"] == 1.0
    assert result["evidence"] == finding["evidence"]


def test_confirming_findings_for_a_pattern_raises_future_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    for i in range(5):
        record_outcome(db_path, f"id{i}", "ast:missing_column:resolved", "confirmed")

    [result] = apply_reliability([_finding()], db_path)

    assert result["confidence"] > 0.5
    assert result["confidence"] <= 1.0


def test_dismissing_findings_for_a_pattern_lowers_future_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    for i in range(5):
        record_outcome(db_path, f"id{i}", "ast:missing_column:resolved", "false_positive")

    [result] = apply_reliability([_finding()], db_path)

    assert result["confidence"] < 1.0


def test_recalibration_appends_a_historical_reliability_evidence_entry(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    record_outcome(db_path, "id0", "ast:missing_column:resolved", "confirmed")

    [result] = apply_reliability([_finding()], db_path)

    assert [e["signal"] for e in result["evidence"]] == [
        "ast_fully_resolved",
        "historical_reliability",
    ]


def test_recalibration_only_affects_findings_of_the_matching_pattern(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    for i in range(5):
        record_outcome(db_path, f"id{i}", "ast:missing_column:resolved", "false_positive")

    unaffected = _finding(category="type_mismatch", evidence=[])
    findings = apply_reliability([_finding(), unaffected], db_path)

    assert findings[0]["confidence"] < 1.0
    assert findings[1]["confidence"] == 1.0


def test_recalibrated_confidence_never_exceeds_one(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    for i in range(20):
        record_outcome(db_path, f"id{i}", "ast:missing_column:resolved", "confirmed")

    [result] = apply_reliability([_finding()], db_path)

    assert result["confidence"] <= 1.0


def test_dismissing_partial_findings_does_not_affect_resolved_findings_g4(
    tmp_path: Path,
) -> None:
    """SPEC-12 Amendment v1.1 G4: a dismissed partial-resolution finding
    (``kind="review"``, demoted by R3 taint) must NOT lower reliability
    for a fully-resolved finding of the same category. Pre-G4 they shared
    pattern_id ``"ast:<category>"`` and dismissals bled across — the exact
    auto-demotion hazard T7 names.
    """
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    # 5 dismissals recorded under the PARTIAL pattern track.
    for i in range(5):
        record_outcome(db_path, f"id{i}", "ast:missing_column:partial", "false_positive")

    # A fully-resolved finding (default kind="fail") should be untouched.
    resolved = _finding()
    # A partial finding (kind="review", demoted) should be lowered.
    partial = _finding(kind="review", demoted_from="error")

    results = apply_reliability([resolved, partial], db_path)

    assert results[0]["confidence"] == 1.0, "resolved track must not share dismissals with partial"
    assert results[1]["confidence"] < 1.0, "partial track should reflect its own dismissals"


def test_missing_database_leaves_findings_unadjusted(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    finding = _finding()

    [result] = apply_reliability([finding], db_path)

    assert result is finding
