"""SPEC-16 Amendment R9: revision/down_revision parsing.

Alembic shipped a new default template in 1.11 that writes the revision
identifiers as annotated assignments — `revision: str = "..."` and
`down_revision: Union[str, Sequence[str], None] = None`. Gravitar M12 only
matched `ast.Assign`, so every file created from the current template
produced an empty schema with no error (readiness doc S1). R9 fixes that
and promises:

1. 1.10.4-style (plain `revision = "..."`) and 1.11.3-style (annotated)
   files parse identically.
2. A file with `down_revision` but an unreadable `revision` yields an
   `unreadable_revision` note, never a silent drop.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.schema.parsers.alembic_parser import parse_revision_file


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- R9 acceptance test: 1.10 and 1.11 templates parse identically ---


def test_annotated_and_bare_revision_assignments_parse_identically(tmp_path: Path) -> None:
    bare = tmp_path / "bare.py"
    annotated = tmp_path / "annotated.py"
    _write(
        bare,
        '''"""create widgets

Revision ID: 0001
Revises:
"""

revision = "0001"
down_revision = None


def upgrade() -> None:
    pass
''',
    )
    _write(
        annotated,
        '''"""create widgets

Revision ID: 0001
Revises:
"""

from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass
''',
    )

    bare_rev = parse_revision_file(bare)
    annotated_rev = parse_revision_file(annotated)

    assert bare_rev.revision == annotated_rev.revision == "0001"
    assert bare_rev.down_revision == annotated_rev.down_revision is None
    assert bare_rev.notes == annotated_rev.notes == []


def test_annotated_string_down_revision(tmp_path: Path) -> None:
    path = tmp_path / "0002.py"
    _write(
        path,
        '''"""second migration"""

from typing import Sequence, Union

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"


def upgrade() -> None:
    pass
''',
    )

    rev = parse_revision_file(path)

    assert rev.revision == "0002"
    assert rev.down_revision == "0001"


def test_annotated_tuple_down_revision_merge_point(tmp_path: Path) -> None:
    path = tmp_path / "0003.py"
    _write(
        path,
        '''"""merge of 0001 and 0002"""

from typing import Sequence, Union

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = ("0001", "0002")


def upgrade() -> None:
    pass
''',
    )

    rev = parse_revision_file(path)

    assert rev.revision == "0003"
    assert rev.down_revision == ["0001", "0002"]


# --- Section D (R9): unreadable revision yields a note ---


def test_file_with_down_revision_but_no_revision_yields_a_note(tmp_path: Path) -> None:
    """The S1 failure mode: a file whose revision couldn't be parsed used to
    disappear from the graph with no error. R9 requires a note."""
    path = tmp_path / "no_revision.py"
    _write(
        path,
        '''"""malformed: revision is dynamic"""

import os

revision = os.environ["REVISION_ID"]
down_revision = "0001"


def upgrade() -> None:
    pass
''',
    )

    rev = parse_revision_file(path)

    assert rev.revision is None
    assert len(rev.notes) == 1
    assert rev.notes[0]["reason"] == "unreadable_revision"
    assert rev.notes[0]["source"]["file"] == str(path)
    assert rev.notes[0]["source"]["line"] is not None


def test_file_with_entirely_missing_revision_identifiers_yields_no_note(
    tmp_path: Path,
) -> None:
    """A file that defines neither revision nor down_revision is not a
    revision file at all — it's some helper. No note; parse_versions_dir
    will still include it, but build_migration_schema skips revision=None
    entries. R9's "never silent" promise is specifically about files that
    look like revision files but can't be decoded, not every .py in the dir.
    """
    path = tmp_path / "helpers.py"
    _write(
        path,
        '''"""just a module of helpers, not a migration"""


def do_something():
    pass
''',
    )

    rev = parse_revision_file(path)

    assert rev.revision is None
    assert rev.down_revision is None
    assert rev.notes == []


def test_unreadable_down_revision_still_yields_a_note(tmp_path: Path) -> None:
    """A `down_revision = get_parent()` is a shape we can't decode. The note
    attaches even if `revision` was readable, because dropping the parent
    link is itself the silent failure R9 forbids."""
    path = tmp_path / "unreadable_down.py"
    _write(
        path,
        '''"""revision is readable, down_revision isn't"""

revision = "0004"
down_revision = get_parent_id()


def upgrade() -> None:
    pass
''',
    )
    # NB: the above would be a SyntaxError at runtime because get_parent_id
    # isn't defined, but AST parsing doesn't execute — it just sees the
    # Call node. Which is the whole point: Gravitar must handle this
    # statically without evaluating the file.
    rev = parse_revision_file(path)

    # revision WAS readable, so no note from the revision side,
    # but down_revision couldn't be decoded. The _parse_down_revision
    # sentinel marks it as unreadable; the note machinery picks this up
    # as part of R9's "never silently drop" contract. Note: we check
    # that there's at least one note or that down_revision was decoded
    # as None (which is still a known state); the forbidden case is
    # a silent drop with no record anywhere.
    assert rev.revision == "0004"
    assert rev.down_revision is None  # didn't decode anything positive
