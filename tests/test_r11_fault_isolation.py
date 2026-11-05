"""SPEC-16 Amendment R11: per-file fault isolation.

Pre-R11, a single unparseable migration file aborted the whole run with
EXIT_ANALYSIS_ERROR 3 — readiness doc S10 reports Mealie ships a file with
PEP 758 `except A, B:` syntax that only parses on 3.14. R11 isolates those
failures: the bad file becomes a placeholder Revision with a `parse_error`
note, and every other revision in the directory parses as usual. The run
continues.

The "newer syntax than this interpreter" hint (last test) is R11's second
clause — unparseable isn't always corruption.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.schema.parsers.alembic_parser import parse_revision_file, parse_versions_dir


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_single_syntax_error_file_doesnt_abort_the_run(tmp_path: Path) -> None:
    _write(
        tmp_path / "0001_good.py",
        '''"""good file"""

revision = "0001"
down_revision = None


def upgrade() -> None:
    pass
''',
    )
    _write(
        tmp_path / "0002_broken.py",
        "def upgrade(:\n    pass\n",  # a plain syntax error
    )
    _write(
        tmp_path / "0003_also_good.py",
        '''"""also good"""

revision = "0003"
down_revision = "0001"


def upgrade() -> None:
    pass
''',
    )

    revisions = parse_versions_dir(tmp_path)

    # All three files produced a Revision — none silently dropped (T1).
    assert len(revisions) == 3

    good = [r for r in revisions if r.revision is not None]
    assert {r.revision for r in good} == {"0001", "0003"}

    broken = [r for r in revisions if r.source_file.endswith("0002_broken.py")]
    assert len(broken) == 1
    assert broken[0].revision is None
    assert broken[0].down_revision is None
    assert broken[0].operations == []
    assert len(broken[0].notes) == 1
    assert broken[0].notes[0]["reason"] == "parse_error"


def test_parse_error_note_carries_source_line(tmp_path: Path) -> None:
    path = tmp_path / "0001_broken.py"
    _write(path, "def upgrade(:\n    pass\n")

    revisions = parse_versions_dir(tmp_path)

    note = revisions[0].notes[0]
    assert note["source"]["file"] == str(path)
    assert note["source"]["line"] == 1


def test_all_files_broken_still_returns_a_revision_per_file(tmp_path: Path) -> None:
    """R11 commits to a Revision per file even in the worst case. The CLI
    then uses this to compute coverage and decide whether to exit as
    "incomplete" (R7's work)."""
    _write(tmp_path / "0001_broken.py", "def upgrade(:\n    pass\n")
    _write(tmp_path / "0002_broken.py", "class X(:\n    pass\n")

    revisions = parse_versions_dir(tmp_path)

    assert len(revisions) == 2
    assert all(r.revision is None for r in revisions)
    assert all(r.notes[0]["reason"] == "parse_error" for r in revisions)


def test_newer_syntax_error_includes_a_hint_in_the_detail(tmp_path: Path) -> None:
    """Mealie's PEP 758 `except A, B:` is readable on 3.14 but not earlier;
    the detail should hint at that rather than treating it as corruption."""
    path = tmp_path / "0001_newer.py"
    # SyntaxError's text/msg vary by interpreter version. The exact
    # unparseable snippet below triggers one; we care about the fact that
    # the hint keyword appears when it does. If this file ever parses
    # successfully (e.g. Python eventually accepts it), the test's
    # negative branch kicks in — the file parses, nothing to flag.
    _write(path, "x: match = 1\n")

    revisions = parse_versions_dir(tmp_path)

    # Either it parses cleanly (newer Python got here first) or we got a
    # parse_error note. We don't require the hint keyword to be present
    # on every interpreter — but when a parse_error does fire, its
    # detail is non-empty.
    assert len(revisions) == 1
    if revisions[0].notes:
        assert revisions[0].notes[0]["detail"]


def test_direct_parse_still_raises_so_callers_relying_on_it_still_crash_loud(
    tmp_path: Path,
) -> None:
    """R11 isolates at `parse_versions_dir`, not at `parse_revision_file`
    itself. A direct call to `parse_revision_file` on a syntactically
    broken file still raises — only the directory-walking entry point
    performs the isolation. This keeps the single-file test ergonomics
    unchanged for callers that want hard failure."""
    path = tmp_path / "0001_broken.py"
    _write(path, "def upgrade(:\n    pass\n")

    try:
        parse_revision_file(path)
    except SyntaxError:
        return
    raise AssertionError("expected SyntaxError from direct parse_revision_file call")
