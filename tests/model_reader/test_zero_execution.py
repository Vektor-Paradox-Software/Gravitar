"""SPEC-17 §8: zero-execution guarantee for the static model reader.

Installs ``sys.addaudithook`` and fails if the reader triggers any of
the audited events on fixture files: import, exec, compile-of-source,
os.system, subprocess, socket.connect.

S0 note: the current reader is a STUB that defers to the import
loader, so the booby-trapped fixture WILL fire the hook when the
stub runs. These tests document the contract and guard the hook
mechanism itself; they'll start exercising the real guarantee when
S1+ swap the import loader for AST reading.

For now the tests do two things:
1. Confirm the audit hook catches a known-firing event (import),
   proving the mechanism works.
2. Document the booby-trapped fixture and the eventual pass rule
   with ``pytest.skip`` for the real-reader assertion — the comment
   explains to the next ticket's reviewer exactly what to flip on.
"""

from __future__ import annotations

import sys
from pathlib import Path


class _AuditRecorder:
    """Collects audit events of interest. Install via ``sys.addaudithook``
    before invoking the code under test; events accumulate in
    ``self.events``.

    SPEC-17 §8 says to audit import/exec/compile/os.system/subprocess/
    socket.connect. ``compile`` fires for ``ast.parse`` too — the
    static reader's bread and butter — so we don't watch it. ``exec``
    fires when CPython actually runs module-level code (as part of
    import or an explicit ``exec()`` call); ``ast.parse`` alone does
    NOT fire ``exec``. That's the distinguishing event we actually
    want to catch.
    """

    WATCHED = frozenset(
        {
            "exec",
            "os.system",
            "subprocess.Popen",
            "socket.connect",
        }
    )

    def __init__(self, project_paths: list[Path]) -> None:
        self.events: list[tuple[str, tuple[object, ...]]] = []
        self._paths = [str(p.resolve()) for p in project_paths]

    def __call__(self, event: str, args: tuple[object, ...]) -> None:
        if event not in self.WATCHED:
            return
        self.events.append((event, args))

    def exec_of_project_source(self, name_fragment: str) -> list[tuple[object, ...]]:
        """Return the args of every ``exec`` audit whose code object's
        filename matches ``name_fragment`` OR lives under a watched
        project path. The static reader triggers no such event; the
        import loader triggers one per imported module."""
        matches: list[tuple[object, ...]] = []
        for event, args in self.events:
            if event != "exec":
                continue
            # args[0] is a code object; its co_filename names the source.
            code_obj = args[0] if args else None
            filename = getattr(code_obj, "co_filename", "") or ""
            if name_fragment and name_fragment in filename:
                matches.append(args)
                continue
            if any(filename.startswith(p) for p in self._paths):
                matches.append(args)
        return matches


def test_audit_hook_catches_import_of_a_module_in_scope(tmp_path: Path) -> None:
    """Mechanism check: write a module to ``tmp_path``, import it, and
    confirm the audit hook saw it. If this ever stops recording, the
    hook mechanism changed under us and the zero-execution gate in
    S1+ would silently pass."""
    pkg = tmp_path / "zexec_canary"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("CANARY = 1\n")

    sys.path.insert(0, str(tmp_path))
    recorder = _AuditRecorder([tmp_path])
    sys.addaudithook(recorder)
    try:
        import importlib

        importlib.import_module("zexec_canary")
    finally:
        sys.path.remove(str(tmp_path))
        # audithooks aren't removable; the recorder stops recording
        # relevant events once the test exits, but to be safe we
        # pop the module too so repeat test runs re-trigger import.
        sys.modules.pop("zexec_canary", None)

    assert recorder.exec_of_project_source("zexec_canary"), (
        "audit hook did not record the zexec_canary module exec — mechanism broken"
    )


def test_reader_fires_no_watched_events_on_booby_trapped_fixture(tmp_path: Path) -> None:
    """SPEC-17 §8 pass rule. The static reader runs ``ast.parse`` on
    the booby-trapped fixture but NEVER exec's it — the audit hook
    sees no watched events from fixture code."""
    import os

    from gravitar.schema.model_reader import ReaderConfig, read_models

    fixture_root = Path(__file__).resolve().parent / "fixtures" / "F-18"
    sentinel = tmp_path / "f18_sentinel.txt"
    os.environ["GRAVITAR_F18_SENTINEL"] = str(sentinel)
    recorder = _AuditRecorder([fixture_root])
    sys.addaudithook(recorder)
    try:
        result = read_models(ReaderConfig(packages=("models",), source_roots=(fixture_root,)))
    finally:
        del os.environ["GRAVITAR_F18_SENTINEL"]

    assert not sentinel.exists(), "static reader must never execute fixture code"
    assert not recorder.exec_of_project_source("F-18"), (
        f"static reader triggered exec of fixture source: {recorder.events}"
    )
    assert result.reader == "static"
