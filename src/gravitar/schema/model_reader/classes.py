"""SPEC-17 S2 §6 Phase 4-5: class graph, MRO, mapped-class identification.

Builds a cross-module ``ClassGraph`` over the already-parsed
``ModuleGraph`` from S1. For each class the reader needs to know:

- its bases, resolved through the module's symbol table (what name
  points at what class in which file);
- its C3 linearization (so mixin columns can be attributed in
  Python attribute precedence);
- whether its MRO reaches a recognized declarative base — i.e.
  whether it's a *mapped* class;
- whether an external base is involved (SPEC §5.3 S3: scoped note);
- whether it's ``__abstract__``.

No column reading yet — that lands in S3 and consumes this graph.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from gravitar.schema import SourceLocation, Unresolved, make_unresolved
from gravitar.schema.model_reader.module_graph import ModuleGraph
from gravitar.schema.model_reader.symbols import (
    CallRef,
    ClassRef,
    ImportFrom,
    ImportModule,
    ModuleSyms,
)

#: Names a module-level ``NAME = CALL(...)`` must map onto to count
#: as a declarative base. ``declarative_base()`` + ``registry()
#: .generate_base()`` are the SA 1.4-era patterns; direct subclassing
#: of ``DeclarativeBase`` is the SA 2.0+ one and is handled by
#: ``_DECLARATIVE_BASE_CLASSES`` below.
_DECLARATIVE_BASE_FACTORIES: frozenset[str] = frozenset(
    {
        "declarative_base",
        "generate_base",  # registry(...).generate_base()
    }
)

#: SA 2.0+ names — if a class's base list reaches any of these
#: (through imports or in-project transitive bases), the class is
#: mapped. ``DeclarativeBase`` is the public one; the harness also
#: accepts ``MappedAsDataclass`` etc. only as mixins (they don't
#: make a class mapped by themselves).
_DECLARATIVE_BASE_CLASSES: frozenset[str] = frozenset(
    {
        "DeclarativeBase",
        "DeclarativeBaseNoMeta",
    }
)

#: SA/stdlib base classes that are *known* columnless — a mixin
#: that extends them doesn't contribute columns, so a class with
#: these as external bases does NOT need a scoped note (SPEC §5.3).
_KNOWN_COLUMNLESS: frozenset[str] = frozenset(
    {
        "object",
        "ABC",
        "Protocol",
        "DeclarativeBase",  # itself contributes no columns
        "DeclarativeBaseNoMeta",
        "Generic",
    }
)


@dataclass(frozen=True)
class BaseRef:
    """One base of a class, resolved as far as the symbol tables
    allow. ``class_name`` is populated only when the base points at
    an in-project class."""

    name: str  # the identifier used in the base list, e.g. "Base"
    module: str | None  # the module the base lives in (None if local)
    class_name: str | None = None  # resolved in-project class name
    is_external: bool = False  # outside the project, not columnless-known
    is_declarative_base: bool = False  # recognized SA declarative base


@dataclass
class ClassInfo:
    """SPEC-17 §6.1 ClassInfo (S2 subset: no body yet; S3 adds it)."""

    qualname: str  # module.ClassName
    module: str
    class_name: str
    line: int
    bases: list[BaseRef] = field(default_factory=list)
    mro: list[str] | None = None  # None iff linearization failed
    abstract: bool = False
    mapped: bool = False
    external_base: bool = False  # True iff any base is external & columnful
    conditional: bool = False  # inside try/except ImportError
    class_hint: str = ""  # scope hint for notes when table is unknown


@dataclass
class ClassGraph:
    """Project-wide class graph. Keys are ``module.ClassName``."""

    classes: dict[str, ClassInfo] = field(default_factory=dict)
    notes: list[Unresolved] = field(default_factory=list)

    def mapped(self) -> list[ClassInfo]:
        """Classes that are both mapped AND not abstract (abstract
        classes contribute columns but no table)."""
        return [c for c in self.classes.values() if c.mapped and not c.abstract]

    def by_class_name(self, class_name: str) -> ClassInfo | None:
        """First class with this unqualified name. Used by MRO
        linearization when a base refers to a class by bare name."""
        for info in self.classes.values():
            if info.class_name == class_name:
                return info
        return None


# --- Build the graph ---


def build_class_graph(module_graph: ModuleGraph) -> ClassGraph:
    """Collect every ClassDef across the parsed modules, resolve
    bases through the module's symbol table, mark mapped classes
    reachable from a declarative base, and compute MRO."""
    graph = ClassGraph()

    # Phase 1: collect class definitions from each module's AST.
    _collect_classes(module_graph, graph)
    # Phase 2: resolve bases through symbol tables (cross-module).
    _resolve_bases(module_graph, graph)
    # Phase 3: identify mapped classes by walking bases transitively.
    _mark_mapped(graph)
    # Phase 4: C3 linearization for MRO.
    _compute_mros(graph)
    # Phase 5: emit scoped notes for abstract / external-base cases
    #          that will matter when S3 produces findings.
    _emit_notes(module_graph, graph)
    return graph


def _collect_classes(module_graph: ModuleGraph, graph: ClassGraph) -> None:
    """One ClassInfo per ClassDef at module scope."""
    for module, syms in module_graph.modules.items():
        tree = _parse_once(syms.path)
        if tree is None:
            continue
        for stmt in tree.body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            qualname = f"{module}.{stmt.name}"
            is_abstract = _is_abstract_marker(stmt)
            info = ClassInfo(
                qualname=qualname,
                module=module,
                class_name=stmt.name,
                line=stmt.lineno,
                bases=_shallow_bases(stmt),
                abstract=is_abstract,
                class_hint=stmt.name,
            )
            graph.classes[qualname] = info


def _parse_once(path: Path) -> ast.Module | None:
    """Re-parse a model file. S1 already parsed everything once, but
    the ``ModuleSyms`` doesn't retain the AST (keeps the memory
    footprint small). For a cross-check pass, re-parsing is cheap and
    keeps the public API simple. ``files._parse_file`` is the
    canonical path if we need to centralize this later."""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None


def _is_abstract_marker(cls: ast.ClassDef) -> bool:
    """A ClassDef with ``__abstract__ = True`` at the top of its
    body."""
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            tgt = stmt.targets[0]
            if (
                isinstance(tgt, ast.Name)
                and tgt.id == "__abstract__"
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is True
            ):
                return True
    return False


def _shallow_bases(cls: ast.ClassDef) -> list[BaseRef]:
    """Base refs as named — module resolution happens in
    ``_resolve_bases`` once we have the whole class graph."""
    out: list[BaseRef] = []
    for base in cls.bases:
        if isinstance(base, ast.Name):
            out.append(BaseRef(name=base.id, module=None))
        elif isinstance(base, ast.Attribute):
            # ``some_pkg.Base`` — carry the attribute name as the base
            # name; the resolver below will combine it with the
            # import binding.
            out.append(BaseRef(name=base.attr, module=_dotted_prefix(base)))
    return out


def _dotted_prefix(node: ast.Attribute) -> str | None:
    """Collect the leading dotted name of ``a.b.X``: returns ``a.b``.
    Returns None for shapes we can't flatten."""
    parts: list[str] = []
    cur: ast.expr = node.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# --- Resolve bases through imports ---


