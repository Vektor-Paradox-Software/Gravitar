"""GitHub Actions annotations formatter. SPEC-16 Amendment R14 (remainder).

Emits GitHub Actions `workflow commands`_ (``::error``, ``::warning``,
``::notice``) — the single-line format GitHub's log parser picks up and
converts to inline annotations on PR diffs and the Actions UI. One
line per finding. Baselined findings are rendered as ``::notice`` when
``--show-all`` is active and skipped otherwise, so a CI with
``continue-on-error: false`` doesn't trip on known drift.

Format mapping (SPEC-16 §B tier/kind semantics):

- ``kind="fail"`` + ``severity="error"``   → ``::error``
- ``kind="fail"`` + ``severity="warning"`` → ``::warning``
- ``kind="review"``                        → ``::notice``
- ``baselined``                            → ``::notice`` (with ``--show-all``)

Workflow-command escaping (GitHub Actions documentation):

- ``%`` → ``%25``
- ``\r`` → ``%0D``
- ``\n`` → ``%0A``

Applied to the message AND to property values (file, title) that could
otherwise break the line. Notes are emitted as ``::notice`` on the
file they anchor to so Code Scanning–less projects still see them.

.. _workflow commands:
   https://docs.github.com/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions
"""

from __future__ import annotations

from gravitar.feedback.ids import finding_id
from gravitar.schema import DriftFinding, Unresolved


def _escape_value(text: str) -> str:
    """Escape a single value for a workflow-command property or body.

    GitHub's parser is line-based: a bare newline would split the
    annotation into pieces. Order matters — escape ``%`` first so the
    other escapes' ``%`` characters aren't themselves re-escaped.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command_for(finding: DriftFinding, baselined: bool) -> str:
    """Pick the workflow-command verb. A baselined finding is always
    ``notice``: it's drift the team has already accepted, surfaced for
    reviewer context, not for CI failure."""
    if baselined:
        return "notice"
    if finding["kind"] == "review":
        return "notice"
    if finding["severity"] == "error":
        return "error"
    return "warning"


def _format_finding(finding: DriftFinding, baselined: bool) -> str:
    """One workflow-command line per finding."""
    command = _command_for(finding, baselined)
    source = finding["source"]
    properties: list[str] = [f"file={_escape_value(source['file'])}"]
    if source["line"] is not None:
        properties.append(f"line={source['line']}")
    title_bits: list[str] = [finding["category"]]
    if finding["table"]:
        location = finding["table"]
        if finding["column"]:
            location = f"{location}.{finding['column']}"
        title_bits.append(location)
    title = " · ".join(title_bits)
    if baselined:
        title = f"{title} (baselined)"
    properties.append(f"title={_escape_value(title)}")
    message = finding["message"]
    if finding["suggestion"]:
        message = f"{message} — {finding['suggestion']}"
    # Finding id is the stable anchor dashboards use to track a finding
    # across runs; including it in the annotation lets a reviewer feed
    # it straight to `gravitar feedback confirm/dismiss`.
    message = f"{message} [id: {finding_id(finding)}]"
    return f"::{command} {','.join(properties)}::{_escape_value(message)}"


def _format_note(note: Unresolved) -> str:
    """Notes become ``::notice`` so they appear on the PR diff. A
    ``parse_error`` note is still a notice here — it's informational,
    not CI-failing; the exit code already fails the run."""
    source = note["source"]
    properties: list[str] = [f"file={_escape_value(source['file'])}"]
    if source["line"] is not None:
        properties.append(f"line={source['line']}")
    title = f"note · {note['reason']}"
    properties.append(f"title={_escape_value(title)}")
    return f"::notice {','.join(properties)}::{_escape_value(note['detail'])}"


def format_github(
    findings: list[DriftFinding],
    tables_analyzed: int,
    columns_analyzed: int,
    *,
    notes: list[Unresolved] | None = None,
    baselined_ids: frozenset[str] | set[str] | None = None,
) -> str:
    """Render findings + notes as GitHub Actions workflow commands.

    Returns the empty string if there's nothing to annotate. GitHub's
    log parser ignores unrecognized lines, so running this inside a
    non-Actions environment is harmless — the output just doesn't
    render inline.

    ``tables_analyzed`` / ``columns_analyzed`` are accepted for a
    consistent formatter signature but not emitted: GitHub annotations
    don't have a run-level summary slot.
    """
    _ = tables_analyzed, columns_analyzed  # unused; signature parity
    notes = notes or []
    baselined_set = baselined_ids or frozenset()
    lines: list[str] = []
    for finding in findings:
        lines.append(_format_finding(finding, baselined=finding_id(finding) in baselined_set))
    for note in notes:
        lines.append(_format_note(note))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
