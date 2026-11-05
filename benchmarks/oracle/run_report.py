"""SPEC-18 T0 skeleton: emit the summary.json shape without the Postgres
oracle (which is T1). The keys are the ones Section 8 declares; v2
tickets fill in the values.

Named `run_report.py` (not `report.py`) to avoid colliding with the
pre-SPEC-18 `benchmarks/report.py` from the simple-oracle harness —
both are importable alongside each other during the transition period.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path
from typing import Any

from benchmarks.oracle.config import ProjectUnit


def _gravitar_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def empty_summary(units: list[ProjectUnit], *, run_id: str | None = None) -> dict[str, Any]:
    """Build the SPEC-18 §8 summary.json shape with placeholder values.
    Keys sorted; times redacted to the run_id so byte-identical input
    produces byte-identical output (§10 reproducibility rule)."""
    return {
        "corpus_commit": "unknown",
        "gate_decision": "pending",
        "gravitar_commit": _gravitar_commit(),
        "gravitar_reader": "import",
        "postgres": "pending",  # T1 fills in the pinned digest
        "roll_up": {
            "error_fp_total": 0,
            "hard_recall": 0.0,
            "mainline_recall": 0.0,
            "warning_fp_rate": 0.0,
        },
        "run_id": run_id or _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        "units": [
            {
                "agreement": {"overall": 0.0, "by_category": {}},
                "false_positives": {"error": 0, "warning": 0, "review": 0},
                "gravitar": {
                    "coverage_full": 0,
                    "coverage_partial": 0,
                    "coverage_skipped": 0,
                    "error": 0,
                    "review": 0,
                    "warning": 0,
                },
                "oracle": {
                    "policy_changes": None,
                    "policy_filtered": None,
                    "strict_changes": None,
                },
                "project": unit.project,
                "scope_deductions": {},
                "seeded": {"hard_recall": 0.0, "mainline_recall": 0.0},
                "unit": unit.unit,
                "unrunnable": {"reason": "T0_skeleton_only"},
            }
            for unit in units
        ],
    }


def write_summary(summary: dict[str, Any], out_path: Path) -> None:
    """Write summary.json with sorted keys so two runs on identical
    inputs produce byte-identical files (SPEC-18 §10)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
