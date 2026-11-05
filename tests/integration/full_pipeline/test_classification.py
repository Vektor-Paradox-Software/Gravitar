"""Scenario B: signature classification
feeding the schema state, Alembic only — Django has no raw-SQL equivalent
(see test_parity.py's module docstring for the full matrix and why).

Pre-R12 (M12 original): a migration whose raw-SQL ADD COLUMN matched a known
pattern produced a higher-confidence warning finding than an unrecognized
raw SQL statement, with a `sql_pattern_match` entry in the evidence trail.

Post-R12 (SPEC-16 amendment): a recognized DDL pattern is actually applied
to the schema state, so no finding is emitted at all (the drift that would
have signalled is now resolved by the apply). DML statements like
``UPDATE ...`` are classified as inert — also no finding. This is a
genuine behavior change: Scenario B's original claim ("matched scores
higher than unmatched") no longer holds because neither path emits a
finding at all.

The *amendment-era* claim this file asserts is weaker but more accurate:
a matched ADD COLUMN modifies schema state so the migration-side column
appears in the comparator's view of the world. The best test of that is
not a finding count — it's the resulting schema state.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_helpers import copy_fixture, run_check


def test_matched_add_column_resolves_cleanly(tmp_path: Path) -> None:
    """R12: ``ALTER TABLE widgets ADD COLUMN flag BOOLEAN`` is matched and
    applied. The model matches (minus `flag`, which doesn't exist on the
    model side), so the run produces one orphaned_column finding for
    `flag` — exactly what native op.add_column would have produced."""
    repo = copy_fixture("raw_sql_known_pattern", tmp_path)

    payload = run_check(repo)

    # The migration adds `widgets.flag` via raw SQL, model doesn't declare
    # it — so after R12 applies the recovered DDL, this looks exactly like
    # a native op.add_column on an undeclared column: orphaned_column.
    orphaned = [
        f
        for f in payload["findings"]
        if f["category"] == "orphaned_column" and f["column"] == "flag"
    ]
    assert len(orphaned) == 1


def test_unrecognized_dml_is_inert(tmp_path: Path) -> None:
    """R12: ``UPDATE widgets SET id = id`` is DML — inert. Pre-R12 this was
    a broken_chain warning at 0.3 confidence; now it's silent (correctly)."""
    repo = copy_fixture("raw_sql_unrecognized", tmp_path)

    payload = run_check(repo)

    # No findings — the DML doesn't modify schema, and the model matches
    # the migration's created table. Also no dynamic_sql note (the string
    # was fully visible, just inert).
    assert payload["findings"] == []
    assert not any(n["reason"] == "dynamic_sql" for n in payload["notes"])
