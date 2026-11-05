"""Classify a Gravitar run against an oracle. SPEC-16 R0.

Takes:
- Gravitar's JSON envelope (from ``gravitar check --format json``)
- The oracle findings list
- A triage map (``cause`` tags per `(project, outcome, category, table, column)` key)

Produces a list of ``ClassifiedFinding`` — TP/FP/FN with mandatory
cause attribution. ``classify()`` returns the list; the caller
(``run.py``) is responsible for refusing to continue when any FP/FN
has ``cause is None`` (SPEC §Attribution: "run.py exits non-zero if
any item is untagged").
"""

from __future__ import annotations

from typing import Any

from benchmarks.schema import (
    Category,
    ClassifiedFinding,
    OracleFinding,
    Project,
)

_SUPPORTED_CATEGORIES: frozenset[str] = frozenset(
    {
        "missing_table",
        "missing_column",
        "type_mismatch",
        "nullability_mismatch",
        "orphaned_column",
        "orphaned_reference",
        "broken_chain",
        "unknown_type",
    }
)


def _finding_key(
    finding: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """SPEC §Matching: identity is ``(category, table, column)``."""
    return (finding["category"], finding.get("table"), finding.get("column"))


def _triage_key(
    project_name: str,
    outcome: str,
    category: str,
    table: str | None,
    column: str | None,
) -> str:
    """Stable string key for triage.yml entries so a human can grep for
    exactly which case they're tagging."""
    return f"{project_name}|{outcome}|{category}|{table or ''}|{column or ''}"


def _auto_attribute(
    outcome: str,
    finding: dict[str, Any] | None,
    tainted_tables: set[str],
) -> str | None:
    """Rules the harness can apply without human triage. Everything
    else must be in triage.yml or exits non-zero."""
    if outcome == "tp":
        return None  # no cause for a true positive
    if finding is not None:
        # Finding on a tainted table — the walker couldn't fully resolve
        # the migration side, so a mismatch here is explained by R3's
        # scoped unresolved note.
        table = finding.get("table")
        if table and table in tainted_tables:
            return "unresolved_taint"
    return None


def classify(
    project: Project,
    envelope: dict[str, Any],
    oracle_findings: list[OracleFinding],
    triage: dict[str, dict[str, str]],
) -> list[ClassifiedFinding]:
    """Compare Gravitar's findings against the oracle, apply auto-
    attribution, and merge in human triage.

    ``triage`` is the parsed triage.yml, keyed by ``_triage_key``: each
    entry is ``{"cause": "<tag>", "note": "<free text>"}``.
    """
    gravitar_findings = envelope.get("findings", [])
    coverage = envelope.get("coverage", {})
    # ``tainted_tables`` is a Gravitar-side projection: the comparator
    # exposes it on the envelope, but if a future format change drops
    # the field, the auto-attribution just doesn't fire. The classify
    # pass then needs more triage.yml entries, not a crash.
    tainted_tables: set[str] = set(coverage.get("tainted_tables") or [])

    # Filter oracles per oracle_scope.types (SPEC §Type-comparison scope).
    oracles = [
        f for f in oracle_findings if project.oracle_scope_types or f.category != "type_mismatch"
    ]
    oracle_keys = {f.key() for f in oracles}

    gravitar_findings_by_key: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for f in gravitar_findings:
        key = _finding_key(f)
        # Keep the first occurrence; duplicates in one run are unlikely.
        gravitar_findings_by_key.setdefault(key, f)

    classified: list[ClassifiedFinding] = []

    # TP + FP (iterating Gravitar's output).
    for key, finding in gravitar_findings_by_key.items():
        category: Category = finding["category"]
        if category not in _SUPPORTED_CATEGORIES:
            # Should never happen (Gravitar's categories are closed), but
            # guard against a future addition that the harness hasn't
            # been taught about yet.
            continue
        if project.oracle_scope_types and category == "type_mismatch" and key in oracle_keys:
            outcome = "tp"
        elif not project.oracle_scope_types and category == "type_mismatch":
            # Can't trust the oracle on this category; count against neither
            # TP nor FP. Still record a classification so the report shows
            # that the oracle was out of scope here.
            classified.append(
                ClassifiedFinding(
                    outcome="tp",  # treated as a non-counting pass
                    category=category,
                    table=finding.get("table"),
                    column=finding.get("column"),
                    cause=None,
                    note="type_mismatch skipped (oracle_scope.types=false)",
                    finding_id=finding.get("finding_id"),
                )
            )
            continue
        elif key in oracle_keys:
            outcome = "tp"
        else:
            severity = finding.get("severity", "error")
            outcome = "fp_error" if severity == "error" else "fp_warning"

        cause: str | None = None
        note: str | None = None
        if outcome != "tp":
            cause = _auto_attribute(outcome, finding, tainted_tables)
            if cause is None:
                triage_entry = triage.get(
                    _triage_key(
                        project.name,
                        outcome,
                        category,
                        finding.get("table"),
                        finding.get("column"),
                    )
                )
                if triage_entry is not None:
                    cause = triage_entry.get("cause")
                    note = triage_entry.get("note")

        classified.append(
            ClassifiedFinding(
                outcome=outcome,  # type: ignore[arg-type]
                category=category,
                table=finding.get("table"),
                column=finding.get("column"),
                cause=cause,  # type: ignore[arg-type]
                note=note,
                finding_id=finding.get("finding_id"),
            )
        )

    # FN (iterating oracles not matched by any Gravitar finding).
    gravitar_keys = set(gravitar_findings_by_key.keys())
    for oracle in oracles:
        key = oracle.key()
        if key in gravitar_keys:
            continue
        triage_entry = triage.get(
            _triage_key(project.name, "fn", oracle.category, oracle.table, oracle.column)
        )
        cause = None
        note = oracle.note
        if triage_entry is not None:
            cause = triage_entry.get("cause")
            note = triage_entry.get("note") or oracle.note
        classified.append(
            ClassifiedFinding(
                outcome="fn",
                category=oracle.category,
                table=oracle.table,
                column=oracle.column,
                cause=cause,  # type: ignore[arg-type]
                note=note,
                finding_id=None,
            )
        )

    return classified


def untagged(classified: list[ClassifiedFinding]) -> list[ClassifiedFinding]:
    """Return the FP/FN items that still have no ``cause``. SPEC §Attribution
    requires every FP/FN to be tagged; ``run.py`` exits non-zero if this
    list is non-empty."""
    return [c for c in classified if c.outcome != "tp" and c.cause is None]
