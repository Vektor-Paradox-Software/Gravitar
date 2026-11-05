"""Shared types for the benchmark harness. SPEC-16 R0.

Everything the harness passes between stages flows through these
dataclasses: manifest entries, oracle findings, classified outcomes,
cause tags. The report generator reads these; nothing in ``src/``
touches this module (R0 is dev-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Category = Literal[
    "missing_table",
    "missing_column",
    "type_mismatch",
    "nullability_mismatch",
    "orphaned_column",
    "orphaned_reference",
    "broken_chain",
    "unknown_type",
]

Cause = Literal[
    "batch_op",
    "class_form_type",
    "type_alias",
    "type_variant",
    "custom_type",
    "loop_or_helper",
    "schema_qualified",
    "pk_constraint_nullability",
    "fk_typed_column",
    "import_failure",
    "unresolved_taint",
    "oracle_artifact",
    "other",
]

Outcome = Literal["tp", "fp_error", "fp_warning", "fn"]
RunStatus = Literal["ran", "analysis_error"]


@dataclass(frozen=True)
class OracleFinding:
    """One drift the oracle expects Gravitar to report. ``source`` is
    optional context for human triage; identity is `(category, table,
    column)` only, matching SPEC §Matching."""

    category: Category
    table: str | None
    column: str | None
    note: str | None = None  # free-text, written by whoever authored the oracle

    def key(self) -> tuple[str, str | None, str | None]:
        return (self.category, self.table, self.column)


@dataclass(frozen=True)
class ClassifiedFinding:
    """A single classified outcome for the report. ``cause`` is required
    for every FP and FN — ``run.py`` exits non-zero on untagged items."""

    outcome: Outcome
    category: Category
    table: str | None
    column: str | None
    cause: Cause | None  # None only when outcome=="tp"
    note: str | None = None  # free-text for `other` cause or human triage
    finding_id: str | None = None  # Gravitar finding id (None for FN)


@dataclass
class Project:
    """One manifest entry.

    - ``oracle_runner="static"``: load an OracleFinding list from a YAML
      file at ``oracle_path``.
    - ``oracle_runner="postgres"``: run the real-DB comparator (v2;
      scaffold only in v1).

    ``oracle_scope.types=False`` excludes ``type_mismatch`` from
    scoring, per the Type-comparison scope rule for SQLite-reflected
    projects.
    """

    name: str
    project_path: Path
    oracle_path: Path | None = None
    oracle_runner: Literal["static", "postgres"] = "static"
    gravitar_config: Path | None = None  # overrides .gravitar.yml inside project_path
    oracle_scope_types: bool = True
    notes: str = ""


@dataclass
class ProjectResult:
    """Outcome of running the harness against one project."""

    project: Project
    status: RunStatus
    error: str | None = None  # populated iff status == "analysis_error"
    classified: list[ClassifiedFinding] = field(default_factory=list)
    tainted_tables: set[str] = field(default_factory=set)
    raw_envelope: dict[str, Any] = field(default_factory=dict)

    @property
    def tp(self) -> int:
        return sum(1 for c in self.classified if c.outcome == "tp")

    @property
    def fp_error(self) -> int:
        return sum(1 for c in self.classified if c.outcome == "fp_error")

    @property
    def fp_warning(self) -> int:
        return sum(1 for c in self.classified if c.outcome == "fp_warning")

    @property
    def fn(self) -> int:
        return sum(1 for c in self.classified if c.outcome == "fn")

    def cause_histogram(self) -> dict[str, int]:
        histogram: dict[str, int] = {}
        for item in self.classified:
            if item.cause is None:
                continue
            histogram[item.cause] = histogram.get(item.cause, 0) + 1
        return histogram