def _resolve_bases(module_graph: ModuleGraph, graph: ClassGraph) -> None:
    """For each ClassInfo, use its module's symbol table to resolve
    each base to either an in-project class qualname or mark it as
    external / declarative."""
    for info in graph.classes.values():
        syms = module_graph.resolve(info.module)
        if syms is None:
            continue
        resolved: list[BaseRef] = []
        for base in info.bases:
            resolved.append(_resolve_single_base(base, syms, graph))
        info.bases = resolved


def _resolve_single_base(base: BaseRef, syms: ModuleSyms, graph: ClassGraph) -> BaseRef:
    """One base, resolved against the module's bindings. Handles:

    - ``class X(Base):`` where ``Base = declarative_base()`` in the
      same module → declarative-base.
    - ``class X(ParentCls):`` where ``ParentCls`` is defined in the
      same module → in-project class ref.
    - ``class X(SomeMixin):`` from ``from .mixins import SomeMixin``
      → in-project class ref via import resolution.
    - ``class X(DeclarativeBase):`` from
      ``from sqlalchemy.orm import DeclarativeBase`` → declarative-
      base (external, but recognized as mapping-anchor).
    - Anything else → external.
    """
    # Prefix form `pkg.Name` — walk through ImportModule.
    if base.module is not None:
        top = base.module.split(".")[0]
        binding = syms.bindings.get(top)
        if isinstance(binding, ImportModule) and binding.module.startswith("sqlalchemy"):
            # ``sa.DeclarativeBase`` or similar
            if base.name in _DECLARATIVE_BASE_CLASSES:
                return BaseRef(name=base.name, module=binding.module, is_declarative_base=True)
        return BaseRef(name=base.name, module=base.module, is_external=True)

    binding = syms.bindings.get(base.name)
    if isinstance(binding, CallRef) and binding.callable_name in _DECLARATIVE_BASE_FACTORIES:
        return BaseRef(name=base.name, module=syms.qualname, is_declarative_base=True)
    if isinstance(binding, ClassRef):
        qualname = f"{syms.qualname}.{binding.class_name}"
        if qualname in graph.classes:
            return BaseRef(
                name=base.name,
                module=syms.qualname,
                class_name=binding.class_name,
            )
    if isinstance(binding, ImportFrom):
        source_module = binding.module
        target_qualname = f"{source_module}.{binding.attr}"
        if target_qualname in graph.classes:
            return BaseRef(
                name=base.name,
                module=source_module,
                class_name=binding.attr,
            )
        if binding.attr in _DECLARATIVE_BASE_CLASSES:
            return BaseRef(name=base.name, module=source_module, is_declarative_base=True)
        return BaseRef(name=base.name, module=source_module, is_external=True)
    # Fallback — not in the import map at all. Could be a built-in
    # (object), a star-import binding we didn't resolve, or a dynamic
    # expression. Mark external but check the columnless allowlist.
    if base.name in _KNOWN_COLUMNLESS:
        return BaseRef(name=base.name, module=None, is_external=False)
    return BaseRef(name=base.name, module=None, is_external=True)


