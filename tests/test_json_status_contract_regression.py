"""Regression test for the JSON ``status`` field contract.

Pins the actual-implementation contract documented in
``docs/configuration.md``: the JSON ``status`` field emits exactly
``"clean"``, ``"drift"``, or ``"error"``. Incomplete coverage is
signaled through **exit code 11** only; ``status`` continues to
reflect findings presence.

Guards against three regressions this file blocks:

1. A refactor to ``src/gravitar/output/json.py`` that starts emitting
   ``status: "incomplete"`` without matching changes to docs and the
   JSON schema. Downstream consumers that follow the documented enum
   would reject the new value as invalid.
2. The reverse — a change that drops ``"error"`` status handling or
   silently returns ``"clean"`` on error paths.
3. A change that makes ``--strict-coverage`` affect the ``status``
   field directly (today it only affects exit code 11).

The test exercises the public CLI via a subprocess so a wrapper-layer
change (click option forwarding, error-emission routing) is caught in
addition to the formatter's own contract.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

_FIXTURE_WITH_DYNAMIC_SQL = {
    "models/__init__.py": (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(50), nullable=False)\n"
    ),
    "alembic.ini": "[alembic]\nscript_location = .\n",
    "versions/0001.py": (
        '"""dynamic SQL note fixture"""\n'
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        'revision = "0001"\n'
        "down_revision = None\n"
        "def upgrade():\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("name", sa.String(length=50), nullable=False),\n'
        "    )\n"
        "    stmt = \"UPDATE widgets SET name='x'\"\n"
        "    op.execute(stmt)\n"
    ),
}


def _write_fixture(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _check(path: Path, *extra: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "gravitar", "check", str(path), "--format", "json", *extra],
        capture_output=True,
        text=True,
    )
    envelope = json.loads(proc.stdout)
    return proc.returncode, envelope


def test_status_enum_matches_documented_values() -> None:
    """The documented ``status`` enum is {clean, drift, error}. If
    this test ever has to be updated to include a new value, the
    schema and docs must change in lockstep — the spec is explicit
    that these three stay the stable public contract."""
    from gravitar.output.json import format_json

    output_clean = json.loads(format_json([], tables_analyzed=0, columns_analyzed=0))
    assert output_clean["status"] == "clean"

    # Drift path exercised in the baseline regression file.
    # Error path exercised below.


def test_strict_coverage_incomplete_emits_exit_11_without_touching_status() -> None:
    """`--strict-coverage` on a fixture with a partial-coverage note:
    exit code 11, but ``status`` MUST stay ``"clean"`` (no findings)
    or ``"drift"`` (findings present) — never ``"incomplete"``."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_fixture(root, _FIXTURE_WITH_DYNAMIC_SQL)

        exit_code, envelope = _check(root, "--strict-coverage")

        assert exit_code == 11, f"expected EXIT_INCOMPLETE=11, got {exit_code}"
        # Status must NOT be "incomplete" — the documented contract
        # routes incompleteness through the exit code, not the status.
        assert envelope["status"] in {"clean", "drift"}, (
            f"status must be 'clean' or 'drift', not {envelope['status']!r} — "
            "incomplete-coverage is signaled via exit code 11 only"
        )
        # Coverage must show the partial.
        assert envelope["coverage"]["revisions_partial"] >= 1
        # Note must be present (dynamic_sql).
        assert any(n["reason"] == "dynamic_sql" for n in envelope["notes"])


def test_strict_coverage_fully_resolved_run_stays_clean_and_exits_zero() -> None:
    """The flip side: `--strict-coverage` with full coverage must
    exit 0 and status ``"clean"``, same as without the flag."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_fixture(
            root,
            {
                "models/__init__.py": _FIXTURE_WITH_DYNAMIC_SQL["models/__init__.py"],
                "alembic.ini": _FIXTURE_WITH_DYNAMIC_SQL["alembic.ini"],
                "versions/0001.py": (
                    '"""clean create"""\n'
                    "import sqlalchemy as sa\n"
                    "from alembic import op\n"
                    'revision = "0001"\n'
                    "down_revision = None\n"
                    "def upgrade():\n"
                    '    op.create_table("widgets",\n'
                    '        sa.Column("id", sa.Integer(), primary_key=True),\n'
                    '        sa.Column("name", sa.String(length=50), nullable=False),\n'
                    "    )\n"
                ),
            },
        )

        exit_code, envelope = _check(root, "--strict-coverage")

        assert exit_code == 0
        assert envelope["status"] == "clean"


def test_error_path_emits_status_error_and_exit_2() -> None:
    """A ConfigError path (no models, no alembic.ini) must produce
    `status: "error"` and exit code 2 — not "incomplete" or any
    other alternative value."""
    with tempfile.TemporaryDirectory() as td:
        exit_code, envelope = _check(Path(td))

        assert exit_code == 2
        assert envelope["status"] == "error"
        assert envelope.get("error"), "error path must carry the error message"
