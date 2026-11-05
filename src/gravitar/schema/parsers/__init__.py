"""DSL-agnostic migration operation representation and parser protocol.

`Operation` is the one shape every parser resolves down to, regardless of
source DSL: plain values (table/column names, canonical type strings,
booleans), never a DSL-specific object (an `ast.expr`, a live Django operation
instance). `migrations.py`'s schema-state builder (`build_migration_schema` /
`_apply_operations`) consumes exactly this shape and needs no knowledge of
which parser produced it — an explicit interface rather than an implicit
convention inside one module.

`Revision` is an identified unit of migration with zero or more parents and
the operations it contributes. Alembic's `down_revision` and Django's
`dependencies` are both, structurally, "parent identifiers" — `migrations.py`'s
graph algorithms (gap/cycle/head detection, topological replay) operate on
that shape alone and are reused unchanged for both DSLs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from gravitar.schema import Unresolved


@dataclass
class ColumnSpec:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False
    unresolved: list[Unresolved] = field(default_factory=list)  # SPEC-16 v1.0 §4


@dataclass
class Operation:
    """A single schema-mutating operation, already resolved to plain values by
    whichever parser produced it.
    """

    op: str
    line: int
    table: str | None = None
    columns: list[ColumnSpec] = field(default_factory=list)  # create_table (N) / add_column (1)
    column_name: str | None = None  # drop_column, alter_column, rename_column (source)
    new_table: str | None = None  # rename_table target
    new_column_name: str | None = None  # rename_column target
    type: str | None = None  # alter_column
    nullable: bool | None = None  # alter_column
    raw_sql: str | None = None  # execute
    unresolved: list[Unresolved] = field(default_factory=list)  # SPEC-16 v1.0 §4
    conditional: bool = False  # SPEC-16 v1.0 §4 (R4 populates; default False)


@dataclass
class Revision:
    revision: str | None
    down_revision: str | list[str] | None
    operations: list[Operation]
    source_file: str
    # SPEC-16 v1.0 §4: unresolved notes the parser emitted for this revision
    # (parse errors, revision-level problems, revision-scoped unresolveds).
    # Operation-level unresolveds live on Operation itself; migrations.py
    # aggregates both into the MigrationResult.
    notes: list[Unresolved] = field(default_factory=list)
    # SPEC-12 Amendment v1.1 Scenario E: a count of ops this parser correctly
    # recognized as inert — ``create_index``, ``AddIndex``, bulk-insert
    # helpers, etc. Not applied (not in operations), not noted (fully
    # understood), but still accounted for so the "nothing silently
    # dropped" invariant can be tested as ``nodes_seen == applied +
    # inert_count + len(notes)``.
    inert_count: int = 0


class MigrationParser(Protocol):
    def parse(self, migration_path: str) -> Revision: ...
