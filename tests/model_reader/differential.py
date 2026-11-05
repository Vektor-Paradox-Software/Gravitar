"""SPEC-17 §10.1 differential harness.

For every fixture the harness compares a Gravitar-produced schema
("the reader under test") against a sandboxed truth schema generated
by importing the fixture's models with real SQLAlchemy in a subprocess.

Pass rule (SPEC §10.1):
- For every column, either the reader result equals truth,
- OR the reader result carries a note scoped to that column/table.
- A mismatch with no note is a P0 defect ("silent wrong answer") —
  the exact failure mode the project exists to avoid.

The subprocess is the sandbox: anything the fixture modules do at
import time (and §10 fixtures F-18, F-23 deliberately misbehave)
affects only the child, never the test host. The parent reads JSON
over stdout — a clean protocol, no pickled objects crossing the
boundary.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gravitar.schema import Schema, Unresolved
from gravitar.schema.model_reader import ModelResult, ReaderConfig, read_models

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# --- Sandbox subprocess for the import-based truth side ---

_SANDBOX_SOURCE = textwrap.dedent(
    """\
    import json
    import sys
    sys.path.insert(0, {repo_root!r})
    sys.path.insert(0, {fixture_root!r})
    from gravitar.schema.models import load_model_schema
    base_classes = {base_classes!r}
    schema = load_model_schema({packages!r}, base_classes)
    # TypedDicts serialize as plain dicts. source_file / source_line
    # are stripped for comparison — the subprocess sees different paths
    # than the parent would under a static reader.
    out = {{}}
    for name, table in schema.items():
        out[name] = {{
            "name": table["name"],
            "columns": {{
                cname: {{
                    "name": col["name"],
                    "type": col["type"],
                    "nullable": col["nullable"],
                    "primary_key": col["primary_key"],
                }}
                for cname, col in table["columns"].items()
            }},
        }}
    print(json.dumps(out))
    """
)


def _import_truth(
    fixture_root: Path,
    packages: list[str],
    base_classes: list[str] | None,
) -> dict[str, Any]:
    """Spawn a subprocess that imports the fixture's models under real
    SQLAlchemy and returns the canonical Schema dict (comparable shape;
    source_file/source_line stripped since static vs import can't agree
    on those by construction)."""
    code = _SANDBOX_SOURCE.format(
        repo_root=str(REPO_ROOT / "src"),
        fixture_root=str(fixture_root),
        packages=packages,
        base_classes=base_classes,
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"import truth sandbox failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return _parse_sandbox_json(proc.stdout)


def _parse_sandbox_json(output: str) -> dict[str, Any]:
    """The sandbox prints exactly one JSON object on its final stdout
    line. Any leading prints (fixture warnings, SA deprecation notices)
    are stripped; the parser accepts the last non-empty line."""
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("import truth sandbox returned no output")
    return json.loads(lines[-1])  # type: ignore[no-any-return]


# --- Comparison + pass rule ---


@dataclass
class ColumnDiff:
    table: str
    column: str
    field: str  # "type" | "nullable" | "primary_key" | "missing_in_reader" | "missing_in_truth"
    truth_value: Any
    reader_value: Any


@dataclass
class DifferentialOutcome:
    """One differential run's full accounting. ``silent_mismatches`` is
    the P0 list — mismatches not explained by a scoped note. Any
    non-empty value here fails the test."""

    silent_mismatches: list[ColumnDiff]
    explained_mismatches: list[ColumnDiff]  # mismatch covered by a note
    truth_tables: set[str]
    reader_tables: set[str]

    def passed(self) -> bool:
        return not self.silent_mismatches

    def summary(self) -> str:
        parts = [
            f"tables (truth / reader): {len(self.truth_tables)} / {len(self.reader_tables)}",
            f"silent mismatches: {len(self.silent_mismatches)}",
            f"explained mismatches (note-scoped): {len(self.explained_mismatches)}",
        ]
        return " · ".join(parts)


def _notes_cover(notes: list[Unresolved], table: str | None, column: str | None) -> bool:
    """SPEC §10.1: a mismatch is explained if there is a note scoped to
    the mismatching column (narrowest) OR to its table OR global
    (``tables=None``). Column-scoped notes beat table-scoped beats
    global, but any covering note suffices for the pass rule."""
    for note in notes:
        if note["columns"] is not None and column is not None and column in note["columns"]:
            return True
        if note["tables"] is None:  # global
            return True
        if table is not None and table in note["tables"]:
            return True
    return False


def _compare_tables(
    truth: dict[str, Any],
    reader_schema: Schema,
    reader_notes: list[Unresolved],
) -> DifferentialOutcome:
    truth_tables = set(truth.keys())
    reader_tables = set(reader_schema.keys())
    silent: list[ColumnDiff] = []
    explained: list[ColumnDiff] = []

    def record(diff: ColumnDiff) -> None:
        if _notes_cover(reader_notes, diff.table or None, diff.column or None):
            explained.append(diff)
        else:
            silent.append(diff)

    # Tables in truth but missing from reader.
    for tname in truth_tables - reader_tables:
        record(
            ColumnDiff(
                table=tname,
                column="",
                field="missing_in_reader",
                truth_value=sorted(truth[tname]["columns"]),
                reader_value=None,
            )
        )
    # Tables in reader but missing from truth.
    for tname in reader_tables - truth_tables:
        record(
            ColumnDiff(
                table=tname,
                column="",
                field="missing_in_truth",
                truth_value=None,
                reader_value=sorted(reader_schema[tname]["columns"]),
            )
        )
    # Shared tables: compare column by column.
    for tname in truth_tables & reader_tables:
        t_cols = truth[tname]["columns"]
        r_cols = reader_schema[tname]["columns"]
        for cname in set(t_cols) - set(r_cols):
            record(
                ColumnDiff(
                    table=tname,
                    column=cname,
                    field="missing_in_reader",
                    truth_value=t_cols[cname],
                    reader_value=None,
                )
            )
        for cname in set(r_cols) - set(t_cols):
            record(
                ColumnDiff(
                    table=tname,
                    column=cname,
                    field="missing_in_truth",
                    truth_value=None,
                    reader_value=r_cols[cname],
                )
            )
        for cname in set(t_cols) & set(r_cols):
            t_col = t_cols[cname]
            r_col = r_cols[cname]
            for field_name in ("type", "nullable", "primary_key"):
                if t_col[field_name] != r_col[field_name]:
                    record(
                        ColumnDiff(
                            table=tname,
                            column=cname,
                            field=field_name,
                            truth_value=t_col[field_name],
                            reader_value=r_col[field_name],
                        )
                    )

    return DifferentialOutcome(
        silent_mismatches=silent,
        explained_mismatches=explained,
        truth_tables=truth_tables,
        reader_tables=reader_tables,
    )


# --- Fixture runner ---


@dataclass(frozen=True)
class FixtureSpec:
    """One differential fixture. ``packages`` is the list of model
    packages the reader (and the truth sandbox) will load from the
    fixture's root. The fixture directory layout matters: it IS the
    sys.path root inside the sandbox."""

    name: str
    root: Path
    packages: list[str]
    base_classes: list[str] | None = None


def run_differential(fixture: FixtureSpec) -> tuple[DifferentialOutcome, ModelResult]:
    """Run both sides and classify. Returns the outcome AND the
    reader's own ModelResult so a test can also check notes/coverage
    beyond just the pass rule.

    Each run clears ``sys.modules`` entries matching the fixture's
    packages (and known default roots) so a previous fixture's
    ``models`` module can't contaminate this one through Python's
    import cache. The subprocess side is already clean because every
    subprocess is a fresh interpreter.
    """
    truth = _import_truth(fixture.root, fixture.packages, fixture.base_classes)

    import sys as _sys

    _clear_module_cache(fixture.packages)
    _sys.path.insert(0, str(fixture.root))
    try:
        reader_result = read_models(
            ReaderConfig(
                packages=tuple(fixture.packages),
                # S3 reads from the fixture's filesystem root — the
                # differential harness is responsible for pointing
                # source_roots at the fixture, since the test is the
                # one that knows where the fixture lives.
                source_roots=(fixture.root,),
                base_classes=tuple(fixture.base_classes) if fixture.base_classes else None,
            )
        )
    finally:
        _sys.path.remove(str(fixture.root))
        _clear_module_cache(fixture.packages)

    outcome = _compare_tables(truth, reader_result.schema, reader_result.notes)
    return outcome, reader_result


def _clear_module_cache(packages: list[str]) -> None:
    """Pop every entry in ``sys.modules`` whose dotted name matches one
    of the fixture's packages (or starts with it, for sub-packages).
    Also clears the usual default-model roots (``models``, ``app``,
    ``app.models``) that gravitar's import loader itself tries."""
    import sys as _sys

    roots = set(packages) | {"models", "app", "app.models"}
    for mod_name in list(_sys.modules):
        if any(mod_name == r or mod_name.startswith(r + ".") for r in roots):
            _sys.modules.pop(mod_name, None)


# --- Pytest-friendly assertion ---


def assert_differential_passes(fixture: FixtureSpec) -> None:
    """SPEC §10.1 pass rule enforcer. Prints a readable diagnostic
    before raising so the test output tells the reader exactly which
    column drifted silently."""
    outcome, _ = run_differential(fixture)
    if outcome.passed():
        return
    lines = [
        f"differential P0: {fixture.name}: {outcome.summary()}",
        "silent mismatches (no scoped note explains them):",
    ]
    for diff in outcome.silent_mismatches:
        location = f"{diff.table}.{diff.column}" if diff.column else diff.table
        lines.append(
            f"  - {location} [{diff.field}] truth={diff.truth_value!r}  "
            f"reader={diff.reader_value!r}"
        )
    raise AssertionError("\n".join(lines))
