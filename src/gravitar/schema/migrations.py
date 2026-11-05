"""DSL-agnostic MigrationSchema building.

Walks a revision graph (parsed by any `MigrationParser` — `alembic_parser.py`,
`django_parser.py`) to build the synthetic "final schema state", reports
broken_chain findings for gaps and cycles in the revision history, flags
disagreements between branching heads, and warns about raw SQL (op.execute)
it cannot analyze. Everything here operates on the DSL-agnostic `Operation`/
`Revision` shapes from `schema/parsers/` — this module has no knowledge of
which parser produced them, by design.

`parse_revision_file`/`parse_versions_dir`/`RevisionFile` are re-exported from
`alembic_parser` for backward compatibility with existing imports; new code
should prefer `schema.parsers.alembic_parser` directly.
"""

from __future__ import annotations

import itertools
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from gravitar.analysis import sql_patterns
from gravitar.analysis.confidence import score_finding
from gravitar.schema import (
    Column,
    Coverage,
    DriftFinding,
    Schema,
    SourceLocation,
    Table,
    Unresolved,
    make_unresolved,
    sort_findings,
    sort_unresolved,
)
from gravitar.schema.parsers import Operation, Revision
from gravitar.schema.parsers.alembic_parser import parse_revision_file, parse_versions_dir

__all__ = [
    "DownRevision",
    "Operation",
    "RevisionFile",
    "build_migration_schema",
    "parse_revision_file",
    "parse_versions_dir",
]

DownRevision = str | list[str] | None
RevisionFile = Revision  # backward-compatible alias; see module docstring


def _revision_source(revision: Revision, line: int | None = None) -> SourceLocation:
    return SourceLocation(file=revision.source_file, line=line, revision_id=revision.revision)


def _normalize_parents(down_revision: DownRevision) -> list[str]:
    if down_revision is None:
        return []
    if isinstance(down_revision, str):
        return [down_revision]
    return list(down_revision)


