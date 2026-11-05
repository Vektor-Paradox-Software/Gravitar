"""SPEC-17 S1 §6.2 Phase 2: module symbol tables.

For each parsed file, build a map of module-local names to what they
bind to — ``Import(module, attr)``, ``ClassRef(class_name)``,
``Const(value)``, ``Call(...)``, or ``Unknown`` when the RHS is
beyond the folder's reach.

Cross-module resolution happens later (``classes.py`` reads these
tables when linearizing MRO). This file does zero cross-module work
beyond recording that an import happened; it never traverses or
imports the referenced module.

Supported import shapes (SPEC §6.2):

- ``import x`` / ``import x as y``
- ``from x import a`` / ``from x import a as b``
- ``from x import *`` — bound only if ``x`` has a literal ``__all__``;
  without ``__all__`` it emits ``star_imports`` for the caller to
  handle (S3 fixture F-20).
- Relative imports: ``from .y import a``, ``from ..z import b``.
- ``if TYPE_CHECKING:`` — the body's imports are typing-only; names
  it binds are recorded with ``for_typing=True`` so the column
  reader can distinguish annotation references from runtime refs.
- ``try: from X import Y\nexcept ImportError: from X2 import Y`` —
  SPEC §5.2: the first binding wins; the symbol carries
  ``conditional=True`` so downstream notes can taint the table.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ImportFrom:
    """``from <module> import <attr>`` with optional ``as`` rename."""

    module: str  # fully resolved after `_resolve_relative`; "" for plain `import`
    attr: str  # the name imported from the module
    for_typing: bool = False
    conditional: bool = False  # inside try/except ImportError


@dataclass(frozen=True)
class ImportModule:
    """``import <module>`` with optional ``as`` rename."""

    module: str
    for_typing: bool = False


@dataclass(frozen=True)
class ClassRef:
    """Bound to a locally-defined class."""

    class_name: str
    line: int


@dataclass(frozen=True)
class Const:
    """Bound to a module-level literal we were able to fold."""

    value: object


@dataclass(frozen=True)
class CallRef:
    """Bound to a call expression. Carries the call's attribute
    name (for ``declarative_base()``, ``registry().generate_base()``
    detection) and nothing else; the walker reads AST directly for
    richer calls."""

    callable_name: str
    line: int


@dataclass(frozen=True)
class Unknown:
    """RHS beyond the folder's reach. Keeps a short reason for
    diagnostics; downstream handlers emit a note rather than
    guessing."""

    reason: str


Binding = ImportFrom | ImportModule | ClassRef | Const | CallRef | Unknown


@dataclass
class ModuleSyms:
    """SPEC §6.1 ModuleSyms. One per source file."""

    path: Path
    qualname: str
    bindings: dict[str, Binding] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)
    conditional_depth: int = 0  # internal tracker during walk


def build_module_syms(
    tree: ast.Module, qualname: str, path: Path, is_package: bool = False
) -> ModuleSyms:
    """Walk a module's top-level statements and build its binding
    table. Non-top-level statements (inside function/class bodies)
    are not part of module scope — the walker descends only through
    ``if``/``try`` branches at the module top level, since those are
    the only shapes that introduce new module-level bindings.

    ``is_package`` is True iff this module is a package's
    ``__init__.py`` — relative-import resolution depends on it: for a
    package, ``from .X import Y`` resolves to ``<self>.X``; for a
    submodule, it resolves to ``<parent>.X``.
    """
    syms = ModuleSyms(path=path, qualname=qualname)
    _walk_body(
        tree.body,
        syms,
        qualname=qualname,
        is_package=is_package,
        type_checking=False,
        conditional=False,
    )
    return syms


def _walk_body(
    body: list[ast.stmt],
    syms: ModuleSyms,
    *,
    qualname: str,
    is_package: bool,
    type_checking: bool,
    conditional: bool,
) -> None:
    for stmt in body:
        if isinstance(stmt, ast.If):
            # SPEC-17 §6.2: TYPE_CHECKING branch gets its own flag;
            # ordinary if/else both descended, first unconditional
            # binding wins (handled by dict's existing semantics —
            # we only set if the name isn't already bound at a
            # higher-priority level).
            is_tc_branch = _is_type_checking(stmt.test)
            _walk_body(
                stmt.body,
                syms,
                qualname=qualname,
                is_package=is_package,
                type_checking=type_checking or is_tc_branch,
                conditional=conditional or not is_tc_branch,
            )
            _walk_body(
                stmt.orelse,
                syms,
                qualname=qualname,
                is_package=is_package,
                type_checking=type_checking,
                conditional=conditional or not is_tc_branch,
            )
        elif isinstance(stmt, ast.Try):
            # try/except ImportError pattern: SPEC §5.2 — first binding
            # wins, conditional=True.
            _walk_body(
                stmt.body,
                syms,
                qualname=qualname,
                is_package=is_package,
                type_checking=type_checking,
                conditional=True,
            )
            for handler in stmt.handlers:
                _walk_body(
                    handler.body,
                    syms,
                    qualname=qualname,
                    is_package=is_package,
                    type_checking=type_checking,
                    conditional=True,
                )
            _walk_body(
                stmt.orelse,
                syms,
                qualname=qualname,
                is_package=is_package,
                type_checking=type_checking,
                conditional=conditional,
            )
            _walk_body(
                stmt.finalbody,
                syms,
                qualname=qualname,
                is_package=is_package,
                type_checking=type_checking,
                conditional=conditional,
            )
        elif isinstance(stmt, ast.ImportFrom):
            _bind_import_from(
                stmt,
                syms,
                qualname,
                is_package=is_package,
                for_typing=type_checking,
                conditional_flag=conditional,
            )
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                name = alias.asname or alias.name.split(".")[0]
                _set_if_absent(
                    syms,
                    name,
                    ImportModule(module=alias.name, for_typing=type_checking),
                )
        elif isinstance(stmt, ast.ClassDef):
            _set_if_absent(syms, stmt.name, ClassRef(class_name=stmt.name, line=stmt.lineno))
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            _bind_assign(stmt, syms)
        # Other top-level stmts (FunctionDef, If-with-non-import-body
        # beyond what we descended into) don't introduce symbols we
        # care about for the model reader.


def _bind_import_from(
    stmt: ast.ImportFrom,
    syms: ModuleSyms,
    qualname: str,
    *,
    is_package: bool,
    for_typing: bool,
    conditional_flag: bool,
) -> None:
    module = _resolve_relative(stmt, qualname, is_package=is_package)
    for alias in stmt.names:
        if alias.name == "*":
            syms.star_imports.append(module)
            continue
        local = alias.asname or alias.name
        _set_if_absent(
            syms,
            local,
            ImportFrom(
                module=module,
                attr=alias.name,
                for_typing=for_typing,
                conditional=conditional_flag,
            ),
        )


def _resolve_relative(stmt: ast.ImportFrom, qualname: str, *, is_package: bool) -> str:
    """Convert ``from .x import y`` relative to this module's qualname
    into an absolute dotted module name.

    Python semantics for relative imports differ between packages
    (``__init__.py``) and submodules:
    - In a package, ``from .x`` resolves to ``<self>.x`` (level=1
      means "the package itself").
    - In a submodule, ``from .x`` resolves to ``<parent>.x`` (level=1
      means "my parent package").

    For a package, strip ``level - 1`` trailing parts; for a submodule,
    strip ``level`` parts. Both reduce correctly.
    """
    if stmt.level == 0:
        return stmt.module or ""
    parts = qualname.split(".")
    strip = stmt.level if not is_package else stmt.level - 1
    base = parts[: max(0, len(parts) - strip)] if strip > 0 else list(parts)
    if stmt.module:
        base.append(stmt.module)
    return ".".join(base)


def _bind_assign(stmt: ast.stmt, syms: ModuleSyms) -> None:
    """Bind a simple ``NAME = <literal>`` or ``NAME = <call>`` at
    module scope. More complex RHS → Unknown.
    """
    target: str | None = None
    value: ast.expr | None = None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        if isinstance(stmt.targets[0], ast.Name):
            target = stmt.targets[0].id
            value = stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        if isinstance(stmt.target, ast.Name):
            target = stmt.target.id
            value = stmt.value
    if target is None or value is None:
        return

    # Literal RHS — fold through ast.literal_eval.
    try:
        folded = ast.literal_eval(value)
        _set_if_absent(syms, target, Const(value=folded))
        return
    except (ValueError, TypeError, SyntaxError):
        pass

    # Call RHS — record its name so declarative-base detection can
    # match ``Base = declarative_base()`` later.
    if isinstance(value, ast.Call):
        callable_name: str | None = None
        func = value.func
        if isinstance(func, ast.Name):
            callable_name = func.id
        elif isinstance(func, ast.Attribute):
            callable_name = func.attr
        if callable_name is not None:
            _set_if_absent(
                syms,
                target,
                CallRef(callable_name=callable_name, line=stmt.lineno),
            )
            return

    _set_if_absent(syms, target, Unknown(reason="non-foldable RHS"))


def _set_if_absent(syms: ModuleSyms, name: str, binding: Binding) -> None:
    """SPEC §6.2: first unconditional binding wins. Only overwrite
    an Unknown with a more-specific binding, or when the current
    binding is conditional and the new one isn't."""
    existing = syms.bindings.get(name)
    if existing is None:
        syms.bindings[name] = binding
        return
    if isinstance(existing, Unknown) and not isinstance(binding, Unknown):
        syms.bindings[name] = binding
        return
    # Prefer non-conditional over conditional.
    existing_cond = isinstance(existing, ImportFrom) and existing.conditional
    new_cond = isinstance(binding, ImportFrom) and binding.conditional
    if existing_cond and not new_cond:
        syms.bindings[name] = binding


def _is_type_checking(test: ast.expr) -> bool:
    """Recognize ``if TYPE_CHECKING:`` and ``if typing.TYPE_CHECKING:``.
    Doesn't try to be clever about aliases — those are rare, and the
    cost of missing a TYPE_CHECKING name is just "typing-only
    imports become runtime", which the walker handles safely by not
    reading them for columns."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False
