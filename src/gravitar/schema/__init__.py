"""Shared Schema/Table/Column/DriftFinding shapes (transparency contract,
demote-when-explained).

ModelSchema (built in models.py) and MigrationSchema (built in migrations.py) both
conform to the Schema shape so compare.py can treat them uniformly and produce
DriftFinding objects.

Unresolved notes: an `Unresolved` is Gravitar's record of something it could
not fully interpret — a parse error, an unresolved op, a dynamic SQL string.
Notes live alongside findings and back the "no silent skips" and coverage-line
guarantees of the transparency contract. A finding whose table/column is in
scope for a note gets demoted one tier, with that note's id in
`explaining_notes`.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal, TypedDict


class Column(TypedDict):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    source_file: str
    source_line: int | None


class Table(TypedDict):
    name: str
    columns: dict[str, Column]
    source_file: str
    source_line: int | None


Schema = dict[str, Table]

DriftCategory = Literal[
    "missing_table",
    "missing_column",
    "type_mismatch",
    "nullability_mismatch",
    "orphaned_column",
    "orphaned_reference",
    "broken_chain",
    "unknown_type",
]

# Findings are sorted by this category order, then table name, then column name.
_CATEGORY_ORDER: list[DriftCategory] = [
    "missing_table",
    "missing_column",
    "type_mismatch",
    "nullability_mismatch",
    "orphaned_column",
    "orphaned_reference",
    "broken_chain",
    "unknown_type",
]


class SourceLocation(TypedDict):
    file: str
    line: int | None
    revision_id: str | None


class Evidence(TypedDict):
    signal: str
    weight: float
    detail: str


UnresolvedReason = Literal[
    # v1.0 Section 4 reasons:
    "non_literal_table",
    "non_literal_column",
    "non_literal_attribute",
    "type_unresolved",
    "splat_args",
    "loop_not_literal",
    "conditional_branch",
    "external_call",
    "unrecognized_op",
    # Added by amendment R11 and R9 (not in the v1.0 UNRESOLVED_REASONS tuple;
    # they're structurally the same shape of thing — unreadable input Gravitar
    # has to report rather than silently drop — and belong in the same list):
    "parse_error",
    "unreadable_revision",
    # Added by amendment R12 (dynamic SQL is the "op.execute(variable)" case
    # that previously generated noise findings — now a note, never a finding):
    "dynamic_sql",
    # Added by SPEC-12 Amendment v1.1 R16: a Django Operation class Gravitar
    # doesn't handle (RunSQL, RunPython, SeparateDatabaseAndState, ...).
    # Parallel to Alembic's unrecognized_op; Django-specific name keeps the
    # output honest about which DSL couldn't be classified.
    "django_op_unsupported",
    # Added by SPEC-17 S1: a cycle in the model-reader's import graph.
    # Detected statically (no execution); the note names the modules
    # involved so the reviewer can see which pair hangs each other.
    "import_cycle",
]
"""SPEC-16 v1.0 Section 4 ``UNRESOLVED_REASONS``, extended by amendment R9/R11/R12 and SPEC-17."""


class Unresolved(TypedDict):
    """Gravitar's record of something it could not fully interpret. SPEC-16
    v1.0 Section 4 ("unresolved note"), amendment adds ``columns`` scope.

    The transparency contract (T1) rests on this: every op, file, tree or
    model Gravitar could not fully interpret produces exactly one Unresolved
    with reason, file, line and scope — never a silent drop.

    ``tables`` scopes which tables this note applies to (``None`` = could be
    any table, i.e. global); ``columns`` scopes further to specific
    ``"table.column"`` qualifiers (amendment R3). A scoped note demotes
    findings whose table/column it covers; an unscoped (global) note lowers
    confidence only, never demotes.

    ``id`` isn't in SPEC-16 v1.0's dataclass, but every output format shipped
    since M9 identifies findings by stable id, and SARIF (R14) will need
    stable note ids too — deriving one deterministically from the note's
    content here, rather than later, keeps baselines sane.
    """

    id: str
    reason: UnresolvedReason
    detail: str  # source snippet or short free-text, ≤80 chars per v1.0
    source: SourceLocation
    tables: list[str] | None  # None = global/unscoped
    columns: list[str] | None  # qualified "table.column"; None = scope is table-level or global


FindingKind = Literal["fail", "review"]
"""SARIF 2.1.0 ``kind``. "fail" is the default (the finding stands as its
severity says); "review" is set when a scoped note explained it and it was
demoted a tier (T3)."""


class DriftFinding(TypedDict):
    category: DriftCategory
    severity: Literal["error", "warning"]
    table: str | None
    column: str | None
    model_value: Any | None
    migration_value: Any | None
    message: str
    suggestion: str | None
    confidence: float
    source: SourceLocation
    evidence: list[Evidence]
    live_verified: bool
    kind: FindingKind
    demoted_from: Literal["error", "warning"] | None
    explaining_notes: list[str]


class Coverage(TypedDict):
    """How much of the migration tree Gravitar fully resolved (T4). A revision
    is "fully resolved" iff no note is scoped to it; "partial" iff it has
    notes but was still processed; "skipped" iff it couldn't be parsed at
    all (a parse_error note).
    """

    revisions_total: int
    revisions_fully_resolved: int
    revisions_partial: int
    revisions_skipped: int


def sort_findings(findings: list[DriftFinding]) -> list[DriftFinding]:
    """Sort findings deterministically: category order, then table, then column."""

    def _key(finding: DriftFinding) -> tuple[int, str, str]:
        return (
            _CATEGORY_ORDER.index(finding["category"]),
            finding["table"] or "",
            finding["column"] or "",
        )

    return sorted(findings, key=_key)


def sort_unresolved(notes: list[Unresolved]) -> list[Unresolved]:
    """Deterministic order: (reason, source_file, line, id). Stable across runs
    for golden-file testing and SARIF partialFingerprints stability."""

    def _key(note: Unresolved) -> tuple[str, str, int, str]:
        return (
            note["reason"],
            note["source"]["file"],
            note["source"]["line"] or 0,
            note["id"],
        )

    return sorted(notes, key=_key)


def unresolved_id(
    reason: str,
    source_file: str,
    line: int | None,
    detail: str,
    revision_id: str | None,
) -> str:
    """A short, deterministic id. Stable within a run (so ``explaining_notes``
    references survive sort/filter) and across runs over an unchanged repo
    (so baselines in R14 stay anchored)."""
    key = f"{reason}|{source_file}|{line or ''}|{detail}|{revision_id or ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def make_unresolved(
    reason: UnresolvedReason,
    detail: str,
    source: SourceLocation,
    *,
    tables: list[str] | None = None,
    columns: list[str] | None = None,
) -> Unresolved:
    """Construct an Unresolved with its id derived from the content."""
    return Unresolved(
        id=unresolved_id(reason, source["file"], source["line"], detail, source["revision_id"]),
        reason=reason,
        detail=detail,
        source=source,
        tables=tables,
        columns=columns,
    )
