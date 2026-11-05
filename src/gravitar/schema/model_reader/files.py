"""SPEC-17 S1 §6.2 Phase 1: file collection + per-file ast.parse.

Resolves ``packages`` to filesystem paths by searching ``source_roots``
*without importing*. ``importlib.util.find_spec("a.b")`` is forbidden
because it imports parent package ``a``, running its ``__init__``.
Walk the directory tree instead, treating any directory with
``__init__.py`` as a package.

Per-file parse errors are isolated per SPEC §6.2 — SyntaxError
produces a scoped ``parse_error`` note; the run continues against
the rest of the files. Mealie's "newer-syntax than this interpreter"
case gets a hint in the note's detail (same rule as the Alembic
parser's R11).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from gravitar.schema import SourceLocation, Unresolved, make_unresolved


@dataclass(frozen=True)
class SourceFile:
    """One parsed model file."""

    path: Path
    qualname: str  # dotted module path, e.g. "app.models.widget"
    tree: ast.Module | None  # None iff parse failed
    parse_error: Unresolved | None  # populated iff tree is None
    is_package: bool = False  # True iff this is a package's __init__.py

    @property
    def ok(self) -> bool:
        return self.tree is not None


def collect_model_files(
    source_roots: list[Path], packages: list[str]
) -> tuple[list[SourceFile], list[Unresolved]]:
    """Walk ``source_roots`` to resolve each package in ``packages`` to
    the .py files it contains. Returns the parsed files AND the parse
    errors (as notes) so the caller can thread both into the final
    ``ModelResult``.

    SPEC §6.2 forbids ``importlib.util.find_spec`` because it imports
    parent packages. Resolution here is pure filesystem: for each
    package ``a.b.c``, look for ``<root>/a/b/c/__init__.py`` or
    ``<root>/a/b/c.py``. A directory without ``__init__.py`` is still
    walked (namespace packages) if it matches the dotted path.
    """
    out_files: list[SourceFile] = []
    out_errors: list[Unresolved] = []
    seen_paths: set[Path] = set()

    for pkg in packages:
        parts = pkg.split(".")
        for root in source_roots:
            pkg_dir = root
            for part in parts:
                pkg_dir = pkg_dir / part
            if pkg_dir.is_dir():
                for py in sorted(pkg_dir.rglob("*.py")):
                    if py in seen_paths:
                        continue
                    seen_paths.add(py)
                    rel = py.relative_to(root).with_suffix("")
                    qualname = ".".join(rel.parts)
                    is_package = qualname.endswith(".__init__")
                    if is_package:
                        qualname = qualname[: -len(".__init__")]
                    tree, err = _parse_file(py)
                    out_files.append(
                        SourceFile(
                            path=py,
                            qualname=qualname,
                            tree=tree,
                            parse_error=err,
                            is_package=is_package,
                        )
                    )
                    if err is not None:
                        out_errors.append(err)
                break
            # Single-module case: pkg = "a.b" and <root>/a/b.py exists.
            as_file = pkg_dir.with_suffix(".py")
            if as_file.is_file():
                if as_file in seen_paths:
                    continue
                seen_paths.add(as_file)
                tree, err = _parse_file(as_file)
                out_files.append(
                    SourceFile(
                        path=as_file,
                        qualname=pkg,
                        tree=tree,
                        parse_error=err,
                        is_package=False,
                    )
                )
                if err is not None:
                    out_errors.append(err)
                break

    return out_files, out_errors


def _parse_file(path: Path) -> tuple[ast.Module | None, Unresolved | None]:
    """SPEC §6.2: ``ast.parse`` with SyntaxError isolated to a note.
    Compile errors never bubble up — Mealie's PEP-758 files parsed
    under an older interpreter still allow the rest of the run to
    continue."""
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as e:
        note = make_unresolved(
            "parse_error",
            f"could not read file: {e}",
            SourceLocation(file=str(path), line=None, revision_id=None),
        )
        return None, note
    try:
        return ast.parse(src, filename=str(path)), None
    except SyntaxError as e:
        detail = _parse_error_detail(e)
        line = getattr(e, "lineno", None)
        note = make_unresolved(
            "parse_error",
            detail,
            SourceLocation(file=str(path), line=line, revision_id=None),
        )
        return None, note


_NEWER_SYNTAX_SIGNS = (
    "except*",  # PEP 654 (3.11)
    "match",  # PEP 634 (3.10)
    "f-string: ",  # PEP 701 (3.12)
    "except A, B",  # PEP 758
)


def _parse_error_detail(exc: SyntaxError) -> str:
    msg = str(exc) or type(exc).__name__
    text = (exc.text or "").lower()
    msg_l = msg.lower()
    if any(s.lower() in msg_l or s.lower() in text for s in _NEWER_SYNTAX_SIGNS):
        return f"parsed under older interpreter — {msg} (install a newer Python)"
    return msg[:120]


# Mild helper reused by symbols.py so it doesn't need its own regex.
_DOTTED_RE = re.compile(r"^[A-Za-z_][\w.]*$")


def is_dotted_name(name: str) -> bool:
    return bool(_DOTTED_RE.match(name))
