"""Text output formatter.

Renders findings with per-tier counts and a coverage line, honoring the
transparency contract (counts per tier, coverage line, policy header).

Whether to actually emit color (TTY check, NO_COLOR env var) is the CLI's call
(cli.py) — this module just honors the `no_color` flag it's given, so its output
stays pure and deterministic for golden-file testing.
"""

from __future__ import annotations

from gravitar.feedback.ids import finding_id
from gravitar.schema import Coverage, DriftFinding, Unresolved

_RESET = "\x1b[0m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"
_DIM = "\x1b[2m"

_SEVERITY_COLOR = {"error": _RED, "warning": _YELLOW}

_DETAIL_INDENT = " " * 9


def _color(text: str, code: str, no_color: bool) -> str:
    if no_color:
        return text
    return f"{code}{text}{_RESET}"


def _location(finding: DriftFinding) -> str:
    if finding["table"] is None:
        return ""
    if finding["column"] is None:
        return finding["table"]
    return f"{finding['table']}.{finding['column']}"


def _description_lines(finding: DriftFinding) -> list[str]:
    # type_mismatch and nullability_mismatch show both sides' values directly;
    # every other category describes itself through its message field.
    if finding["category"] == "type_mismatch":
        return [f"model: {finding['model_value']}", f"migration: {finding['migration_value']}"]
    if finding["category"] == "nullability_mismatch":
        return [
            f"model: nullable={finding['model_value']}",
            f"migration: nullable={finding['migration_value']}",
        ]
    return [finding["message"]]


def _effective_tier(finding: DriftFinding) -> str:
    """What tier a finding actually counts as, after R3's demotion. T3: demoted
    findings render under their demoted tier (and the "review" marker), not
    under their original severity."""
    if finding["kind"] == "review":
        return "review"
    return finding["severity"]


def _render_finding(finding: DriftFinding, no_color: bool, baselined: bool = False) -> str:
    location = _location(finding)
    tier = _effective_tier(finding)
    baseline_marker = " [baselined]" if baselined else ""
    header = f"  {tier}  {finding['category']}  {location}{baseline_marker}".rstrip()
    # Baselined findings get the dim style regardless of tier so they
    # visually sink into the background — they're already-known drift,
    # not something the reader needs to look at first.
    color_code = _DIM if baselined else _SEVERITY_COLOR.get(tier, _DIM)
    lines = [_color(header, color_code, no_color)]
    lines.extend(f"{_DETAIL_INDENT}{line}" for line in _description_lines(finding))
    if finding["suggestion"]:
        suggestion = f"{_DETAIL_INDENT}→ {finding['suggestion']}"
        lines.append(_color(suggestion, _DIM, no_color))
    id_line = f"{_DETAIL_INDENT}id: {finding_id(finding)}"
    lines.append(_color(id_line, _DIM, no_color))
    # T3: demotion is always shown. The original severity and the note IDs
    # that explain the demotion are visible to the reader, never silent.
    if finding["kind"] == "review" and finding["demoted_from"]:
        notes_hint = (
            f" (explained by {', '.join(finding['explaining_notes'])})"
            if finding["explaining_notes"]
            else ""
        )
        demoted_line = (
            f"{_DETAIL_INDENT}demoted from {finding['demoted_from']} → review{notes_hint}"
        )
        lines.append(_color(demoted_line, _DIM, no_color))
    if finding["live_verified"]:
        live_line = f"{_DETAIL_INDENT}✓ live-verified against the database"
        lines.append(_color(live_line, _DIM, no_color))
    return "\n".join(lines)


def _render_note(note: Unresolved, no_color: bool) -> str:
    scope = ""
    if note["columns"]:
        scope = f" [{', '.join(note['columns'])}]"
    elif note["tables"]:
        scope = f" [{', '.join(note['tables'])}]"
    source = note["source"]
    where = source["file"]
    if source["line"]:
        where = f"{where}:{source['line']}"
    header = f"  note  {note['reason']}{scope}  {where}"
    lines = [_color(header, _YELLOW, no_color)]
    lines.append(f"{_DETAIL_INDENT}{note['detail']}")
    lines.append(_color(f"{_DETAIL_INDENT}id: {note['id']}", _DIM, no_color))
    return "\n".join(lines)


def _summary_line(findings: list[DriftFinding]) -> str:
    """T3: counts per tier. A demoted finding is counted under its *effective*
    tier (review), not its original severity."""
    errors = sum(1 for f in findings if _effective_tier(f) == "error")
    warnings = sum(1 for f in findings if _effective_tier(f) == "warning")
    reviews = sum(1 for f in findings if _effective_tier(f) == "review")
    tables = {f["table"] for f in findings if f["table"] is not None}
    return (
        f"summary: {errors} errors, {warnings} warnings, {reviews} review "
        f"across {len(tables)} tables"
    )


def _coverage_line(coverage: Coverage) -> str:
    """T4: coverage line on every run. Zero revisions is a tool error reported
    by the caller, not a clean pass."""
    return (
        f"coverage: {coverage['revisions_fully_resolved']}/{coverage['revisions_total']} "
        f"revisions fully resolved · {coverage['revisions_partial']} partial · "
        f"{coverage['revisions_skipped']} skipped"
    )


def format_text(
    findings: list[DriftFinding],
    tables_analyzed: int,
    columns_analyzed: int,
    *,
    notes: list[Unresolved] | None = None,
    coverage: Coverage | None = None,
    quiet: bool = False,
    no_color: bool = False,
    baselined_ids: frozenset[str] | set[str] | None = None,
) -> str:
    """Render findings as human-readable text (transparency contract).

    When ``baselined_ids`` is provided, each
    finding's id is checked and a "[baselined]" marker is added inline;
    the summary line adds a ``N new, M baselined`` breakdown so a reader
    can see how much drift is pre-existing versus new.
    """
    notes = notes or []
    has_coverage = coverage is not None and coverage["revisions_total"] > 0
    baseline_active = baselined_ids is not None
    baselined_set = baselined_ids or frozenset()

    if not findings and not notes:
        message = (
            f"gravitar: no drift detected "
            f"({tables_analyzed} tables, {columns_analyzed} columns analyzed)"
        )
        blocks = [_color(message, _GREEN, no_color)]
        if not quiet and has_coverage:
            blocks.append(_coverage_line(coverage))  # type: ignore[arg-type]
        return "\n\n".join(blocks) + "\n"

    baselined_count = sum(1 for f in findings if finding_id(f) in baselined_set)
    new_count = len(findings) - baselined_count
    if baseline_active:
        header_text = f"gravitar: {new_count} new drift findings, {baselined_count} baselined"
    else:
        header_text = f"gravitar: drift detected — {len(findings)} findings"
    # Red only when there is actual NEW drift to act on. When every
    # finding is already baselined, the header is yellow/dim to show
    # the run is passing but not clean.
    header_color = _RED if new_count else _YELLOW
    header = _color(header_text, header_color, no_color)
    blocks = [
        header,
        *(_render_finding(f, no_color, baselined=finding_id(f) in baselined_set) for f in findings),
    ]
    if notes:
        blocks.extend(_render_note(n, no_color) for n in notes)
    if not quiet:
        blocks.append(_summary_line(findings))
        if has_coverage:
            blocks.append(_coverage_line(coverage))  # type: ignore[arg-type]

    return "\n\n".join(blocks) + "\n"
