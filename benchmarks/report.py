"""REPORT.md generator for the benchmark harness. SPEC-16 R0.

Produces one `REPORT.md` per run directory with:
- Top-of-file totals (sum across projects)
- Per-project section: outcome (ran/analysis_error), oracle scope,
  TP/FP/FN counts, cause histogram, every `other`-tagged entry listed
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from benchmarks.schema import ClassifiedFinding, ProjectResult


def _severity_mark(outcome: str) -> str:
    return {"tp": "TP", "fp_error": "FP(err)", "fp_warning": "FP(warn)", "fn": "FN"}.get(
        outcome, outcome
    )


def _render_table_row(c: ClassifiedFinding) -> str:
    # TPs don't require a cause (they're correct matches, not failures);
    # only FPs and FNs are untagged-worthy.
    if c.outcome == "tp":
        cause = "—"
    else:
        cause = c.cause or "**UNTAGGED**"
    location = ".".join(x for x in (c.table, c.column) if x) or "—"
    note = (c.note or "").replace("|", r"\|")[:80]
    return f"| {_severity_mark(c.outcome)} | {c.category} | {location} | {cause} | {note} |"


def _render_project_section(result: ProjectResult) -> str:
    p = result.project
    lines: list[str] = [f"## {p.name}", ""]
    lines.append(f"- status: `{result.status}`")
    if result.error:
        lines.append(f"- error: `{result.error}`")
    lines.append(f"- project path: `{p.project_path}`")
    lines.append(f"- oracle runner: `{p.oracle_runner}`")
    lines.append(f"- oracle_scope.types: `{p.oracle_scope_types}`")
    if p.notes:
        lines.append(f"- notes: {p.notes}")
    lines.append("")

    if result.status == "analysis_error":
        return "\n".join(lines) + "\n"

    lines.append(
        f"**TP {result.tp}  ·  FP(err) {result.fp_error}  ·  "
        f"FP(warn) {result.fp_warning}  ·  FN {result.fn}**"
    )
    histogram = result.cause_histogram()
    if histogram:
        bits = ", ".join(f"{k}={v}" for k, v in sorted(histogram.items()))
        lines.append(f"causes: {bits}")
    lines.append("")

    if result.classified:
        lines.append("| outcome | category | location | cause | note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for c in result.classified:
            lines.append(_render_table_row(c))
        lines.append("")

    others = [c for c in result.classified if c.cause == "other"]
    if others:
        lines.append("### `other`-tagged items (new causes to attribute)")
        lines.append("")
        for c in others:
            location = ".".join(x for x in (c.table, c.column) if x) or "—"
            lines.append(
                f"- {_severity_mark(c.outcome)} {c.category} `{location}` — {c.note or ''}"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def render_report(results: list[ProjectResult], generated_at: _dt.datetime | None = None) -> str:
    """Build the full REPORT.md text from a list of per-project results.
    Deterministic: inputs → identical bytes, which lets a committed
    ``benchmarks/results/baseline/REPORT.md`` be diffed against a fresh
    run to see exactly what changed."""
    generated_at = generated_at or _dt.datetime.now(tz=_dt.UTC)
    totals = {
        "tp": sum(r.tp for r in results if r.status == "ran"),
        "fp_error": sum(r.fp_error for r in results if r.status == "ran"),
        "fp_warning": sum(r.fp_warning for r in results if r.status == "ran"),
        "fn": sum(r.fn for r in results if r.status == "ran"),
    }
    errored = [r for r in results if r.status == "analysis_error"]

    out: list[str] = [
        "# Gravitar benchmark report",
        "",
        f"- generated: {generated_at.isoformat(timespec='seconds')}",
        (
            f"- projects: {len(results)}  "
            f"(ran: {len(results) - len(errored)}, analysis_error: {len(errored)})"
        ),
        f"- **totals**: TP {totals['tp']}  ·  FP(err) {totals['fp_error']}  "
        f"·  FP(warn) {totals['fp_warning']}  ·  FN {totals['fn']}",
        "",
    ]
    out.append("## Projects")
    out.append("")
    for result in results:
        out.append(_render_project_section(result))
    return "\n".join(out) + "\n"


def write_result_json(result: ProjectResult, path: Path) -> None:
    """Write a per-project JSON next to the report. Used to feed
    downstream tooling (SPEC-15 harness) or to diff two runs
    programmatically."""
    import json

    payload = {
        "project": result.project.name,
        "status": result.status,
        "error": result.error,
        "tp": result.tp,
        "fp_error": result.fp_error,
        "fp_warning": result.fp_warning,
        "fn": result.fn,
        "classified": [
            {
                "outcome": c.outcome,
                "category": c.category,
                "table": c.table,
                "column": c.column,
                "cause": c.cause,
                "note": c.note,
                "finding_id": c.finding_id,
            }
            for c in result.classified
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
