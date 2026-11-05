"""SPEC-18 §3.2 scope boundary — deductions applied to BOTH sides
before scoring.

Gravitar does not compare these categories today, so a diff from
Alembic on any of them is an oracle artifact, not drift. Deducting
them from both the oracle and the Gravitar sets keeps the scoring
honest ("agreement with the oracle on the subset we support"), and
the deduction count per category is reported per unit so the scope
boundary is visible in every REPORT.md.

Adding a category here is a deliberate code change — ``run.py`` and
``summary.json`` echo this list — not an inference-time decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Alembic compare_metadata diff types that Gravitar does NOT compare.
#: Keys are what the Alembic autogen diff emits (`add_index`,
#: `remove_fk`, etc.); the harness strips any oracle entry whose
#: ``kind`` is in this set before scoring.
DEDUCTED_ORACLE_KINDS: frozenset[str] = frozenset(
    {
        # Indexes and constraints — presence not compared; FK TARGET
        # changes are compared (handled upstream in the diff mapper).
        "add_index",
        "remove_index",
        "add_constraint",
        "remove_constraint",
        "add_fk",
        "remove_fk",
        # Server-side defaults, comments, visibility.
        "modify_default",
        "modify_server_default",
        "modify_comment",
        "add_table_comment",
        "remove_table_comment",
        # Index-only columns' nullability (an index-only column's null
        # flag doesn't materially drive the schema).
        "modify_nullable_index_only",
        # Objects outside Gravitar's model surface entirely.
        "add_view",
        "remove_view",
        "add_trigger",
        "remove_trigger",
        "add_function",
        "remove_function",
        "add_procedure",
        "remove_procedure",
        "add_sequence",
        "remove_sequence",
        "add_enum_type",
        "remove_enum_type",
        "add_grant",
        "remove_grant",
        "add_policy",
        "remove_policy",
    }
)

#: Gravitar categories that have no oracle counterpart under the
#: current scope. The harness drops Gravitar findings in these
#: categories from the Gravitar set before scoring — e.g.
#: ``unknown_type`` is a Gravitar-only concept (walker admission of
#: incomplete resolution), not something the oracle can emit.
DEDUCTED_GRAVITAR_CATEGORIES: frozenset[str] = frozenset({"unknown_type", "broken_chain"})


@dataclass(frozen=True)
class ScopeDeduction:
    """One deduction's accounting: which side it was on and what it
    was. Reported per unit in summary.json so a reader can audit the
    boundary without re-running the oracle."""

    side: str  # "oracle" | "gravitar"
    kind: str  # the dropped Alembic diff kind or Gravitar category
    table: str | None
    column: str | None


def apply_scope_deductions(
    oracle_diffs: list[dict[str, Any]],
    gravitar_findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ScopeDeduction]]:
    """Strip out-of-scope items from both sides. Returns the filtered
    oracle list, filtered gravitar list, and a flat list of what was
    deducted (for the per-unit scope_deductions report)."""
    deductions: list[ScopeDeduction] = []

    kept_oracle: list[dict[str, Any]] = []
    for diff in oracle_diffs:
        kind = str(diff.get("kind", ""))
        if kind in DEDUCTED_ORACLE_KINDS:
            deductions.append(
                ScopeDeduction(
                    side="oracle",
                    kind=kind,
                    table=diff.get("table"),
                    column=diff.get("column"),
                )
            )
            continue
        kept_oracle.append(diff)

    kept_gravitar: list[dict[str, Any]] = []
    for finding in gravitar_findings:
        category = str(finding.get("category", ""))
        if category in DEDUCTED_GRAVITAR_CATEGORIES:
            deductions.append(
                ScopeDeduction(
                    side="gravitar",
                    kind=category,
                    table=finding.get("table"),
                    column=finding.get("column"),
                )
            )
            continue
        kept_gravitar.append(finding)

    return kept_oracle, kept_gravitar, deductions


def deduction_histogram(deductions: list[ScopeDeduction]) -> dict[str, int]:
    """Per-kind count across both sides for the report."""
    hist: dict[str, int] = {}
    for d in deductions:
        hist[d.kind] = hist.get(d.kind, 0) + 1
    return hist
