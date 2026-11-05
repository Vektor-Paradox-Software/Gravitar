"""SPEC-17 S1 tests: files + symbols + consts + module_graph.

Tests the AST pre-pass in isolation of the eventual column reader.
Each test either exercises one shape from §6.2 or asserts the
behavior S1 is supposed to produce for a specific fixture (F-18
through F-23).
"""

from __future__ import annotations

import ast
from pathlib import Path

from gravitar.schema.model_reader.consts import collect_module_constants
from gravitar.schema.model_reader.files import collect_model_files
from gravitar.schema.model_reader.module_graph import (
    build_module_graph,
    expand_star_import,
)
from gravitar.schema.model_reader.symbols import (
    CallRef,
    ClassRef,
    Const,
    ImportFrom,
    ImportModule,
    Unknown,
    build_module_syms,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- files.collect_model_files ---


def test_collects_py_files_without_importing(tmp_path: Path) -> None:
    """Baseline: package laid out as source files on disk; collect
    without ever triggering ``exec`` on any of them."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "a.py").write_text("y = 2\n")

    files, errors = collect_model_files([tmp_path], ["pkg"])

    assert len(files) == 2
    assert errors == []
    # Verify no module is cached in sys.modules.
    import sys

    assert "pkg" not in sys.modules
    assert "pkg.a" not in sys.modules


def test_single_module_resolution_without_init_py(tmp_path: Path) -> None:
    """A plain ``pkg = pkg.py`` (not a dir with __init__) resolves."""
    (tmp_path / "flat.py").write_text("x = 1\n")

    files, _ = collect_model_files([tmp_path], ["flat"])

    assert len(files) == 1
    assert files[0].qualname == "flat"


def test_syntax_error_is_isolated_as_parse_error_note(tmp_path: Path) -> None:
    """SPEC §6.2: a SyntaxError in one file becomes a parse_error
    note; the rest of the run continues against the remaining files."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "broken.py").write_text("def foo(:\n")
    (pkg / "ok.py").write_text("y = 2\n")

    files, errors = collect_model_files([tmp_path], ["pkg"])

    assert len(files) == 3
    assert len(errors) == 1
    assert errors[0]["reason"] == "parse_error"
    # Broken file is tracked but its tree is None.
    broken = next(f for f in files if "broken.py" in str(f.path))
    assert broken.tree is None
    assert broken.parse_error is not None


# --- symbols.build_module_syms ---


def test_from_import_with_as_rename(tmp_path: Path) -> None:
    tree = ast.parse("from sqlalchemy import String as Str\n")
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    assert isinstance(syms.bindings["Str"], ImportFrom)
    assert syms.bindings["Str"].module == "sqlalchemy"
    assert syms.bindings["Str"].attr == "String"
    assert syms.bindings["Str"].for_typing is False


def test_relative_import_resolves_to_absolute(tmp_path: Path) -> None:
    tree = ast.parse("from .widgets import Widget\n")
    syms = build_module_syms(tree, "pkg.models.__init__", tmp_path / "x.py")

    binding = syms.bindings["Widget"]
    assert isinstance(binding, ImportFrom)
    # Level 1 from pkg.models.__init__ resolves to pkg.models.widgets.
    assert binding.module == "pkg.models.widgets"


def test_parent_relative_import(tmp_path: Path) -> None:
    tree = ast.parse("from ..shared import Common\n")
    syms = build_module_syms(tree, "pkg.a.b", tmp_path / "x.py")

    assert syms.bindings["Common"].module == "pkg.shared"  # type: ignore[union-attr]


def test_star_import_recorded_separately(tmp_path: Path) -> None:
    tree = ast.parse("from .shared import *\n")
    syms = build_module_syms(tree, "pkg.models", tmp_path / "x.py")

    assert syms.star_imports == ["pkg.shared"]
    # Star imports don't bind individual names at parse time.
    assert "ExportedBase" not in syms.bindings


def test_type_checking_block_flags_imports(tmp_path: Path) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from mymod import Secret\n"
    )
    syms = build_module_syms(tree, "pkg", tmp_path / "x.py")

    binding = syms.bindings["Secret"]
    assert isinstance(binding, ImportFrom)
    assert binding.for_typing is True


def test_try_except_import_marks_first_binding_conditional(tmp_path: Path) -> None:
    """SPEC §5.2: first binding in try/except ImportError wins; it
    carries conditional=True so downstream notes can taint the
    column."""
    tree = ast.parse(
        "try:\n"
        "    from pg import JSONB as JsonType\n"
        "except ImportError:\n"
        "    from fallback import JSON as JsonType\n"
    )
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    binding = syms.bindings["JsonType"]
    assert isinstance(binding, ImportFrom)
    assert binding.module == "pg"  # first binding wins
    assert binding.conditional is True


def test_class_def_bound_as_class_ref(tmp_path: Path) -> None:
    tree = ast.parse("class Widget:\n    pass\n")
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    binding = syms.bindings["Widget"]
    assert isinstance(binding, ClassRef)
    assert binding.class_name == "Widget"


def test_literal_assignment_folded_as_const(tmp_path: Path) -> None:
    tree = ast.parse("X = 42\nY = 'hi'\nZ = [1, 2, 3]\n")
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    assert isinstance(syms.bindings["X"], Const)
    assert syms.bindings["X"].value == 42
    assert syms.bindings["Y"].value == "hi"  # type: ignore[union-attr]
    assert syms.bindings["Z"].value == [1, 2, 3]  # type: ignore[union-attr]


def test_call_assignment_bound_as_call_ref(tmp_path: Path) -> None:
    """``Base = declarative_base()`` — the reader needs to find this
    later when deciding which classes are mapped. For S1 we only
    record the call's name."""
    tree = ast.parse("Base = declarative_base()\n")
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    binding = syms.bindings["Base"]
    assert isinstance(binding, CallRef)
    assert binding.callable_name == "declarative_base"


def test_non_foldable_rhs_bound_as_unknown(tmp_path: Path) -> None:
    tree = ast.parse("X = some_func(x) + 1\n")
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    assert isinstance(syms.bindings["X"], Unknown)


def test_plain_import_strips_dotted_prefix(tmp_path: Path) -> None:
    """``import x.y`` binds the top-level name ``x``, not ``x.y``."""
    tree = ast.parse("import pkg.sub\n")
    syms = build_module_syms(tree, "mymod", tmp_path / "x.py")

    assert "pkg" in syms.bindings
    assert isinstance(syms.bindings["pkg"], ImportModule)
    assert syms.bindings["pkg"].module == "pkg.sub"


# --- Module graph: cycles + star expansion ---


def test_cycle_detection_breaks_and_emits_note(tmp_path: Path) -> None:
    """SPEC §6.2 Phase 2: cyclic imports resolve without hanging and
    produce an ``import_cycle`` note."""
    files, _ = collect_model_files([FIXTURES / "F-23"], ["models"])
    graph = build_module_graph(files)

    cycle_notes = [n for n in graph.notes if n["reason"] == "import_cycle"]
    assert cycle_notes, "cycle must produce a note"
    assert graph.cycles, "cycle list must be populated"


def test_star_import_expansion_via_literal_all(tmp_path: Path) -> None:
    files, _ = collect_model_files([FIXTURES / "F-20"], ["models"])
    graph = build_module_graph(files)

    names = expand_star_import("models.shared", graph)
    assert names is not None
    assert "ExportedBase" in names
    assert "Widget" in names


def test_star_import_expansion_without_all_returns_none(tmp_path: Path) -> None:
    """SPEC §5.2 F-20: star-import of a module without ``__all__``
    must NOT auto-resolve. ``expand_star_import`` returns None; the
    caller handles that by emitting a scoped note."""
    files, _ = collect_model_files([FIXTURES / "F-20"], ["models"])
    graph = build_module_graph(files)

    names = expand_star_import("models.private", graph)
    assert names is None


def test_module_graph_populates_constants_per_module() -> None:
    """SPEC §6 principle 3: shared constant folder. ``collect_module_
    constants`` must agree with the alembic parser's folder."""
    tree = ast.parse("TABLE = 'widgets'\nNUMS = (1, 2, 3)\n")
    consts = collect_module_constants(tree)
    assert consts["TABLE"] == "widgets"
    assert consts["NUMS"] == (1, 2, 3)


# --- End-to-end: F-18 proves parse-without-execute ---


def test_f18_parses_without_firing_sentinel(tmp_path: Path) -> None:
    """SPEC-17 §8 pass rule in miniature: parse the booby-trapped
    fixture WITHOUT the sentinel appearing on disk. ``ast.parse``
    alone never writes files, never opens sockets."""
    import os

    sentinel = tmp_path / "f18_sentinel.txt"
    os.environ["GRAVITAR_F18_SENTINEL"] = str(sentinel)
    try:
        files, _ = collect_model_files([FIXTURES / "F-18"], ["models"])
    finally:
        del os.environ["GRAVITAR_F18_SENTINEL"]

    assert len(files) == 1
    assert files[0].tree is not None  # parsed cleanly
    assert not sentinel.exists(), "collect_model_files must not execute the module"


def test_f19_parses_in_current_interpreter() -> None:
    """F-19 uses 3.10+ match statement; on 3.11+ it parses fine. The
    isolation path is tested separately by handcrafting a broken
    file. This test confirms the fixture itself is parseable here."""
    files, errors = collect_model_files([FIXTURES / "F-19"], ["models"])
    assert errors == []
    assert all(f.tree is not None for f in files)


def test_f20_graph_sees_both_submodules() -> None:
    files, _ = collect_model_files([FIXTURES / "F-20"], ["models"])
    graph = build_module_graph(files)

    assert "models" in graph.modules
    assert "models.shared" in graph.modules
    assert "models.private" in graph.modules
    # Package __init__ records both star imports.
    assert set(graph.modules["models"].star_imports) >= {"models.shared", "models.private"}


def test_f21_try_except_import_pattern_binds_first_import() -> None:
    """F-21: first binding wins, conditional=True."""
    files, _ = collect_model_files([FIXTURES / "F-21"], ["models"])
    graph = build_module_graph(files)

    syms = graph.modules["models"]
    binding = syms.bindings["JsonType"]
    assert isinstance(binding, ImportFrom)
    assert "postgresql" in binding.module
    assert binding.conditional is True
