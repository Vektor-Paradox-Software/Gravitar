"""JSON output formatter.

schema_version 2 adds `coverage`, `notes`, and per-finding
`kind`/`demoted_from`/`explaining_notes` (see docs/configuration.md).

`schema_version` is the compatibility contract for consumers of this output — any
breaking change to the envelope shape bumps it; v2 is the first bump since 0.1.0.
"""

from __future__ import annotations

import json as _json
from typing import Any

from gravitar import __version__
from gravitar.feedback.ids import finding_id
from gravitar.schema import Coverage, DriftFinding, Unresolved

SCHEMA_VERSION = 2


def format_json(
    findings: list[DriftFinding],
    tables_analyzed: int,
    columns_analyzed: int,
    *,
    notes: list[Unresolved] | None = None,
    coverage: Coverage | None = None,
    baselined_ids: frozenset[str] | set[str] | None = None,
) -> str:
    """Render findings as the versioned JSON envelope.

    `notes`/`coverage` default to empty/zero so a caller that doesn't yet
    track them (older in-tree tests, direct library users) still gets a
    schema-valid envelope.

    SPEC-16 R14 (remainder): when ``baselined_ids`` is provided (even if
    empty), the envelope includes a ``baseline_active: true`` marker and
    each finding carries a ``baselined: bool`` field so consumers can
    distinguish new drift from pre-existing drift (ratchet workflow).
    """
    notes = notes or []
    coverage = coverage or Coverage(
        revisions_total=0,
        revisions_fully_resolved=0,
        revisions_partial=0,
        revisions_skipped=0,
    )
    baseline_active = baselined_ids is not None
    baselined_set = baselined_ids or frozenset()
    errors = sum(
        1
        for f in findings
        if f["severity"] == "error" and f["kind"] == "fail" and finding_id(f) not in baselined_set
    )
    warnings = sum(
        1
        for f in findings
        if f["severity"] == "warning" and f["kind"] == "fail" and finding_id(f) not in baselined_set
    )
    reviews = sum(
        1 for f in findings if f["kind"] == "review" and finding_id(f) not in baselined_set
    )

    def _render(finding: DriftFinding) -> dict[str, Any]:
        fid = finding_id(finding)
        out: dict[str, Any] = {**dict(finding), "finding_id": fid}
        if baseline_active:
            out["baselined"] = fid in baselined_set
        return out

    envelope: dict[str, Any] = {
        "gravitar_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "status": "clean" if not findings else "drift",
        "summary": {
            "tables_analyzed": tables_analyzed,
            "columns_analyzed": columns_analyzed,
            "errors": errors,
            "warnings": warnings,
            "reviews": reviews,
        },
        "coverage": dict(coverage),
        "findings": [_render(finding) for finding in findings],
        "notes": [dict(note) for note in notes],
    }
    if baseline_active:
        envelope["baseline_active"] = True
    return _json.dumps(envelope, indent=2) + "\n"


def format_json_error(message: str) -> str:
    """Render a ConfigError/AnalysisError as the same envelope shape, status="error".

    So a caller piping ``gravitar check --format json`` doesn't have to special-case
    stderr text to detect failure — the envelope shape is consistent whether the
    run was clean, found drift, or couldn't complete at all.
    """
    envelope: dict[str, Any] = {
        "gravitar_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "summary": {
            "tables_analyzed": 0,
            "columns_analyzed": 0,
            "errors": 0,
            "warnings": 0,
            "reviews": 0,
        },
        "coverage": {
            "revisions_total": 0,
            "revisions_fully_resolved": 0,
            "revisions_partial": 0,
            "revisions_skipped": 0,
        },
        "findings": [],
        "notes": [],
        "error": message,
    }
    return _json.dumps(envelope, indent=2) + "\n"
