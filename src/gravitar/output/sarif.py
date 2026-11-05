"""SARIF 2.1.0 output formatter. SPEC-16 Amendment R14.

SARIF (Static Analysis Results Interchange Format, OASIS 2.1.0) is the
format GitHub Code Scanning, Azure DevOps, and most CI dashboards
consume natively. Emitting SARIF alongside Gravitar's own JSON lets a
run flow into those dashboards without a translation layer.

Mapping Gravitar → SARIF:

- ``DriftCategory``        → ``result.ruleId`` (one rule per category)
- ``severity`` + ``kind``  → ``result.level`` and ``result.kind``:
                             - kind="fail", severity="error"   → level=error, kind=fail
                             - kind="fail", severity="warning" → level=warning, kind=fail
                             - kind="review"                   → level=note, kind=review
- ``confidence``           → ``result.rank`` (×100 per the readiness doc's
                             "``rank`` 0-100 confidence" bullet)
- ``finding_id``           → ``result.partialFingerprints.findingId``
- ``source``               → ``result.locations[0].physicalLocation``
- ``evidence[*]``          → part of the message text (concatenated as
                             "signal: detail" lines) so a reviewer can
                             read the reasoning in-dashboard
- ``Unresolved`` notes     → ``run.invocations[0].toolExecutionNotifications``,
                             SARIF's dedicated channel for things the tool
                             couldn't interpret
- ``Coverage``             → ``run.properties`` (SARIF has no first-class
                             coverage field; properties is the escape hatch)

Baseline/ratchet mode and ``--show-all`` are deferred.
"""

from __future__ import annotations

import json as _json
from typing import Any

from gravitar import __version__
from gravitar.feedback.ids import finding_id
from gravitar.schema import Coverage, DriftFinding, Unresolved

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json"
)

# One SARIF rule per Gravitar drift category, so GitHub Code Scanning can
# filter by rule and dashboards can show category-level histograms.
_RULES: list[dict[str, Any]] = [
    {
        "id": "missing_table",
        "name": "MissingTable",
        "shortDescription": {"text": "A model declares a table no migration creates."},
    },
    {
        "id": "missing_column",
        "name": "MissingColumn",
        "shortDescription": {"text": "A model declares a column no migration adds."},
    },
    {
        "id": "type_mismatch",
        "name": "TypeMismatch",
        "shortDescription": {"text": "A column's type differs between model and migration."},
    },
    {
        "id": "nullability_mismatch",
        "name": "NullabilityMismatch",
        "shortDescription": {
            "text": "A column's nullable attribute differs between model and migration."
        },
    },
    {
        "id": "orphaned_column",
        "name": "OrphanedColumn",
        "shortDescription": {"text": "A migration adds a table/column no model declares."},
    },
    {
        "id": "orphaned_reference",
        "name": "OrphanedReference",
        "shortDescription": {
            "text": "A raw-SQL op references a table/column not in the schema state."
        },
    },
    {
        "id": "broken_chain",
        "name": "BrokenChain",
        "shortDescription": {
            "text": "A gap, cycle, or disagreement in the migration revision history."
        },
    },
    {
        "id": "unknown_type",
        "name": "UnknownType",
        "shortDescription": {"text": "A column type could not be normalized on at least one side."},
    },
]


def _result_level(finding: DriftFinding) -> str:
    """SARIF 2.1.0 ``level`` field. R3-demoted findings land on ``note``;
    everything else maps straight from the finding's severity."""
    if finding["kind"] == "review":
        return "note"
    return finding["severity"]  # "error" or "warning"


def _result_kind(finding: DriftFinding) -> str:
    """SARIF ``kind``: ``fail`` for a problem the user should act on;
    ``review`` for a demoted finding that warrants human review but
    isn't a hard failure (SARIF spec: "warrants human review")."""
    return finding["kind"]  # already SARIF-aligned by R3's naming


