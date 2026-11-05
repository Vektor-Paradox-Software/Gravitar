"""Wires pattern reliability (reliability.py) into DriftFinding.confidence, using
outcome counts recorded in feedback.sqlite (store.py). Applies the reliability
multiplier on top of the base confidence formula.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.analysis.confidence import clamp
from gravitar.feedback.ids import pattern_id as pattern_id_for
from gravitar.feedback.reliability import reliability_multiplier
from gravitar.feedback.store import pattern_counts
from gravitar.schema import DriftFinding, Evidence


def apply_reliability(findings: list[DriftFinding], db_path: Path) -> list[DriftFinding]:
    """Multiply each finding's confidence by its pattern's historical reliability,
    appending an evidence entry recording the adjustment. A pattern with no
    recorded outcomes yet is left unadjusted — see reliability_multiplier.
    """
    counts = pattern_counts(db_path)
    return [_recalibrate(finding, counts) for finding in findings]


def _recalibrate(finding: DriftFinding, counts: dict[str, tuple[int, int]]) -> DriftFinding:
    pattern = pattern_id_for(finding)
    confirm_count, dismiss_count = counts.get(pattern, (0, 0))
    multiplier = reliability_multiplier(confirm_count, dismiss_count)
    if multiplier == 1.0:
        return finding

    evidence = [
        *finding["evidence"],
        Evidence(
            signal="historical_reliability",
            weight=multiplier,
            detail=f"pattern {pattern!r}: {confirm_count} confirmed, {dismiss_count} dismissed",
        ),
    ]
    return DriftFinding(
        category=finding["category"],
        severity=finding["severity"],
        table=finding["table"],
        column=finding["column"],
        model_value=finding["model_value"],
        migration_value=finding["migration_value"],
        message=finding["message"],
        suggestion=finding["suggestion"],
        confidence=clamp(finding["confidence"] * multiplier),
        source=finding["source"],
        evidence=evidence,
        live_verified=finding["live_verified"],
        kind=finding["kind"],
        demoted_from=finding["demoted_from"],
        explaining_notes=finding["explaining_notes"],
    )