# --- Mapped-class identification ---


def _mark_mapped(graph: ClassGraph) -> None:
    """BFS from each class through its bases. A class is mapped if
    the walk reaches a declarative-base ref. Also sets ``external_base``
    along the way for SPEC §5.3 note emission."""
    for info in graph.classes.values():
        visited: set[str] = set()
        queue: list[ClassInfo] = [info]
        reached_declarative = False
        has_external = False
        while queue:
            current = queue.pop(0)
            if current.qualname in visited:
                continue
            visited.add(current.qualname)
            for base in current.bases:
                if base.is_declarative_base:
                    reached_declarative = True
                if base.is_external:
                    has_external = True
                if base.class_name is not None and base.module is not None:
                    target_qualname = f"{base.module}.{base.class_name}"
                    next_info = graph.classes.get(target_qualname)
                    if next_info is not None:
                        queue.append(next_info)
                elif base.class_name is None and base.module is None:
                    # Bare name — try to resolve across the project by name only.
                    nxt = graph.by_class_name(base.name)
                    if nxt is not None and nxt.qualname != info.qualname:
                        queue.append(nxt)
        info.mapped = reached_declarative
        info.external_base = has_external


# --- C3 linearization ---


def _compute_mros(graph: ClassGraph) -> None:
    """C3 linearization over the project classes. External bases are
    treated as leaves (their own MRO is just themselves)."""
    for info in graph.classes.values():
        try:
            info.mro = _c3_linearize(info, graph)
        except _LinearizationError:
            info.mro = None  # S3 can emit a scoped note then


class _LinearizationError(RuntimeError):
    pass


def _c3_linearize(info: ClassInfo, graph: ClassGraph) -> list[str]:
    """Classic C3 merge. Returns qualnames in MRO order (most-
    derived first)."""
    parents: list[list[str]] = []
    for base in info.bases:
        if base.class_name and base.module:
            target = f"{base.module}.{base.class_name}"
            parent_info = graph.classes.get(target)
            if parent_info is not None:
                try:
                    parents.append(_c3_linearize(parent_info, graph))
                except _LinearizationError:
                    raise
                continue
        # External or declarative base → leaf.
        leaf = f"<external:{base.module}.{base.name}>" if base.module else f"<external:{base.name}>"
        parents.append([leaf])

    base_names = [
        (
            f"{b.module}.{b.class_name}"
            if b.module and b.class_name
            else f"<external:{b.module}.{b.name}>"
            if b.module
            else f"<external:{b.name}>"
        )
        for b in info.bases
    ]

    sequences: list[list[str]] = [list(p) for p in parents]
    if base_names:
        sequences.append(list(base_names))
    merged: list[str] = [info.qualname]
    while any(sequences):
        for seq in sequences:
            if not seq:
                continue
            head = seq[0]
            if not any(head in rest[1:] for rest in sequences):
                merged.append(head)
                for rest in sequences:
                    if rest and rest[0] == head:
                        rest.pop(0)
                sequences = [s for s in sequences if s]
                break
        else:
            raise _LinearizationError(f"inconsistent MRO for {info.qualname}")
    return merged


# --- Notes ---


def _emit_notes(module_graph: ModuleGraph, graph: ClassGraph) -> None:
    """SPEC §5.3: a mapped class with an external (not-known-columnless)
    base can't be fully resolved — the external base may contribute
    columns we can't see. Emit a scoped note so S3's findings on this
    table carry taint."""
    for info in graph.classes.values():
        if not info.mapped:
            continue
        if not info.external_base:
            continue
        syms = module_graph.resolve(info.module)
        source_file = str(syms.path) if syms else ""
        graph.notes.append(
            make_unresolved(
                "external_call",
                f"class {info.class_name!r} has an external base that may add columns",
                SourceLocation(file=source_file, line=info.line, revision_id=None),
                tables=None,  # table name resolution is S4; scope by class hint
            )
        )
