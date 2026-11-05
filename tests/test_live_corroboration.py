"""Live (read-only) database corroboration tests.

Run against a real, ephemeral SQLite database (no mocking, no Docker, no
external service or credentials needed) rather than Postgres — this keeps
the primary suite dependency-free while still exercising the real SQLAlchemy
reflection path end-to-end, which a pure mock of `introspect_single_column`
wouldn't.
"""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from gravitar.analysis.live_corroboration import (
    LIVE_CORROBORATION_BONUS,
    LIVE_CORROBORATION_THRESHOLD,
    corroborate,
    corroboration_bonus_live,
)
from gravitar.schema import DriftFinding, Evidence, SourceLocation


def _finding(**overrides: Any) -> DriftFinding:
    base: DriftFinding = DriftFinding(
        category="unknown_type",
        severity="warning",
        table="widgets",
        column="name",
        model_value="unknown:CustomType",
        migration_value="unknown:CustomType",
        message="column type could not be normalized on at least one side",
        suggestion=None,
        confidence=0.5,
        source=SourceLocation(file="models/__init__.py", line=10, revision_id=None),
        evidence=[Evidence(signal="unknown_type", weight=0.5, detail="")],
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


@pytest.fixture
def live_db_url(tmp_path: Path) -> str:
    db_path = tmp_path / "live.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE widgets ("
                "id INTEGER PRIMARY KEY, "
                "name VARCHAR(50) NOT NULL, "
                "active BOOLEAN"
                ")"
            )
        )
    engine.dispose()
    return f"sqlite:///{db_path}"


# --- Section 4: threshold and table/column gating ---


def test_finding_at_threshold_is_unchanged(live_db_url: str) -> None:
    finding = _finding(confidence=LIVE_CORROBORATION_THRESHOLD)

    [result] = corroborate([finding], live_db_url)

    assert result is finding


def test_fully_confident_finding_is_never_queried(live_db_url: str) -> None:
    finding = _finding(confidence=1.0, table="does_not_exist", column="also_missing")

    [result] = corroborate([finding], live_db_url)

    assert result is finding


def test_finding_without_table_is_unchanged(live_db_url: str) -> None:
    finding = _finding(table=None)

    [result] = corroborate([finding], live_db_url)

    assert result is finding


def test_finding_without_column_is_unchanged(live_db_url: str) -> None:
    finding = _finding(column=None)

    [result] = corroborate([finding], live_db_url)

    assert result is finding


# --- Section 6: neutral results ---


def test_nonexistent_table_is_neutral(live_db_url: str) -> None:
    finding = _finding(table="does_not_exist")

    [result] = corroborate([finding], live_db_url)

    assert result is finding
    assert result["live_verified"] is False


def test_nonexistent_column_is_neutral(live_db_url: str) -> None:
    finding = _finding(column="does_not_exist")

    [result] = corroborate([finding], live_db_url)

    assert result is finding


