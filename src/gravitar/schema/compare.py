"""Schema comparison algorithm.

Broken-chain findings are produced separately by migrations.py's graph analysis
(they describe problems in the revision history itself, not a model/migration
disagreement) and are merged in by the caller before sorting the combined list.
"""

from __future__ import annotations

import fnmatch

from gravitar.analysis.confidence import score_finding
from gravitar.schema import Column, DriftFinding, Schema, SourceLocation, Table, sort_findings


def _source(file: str, line: int | None) -> SourceLocation:
    return SourceLocation(file=file, line=line, revision_id=None)


def compare_schemas(model_schema: Schema, migration_schema: Schema) -> list[DriftFinding]:
    """Compare ModelSchema and MigrationSchema, returning missing/orphaned/mismatch
    findings (steps 1-4 of Section 3.3). Pre-sorted; merge in broken_chain findings
    and call `sort_findings` again if combining with those.
    """
    findings: list[DriftFinding] = []

    model_tables = set(model_schema)
    migration_tables = set(migration_schema)

    for table_name in model_tables - migration_tables:
        findings.append(_missing_table(model_schema[table_name]))

    for table_name in migration_tables - model_tables:
        findings.append(_orphaned_table(migration_schema[table_name]))

    for table_name in model_tables & migration_tables:
        findings.extend(
            _compare_columns(table_name, model_schema[table_name], migration_schema[table_name])
        )

    return sort_findings(findings)


