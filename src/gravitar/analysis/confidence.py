"""Confidence scoring.

Callers (compare.py, migrations.py) don't have a single Alembic op call backing
every finding — several categories come from a full static schema diff rather
than one operation. `base_confidence` is therefore keyed by the *resolution
signal* a finding was built from, not by op type directly: "ast_fully_resolved"
covers every v1 category built from a complete static comparison, "unknown_type"
covers a type neither side's introspection could normalize, and
"raw_sql_unresolved" covers an `op.execute()` call the migration parser can't
see into.

`unresolved_penalty`/`corroboration_bonus` are fixed per-item deductions/bonuses
so the raw-SQL signature classifier can plug real unresolved AST nodes
and matched SQL signatures into `score_finding` without this formula changing
shape.
"""

from __future__ import annotations

from collections.abc import Sequence

from gravitar.schema import Evidence

# base_confidence(op_type) — see module docstring for why this is keyed by
# resolution signal rather than Alembic op type.
_BASE_CONFIDENCE: dict[str, float] = {
    "ast_fully_resolved": 1.0,
    "unknown_type": 0.5,
    "raw_sql_unresolved": 0.3,
    # Lower than raw_sql_unresolved: the raw-SQL classifier *did* recover the
    # operation's structure (we know exactly what it's trying to do), but the
    # table/column it names isn't present in the schema state being built — a
    # concrete, concerning signal, not mere ambiguity.
    "orphaned_reference": 0.2,
}

# Fixed per-item deduction/bonus.
UNRESOLVED_NODE_PENALTY = 0.15
SIGNATURE_MATCH_BONUS = 0.2


def base_confidence(signal: str) -> float:
    """Fixed lookup table entry for a resolution signal (Section 4)."""
    return _BASE_CONFIDENCE[signal]


def unresolved_penalty(_node: object) -> float:
    """Fixed deduction per AST node the walker could not statically resolve."""
    return UNRESOLVED_NODE_PENALTY


def corroboration_bonus(_signature: str) -> float:
    """Fixed addition per raw-SQL pattern the Section 5 classifier matched."""
    return SIGNATURE_MATCH_BONUS


def score_finding(
    signal: str,
    detail: str,
    *,
    unresolved_nodes: Sequence[object] = (),
    matched_signatures: Sequence[str] = (),
) -> tuple[float, list[Evidence]]:
    """Compute a finding's confidence score and its evidence trail (Section 4).

    `evidence[0]` always records the base signal; one entry follows per
    unresolved node (negative weight) and per matched signature (positive
    weight), in that order, so the trail is reproducible from its inputs alone.
    """
    base = base_confidence(signal)
    evidence: list[Evidence] = [Evidence(signal=signal, weight=base, detail=detail)]
    total = base

    for node in unresolved_nodes:
        penalty = unresolved_penalty(node)
        total -= penalty
        evidence.append(Evidence(signal="unresolved_node", weight=-penalty, detail=str(node)))

    for matched_signature in matched_signatures:
        bonus = corroboration_bonus(matched_signature)
        total += bonus
        evidence.append(
            Evidence(signal="sql_pattern_match", weight=bonus, detail=matched_signature)
        )

    return clamp(total), evidence


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
