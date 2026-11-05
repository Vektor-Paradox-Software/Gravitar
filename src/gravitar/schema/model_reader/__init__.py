"""SPEC-17 static model reader. Read SQLAlchemy models without
importing or executing project code.

Current state (S0 + S1): the package exposes ``read_models`` with the
stable surface. S0 defers Schema production to the import-based
loader so the differential harness has a target. S1 adds a parallel
AST pre-pass (``build_module_graph``) that produces a
``ModuleGraph`` without ever importing project code — S3+ will
read columns off this graph instead of the import loader.

The spec is explicit (§13 Decision 1): once S7 lands, the default
reader flips to ``static`` and the import reader becomes opt-in via
``models.reader: import`` or ``--trust-project-code``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gravitar.schema import Column, Schema, Table, Unresolved
from gravitar.schema.model_reader.classes import ClassGraph, ClassInfo, build_class_graph
from gravitar.schema.model_reader.columns import (
    ColumnDecl,
    ModuleTypeContext,
    build_module_type_context,
    collect_annotation_aliases,
    read_columns,
    resolve_nullability,
)
from gravitar.schema.model_reader.files import collect_model_files
from gravitar.schema.model_reader.module_graph import ModuleGraph, build_module_graph

__all__ = [
    "ModelResult",
    "ModelCoverage",
    "ReaderConfig",
    "read_models",
    "build_module_graph",
    "build_class_graph",
    "collect_model_files",
    "ClassGraph",
    "ClassInfo",
    "ColumnDecl",
    "ModuleGraph",
]


@dataclass(frozen=True)
class ModelCoverage:
    """SPEC-17 §6.1 ModelCoverage. Feeds the R7 coverage line's model
    side: "Models: N/M classes fully resolved · K with notes · S skipped"."""

    classes_total: int = 0
    classes_full: int = 0
    classes_with_notes: int = 0
    classes_skipped: int = 0
    tables: int = 0
    columns: int = 0


@dataclass
class ModelResult:
    """SPEC-17 §6.1. The reader returns the canonical ``Schema`` the
    rest of the pipeline already consumes, plus notes and coverage.
    ``reader`` is "static" | "import" | "auto"; provenance surfaces in
    findings per §7 so feedback keyed by pattern can separate the two
    readers' false-positive profiles.

    S1 adds ``module_graph`` — the parsed-AST view that S3+ will
    consume. It's populated on every call (even when ``reader`` is
    the import stub) so downstream tooling can inspect what the
    static reader *would* see.
    """

    schema: Schema
    notes: list[Unresolved] = field(default_factory=list)
    coverage: ModelCoverage = field(default_factory=ModelCoverage)
    reader: str = "static"
    module_graph: ModuleGraph | None = None


@dataclass(frozen=True)
class ReaderConfig:
    """SPEC-17 §9 config shape. ``packages`` + ``source_roots`` locate
    model modules without importing them; ``bases`` is an optional
    explicit list of declarative-base names (Flask-SQLAlchemy
    ``db.Model``, etc); ``type_map`` is shared with R13.

    S0 only reads ``packages`` (passed through to the stub) and
    ``base_classes`` (same, forwarded to the import loader when it's
    the stub). S8 wires the full config.
    """

    packages: tuple[str, ...] = ()
    source_roots: tuple[Path, ...] = ()
    bases: tuple[str, ...] = ()
    base_classes: tuple[str, ...] | None = None
    type_map: dict[str, str] = field(default_factory=dict)


def read_models(config: ReaderConfig) -> ModelResult:
    """SPEC-17 S0–S3: build Schema from the AST without importing
    project code. Each mapped class's columns are collected over its
    full MRO so mixin columns are attributed; the first binding of a
    given key wins (Python attribute precedence).

    S4+ will handle computed table names, inheritance kinds, and
    MetaData grouping. S3's limit: literal ``__tablename__`` only;
    classes without one are skipped with a scoped note.
    """
    import ast

    # Resolve source files and parse them.
    source_roots = list(config.source_roots) or [Path(".")]
    files, parse_errors = collect_model_files(source_roots, list(config.packages))

    # S1 side pass + S2 class graph.
    module_graph = build_module_graph(files)
    module_graph.notes = parse_errors + module_graph.notes
    class_graph = build_class_graph(module_graph)

    # Per-module Annotated-alias tables + S5 type contexts, built once.
    from gravitar.schema.model_reader.columns import AnnotationInfo

    alias_maps: dict[str, dict[str, AnnotationInfo]] = {}
    type_ctxs: dict[str, ModuleTypeContext] = {}
    file_asts: dict[str, ast.Module] = {}
    # Which classes count as declarative bases per module — needed so
    # ``build_module_type_context`` knows where to look for a
    # ``type_annotation_map`` attribute. A class qualifies if ANY of
    # its own bases is a recognized declarative base (``class Base(
    # DeclarativeBase):`` is the paradigm case).
    base_class_names: dict[str, list[str]] = {}
    for info in class_graph.classes.values():
        if any(b.is_declarative_base for b in info.bases):
            base_class_names.setdefault(info.module, []).append(info.class_name)
    for src in files:
        if src.tree is not None:
            file_asts[src.qualname] = src.tree
            alias_maps[src.qualname] = collect_annotation_aliases(src.tree)
            type_ctxs[src.qualname] = build_module_type_context(
                src.tree, base_class_names.get(src.qualname, [])
            )

    schema: Schema = {}
    reader_notes: list[Unresolved] = list(module_graph.notes) + list(class_graph.notes)
    classes_with_notes = 0
    skipped = 0

    for info in class_graph.mapped():
        table_name = _resolve_literal_tablename(info, file_asts)
        if table_name is None:
            skipped += 1
            continue  # S4 will handle computed names

        decls = _collect_mro_columns(info, class_graph, file_asts, alias_maps)
        if not decls:
            classes_with_notes += 1
            continue

        resolved_syms = module_graph.resolve(info.module)
        file_path = str(resolved_syms.path) if resolved_syms is not None else ""
        ctx = type_ctxs.get(info.module)
        columns: dict[str, Column] = {}
        for decl in decls:
            columns[decl.name] = Column(
                name=decl.name,
                type=decl.resolved_type(ctx) or "unknown:missing",
                nullable=bool(
                    resolve_nullability(decl) if resolve_nullability(decl) is not None else True
                ),
                primary_key=bool(decl.primary_key),
                source_file=file_path,
                source_line=decl.line,
            )
        schema[table_name] = Table(
            name=table_name,
            columns=columns,
            source_file=file_path,
            source_line=info.line,
        )

    coverage = ModelCoverage(
        classes_total=len(class_graph.mapped()),
        classes_full=len(class_graph.mapped()) - classes_with_notes - skipped,
        classes_with_notes=classes_with_notes,
        classes_skipped=skipped,
        tables=len(schema),
        columns=sum(len(t["columns"]) for t in schema.values()),
    )
    return ModelResult(
        schema=schema,
        notes=reader_notes,
        coverage=coverage,
        reader="static",
        module_graph=module_graph,
    )


def _resolve_literal_tablename(info, file_asts) -> str | None:  # type: ignore[no-untyped-def]
    """SPEC-17 S3: literal ``__tablename__`` only. S4 handles
    computed names (declared_attr, pure-string evaluator)."""
    import ast

    tree = file_asts.get(info.module)
    if tree is None:
        return None
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or cls.name != info.class_name:
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                tgt = stmt.targets[0]
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "__tablename__"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    return stmt.value.value
    return None


def _collect_mro_columns(info, class_graph, file_asts, alias_maps) -> list[ColumnDecl]:  # type: ignore[no-untyped-def]
    """Walk the class's MRO from most-base to most-derived so later
    (more-derived) classes can override earlier ones. Mixin columns
    are attributed to their origin class, which S7 will use for the
    SARIF physical location."""
    import ast

    mro = info.mro or [info.qualname]
    seen_keys: set[str] = set()
    out: list[ColumnDecl] = []
    for qualname in reversed(mro):
        parts = qualname.split(".")
        if parts and parts[0].startswith("<external:"):
            continue
        if "." not in qualname:
            continue
        module = ".".join(parts[:-1])
        class_name = parts[-1]
        tree = file_asts.get(module)
        if tree is None:
            continue
        aliases = alias_maps.get(module, {})
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == class_name:
                decls = read_columns(cls, class_name, aliases)
                for d in decls:
                    if d.key in seen_keys:
                        continue
                    seen_keys.add(d.key)
                    out.append(d)
                break
    return out
