"""Django migration parsing (notes + per-file isolation, parity with Alembic's
notes/isolation work).

Django migration files are declarative: a module-level `Migration` class whose
`operations` list already holds constructed operation objects (`CreateModel`,
`AddField`, ...) wrapping already-resolved field instances — there's no AST to
walk, unlike Alembic's imperative `op.*` calls. Importing a Django migration
file is safe *without* `django.setup()`/an app registry: constructing a
migration, operation, or bare field object has no side effects and doesn't
attach it to a live model or database (verified empirically — plain
`importlib` works). Only introspecting live `django.db.models.Model` classes
(the model side, `django_models.py`) needs Django's app registry configured.

**Trust boundary (R16d).** Django migration files are *imported* — Python
module execution, not AST walking. Any import-time side effects a migration
file carries will run under Gravitar. That's the same trust boundary as the
import-based model loader (R15 will remove it); for now, run Gravitar only on
code you'd be willing to execute anyway. On fork PR CI (R13), this is the
reason to require a sandbox.

Scoped to a single Django app (Non-goals: "not full Django ORM/migration
feature coverage") — `dependencies` entries naming a different app are
silently dropped rather than followed; resolving another app's migration
graph is out of scope for Milestone 10. ``AddIndex``/``RemoveIndex`` are
recognized as known-inert — Gravitar's `Schema` has no index concept, same as
Alembic's `op.create_index` today. Everything
else — `RunSQL`, `RunPython`, `SeparateDatabaseAndState`, custom plugin ops
— produces an `Unresolved` note rather than vanishing silently (R16a; the
transparency contract T1).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from django.db.migrations import operations as django_ops

from gravitar.schema import SourceLocation, Unresolved, make_unresolved
from gravitar.schema.parsers import ColumnSpec, Operation, Revision
from gravitar.schema.parsers.django_types import normalize_django_field


def table_name(app_label: str, model_name: str) -> str:
    """Django's default `db_table` naming convention: `<app_label>_<model>`,
    both lowercased — used because migration operations carry only the bare
    model name, not the live model class (which would know a custom
    `Meta.db_table` override; out of scope for Milestone 10's bounded parser).
    """
    return f"{app_label}_{model_name.lower()}"


def parse_migration_file(path: Path, app_label: str) -> Revision:
    """Parse one Django migration file. R16a emits an `Unresolved` for every
    Django operation Gravitar doesn't fully understand; nothing silent."""
    module = _import_module_from_path(path)
    migration_cls = module.Migration

    down_revision = [
        name
        for (dep_app, name) in getattr(migration_cls, "dependencies", [])
        if dep_app == app_label
    ]

    operations: list[Operation] = []
    notes: list[Unresolved] = []
    inert_count = 0
    for index, django_op in enumerate(getattr(migration_cls, "operations", [])):
        resolved = _resolve_operation(django_op, app_label, index, path, notes)
        if resolved is not None:
            operations.append(resolved)
        elif isinstance(django_op, _KNOWN_INERT_DJANGO_OPS):
            # SPEC-12 Amendment Scenario E: known-inert ops are counted
            # separately so the "nothing silently dropped" accounting
            # identity holds — an AddIndex that correctly produces no
            # Operation and no note must still be representable in the
            # ``nodes_seen = applied + inert_count + len(notes)`` sum.
            inert_count += 1

    return Revision(
        revision=path.stem,
        down_revision=down_revision,
        operations=operations,
        source_file=str(path),
        notes=notes,
        inert_count=inert_count,
    )


def parse_migrations_dir(migrations_dir: Path, app_label: str) -> list[Revision]:
    """Parse every migration file in a Django app's migrations/ directory.

    R16c (per-file isolation, parallel to Alembic's R11): a file that raises
    on load becomes a `parse_error` note on a placeholder Revision; the
    whole run continues against the remaining files. Pre-R16 one broken
    file bombed the whole analysis (parallel to the readiness doc's S10,
    which was Alembic-specific but applies to Django too).
    """
    revisions: list[Revision] = []
    for path in sorted(migrations_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            revisions.append(parse_migration_file(path, app_label))
        except Exception as e:  # noqa: BLE001 — per-file isolation is the whole point
            revisions.append(_parse_error_revision(path, e))
    return revisions


def _parse_error_revision(path: Path, exc: BaseException) -> Revision:
    detail = (str(exc) or type(exc).__name__)[:80]
    note = make_unresolved(
        "parse_error",
        detail,
        SourceLocation(file=str(path), line=None, revision_id=None),
    )
    return Revision(
        revision=None,
        down_revision=None,
        operations=[],
        source_file=str(path),
        notes=[note],
    )


def _import_module_from_path(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"_gravitar_django_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Django migration file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_field(name: str, field: Any) -> ColumnSpec:
    # attname accounts for Django's own renaming convention (a ForeignKey named
    # "author" has attname "author_id", the real column name); falls back to
    # the field's declared name for anything that doesn't define one.
    column_name = getattr(field, "attname", name)
    return ColumnSpec(
        name=column_name,
        type=normalize_django_field(field),
        nullable=bool(getattr(field, "null", False)),
        primary_key=bool(getattr(field, "primary_key", False)),
    )


# R16a: Django operation classes that correctly perform no column-shape
# change — Gravitar's Schema doesn't model what they do (indexes, constraints,
# model-only options), so there's nothing to apply AND nothing to flag. Silent
# on purpose; the transparency invariant (T1) only forbids silent drops of
# ops Gravitar *could* have known it couldn't handle.
_KNOWN_INERT_DJANGO_OPS: tuple[type, ...] = tuple(
    cls
    for cls in (
        getattr(django_ops, name, None)
        for name in (
            "AddIndex",
            "RemoveIndex",
            "AddConstraint",
            "RemoveConstraint",
            "AlterModelOptions",
            "AlterModelManagers",
            "AlterOrderWithRespectTo",
            "AlterUniqueTogether",
            "AlterIndexTogether",
            "AlterModelTableComment",
            "AlterFieldComment",
        )
    )
    if cls is not None
)


def _resolve_operation(
    op: Any, app_label: str, line: int, path: Path, notes: list[Unresolved]
) -> Operation | None:
    """Return an Operation (handled ops), None (known-inert), or append an
    `Unresolved` note to `notes` and return None (anything else). The
    returned-None vs noted-None distinction is exactly R16a: nothing
    silently drops.
    """
    if isinstance(op, django_ops.CreateModel):
        columns = [_resolve_field(name, field) for name, field in op.fields]
        return Operation(
            op="create_table", line=line, table=table_name(app_label, op.name), columns=columns
        )

    if isinstance(op, django_ops.DeleteModel):
        return Operation(op="drop_table", line=line, table=table_name(app_label, op.name))

    if isinstance(op, django_ops.AddField):
        return Operation(
            op="add_column",
            line=line,
            table=table_name(app_label, op.model_name),
            columns=[_resolve_field(op.name, op.field)],
        )

    if isinstance(op, django_ops.RemoveField):
        # RemoveField carries only the declared field name, never the field
        # instance (Django doesn't need one to drop a column) — so unlike
        # AddField/AlterField this can't resolve a ForeignKey's real "_id"
        # column name. A known, honest limitation, not a crash.
        return Operation(
            op="drop_column",
            line=line,
            table=table_name(app_label, op.model_name),
            column_name=op.name,
        )

    if isinstance(op, django_ops.AlterField):
        spec = _resolve_field(op.name, op.field)
        return Operation(
            op="alter_column",
            line=line,
            table=table_name(app_label, op.model_name),
            # The *pre-alter* declared name is the lookup key into the schema
            # being built (matching _op_alter_column's expectation) — not
            # spec.name, which is the *post-alter* resolved attname and could
            # differ if the field's category changed (e.g. plain -> ForeignKey).
            column_name=op.name,
            type=spec.type,
            nullable=spec.nullable,
        )

    # R16b: RenameField/RenameModel/AlterModelTable. Pre-R16 these were
    # dropped — a renamed field read as a false missing+orphan pair.
    if isinstance(op, django_ops.RenameField):
        return Operation(
            op="rename_column",
            line=line,
            table=table_name(app_label, op.model_name),
            column_name=op.old_name,
            new_column_name=op.new_name,
        )

    if isinstance(op, django_ops.RenameModel):
        return Operation(
            op="rename_table",
            line=line,
            table=table_name(app_label, op.old_name),
            new_table=table_name(app_label, op.new_name),
        )

    if isinstance(op, django_ops.AlterModelTable):
        # AlterModelTable changes Meta.db_table (the actual table name).
        # Treat as a table rename from the default name to the new one.
        new_table = op.table if op.table else table_name(app_label, op.name)
        return Operation(
            op="rename_table",
            line=line,
            table=table_name(app_label, op.name),
            new_table=new_table,
        )

    # Known-inert: counted, not noted (R16a's "known-inert → counted" branch).
    if isinstance(op, _KNOWN_INERT_DJANGO_OPS):
        return None

    # R16a: everything else — RunSQL, RunPython, SeparateDatabaseAndState,
    # custom plugin ops — becomes an Unresolved note. Try to recover the
    # model name for scoping if the op exposes one; fall back to global
    # scope otherwise.
    model_name = getattr(op, "model_name", None)
    tables = [table_name(app_label, model_name)] if isinstance(model_name, str) else None
    notes.append(
        make_unresolved(
            "django_op_unsupported",
            type(op).__name__,
            SourceLocation(file=str(path), line=line, revision_id=path.stem),
            tables=tables,
        )
    )
    return None
