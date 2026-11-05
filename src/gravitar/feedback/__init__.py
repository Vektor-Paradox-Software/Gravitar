"""Local, per-repo outcome feedback and pattern reliability.

Users mark a DriftFinding confirmed (real drift) or dismissed (false positive)
via `gravitar feedback confirm/dismiss`; outcomes are tracked per pattern_id
and feed back into the confidence formula as a reliability multiplier
(reliability.py, recalibrate.py).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FindingOutcome:
    finding_id: str
    pattern_id: str
    verdict: str  # "confirmed" | "false_positive"
    timestamp: str


@dataclass(frozen=True)
class PatternReliability:
    pattern_id: str
    confirm_count: int
    dismiss_count: int
    reliability: float
