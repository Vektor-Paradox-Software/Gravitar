"""SPEC-18 §5 scoring. One source of truth for every metric in the
report; no downstream re-derivation.

For a unit, after scope deductions on both sides:
- Oracle set O: strict-mode changes keyed by (category, table, column).
- Gravitar set G: findings (category, table, column), tiered.

Outcomes:
- true_positives: O ∩ G
- false_positives: G \\ O (split error/warning/review tier)
- false_negatives: O \\ G
- taint-explained misses: (O \\ G) ∩ tainted_tables — counted but
  not scored as FN at warning/review tier (R3 demote-only-when-
  explained); still FN at error tier.
- agreement by category and overall: |O ∩ G| / |O ∪ G|

The input Alembic diff kinds must be pre-mapped to Gravitar
categories (``add_column`` → ``missing_column``, etc.) before being
passed here.
That mapping is the oracle runner's job (T4/T5); score.py just
consumes already-mapped (category, table, column) tuples.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

Tier = Literal["error", "warning", "review"]


def _finding_tier(finding: dict[str, Any]) -> Tier:
    """SPEC-16 R3: ``kind == 'review'`` beats severity; otherwise the
    severity ('error'/'warning') IS the tier."""
    if finding.get("kind") == "review":
        return "review"
    severity = finding.get("severity", "error")
    if severity == "warning":
        return "warning"
    return "error"


def _key(entry: dict[str, Any]) -> tuple[str, str | None, str | None]:
    return (
        str(entry.get("category", "")),
        entry.get("table"),
        entry.get("column"),
    )


@dataclass
class TierCounts:
    """SPEC-16 v1.1 §B tier gate numbers per unit."""

    error: int = 0
    warning: int = 0
    review: int = 0

    @property
    def total(self) -> int:
        return self.error + self.warning + self.review

    def as_dict(self) -> dict[str, int]:
        return {"error": self.error, "warning": self.warning, "review": self.review}


@dataclass
class UnitScore:
    """SPEC-18 §5 outputs for one unit. ``agreement_by_category`` is
    the per-category Jaccard; ``agreement_overall`` is the single
    Jaccard across all categories combined."""

    true_positives: int = 0
    false_positives: TierCounts = field(default_factory=TierCounts)
    false_negatives: TierCounts = field(default_factory=TierCounts)
    taint_explained_misses: int = 0
    tp_tainted: int = 0
    fp_tainted: int = 0
    agreement_overall: float = 0.0
    agreement_by_category: dict[str, float] = field(default_factory=dict)

    # Flat details for the report.
    tp_items: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    fp_items: list[tuple[Tier, str, str | None, str | None]] = field(default_factory=list)
    fn_items: list[tuple[Tier, str, str | None, str | None]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives.as_dict(),
            "false_negatives": self.false_negatives.as_dict(),
            "taint_explained_misses": self.taint_explained_misses,
            "tp_tainted": self.tp_tainted,
            "fp_tainted": self.fp_tainted,
            "agreement": {
                "overall": round(self.agreement_overall, 4),
                "by_category": {
                    k: round(v, 4) for k, v in sorted(self.agreement_by_category.items())
                },
            },
        }


def _oracle_fn_tier(diff: dict[str, Any], tainted_tables: set[str]) -> Tier:
    """SPEC-18 §5 edge case: a miss on a tainted table is NOT scored
    as warning/review-tier FN (R3 taint explains it), but IS still FN
    at error tier.

    The oracle doesn't know Gravitar's tiers; the harness uses the
    category to decide what tier a hypothetical Gravitar finding
    *would* have been. ``missing_column`` / ``missing_table`` are
    error-tier by default; everything else is warning-tier.
    """
    _ = tainted_tables  # tier assignment is category-based; taint affects accounting
    category = diff.get("category", "")
    if category in {"missing_table", "missing_column", "type_mismatch", "nullability_mismatch"}:
        return "error"
    return "warning"


def score_unit(
    oracle_diffs: list[dict[str, Any]],
    gravitar_findings: list[dict[str, Any]],
    *,
    tainted_tables: set[str] | None = None,
) -> UnitScore:
    """SPEC-18 §5 scoring for one unit. Inputs are post-scope-deduction
    sets (apply ``scope.apply_scope_deductions`` first).

    ``tainted_tables`` is from Gravitar's own coverage output; a diff
    whose table is in this set has its FN counted but demoted below
    error tier."""
    tainted = tainted_tables or set()

    oracle_by_key: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for diff in oracle_diffs:
        oracle_by_key[_key(diff)] = diff

    gravitar_by_key: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for finding in gravitar_findings:
        gravitar_by_key.setdefault(_key(finding), finding)

    oracle_keys = set(oracle_by_key)
    gravitar_keys = set(gravitar_by_key)

    score = UnitScore()

    # TPs and FPs.
    for key in gravitar_keys:
        finding = gravitar_by_key[key]
        table = key[1]
        is_tainted = bool(table and table in tainted)
        if key in oracle_keys:
            score.true_positives += 1
            score.tp_items.append(key)
            if is_tainted:
                score.tp_tainted += 1
        else:
            tier = _finding_tier(finding)
            if is_tainted:
                # SPEC-18 §5: fp_tainted is reported but excluded from
                # the error-tier FP count (R3 taint demotes the
                # finding's severity for scoring purposes).
                score.fp_tainted += 1
                # Still record under warning tier (never error) for
                # roll-up; the exclusion is only from error.
                if tier == "error":
                    score.false_positives.warning += 1
                else:
                    setattr(score.false_positives, tier, getattr(score.false_positives, tier) + 1)
                score.fp_items.append(("warning" if tier == "error" else tier, *key))
            else:
                setattr(score.false_positives, tier, getattr(score.false_positives, tier) + 1)
                score.fp_items.append((tier, *key))

    # FNs.
    for key in oracle_keys - gravitar_keys:
        diff = oracle_by_key[key]
        table = key[1]
        tier = _oracle_fn_tier(diff, tainted)
        if table and table in tainted and tier != "error":
            # SPEC-18 §5: not scored as warning/review FN; still
            # counted in taint_explained_misses for the report.
            score.taint_explained_misses += 1
            continue
        setattr(score.false_negatives, tier, getattr(score.false_negatives, tier) + 1)
        score.fn_items.append((tier, *key))

    # Agreement — per category and overall.
    by_cat_o: dict[str, set[tuple[str, str | None, str | None]]] = defaultdict(set)
    by_cat_g: dict[str, set[tuple[str, str | None, str | None]]] = defaultdict(set)
    for key in oracle_keys:
        by_cat_o[key[0]].add(key)
    for key in gravitar_keys:
        by_cat_g[key[0]].add(key)
    for category in sorted(set(by_cat_o) | set(by_cat_g)):
        inter = by_cat_o[category] & by_cat_g[category]
        union = by_cat_o[category] | by_cat_g[category]
        score.agreement_by_category[category] = len(inter) / len(union) if union else 1.0
    overall_union = oracle_keys | gravitar_keys
    score.agreement_overall = (
        len(oracle_keys & gravitar_keys) / len(overall_union) if overall_union else 1.0
    )

    return score


# --- Gate roll-up (SPEC-16 v1.1 §B tier gate; feeds gate.json) ---


@dataclass
class GateDecision:
    """SPEC-18 §8 gate_decision line, derived from per-unit scores."""

    passed: bool
    first_failure: str | None
    error_fp_total: int
    warning_fp_rate: float
    taint_explained_misses: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "first_failure": self.first_failure,
            "error_fp_total": self.error_fp_total,
            "warning_fp_rate": round(self.warning_fp_rate, 4),
            "taint_explained_misses": self.taint_explained_misses,
        }


def decide_gate(
    scores: list[UnitScore],
    *,
    pre_1_0: bool = True,
    warning_fp_rate_cap: float = 0.10,
) -> GateDecision:
    """SPEC-16 v1.1 §B gate: at pre-1.0, ≤1 error-FP total across the
    corpus; at GA, 0. Warning-FP rate ≤ 10%. First failure is reported
    for CI consumers reading ``gate.json``.

    Recall gates (seeded + hard) depend on T7/T8 output and are
    decided elsewhere."""
    error_fp_total = sum(s.false_positives.error for s in scores)
    error_fp_cap = 1 if pre_1_0 else 0
    warning_fps = sum(s.false_positives.warning for s in scores)
    # §B says "effective-FP rate" with a minimum pattern count of 10
    # occurrences; a per-unit score can't know the pattern count, so
    # the rate here is total-warning-FPs / total-oracle-set. Downstream
    # tooling can re-aggregate from fp_items if a stricter pattern
    # rule is needed later.
    oracle_total = sum(
        s.true_positives
        + s.false_negatives.error
        + s.false_negatives.warning
        + s.false_negatives.review
        for s in scores
    )
    warning_fp_rate = (warning_fps / oracle_total) if oracle_total else 0.0
    taint_explained = sum(s.taint_explained_misses for s in scores)

    first_failure: str | None = None
    if error_fp_total > error_fp_cap:
        first_failure = f"error_fp_total={error_fp_total} > cap={error_fp_cap}"
    elif warning_fp_rate > warning_fp_rate_cap:
        first_failure = f"warning_fp_rate={warning_fp_rate:.3f} > cap={warning_fp_rate_cap:.3f}"

    return GateDecision(
        passed=first_failure is None,
        first_failure=first_failure,
        error_fp_total=error_fp_total,
        warning_fp_rate=warning_fp_rate,
        taint_explained_misses=taint_explained,
    )
