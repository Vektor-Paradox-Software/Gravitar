"""Deterministic identifiers for findings and the patterns behind them."""

from __future__ import annotations

import hashlib

from gravitar.schema import DriftFinding

_FINDING_ID_LENGTH = 12


def finding_id(finding: DriftFinding) -> str:
    """A stable id derived from table+column+category+revision_id.

    Stable across repeated `gravitar check` runs against an unchanged repo, so a
    user can copy an id from `check` output and pass it to `gravitar feedback
    confirm`/`dismiss` later — those commands re-run analysis and look this id
    back up rather than caching finding data (see cli.py).

    Raw-SQL fingerprints: when table and column are both
    None (the ``broken_chain`` case), multiple findings in one
    revision used to collide on the same id. Mixing in the source line as
    a tiebreaker there keeps baselines stable for every normal finding
    while preventing the collision — a revision at line 42 and the same
    revision at line 47 are different events.
    """
    parts = [
        finding["category"],
        finding["table"] or "",
        finding["column"] or "",
        finding["source"]["revision_id"] or "",
    ]
    if finding["table"] is None and finding["column"] is None:
        line = finding["source"]["line"]
        if line is not None:
            parts.append(str(line))
    key = "|".join(parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:_FINDING_ID_LENGTH]


def pattern_id(finding: DriftFinding) -> str:
    """The pattern a finding's confidence should be recalibrated against.

    A matched raw-SQL signature is specific and reusable across repos
    (e.g. "sql:add_column"), so it takes priority. Everything else falls back
    to its drift category, prefixed as ``"ast:<...>"``.

    Ast-prefixed patterns carry a
    *resolution class* suffix — ``ast:<category>:resolved`` vs
    ``ast:<category>:partial`` — so dismissing noisy partial-resolution
    findings (ones demoted to review because of taint) doesn't bleed into
    the reliability track for fully-resolved findings of the same
    category. Pre-G4, dismissing one ``type_mismatch`` partial would
    lower reliability for *every* ``type_mismatch``, including real,
    fully-resolved ones — exactly the auto-demotion hazard T7 names.
    sql_pattern_match ids stay unchanged: a matched SQL signature's
    quality is already encoded in which pattern matched.
    """
    for entry in finding["evidence"]:
        if entry["signal"] == "sql_pattern_match":
            return entry["detail"]
    resolution = "partial" if finding["kind"] == "review" else "resolved"
    return f"ast:{finding['category']}:{resolution}"