def _result_message(finding: DriftFinding) -> dict[str, str]:
    """Concatenate the finding's own message with its evidence trail
    so a dashboard reviewer can see the reasoning without expanding
    into a separate properties blob."""
    lines = [finding["message"]]
    for entry in finding["evidence"]:
        lines.append(f"- {entry['signal']}: {entry['detail']}")
    if finding["suggestion"]:
        lines.append(f"Suggestion: {finding['suggestion']}")
    if finding["kind"] == "review" and finding["demoted_from"]:
        lines.append(
            f"(Demoted from {finding['demoted_from']} → review by notes: "
            f"{', '.join(finding['explaining_notes']) or 'see run notifications'})"
        )
    return {"text": "\n".join(lines)}


def _result_location(finding: DriftFinding) -> dict[str, Any]:
    source = finding["source"]
    region: dict[str, Any] = {}
    if source["line"] is not None:
        region["startLine"] = source["line"]
    location: dict[str, Any] = {
        "physicalLocation": {
            "artifactLocation": {"uri": source["file"]},
        }
    }
    if region:
        location["physicalLocation"]["region"] = region
    return location


def _finding_to_result(finding: DriftFinding, baseline_state: str | None = None) -> dict[str, Any]:
    """Build the SARIF ``result`` object. When ``baseline_state`` is set
    (``"unchanged"`` for a baselined finding, ``"new"`` otherwise), SARIF's
    native ``baselineState`` field is included — GitHub Code Scanning and
    other dashboards use it to style baselined findings differently."""
    result: dict[str, Any] = {
        "ruleId": finding["category"],
        "level": _result_level(finding),
        "kind": _result_kind(finding),
        "rank": finding["confidence"] * 100,
        "message": _result_message(finding),
        "locations": [_result_location(finding)],
        "partialFingerprints": {"findingId": finding_id(finding)},
    }
    if baseline_state is not None:
        result["baselineState"] = baseline_state
    return result


def _note_to_notification(note: Unresolved) -> dict[str, Any]:
    """SARIF notifications are the right channel for things the tool
    could NOT interpret — exactly the role of an Unresolved note. Each
    note becomes a notification with ``level: warning`` (parse errors
    are ``error``) and the scope / detail preserved in properties."""
    level = "error" if note["reason"] == "parse_error" else "warning"
    properties: dict[str, Any] = {"reason": note["reason"]}
    if note["tables"] is not None:
        properties["tables"] = note["tables"]
    if note["columns"] is not None:
        properties["columns"] = note["columns"]
    source = note["source"]
    location: dict[str, Any] = {
        "physicalLocation": {
            "artifactLocation": {"uri": source["file"]},
        }
    }
    if source["line"] is not None:
        location["physicalLocation"]["region"] = {"startLine": source["line"]}
    return {
        "level": level,
        "message": {"text": note["detail"]},
        "locations": [location],
        "properties": properties,
    }


def format_sarif(
    findings: list[DriftFinding],
    tables_analyzed: int,
    columns_analyzed: int,
    *,
    notes: list[Unresolved] | None = None,
    coverage: Coverage | None = None,
    baselined_ids: frozenset[str] | set[str] | None = None,
) -> str:
    """Render findings + notes + coverage as a SARIF 2.1.0 log, suitable
    for ``gh api repos/O/R/code-scanning/sarifs`` upload or any OASIS-
    compliant consumer.

    SPEC-16 R14 (remainder): when ``baselined_ids`` is provided, each
    result carries SARIF's ``baselineState`` (``"unchanged"`` for a
    baselined finding, ``"new"`` otherwise) so GitHub Code Scanning
    styles pre-existing drift differently from new drift.
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

    def _result_for(finding: DriftFinding) -> dict[str, Any]:
        if not baseline_active:
            return _finding_to_result(finding)
        state = "unchanged" if finding_id(finding) in baselined_set else "new"
        return _finding_to_result(finding, baseline_state=state)

    log: dict[str, Any] = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "gravitar",
                        "version": __version__,
                        "informationUri": "https://github.com/vektor-paradox-software/Gravitar",
                        "rules": _RULES,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "toolExecutionNotifications": [
                            _note_to_notification(note) for note in notes
                        ],
                    }
                ],
                "results": [_result_for(f) for f in findings],
                "properties": {
                    "tablesAnalyzed": tables_analyzed,
                    "columnsAnalyzed": columns_analyzed,
                    "coverage": dict(coverage),
                    **({"baselineActive": True} if baseline_active else {}),
                },
            }
        ],
    }
    return _json.dumps(log, indent=2) + "\n"
