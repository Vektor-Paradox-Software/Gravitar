"""SPEC-15 §6 — real-world validation harness.

Runs ``gravitar check`` against a target repo and emits a structured
report for human review. The harness does NOT automate TP/FP
classification — that judgment requires reading the actual migration
and model code against each finding (§4.1, §4.2). It automates the
mechanical parts: fetching the repo, running the check, applying
optional seeded-drift scenarios, bucketing findings by category and
confidence, timing the run.

Usage::

    scripts/validate_real_world.py \\
        --repo /local/path/or/https://github.com/org/repo.git \\
        --seed-drift seeds.yml \\
        --report-out report.md

Produces ``<report-out>`` (Markdown) and ``<report-out>.json``
(the parsed envelope + harness metadata). Clones to a temp directory
when ``--repo`` is a URL; uses the local checkout read-only when it's a
path.

Seed-drift scenario file (YAML, optional)::

    mutations:
      - type: add_model_column
        model_file: app/models/widget.py
        after_line: "deleted_at = Column(DateTime(), nullable=True)"
        column: "flag = Column(Boolean, nullable=False, server_default='false')"
        description: "model declares flag; no migration adds it"

Supported mutation types are deliberately narrow — ``add_model_column``
is the one §4.2 explicitly calls out as "the realistic drift scenario";
other patterns from SPEC-13 can be seeded by hand-editing the fork
before running the harness. Keeping the harness small is the point: it
automates fetch + run + report, not the judgment calls on top of them.

Non-goals (SPEC-15 §2):
- Not a replacement for the fixture suite.
- Not fuzzing.
- Does not automate TP/FP classification.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --- Repo acquisition ---


def _looks_like_url(target: str) -> bool:
    """A string names a remote repo if it's a URL or a known shorthand.
    Local paths with http in them would be unusual, but err toward
    treating as path if it exists on disk."""
    if Path(target).exists():
        return False
    return target.startswith(("http://", "https://", "git@", "git://", "ssh://"))


def _resolve_repo(target: str, work_dir: Path) -> Path:
    """Return a local path to the target repo. Clones to ``work_dir``
    when ``target`` is a URL; otherwise just resolves the given path."""
    if _looks_like_url(target):
        dest = work_dir / "repo"
        print(f"gravitar-validate: cloning {target} -> {dest}", file=sys.stderr)
        subprocess.run(
            ["git", "clone", "--depth", "1", target, str(dest)],
            check=True,
        )
        return dest
    path = Path(target).resolve()
    if not path.exists():
        raise FileNotFoundError(f"--repo {target!r}: no such path and not a recognized URL")
    if not path.is_dir():
        raise ValueError(f"--repo {target!r}: not a directory")
    return path


# --- Seed-drift ---


@dataclass
class Mutation:
    """One seeded-drift scenario (SPEC-15 §4.2). ``description`` is
    carried through to the report so the reviewer can map each surfaced
    finding back to the mutation that planted it."""

    mutation_type: str
    model_file: str
    description: str
    column: str = ""
    after_line: str = ""


def _load_mutations(path: Path) -> list[Mutation]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a top-level mapping")
    raw = data.get("mutations") or []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'mutations' must be a list")
    out: list[Mutation] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: mutations[{idx}] must be a mapping")
        if "type" not in entry or "model_file" not in entry:
            raise ValueError(f"{path}: mutations[{idx}] requires 'type' and 'model_file'")
        out.append(
            Mutation(
                mutation_type=str(entry["type"]),
                model_file=str(entry["model_file"]),
                description=str(entry.get("description", "")),
                column=str(entry.get("column", "")),
                after_line=str(entry.get("after_line", "")),
            )
        )
    return out


def _apply_mutation(repo: Path, mutation: Mutation) -> None:
    """Mutate the repo in-place to plant a seed. Only ``add_model_column``
    is implemented in-harness; other mutation types are expected to be
    hand-authored on the fork before invoking the harness.
    """
    if mutation.mutation_type != "add_model_column":
        raise NotImplementedError(
            f"seed-drift mutation {mutation.mutation_type!r}: not implemented in-harness. "
            "Hand-author the mutation on the fork before running the harness. "
            "See SPEC-15 §4.2."
        )
    model_path = repo / mutation.model_file
    if not model_path.exists():
        raise FileNotFoundError(
            f"seed-drift mutation targets {mutation.model_file!r} but path does not exist in repo"
        )
    text = model_path.read_text(encoding="utf-8")
    if mutation.after_line and mutation.after_line in text:
        # Insert `column` immediately after the matched line, preserving
        # the anchor's own leading whitespace on the new line so the
        # inserted column stays inside the same class body.
        anchor = mutation.after_line
        indent = anchor[: len(anchor) - len(anchor.lstrip())]
        insertion = f"{anchor}\n{indent}{mutation.column}"
        text = text.replace(anchor, insertion, 1)
    else:
        # No anchor given — append at end-of-file inside the models module.
        text = text.rstrip() + f"\n    {mutation.column}\n"
    model_path.write_text(text, encoding="utf-8")


# --- gravitar check ---


@dataclass
class RunResult:
    exit_code: int
    duration_s: float
    envelope: dict[str, Any] = field(default_factory=dict)
    raw_stdout: str = ""
    raw_stderr: str = ""


def _run_gravitar(repo: Path, timeout_s: int = 300) -> RunResult:
    cmd = [
        sys.executable,
        "-m",
        "gravitar",
        "check",
        str(repo),
        "--format",
        "json",
        "--no-baseline",
    ]
    print(f"gravitar-validate: running {' '.join(cmd)}", file=sys.stderr)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - t0
        return RunResult(
            exit_code=-1,
            duration_s=duration,
            raw_stderr=f"timeout after {timeout_s}s: {e}",
        )
    duration = time.monotonic() - t0
    try:
        envelope = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        envelope = {}
    return RunResult(
        exit_code=proc.returncode,
        duration_s=duration,
        envelope=envelope,
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
    )


# --- Report ---


def _confidence_bucket(c: float) -> str:
    """Confidence tier bands: ≥0.9 high, 0.7–0.9 medium, <0.7 low — the
    useful reviewer buckets, not continuous confidence inspection."""
    if c >= 0.9:
        return ">=0.9"
    if c >= 0.7:
        return "0.7-0.9"
    return "<0.7"


def _histogram_counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(f.get(key, "") for f in items))


def _build_report(
    repo_label: str,
    seed_file: str | None,
    mutations: list[Mutation],
    result: RunResult,
) -> dict[str, Any]:
    envelope = result.envelope
    findings = envelope.get("findings", []) if isinstance(envelope, dict) else []
    notes = envelope.get("notes", []) if isinstance(envelope, dict) else []
    coverage = envelope.get("coverage", {}) if isinstance(envelope, dict) else {}
    summary = envelope.get("summary", {}) if isinstance(envelope, dict) else {}

    confidence_hist = Counter(_confidence_bucket(float(f.get("confidence", 0.0))) for f in findings)
    tier_hist = Counter(
        ("review" if f.get("kind") == "review" else f.get("severity", "error")) for f in findings
    )

    return {
        "harness_version": 1,
        "generated_at": _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds"),
        "repo": repo_label,
        "seed_drift": {
            "scenario_file": seed_file,
            "mutations_applied": [
                {
                    "type": m.mutation_type,
                    "model_file": m.model_file,
                    "description": m.description,
                }
                for m in mutations
            ],
        },
        "run": {
            "exit_code": result.exit_code,
            "duration_s": round(result.duration_s, 2),
            "stderr_tail": result.raw_stderr[-500:] if result.raw_stderr else "",
        },
        "envelope_summary": summary,
        "coverage": coverage,
        "distribution": {
            "by_category": _histogram_counts(findings, "category"),
            "by_tier": dict(tier_hist),
            "by_confidence_bucket": dict(confidence_hist),
            "note_reasons": _histogram_counts(notes, "reason"),
        },
        "findings_count": len(findings),
        "notes_count": len(notes),
        "findings": findings,
        "notes": notes,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Gravitar real-world validation",
        "",
        f"- generated: {report['generated_at']}",
        f"- repo: `{report['repo']}`",
        f"- run exit code: `{report['run']['exit_code']}`  "
        f"·  duration: {report['run']['duration_s']}s",
        f"- findings: **{report['findings_count']}**  ·  notes: {report['notes_count']}",
        "",
    ]

    if report["seed_drift"]["scenario_file"]:
        lines.append("## Seed-drift")
        lines.append("")
        lines.append(f"Scenario file: `{report['seed_drift']['scenario_file']}`")
        lines.append("")
        for m in report["seed_drift"]["mutations_applied"]:
            lines.append(f"- `{m['type']}` → `{m['model_file']}` — {m['description']}")
        lines.append("")

    lines.append("## Distribution")
    lines.append("")
    for group, counts in report["distribution"].items():
        if not counts:
            continue
        lines.append(f"**{group}**")
        for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"- {k or '(none)'}: {v}")
        lines.append("")

    lines.append("## Coverage")
    lines.append("")
    for k, v in report["coverage"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append("## Findings (for manual TP/FP review)")
    lines.append("")
    lines.append(
        "The harness does NOT classify these. Each row is a candidate "
        "for human review per SPEC-15 §4.1. For each finding, read the "
        "cited migration + model code to decide TP / FP, then record "
        "the pattern in VALIDATION-REPORT.md."
    )
    lines.append("")
    if not report["findings"]:
        lines.append("_No findings._")
    else:
        lines.append("| finding_id | category | tier | confidence | location | message |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for f in report["findings"]:
            tier = "review" if f.get("kind") == "review" else f.get("severity", "")
            location = ".".join(x for x in (f.get("table"), f.get("column")) if x) or "—"
            message = (f.get("message") or "").replace("|", r"\|")[:80]
            lines.append(
                f"| `{f.get('finding_id', '')}` | {f.get('category', '')} | "
                f"{tier} | {f.get('confidence', 0):.2f} | {location} | {message} |"
            )
        lines.append("")

    if report["notes"]:
        lines.append("## Notes (walker couldn't fully resolve)")
        lines.append("")
        lines.append("| reason | scope | file:line |")
        lines.append("| --- | --- | --- |")
        for n in report["notes"]:
            tables = ",".join(n.get("tables") or []) or "(global)"
            src = n.get("source") or {}
            where = f"{src.get('file', '')}:{src.get('line', '')}"
            lines.append(f"| {n.get('reason', '')} | {tables} | {where} |")
        lines.append("")

    return "\n".join(lines) + "\n"


# --- Entry point ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="path or git URL to the target repository")
    parser.add_argument(
        "--seed-drift",
        type=Path,
        default=None,
        help="optional YAML scenario file (SPEC-15 §4.2) applied before the check",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        required=True,
        help="output path for the Markdown report; a .json sibling is also written",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="per-check timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help="when cloning, leave the worktree in place after the run (default: delete)",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="gravitar-validate-") as tmp_name:
        work_dir = Path(tmp_name)
        try:
            repo = _resolve_repo(args.repo, work_dir)
        except (FileNotFoundError, ValueError, subprocess.CalledProcessError) as e:
            print(f"gravitar-validate: cannot resolve repo: {e}", file=sys.stderr)
            return 2

        mutations: list[Mutation] = []
        if args.seed_drift:
            mutations = _load_mutations(args.seed_drift)
            # Seed-drift mutates the repo; when the repo is a local path
            # we clone to the temp dir first so we never touch the user's
            # checkout.
            if not _looks_like_url(args.repo):
                mirror = work_dir / "mirror"
                shutil.copytree(repo, mirror)
                repo = mirror
            print(
                f"gravitar-validate: applying {len(mutations)} seed-drift mutation(s)",
                file=sys.stderr,
            )
            for m in mutations:
                _apply_mutation(repo, m)

        result = _run_gravitar(repo, timeout_s=args.timeout)
        report = _build_report(
            repo_label=str(args.repo),
            seed_file=str(args.seed_drift) if args.seed_drift else None,
            mutations=mutations,
            result=result,
        )

        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(_render_markdown(report), encoding="utf-8")
        json_out = args.report_out.with_suffix(args.report_out.suffix + ".json")
        json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        if args.keep_worktree and _looks_like_url(args.repo):
            # Move the clone outside the temp dir so it survives.
            kept = args.report_out.parent / "worktree"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.move(str(work_dir / "repo"), str(kept))
            print(f"gravitar-validate: kept worktree at {kept}", file=sys.stderr)

        print(f"gravitar-validate: wrote {args.report_out} and {json_out}", file=sys.stderr)

    # The harness exits 0 whether the check found drift or not — the point
    # is to produce a report for manual review, not to gate CI. A tool
    # error (exit 2/3) when we couldn't even run the check still exits 0
    # here because the report captures that outcome; the human reading
    # the report is the gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