def test_introspection_error_is_treated_as_no_value(
    live_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 6: a reflection error for one finding must not crash the run —
    same "no live value" neutral treatment as any other miss."""
    import gravitar.analysis.live_corroboration as mod

    def _boom(_engine: object) -> None:
        raise RuntimeError("reflection blew up")

    monkeypatch.setattr(mod, "inspect", _boom)
    finding = _finding()

    [result] = corroborate([finding], live_db_url)

    assert result is finding


def test_unrecognized_live_type_is_treated_as_no_value(
    live_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gravitar.analysis.live_corroboration as mod

    monkeypatch.setattr(mod, "normalize_type", lambda t: "unknown:Weird")
    finding = _finding()

    [result] = corroborate([finding], live_db_url)

    assert result is finding


# --- Section 4: the two "live value present" branches ---


def test_live_value_matches_migration_side(live_db_url: str) -> None:
    finding = _finding(
        category="unknown_type",
        model_value="unknown:CustomType",
        migration_value="varchar(50)",  # matches the real "name" column live
        confidence=0.5,
    )

    [result] = corroborate([finding], live_db_url)

    assert result["live_verified"] is True
    assert result["confidence"] == pytest.approx(0.5 + LIVE_CORROBORATION_BONUS)
    assert result["evidence"][-1] == {
        "signal": "live_db_corroboration",
        "weight": LIVE_CORROBORATION_BONUS,
        "detail": "live schema matches migration",
    }


def test_live_value_matches_model_side(live_db_url: str) -> None:
    finding = _finding(
        category="unknown_type",
        model_value="varchar(50)",
        migration_value="unknown:CustomType",
        confidence=0.5,
    )

    [result] = corroborate([finding], live_db_url)

    assert result["live_verified"] is True
    assert result["evidence"][-1]["detail"] == "live schema matches model"


def test_live_value_matches_neither_confirms_drift(live_db_url: str) -> None:
    finding = _finding(
        category="type_mismatch",
        model_value="integer",
        migration_value="text",
        confidence=0.5,
    )

    [result] = corroborate([finding], live_db_url)

    assert result["live_verified"] is True
    assert result["confidence"] == 1.0
    assert result["evidence"][-1] == {
        "signal": "live_db_corroboration",
        "weight": 1.0,
        "detail": "live schema matches neither migration nor model",
    }


# --- orphaned_reference: existence-based corroboration (no type comparison) ---


def test_orphaned_reference_corroborated_by_existence(live_db_url: str) -> None:
    finding = _finding(
        category="orphaned_reference",
        table="widgets",
        column="active",
        model_value=None,
        migration_value="ALTER TABLE widgets ADD COLUMN active BOOLEAN",
        confidence=0.2,
    )

    [result] = corroborate([finding], live_db_url)

    assert result["live_verified"] is True
    assert result["confidence"] == pytest.approx(0.2 + LIVE_CORROBORATION_BONUS)
    assert result["evidence"][-1]["detail"] == "live schema confirms the table/column exists"


def test_orphaned_reference_with_no_live_column_is_neutral(live_db_url: str) -> None:
    finding = _finding(
        category="orphaned_reference",
        table="widgets",
        column="does_not_exist",
        model_value=None,
        migration_value="ALTER TABLE widgets ADD COLUMN does_not_exist BOOLEAN",
        confidence=0.2,
    )

    [result] = corroborate([finding], live_db_url)

    assert result is finding


# --- Section 5: connection failure fails soft ---


def test_invalid_url_fails_soft_and_leaves_findings_unchanged() -> None:
    findings = [_finding()]

    result = corroborate(findings, "not-a-valid-database-url")

    assert result == findings
    assert result[0]["live_verified"] is False


def test_unreachable_host_fails_soft() -> None:
    findings = [_finding()]

    result = corroborate(findings, "postgresql://baduser:badpass@256.256.256.256/nope")

    assert result == findings


def test_connection_failure_never_logs_the_url(caplog: pytest.LogCaptureFixture) -> None:
    secret_url = "postgresql://admin:supersecret123@db.internal/prod"

    corroborate([_finding()], secret_url)

    for record in caplog.records:
        assert "supersecret123" not in record.getMessage()
        assert secret_url not in record.getMessage()


# --- corroboration_bonus_live: pure function ---


def test_corroboration_bonus_live_clamps_at_one() -> None:
    assert corroboration_bonus_live(0.9) == 1.0


def test_corroboration_bonus_live_adds_the_bonus() -> None:
    assert corroboration_bonus_live(0.2) == pytest.approx(0.2 + LIVE_CORROBORATION_BONUS)


# --- multiple findings: only the eligible ones are touched ---


def test_only_low_confidence_findings_with_table_and_column_are_touched(live_db_url: str) -> None:
    confident = _finding(confidence=1.0)
    no_column = _finding(column=None, confidence=0.3)
    eligible = _finding(
        category="unknown_type",
        migration_value="varchar(50)",
        model_value="unknown:X",
        confidence=0.5,
    )

    results = corroborate([confident, no_column, eligible], live_db_url)

    assert results[0] is confident
    assert results[1] is no_column
    assert results[2]["live_verified"] is True
