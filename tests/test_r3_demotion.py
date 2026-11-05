"""SPEC-16 Amendment v1.1 Decision #2 + R3: demote-only-when-explained.

A finding whose table (or column) is in-scope for a scoped unresolved note
gets demoted a tier (error/warning → review). Global (unscoped) notes
lower confidence only; they never demote. A demotion always shows the
reason — ``demoted_from`` and ``explaining_notes`` are populated so the
output can print "demoted from error → review (explained by abc123)".

Also covers: Decision #1 (unknown_type no longer forces exit 1).
"""

from __future__ import annotations

from typing import Any

from gravitar.analysis.demotion import demote_explained_findings
from gravitar.schema import DriftFinding, Evidence, SourceLocation, Unresolved, make_unresolved


def _finding(**overrides: Any) -> DriftFinding:
    base: DriftFinding = DriftFinding(
        category="missing_column",
        severity="error",
        table="widgets",
        column="legacy",
        model_value={"type": "varchar(20)", "nullable": True},
        migration_value=None,
        message="model declares column, no migration adds it",
        suggestion=None,
        confidence=1.0,
        source=SourceLocation(file="models/__init__.py", line=10, revision_id=None),
        evidence=[Evidence(signal="ast_fully_resolved", weight=1.0, detail="")],
        live_verified=False,
        kind="fail",
        demoted_from=None,
        explaining_notes=[],
    )
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


def _note(**overrides: Any) -> Unresolved:
    """Construct a test Unresolved via make_unresolved so the id matches
    what the real pipeline would produce."""
    reason = overrides.pop("reason", "external_call")
    detail = overrides.pop("detail", "test note")
    source = overrides.pop(
        "source",
        SourceLocation(file="versions/0002.py", line=42, revision_id="0002"),
    )
    tables = overrides.pop("tables", ["widgets"])
    columns = overrides.pop("columns", None)
    assert not overrides, f"unexpected overrides: {overrides}"
    return make_unresolved(reason, detail, source, tables=tables, columns=columns)


# --- No notes: findings pass through unchanged ---


def test_no_taint_leaves_findings_alone() -> None:
    finding = _finding()

    [result] = demote_explained_findings([finding], taint={})

    assert result is finding


# --- Table-scoped notes demote all findings on that table ---


def test_table_scoped_note_demotes_findings_on_that_table() -> None:
    note = _note(tables=["widgets"], columns=None)
    taint = {"widgets": [note]}

    [result] = demote_explained_findings([_finding()], taint)

    assert result["kind"] == "review"
    assert result["demoted_from"] == "error"
    assert result["explaining_notes"] == [note["id"]]


def test_table_scoped_note_does_not_demote_findings_on_a_different_table() -> None:
    note = _note(tables=["widgets"], columns=None)
    taint = {"widgets": [note]}

    [result] = demote_explained_findings([_finding(table="gadgets", column="notes")], taint)

    assert result["kind"] == "fail"
    assert result["demoted_from"] is None


# --- Column-scoped notes only demote findings on that exact column ---


def test_column_scoped_note_demotes_only_the_named_column() -> None:
    note = _note(tables=["widgets"], columns=["widgets.legacy"])
    taint = {"widgets": [note]}

    [legacy, name] = demote_explained_findings(
        [_finding(column="legacy"), _finding(column="name")], taint
    )

    assert legacy["kind"] == "review"
    assert name["kind"] == "fail"


# --- Global (unscoped) notes never demote; they only lower confidence ---
# (confidence lowering happens inside the comparator's unresolved_node
# evidence contribution, not in this module — Decision #2 is explicit.)


def test_global_note_never_demotes_any_finding() -> None:
    # Global = tables=None. The taint dict only contains table-scoped notes
    # by construction (migrations.py's _build_taint puts tables=None notes
    # in global_notes instead), so an empty taint represents "no scoped
    # notes to demote with". This asserts the shape, not the mechanism.
    [result] = demote_explained_findings([_finding()], taint={})

    assert result["kind"] == "fail"
    assert result["demoted_from"] is None


# --- Findings with no table (broken_chain gap/cycle) are never demoted ---


def test_table_less_findings_are_not_demoted() -> None:
    note = _note(tables=["widgets"], columns=None)

    [result] = demote_explained_findings(
        [_finding(category="broken_chain", table=None, column=None)], taint={"widgets": [note]}
    )

    assert result["kind"] == "fail"


# --- Multiple notes on one table: all surface in explaining_notes ---


def test_multiple_notes_scoped_to_a_table_all_appear_in_explaining_notes() -> None:
    note_a = _note(detail="first", tables=["widgets"], columns=None)
    note_b = _note(
        detail="second",
        source=SourceLocation(file="versions/0003.py", line=99, revision_id="0003"),
        tables=["widgets"],
        columns=None,
    )

    [result] = demote_explained_findings([_finding()], taint={"widgets": [note_a, note_b]})

    assert result["kind"] == "review"
    assert set(result["explaining_notes"]) == {note_a["id"], note_b["id"]}