def _gap_finding(revision: Revision, missing_parent: str) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "revision graph fully resolved from source"
    )
    return DriftFinding(
        category="broken_chain",
        severity="error",
        table=None,
        column=None,
        model_value=None,
        migration_value=revision.revision,
        message=(
            f"revision {revision.revision!r} references down_revision "
            f"{missing_parent!r}, which does not exist"
        ),
        suggestion="check for a deleted, renamed, or unmerged migration file",
        confidence=confidence,
        source=_revision_source(revision),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _cycle_finding(revision: Revision) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "revision graph fully resolved from source"
    )
    return DriftFinding(
        category="broken_chain",
        severity="error",
        table=None,
        column=None,
        model_value=None,
        migration_value=revision.revision,
        message=f"revision {revision.revision!r} is part of a cycle in the migration history",
        suggestion="break the cycle by correcting its down_revision reference(s)",
        confidence=confidence,
        source=_revision_source(revision),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _raw_sql_finding(
    revision: Revision, matched: sql_patterns.SqlMatch | None, *, line: int | None = None
) -> DriftFinding:
    """Build a warning finding for raw SQL Gravitar can't apply (unknown
    pattern, or ``op.execute(variable)`` where ``line`` is still useful as
    the fingerprint tiebreaker). SPEC-16 amendment R12 passes the statement's
    source line through so multiple raw-SQL findings in one revision no
    longer collide on the same ``finding_id``.
    """
    if matched is None:
        confidence, evidence = score_finding(
            "raw_sql_unresolved",
            "op.execute() argument not statically classified into an operation",
        )
    else:
        confidence, evidence = score_finding(
            "raw_sql_unresolved",
            "op.execute() argument not statically classified into an operation",
            matched_signatures=[matched.pattern_id],
        )
    return DriftFinding(
        category="broken_chain",
        severity="warning",
        table=None,
        column=None,
        model_value=None,
        migration_value=revision.revision,
        message=(
            f"revision {revision.revision!r} contains raw SQL (op.execute) that "
            "Gravitar cannot analyze; any table may be affected"
        ),
        suggestion="review this migration by hand to confirm it doesn't introduce undetected drift",
        confidence=confidence,
        source=_revision_source(revision, line=line),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _orphaned_reference_finding(
    revision: Revision, matched: sql_patterns.SqlMatch, sql: str, *, line: int | None = None
) -> DriftFinding:
    detail = f"{matched.pattern_id} matched, but references a table/column not in scope"
    confidence, evidence = score_finding("orphaned_reference", detail)
    location = matched.table if matched.column is None else f"{matched.table}.{matched.column}"
    return DriftFinding(
        category="orphaned_reference",
        severity="warning",
        table=matched.table,
        column=matched.column,
        model_value=None,
        migration_value=sql,
        message=(
            f"revision {revision.revision!r} contains raw SQL recognized as "
            f"{matched.pattern_id!r}, but {location!r} is not present in the schema "
            "state Gravitar has built so far"
        ),
        suggestion=(
            "check for a typo, or a table/column created outside migrations Gravitar can see"
        ),
        confidence=confidence,
        source=_revision_source(revision, line=line),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _disagreement_finding(
    head_a: str, head_b: str, table_name: str, column_name: str, col_a: Column, col_b: Column
) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved",
        "both heads' schema state fully resolved by replaying their operations",
    )
    return DriftFinding(
        category="broken_chain",
        severity="error",
        table=table_name,
        column=column_name,
        model_value=None,
        migration_value={
            head_a: {"type": col_a["type"], "nullable": col_a["nullable"]},
            head_b: {"type": col_b["type"], "nullable": col_b["nullable"]},
        },
        message=f"heads {head_a!r} and {head_b!r} disagree on {table_name}.{column_name}",
        suggestion="merge or reconcile the branches so they agree on this column's final state",
        confidence=confidence,
        source=SourceLocation(
            file=col_a["source_file"], line=col_a["source_line"], revision_id=head_a
        ),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _find_cyclic_revisions(revision_ids: list[str], children: dict[str, list[str]]) -> set[str]:
    """DFS cycle detection over the parent->child dependency graph (an edge parent
    -> child means "child comes after parent"). A back-edge to a node still on the
    current path is a cycle; every node on that path is part of it.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(revision_ids, WHITE)
    cyclic: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        for child in sorted(children.get(node, [])):
            child_color = color.get(child, WHITE)
            if child_color == WHITE:
                visit(child, [*stack, child])
            elif child_color == GRAY:
                cycle_start = stack.index(child) if child in stack else 0
                cyclic.update(stack[cycle_start:])
        color[node] = BLACK

    for revision_id in sorted(revision_ids):
        if color[revision_id] == WHITE:
            visit(revision_id, [revision_id])
    return cyclic


def _bfs_reachable(bases: list[str], children: dict[str, list[str]]) -> set[str]:
    visited: set[str] = set()
    queue: deque[str] = deque(sorted(bases))
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for child in sorted(children.get(node, [])):
            if child not in visited:
                queue.append(child)
    return visited


def _topological_order(reachable: set[str], children: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm with deterministic tie-breaking (lowest revision id first),
    so applying operations produces byte-for-byte identical output across runs
    regardless of dict/set iteration order.
    """
    in_degree = dict.fromkeys(reachable, 0)
    for parent, kids in children.items():
        if parent not in reachable:
            continue
        for kid in kids:
            if kid in reachable:
                in_degree[kid] += 1

    ready = sorted(rid for rid, degree in in_degree.items() if degree == 0)
    order: list[str] = []
    while ready:
        ready.sort()
        node = ready.pop(0)
        order.append(node)
        for child in children.get(node, []):
            if child not in reachable:
                continue
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    return order


def _heads(reachable: set[str], children: dict[str, list[str]]) -> list[str]:
    """Revisions in `reachable` that nothing else in `reachable` names as a parent."""
    return [rid for rid in reachable if not any(c in reachable for c in children.get(rid, []))]


def _ancestors_of(head: str, by_id: dict[str, Revision], reachable: set[str]) -> set[str]:
    """Every reachable revision on the path from a base to `head` (inclusive)."""
    visited = {head}
    queue: deque[str] = deque([head])
    while queue:
        node = queue.popleft()
        for parent in _normalize_parents(by_id[node].down_revision):
            if parent in reachable and parent not in visited:
                visited.add(parent)
                queue.append(parent)
    return visited


def _disagreement_findings(heads: list[str], head_schemas: dict[str, Schema]) -> list[DriftFinding]:
    """Section 5: branching is legal, but if two heads disagree on a shared
    table/column's state, that's a broken_chain finding, not silently unioned.
    """
    findings: list[DriftFinding] = []
    for head_a, head_b in itertools.combinations(sorted(heads), 2):
        schema_a, schema_b = head_schemas[head_a], head_schemas[head_b]
        for table_name in sorted(set(schema_a) & set(schema_b)):
            cols_a = schema_a[table_name]["columns"]
            cols_b = schema_b[table_name]["columns"]
            for column_name in sorted(set(cols_a) & set(cols_b)):
                col_a, col_b = cols_a[column_name], cols_b[column_name]
                if col_a["type"] != col_b["type"] or col_a["nullable"] != col_b["nullable"]:
                    findings.append(
                        _disagreement_finding(head_a, head_b, table_name, column_name, col_a, col_b)
                    )
    return findings


def _op_create_table(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is None:
        return
    columns: dict[str, Column] = {
        spec.name: Column(
            name=spec.name,
            type=spec.type,
            nullable=spec.nullable,
            primary_key=spec.primary_key,
            source_file=source_file,
            source_line=op.line,
        )
        for spec in op.columns
    }
    schema[op.table] = Table(
        name=op.table, columns=columns, source_file=source_file, source_line=op.line
    )


def _op_add_column(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is None or not op.columns:
        return
    spec = op.columns[0]
    table = schema.setdefault(
        op.table,
        Table(name=op.table, columns={}, source_file=source_file, source_line=op.line),
    )
    table["columns"][spec.name] = Column(
        name=spec.name,
        type=spec.type,
        nullable=spec.nullable,
        primary_key=spec.primary_key,
        source_file=source_file,
        source_line=op.line,
    )


def _op_alter_column(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is None or op.column_name is None:
        return
    table = schema.get(op.table)
    if table is None or op.column_name not in table["columns"]:
        return

    column = table["columns"][op.column_name]
    if op.type is not None:
        column["type"] = op.type
    if op.nullable is not None:
        column["nullable"] = op.nullable
    if op.new_column_name is not None and op.new_column_name != op.column_name:
        column["name"] = op.new_column_name
        del table["columns"][op.column_name]
        table["columns"][op.new_column_name] = column


def _op_drop_column(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is None or op.column_name is None:
        return
    table = schema.get(op.table)
    if table is not None:
        table["columns"].pop(op.column_name, None)


def _op_drop_table(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is not None:
        schema.pop(op.table, None)


def _op_rename_table(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is None or op.new_table is None:
        return
    table = schema.pop(op.table, None)
    if table is not None:
        table["name"] = op.new_table
        schema[op.new_table] = table


def _op_rename_column(schema: Schema, op: Operation, source_file: str) -> None:
    if op.table is None or op.column_name is None or op.new_column_name is None:
        return
    table = schema.get(op.table)
    if table is None:
        return
    column = table["columns"].pop(op.column_name, None)
    if column is not None:
        column["name"] = op.new_column_name
        table["columns"][op.new_column_name] = column


_OpHandler = Callable[[Schema, Operation, str], None]

_OP_HANDLERS: dict[str, _OpHandler] = {
    "create_table": _op_create_table,
    "add_column": _op_add_column,
    "alter_column": _op_alter_column,
    "drop_column": _op_drop_column,
    "drop_table": _op_drop_table,
    "rename_table": _op_rename_table,
    "rename_column": _op_rename_column,
}


def _apply_operations(order: list[str], by_id: dict[str, Revision]) -> Schema:
    schema: Schema = {}
    for revision_id in order:
        for op in by_id[revision_id].operations:
            handler = _OP_HANDLERS.get(op.op)
            if handler is not None:
                handler(schema, op, by_id[revision_id].source_file)
    return schema


# SPEC-16 amendment R12: statements that cannot modify schema state. If
# op.execute() runs one of these, there's nothing for Gravitar to apply, nothing
# for anyone to review — classify as inert, emit no finding and no note.
_INERT_SQL_PREFIXES = (
    "UPDATE",
    "INSERT",
    "DELETE",
    "SELECT",
    "WITH",
    "PRAGMA",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "SET",  # SET TRANSACTION, SET LOCAL, etc. — session-scoped, non-schema
    "VACUUM",
    "ANALYZE",
    "COPY",
    "REFRESH",  # REFRESH MATERIALIZED VIEW (view, not table)
)


def _is_inert_sql(sql: str) -> bool:
    stripped = sql.lstrip().lstrip("(").lstrip()
    first_token = stripped.split(None, 1)[0].upper() if stripped else ""
    return first_token in _INERT_SQL_PREFIXES


def _apply_operations_with_raw_sql(
    order: list[str], by_id: dict[str, Revision]
) -> tuple[Schema, list[DriftFinding], list[Unresolved]]:
    """SPEC-16 amendment R12: interleaves raw-SQL classification with ordinary
    op replay in revision order. A recognizable DDL raw SQL op mutates the
    schema state alongside native ops; an inert DML statement emits nothing;
    a dynamic ``op.execute(variable)`` becomes a note; an unrecognized DDL
    stays a warning finding (previously the only path).
    """
    schema: Schema = {}
    findings: list[DriftFinding] = []
    notes: list[Unresolved] = []
    for revision_id in order:
        revision = by_id[revision_id]
        for op in revision.operations:
            handler = _OP_HANDLERS.get(op.op)
            if handler is not None:
                handler(schema, op, revision.source_file)
                continue
            if op.op == "execute":
                _handle_raw_sql(schema, revision, op, findings, notes)
    return schema, findings, notes


def _handle_raw_sql(
    schema: Schema,
    revision: Revision,
    op: Operation,
    findings: list[DriftFinding],
    notes: list[Unresolved],
) -> None:
    """R12 raw-SQL classification. Four branches:
    1. ``op.execute(variable)`` — dynamic, emits a note scoped globally.
    2. Inert DML/PRAGMA/control statement — no finding, no note.
    3. Recognizable DDL whose pattern Gravitar can replay — apply to
       schema state, no finding (the drift it would have signalled is
       now resolved by the application).
    4. Anything else — the existing warning finding.
    """
    if op.raw_sql is None:
        # Dynamic SQL — can't see into it; it stays visible in the output
        # as a note, lowering overall confidence of findings in this revision.
        notes.append(
            make_unresolved(
                "dynamic_sql",
                "op.execute() received a non-literal argument",
                SourceLocation(
                    file=revision.source_file,
                    line=op.line,
                    revision_id=revision.revision,
                ),
            )
        )
        return
    if _is_inert_sql(op.raw_sql):
        return
    matched = sql_patterns.match(op.raw_sql)
    if matched is not None and _apply_recovered_ddl(schema, revision, op, matched):
        return
    # No pattern matched, or the match couldn't be applied (schema reference
    # went orphan). Fall back to the existing _raw_sql_finding_for logic.
    findings.append(_raw_sql_finding_for(revision, op, schema))


def _apply_recovered_ddl(
    schema: Schema, revision: Revision, op: Operation, matched: sql_patterns.SqlMatch
) -> bool:
    """Try to apply a sql_pattern_match's DDL to the schema state. Returns
    True iff the apply landed; False iff the match references a
    table/column that isn't present (orphaned_reference — the caller falls
    back to emitting that finding).

    Only the three column-shaped patterns actually mutate schema state —
    create_index / create_unique_index are correctly recognized but
    Gravitar's Schema has no index concept (same as native op.create_index
    today), so they stay as advisory findings via the caller's fallback.
    """
    table_name = matched.table
    column_name = matched.column
    if matched.pattern_id == "sql:add_column":
        # ADD COLUMN on a known table is clean; on an unknown table it's
        # orphaned_reference. We don't know the type here (R12 doesn't
        # parse column type out of raw SQL) — mark unknown so comparator
        # emits an unknown_type if the model declares one.
        if table_name not in schema:
            return False
        if column_name is None:
            return False
        schema[table_name]["columns"][column_name] = Column(
            name=column_name,
            type="unknown:raw_sql",
            nullable=True,  # safest assumption; raw SQL rarely has NOT NULL
            primary_key=False,
            source_file=revision.source_file,
            source_line=op.line,
        )
        return True
    if matched.pattern_id == "sql:drop_column":
        if table_name not in schema or column_name not in schema[table_name]["columns"]:
            return False
        del schema[table_name]["columns"][column_name]
        return True
    if matched.pattern_id == "sql:alter_column_type":
        if table_name not in schema or column_name not in schema[table_name]["columns"]:
            return False
        schema[table_name]["columns"][column_name]["type"] = "unknown:raw_sql"
        return True
    # create_index / create_unique_index — the caller emits the warning finding
    # (they're correctly recognized but Gravitar's Schema has no indexes).
    return False


@dataclass
class MigrationResult:
    """SPEC-16 v1.0 §4. Everything the comparator and output stages need from
    one migration tree, in one struct. ``taint`` is the fast lookup for R3's
    severity policy (``taint[table]`` is "all notes scoped to this table");
    ``global_notes`` is the ``tables=None`` subset that lowers overall
    confidence but doesn't demote any single finding (amendment Decision #2).
    """

    schema: Schema
    findings: list[DriftFinding]
    notes: list[Unresolved]
    taint: dict[str, list[Unresolved]]
    global_notes: list[Unresolved]
    coverage: Coverage


def build_migration_result(revisions: list[Revision]) -> MigrationResult:
    """Full-fidelity version of :func:`build_migration_schema`: returns the
    schema and findings as before, plus the aggregated unresolved notes,
    per-table taint, and coverage counts (SPEC-16 v1.0 §4 / R7).
    """
    by_id: dict[str, Revision] = {r.revision: r for r in revisions if r.revision is not None}

    findings: list[DriftFinding] = []
    notes: list[Unresolved] = []
    children: dict[str, list[str]] = defaultdict(list)

    # Collect notes from EVERY revision, including the revision=None ones
    # that R11's fault isolation produces for unparseable files. Dropping
    # those here would reintroduce exactly the silent failure R11 fixes.
    for rev in revisions:
        notes.extend(rev.notes)
        for op in rev.operations:
            notes.extend(op.unresolved)
            for spec in op.columns:
                notes.extend(spec.unresolved)

    for rev in by_id.values():
        assert rev.revision is not None
        for parent in _normalize_parents(rev.down_revision):
            if parent not in by_id:
                findings.append(_gap_finding(rev, parent))
            else:
                children[parent].append(rev.revision)

    cyclic = _find_cyclic_revisions(list(by_id), children)
    for revision_id in sorted(cyclic):
        findings.append(_cycle_finding(by_id[revision_id]))

    bases = [rid for rid, rev in by_id.items() if not _normalize_parents(rev.down_revision)]
    reachable = _bfs_reachable(bases, children) - cyclic

    heads = _heads(reachable, children)
    if len(heads) > 1:
        head_schemas = {
            head: _apply_operations(
                _topological_order(_ancestors_of(head, by_id, reachable), children), by_id
            )
            for head in heads
        }
        findings.extend(_disagreement_findings(heads, head_schemas))

    # SPEC-16 amendment R12: interleave non-execute ops and recognizable raw
    # SQL into one pass so recovered DDL actually updates schema state
    # (previously raw SQL only produced findings, never applied).
    schema, raw_sql_findings, raw_sql_notes = _apply_operations_with_raw_sql(
        _topological_order(reachable, children), by_id
    )
    findings.extend(raw_sql_findings)
    notes.extend(raw_sql_notes)

    taint, global_notes = _build_taint(notes)
    coverage = _compute_coverage(revisions, by_id, notes)
    return MigrationResult(
        schema=schema,
        findings=sort_findings(findings),
        notes=sort_unresolved(notes),
        taint=taint,
        global_notes=global_notes,
        coverage=coverage,
    )


def _build_taint(
    notes: list[Unresolved],
) -> tuple[dict[str, list[Unresolved]], list[Unresolved]]:
    """SPEC-16 v1.0 §4. A note with tables=None is "global"; a note with
    tables set adds this note to taint[t] for each t it names."""
    taint: dict[str, list[Unresolved]] = defaultdict(list)
    global_notes: list[Unresolved] = []
    for note in notes:
        if note["tables"] is None:
            global_notes.append(note)
            continue
        for table in note["tables"]:
            taint[table].append(note)
    return dict(taint), global_notes


def _compute_coverage(
    revisions: list[Revision], by_id: dict[str, Revision], notes: list[Unresolved]
) -> Coverage:
    """A revision counts as 'skipped' if the file produced a parse_error note
    (R11 couldn't even read it as Python; its ``revision`` is None and
    ``source_file`` is all we have), 'partial' if any other note is scoped
    to it, and 'fully_resolved' otherwise. Amendment T4.
    """
    skipped_files = {note["source"]["file"] for note in notes if note["reason"] == "parse_error"}
    partial_revisions = {
        note["source"]["revision_id"]
        for note in notes
        if note["reason"] != "parse_error" and note["source"]["revision_id"]
    }
    skipped_count = sum(1 for rev in revisions if rev.source_file in skipped_files)
    total = len(revisions)
    partial_count = len(partial_revisions)
    fully_resolved = total - partial_count - skipped_count
    return Coverage(
        revisions_total=total,
        revisions_fully_resolved=max(0, fully_resolved),
        revisions_partial=partial_count,
        revisions_skipped=skipped_count,
    )


def build_migration_schema(revisions: list[Revision]) -> tuple[Schema, list[DriftFinding]]:
    """Backward-compatible thin wrapper around :func:`build_migration_result`.
    Existing callers (direct test imports) keep working; new code should
    prefer the richer result. SPEC-16 v1.0 §4 explicitly keeps this signature
    unchanged for the migration period.
    """
    result = build_migration_result(revisions)
    return result.schema, result.findings


def _raw_sql_finding_for(revision: Revision, op: Operation, schema: Schema) -> DriftFinding:
    """Classify a single `op.execute(...)` call (Section 5) against the final schema
    state and build the finding it should contribute. A matched pattern whose
    table/column isn't present in `schema` is a distinct, lower-confidence finding
    (Section 6's "orphaned reference" failure mode) rather than a silently accepted
    match.
    """
    if op.raw_sql is None:
        return _raw_sql_finding(revision, matched=None, line=op.line)

    matched = sql_patterns.match(op.raw_sql)
    if matched is None:
        return _raw_sql_finding(revision, matched=None, line=op.line)

    table = schema.get(matched.table)
    # add_column's whole point is a column that doesn't exist yet, so only patterns
    # that act on an *existing* column check for one.
    column_must_preexist = matched.pattern_id in ("sql:drop_column", "sql:alter_column_type")
    column_missing = (
        column_must_preexist and table is not None and matched.column not in table["columns"]
    )
    if table is None or column_missing:
        return _orphaned_reference_finding(revision, matched, op.raw_sql, line=op.line)

    return _raw_sql_finding(revision, matched=matched, line=op.line)
