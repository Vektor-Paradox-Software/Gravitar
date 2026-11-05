"""Benchmark harness entry point. SPEC-16 R0.

Usage:
    python benchmarks/run.py                     # run all projects in manifest.yml
    python benchmarks/run.py --out <dir>         # custom results dir
    python benchmarks/run.py --manifest <path>   # custom manifest

Writes `<out>/<timestamp>/REPORT.md` + one JSON per project. Exits
non-zero if any FP or FN was not auto-attributable AND not covered by
`triage.yml` — SPEC §Attribution: no untagged items.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import yaml

HARNESS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = HARNESS_ROOT.parent

# Make `benchmarks` importable when invoked as a script (no package
# install needed): add the repo root to sys.path before the harness
# modules load.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.classify import classify, untagged  # noqa: E402
from benchmarks.oracle import load_oracle  # noqa: E402
from benchmarks.report import render_report, write_result_json  # noqa: E402
from benchmarks.schema import Project, ProjectResult  # noqa: E402


def _load_manifest(manifest_path: Path) -> list[Project]:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    projects_raw = data.get("projects") or []
    if not isinstance(projects_raw, list):
        raise SystemExit(f"{manifest_path}: 'projects' must be a list")
    projects: list[Project] = []
    for idx, entry in enumerate(projects_raw):
        if not isinstance(entry, dict):
            raise SystemExit(f"{manifest_path}: projects[{idx}] must be a mapping")
        entry_typed = cast("dict[str, Any]", entry)
        name = entry_typed.get("name")
        if not isinstance(name, str):
            raise SystemExit(f"{manifest_path}: projects[{idx}].name required")
        project_rel = entry_typed.get("project_path")
        if not isinstance(project_rel, str):
            raise SystemExit(f"{manifest_path}: projects[{idx}].project_path required")
        oracle_rel = entry_typed.get("oracle")
        oracle_scope = entry_typed.get("oracle_scope") or {}
        projects.append(
            Project(
                name=name,
                project_path=(HARNESS_ROOT / project_rel).resolve(),
                oracle_path=(HARNESS_ROOT / oracle_rel).resolve() if oracle_rel else None,
                oracle_runner=entry_typed.get("oracle_runner", "static"),
                gravitar_config=(HARNESS_ROOT / entry_typed["gravitar_config"]).resolve()
                if entry_typed.get("gravitar_config")
                else None,
                oracle_scope_types=bool(oracle_scope.get("types", True)),
                notes=entry_typed.get("notes", ""),
            )
        )
    return projects


def _load_triage(triage_path: Path) -> dict[str, dict[str, str]]:
    if not triage_path.exists():
        return {}
    data = yaml.safe_load(triage_path.read_text(encoding="utf-8")) or {}
    entries = data.get("entries") or {}
    if not isinstance(entries, dict):
        raise SystemExit(f"{triage_path}: 'entries' must be a mapping")
    return {str(k): dict(v) for k, v in entries.items()}


def _run_gravitar(project: Project) -> tuple[dict[str, Any] | None, str | None]:
    """Run `gravitar check --format json` against the project. Returns
    ``(envelope, None)`` on a parseable JSON response (even if the exit
    code was non-zero — drift is expected), or ``(None, error_msg)``
    when the process produced no JSON (import failure, crash)."""
    cmd = [
        sys.executable,
        "-m",
        "gravitar",
        "check",
        str(project.project_path),
        "--format",
        "json",
        "--no-baseline",
    ]
    if project.gravitar_config is not None:
        cmd.extend(["--config", str(project.gravitar_config)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None, "gravitar check timed out after 300s"
    try:
        envelope = cast("dict[str, Any]", json.loads(result.stdout))
    except json.JSONDecodeError as e:
        err = result.stderr.strip() or result.stdout.strip()[:500]
        return None, f"gravitar emitted non-JSON output: {e}; stderr: {err}"
    if envelope.get("status") == "error":
        return envelope, f"gravitar reported analysis error: {envelope.get('error', '?')}"
    return envelope, None


def _run_project(project: Project, triage: dict[str, dict[str, str]]) -> ProjectResult:
    envelope, error = _run_gravitar(project)
    if envelope is None:
        return ProjectResult(
            project=project,
            status="analysis_error",
            error=error,
        )
    try:
        oracle_findings = load_oracle(project)
    except Exception as e:  # noqa: BLE001 — oracle loader errors must surface
        return ProjectResult(
            project=project,
            status="analysis_error",
            error=f"oracle load failed: {e}",
        )

    classified = classify(project, envelope, oracle_findings, triage)
    coverage = envelope.get("coverage") or {}
    return ProjectResult(
        project=project,
        status="analysis_error" if error else "ran",
        error=error,
        classified=classified,
        tainted_tables=set(coverage.get("tainted_tables") or []),
        raw_envelope=envelope,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=HARNESS_ROOT / "manifest.yml",
        help="path to the manifest file (default: benchmarks/manifest.yml)",
    )
    parser.add_argument(
        "--triage",
        type=Path,
        default=HARNESS_ROOT / "triage.yml",
        help="path to the triage file (default: benchmarks/triage.yml)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=HARNESS_ROOT / "results",
        help="base directory for results (default: benchmarks/results)",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="label for the output directory (default: current timestamp)",
    )
    parser.add_argument(
        "--allow-untagged",
        action="store_true",
        help=(
            "Exit 0 even if some FP/FN items were not auto-attributed and had "
            "no triage.yml entry. SPEC §Attribution forbids this in CI; use "
            "it only when authoring the first-ever triage pass."
        ),
    )
    args = parser.parse_args()

    projects = _load_manifest(args.manifest)
    triage = _load_triage(args.triage)
    if not projects:
        print(f"gravitar-benchmarks: no projects in {args.manifest}", file=sys.stderr)
        return 2

    results: list[ProjectResult] = []
    for project in projects:
        print(f"gravitar-benchmarks: running {project.name}", file=sys.stderr)
        results.append(_run_project(project, triage))

    label = args.label or _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%d-%H%M")
    out_dir = args.out / label
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "REPORT.md").write_text(render_report(results), encoding="utf-8")
    for result in results:
        write_result_json(result, out_dir / f"{result.project.name}.json")

    unattributed = [
        (r, item) for r in results for item in untagged(r.classified) if r.status == "ran"
    ]
    if unattributed and not args.allow_untagged:
        print(
            f"gravitar-benchmarks: {len(unattributed)} untagged FP/FN items — "
            "add triage.yml entries. SPEC §Attribution: run cannot pass with "
            "untagged items.",
            file=sys.stderr,
        )
        for r, item in unattributed[:10]:
            location = ".".join(x for x in (item.table, item.column) if x) or "—"
            print(
                f"  - {r.project.name} {item.outcome} {item.category} {location}", file=sys.stderr
            )
        return 1

    print(f"gravitar-benchmarks: report at {out_dir / 'REPORT.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
