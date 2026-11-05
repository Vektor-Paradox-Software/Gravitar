"""SPEC-17 S1 §6.2 — the parsed-module graph.

Holds the per-file ``ModuleSyms`` plus the inter-module edges derived
from each file's imports. Cycle detection breaks and emits a
scoped note (SPEC §5.2 "conditional" / §6.2 Phase 2) rather than
hanging.

The reader builds this once per run, before touching class graphs
or columns; S3+ consume it when linearizing MROs across files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gravitar.schema import SourceLocation, Unresolved, make_unresolved
from gravitar.schema.model_reader.consts import collect_module_constants
from gravitar.schema.model_reader.files import SourceFile
from gravitar.schema.model_reader.symbols import (
    ImportFrom,
    ImportModule,
    ModuleSyms,
    build_module_syms,
)


@dataclass
class ModuleGraph:
    """Entire parsed project view, built once per run."""

    modules: dict[str, ModuleSyms] = field(default_factory=dict)
    constants: dict[str, dict[str, object]] = field(default_factory=dict)
    notes: list[Unresolved] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)

    def resolve(self, module: str) -> ModuleSyms | None:
        return self.modules.get(module)


def build_module_graph(files: list[SourceFile]) -> ModuleGraph:
    """Build the graph from parsed source files. Skips files whose
    tree is None (they're already represented as parse_error notes by
    ``files.collect_model_files``)."""
    graph = ModuleGraph()
    for src in files:
        if src.tree is None:
            continue
        graph.modules[src.qualname] = build_module_syms(
            src.tree, src.qualname, src.path, is_package=src.is_package
        )
        graph.constants[src.qualname] = collect_module_constants(src.tree)

    cycles = _find_cycles(graph.modules)
    for cycle in cycles:
        graph.cycles.append(cycle)
        # SPEC §6.2 Phase 2: cycle note scoped to the first module in
        # the cycle (its file is the one the reviewer will open first).
        graph.notes.append(
            make_unresolved(
                "import_cycle",
                "import cycle broken: " + " -> ".join(cycle),
                SourceLocation(
                    file=str(graph.modules[cycle[0]].path),
                    line=None,
                    revision_id=None,
                ),
                tables=None,
            )
        )
    return graph


def _find_cycles(modules: dict[str, ModuleSyms]) -> list[list[str]]:
    """DFS-based cycle detection on the (non-typing) import edges.

    Only non-TYPE_CHECKING edges count — a cycle through type-only
    imports is harmless at runtime and should not fire a note.
    """
    edges: dict[str, set[str]] = {}
    for name, syms in modules.items():
        out: set[str] = set()
        for binding in syms.bindings.values():
            if isinstance(binding, (ImportFrom, ImportModule)):
                if getattr(binding, "for_typing", False):
                    continue
                target = binding.module
                if target in modules:
                    out.add(target)
        edges[name] = out

    cycles: list[list[str]] = []
    seen: set[str] = set()

    def dfs(node: str, stack: list[str]) -> None:
        if node in stack:
            cycle_start = stack.index(node)
            cycles.append(stack[cycle_start:] + [node])
            return
        if node in seen:
            return
        for nxt in sorted(edges.get(node, [])):
            dfs(nxt, stack + [node])
        seen.add(node)

    for start in sorted(edges):
        dfs(start, [])
    # De-dup rotations of the same cycle.
    normalized: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for cycle in cycles:
        core = tuple(sorted(set(cycle)))
        if core in normalized:
            continue
        normalized.add(core)
        unique.append(cycle)
    return unique


def expand_star_import(target_module: str, graph: ModuleGraph) -> list[str] | None:
    """SPEC §5.2 F-20: resolve ``from X import *`` through a literal
    ``__all__``. Returns the list of names, or ``None`` if the target
    module isn't in the graph OR has no literal ``__all__`` (reader
    emits a scoped note upstream when that happens)."""
    syms = graph.resolve(target_module)
    if syms is None:
        return None
    all_binding = syms.bindings.get("__all__")
    from gravitar.schema.model_reader.symbols import Const

    if not isinstance(all_binding, Const):
        return None
    value = all_binding.value
    if isinstance(value, (list, tuple)):
        names = [v for v in value if isinstance(v, str)]
        if len(names) == len(value):
            return names
    return None


def _get_root(path: Path, source_roots: list[Path]) -> Path | None:
    for root in source_roots:
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None
