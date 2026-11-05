"""Demote-only-when-explained. SPEC-16 Amendment v1.1 Decision #2 + R3.

A finding whose table (or column) is in-scope for an ``Unresolved`` note gets
demoted one tier (``error``/``warning`` → ``review``). The note's id goes in
``explaining_notes`` so the output can show *which* notes caused the demotion
— T3 requires that demotion never deletes and always shows the reason.

Unscoped (global) notes — ``tables=None`` — never demote: they can't claim to
explain any specific finding, so promoting them to a demotion mechanism would
mean one unreadable migration anywhere demotes every finding in the whole
repo. Decision #2 is explicit about this ("global notes lower confidence
only"). The confidence-lowering happens inside the comparator's existing
``unresolved_node`` evidence contribution, not here.

Column-level scope (``columns=["t.c"]``) is a tighter filter than table
scope (``tables=["t"]``): a note that names a column only demotes findings
on that exact column, not every finding on its table. Table-level scope
demotes any finding on that table.
"""

from __future__ import annotations

from gravitar.schema import DriftFinding, Unresolved


def demote_explained_findings(
    findings: list[DriftFinding], taint: dict[str, list[Unresolved]]
) -> list[DriftFinding]:
    """Return a new list of findings with any that are in-scope for a
    taint note demoted a tier (``kind="review"``, ``demoted_from=<orig>``,
    ``explaining_notes=[note ids]``).

    No side effects on the input list; a finding not covered by taint
    passes through unchanged (same dict identity, not a copy — so the
    rest of the pipeline doesn't accidentally break reference equality).
    """
    out: list[DriftFinding] = []
    for finding in findings:
        explaining = _notes_explaining(finding, taint)
        if not explaining:
            out.append(finding)
            continue
        out.append(_demoted(finding, explaining))
    return out


def _notes_explaining(
    finding: DriftFinding, taint: dict[str, list[Unresolved]]
) -> list[Unresolved]:
    """Which taint notes explain this finding — the test for demotion."""
    table = finding["table"]
    column = finding["column"]
    if table is None:
        # A finding without a table (broken_chain gap/cycle etc.) is a
        # revision-level claim, not a table-level one — no table-scoped
        # note can explain it. Only global notes could, and Decision #2
        # excludes those from demotion.
        return []
    table_notes = taint.get(table, [])
    if not table_notes:
        return []
    # Narrow further by column if the note names columns explicitly:
    # a note scoped to ``columns=["t.other"]`` doesn't explain a finding
    # on ``t.this``, even though it's tainting the same table.
    out: list[Unresolved] = []
    qualified = f"{table}.{column}" if column else None
    for note in table_notes:
        if note["columns"] is None:
            # Table-level scope — explains any finding on this table.
            out.append(note)
            continue
        # Column-level scope — must mention this exact column.
        if qualified and qualified in note["columns"]:
            out.append(note)
    return out


def _demoted(finding: DriftFinding, explaining: list[Unresolved]) -> DriftFinding:
    return DriftFinding(
        category=finding["category"],
        severity=finding["severity"],
        table=finding["table"],
        column=finding["column"],
        model_value=finding["model_value"],
        migration_value=finding["migration_value"],
        message=finding["message"],
        suggestion=finding["suggestion"],
        confidence=finding["confidence"],
        source=finding["source"],
        evidence=finding["evidence"],
        live_verified=finding["live_verified"],
        kind="review",
        demoted_from=finding["severity"],
        explaining_notes=[n["id"] for n in explaining],
    )