def _missing_table(table: Table) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "model declaration fully resolved from source"
    )
    return DriftFinding(
        category="missing_table",
        severity="error",
        table=table["name"],
        column=None,
        model_value=None,
        migration_value=None,
        message="model declares table, no migration creates it",
        suggestion=f'add a migration: alembic revision --autogenerate -m "create {table["name"]}"',
        confidence=confidence,
        source=_source(table["source_file"], table["source_line"]),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _orphaned_table(table: Table) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "migration operation fully resolved from source"
    )
    return DriftFinding(
        category="orphaned_column",
        severity="error",
        table=table["name"],
        column=None,
        model_value=None,
        migration_value=None,
        message="migration creates table, no model declares it",
        suggestion="add the corresponding model, or drop the table in a migration",
        confidence=confidence,
        source=_source(table["source_file"], table["source_line"]),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _compare_columns(
    table_name: str, model_table: Table, migration_table: Table
) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    model_cols = set(model_table["columns"])
    migration_cols = set(migration_table["columns"])

    for column_name in model_cols - migration_cols:
        findings.append(_missing_column(table_name, model_table["columns"][column_name]))

    for column_name in migration_cols - model_cols:
        findings.append(_orphaned_column(table_name, migration_table["columns"][column_name]))

    for column_name in model_cols & migration_cols:
        model_col = model_table["columns"][column_name]
        migration_col = migration_table["columns"][column_name]
        findings.extend(_compare_column_pair(table_name, model_col, migration_col))

    return findings


def _missing_column(table_name: str, column: Column) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "model declaration fully resolved from source"
    )
    return DriftFinding(
        category="missing_column",
        severity="error",
        table=table_name,
        column=column["name"],
        model_value={"type": column["type"], "nullable": column["nullable"]},
        migration_value=None,
        message="model declares column, no migration adds it",
        suggestion=(
            f"add a migration: alembic revision --autogenerate -m "
            f'"add {table_name}.{column["name"]}"'
        ),
        confidence=confidence,
        source=_source(column["source_file"], column["source_line"]),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _orphaned_column(table_name: str, column: Column) -> DriftFinding:
    confidence, evidence = score_finding(
        "ast_fully_resolved", "migration operation fully resolved from source"
    )
    return DriftFinding(
        category="orphaned_column",
        severity="error",
        table=table_name,
        column=column["name"],
        model_value=None,
        migration_value={"type": column["type"], "nullable": column["nullable"]},
        message="migration adds column, no model declares it",
        suggestion="add the column to the model, or drop it in a migration",
        confidence=confidence,
        source=_source(column["source_file"], column["source_line"]),
        evidence=evidence,
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )


def _compare_column_pair(
    table_name: str, model_col: Column, migration_col: Column
) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    column_name = model_col["name"]

    # Section 5: a type either side couldn't normalize is reported as unknown_type
    # (a warning, not type_mismatch/error) — but see cli.py, which still treats it
    # as blocking for exit-code purposes ("treat as type mismatch, exit 1").
    column_source = _source(migration_col["source_file"], migration_col["source_line"])

    if model_col["type"].startswith("unknown:") or migration_col["type"].startswith("unknown:"):
        confidence, evidence = score_finding(
            "unknown_type", "column type could not be normalized on at least one side"
        )
        findings.append(
            DriftFinding(
                category="unknown_type",
                severity="warning",
                table=table_name,
                column=column_name,
                model_value=model_col["type"],
                migration_value=migration_col["type"],
                message="column type could not be normalized on at least one side",
                suggestion="inspect the underlying type by hand; comparison isn't possible",
                confidence=confidence,
                source=column_source,
                evidence=evidence,
                live_verified=False,
                kind="fail",
                demoted_from=None,
                explaining_notes=[],
            )
        )
    elif model_col["type"] != migration_col["type"]:
        confidence, evidence = score_finding(
            "ast_fully_resolved", "both sides' types fully resolved and normalized"
        )
        findings.append(
            DriftFinding(
                category="type_mismatch",
                severity="error",
                table=table_name,
                column=column_name,
                model_value=model_col["type"],
                migration_value=migration_col["type"],
                message="column type differs between model and migration",
                suggestion="decide which is authoritative, then align the other",
                confidence=confidence,
                source=column_source,
                evidence=evidence,
                live_verified=False,
                kind="fail",
                demoted_from=None,
                explaining_notes=[],
            )
        )

    if model_col["nullable"] != migration_col["nullable"]:
        confidence, evidence = score_finding(
            "ast_fully_resolved", "both sides' nullability fully resolved"
        )
        findings.append(
            DriftFinding(
                category="nullability_mismatch",
                severity="error",
                table=table_name,
                column=column_name,
                model_value=model_col["nullable"],
                migration_value=migration_col["nullable"],
                message="column nullability differs between model and migration",
                suggestion="decide which is authoritative, then align the other",
                confidence=confidence,
                source=column_source,
                evidence=evidence,
                live_verified=False,
                kind="fail",
                demoted_from=None,
                explaining_notes=[],
            )
        )

    return findings


def filter_findings(
    findings: list[DriftFinding],
    *,
    checks: dict[str, bool] | None = None,
    exclude_tables: list[str] | None = None,
    exclude_columns: list[str] | None = None,
) -> list[DriftFinding]:
    """Apply `.gravitar.yml`'s `checks` (category toggles) and `exclude` (glob-pattern
    table/column suppression) settings. `unknown_type` isn't independently toggleable
    via `checks` — it isn't one of the six categories in that section's schema.
    """
    checks = checks or {}
    exclude_tables = exclude_tables or []
    exclude_columns = exclude_columns or []

    def _check_enabled(finding: DriftFinding) -> bool:
        if finding["category"] == "unknown_type":
            return True
        return checks.get(finding["category"], True)

    def _excluded(finding: DriftFinding) -> bool:
        table = finding["table"]
        if table is None:
            return False
        if any(fnmatch.fnmatch(table, pattern) for pattern in exclude_tables):
            return True
        column = finding["column"]
        if column is not None:
            qualified = f"{table}.{column}"
            if any(fnmatch.fnmatch(qualified, pattern) for pattern in exclude_columns):
                return True
        return False

    return [f for f in findings if _check_enabled(f) and not _excluded(f)]
