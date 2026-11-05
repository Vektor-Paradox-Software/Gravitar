"""Shared helpers for the full-pipeline integration suite.

Named `pipeline_helpers.py`, not `conftest.py`: neither `tests/` nor this
directory has an `__init__.py`, so pytest's default import mode registers
every conftest.py as the bare top-level module name "conftest" — a second
file with that name here would collide with `tests/conftest.py` (whichever
loads first wins, breaking `from conftest import ...` everywhere else in the
suite). Nothing here needs conftest.py's auto-discovery/fixture-injection
behavior anyway (the suite's `tests/conftest.py` is imported explicitly too,
via `from conftest import ...`, not pytest fixtures) — plain module-level
functions, imported explicitly by each test file, are all this needs.

Every scenario runs `gravitar check`/`gravitar feedback` via a real subprocess
rather than in-process (CliRunner or direct function calls), for two reasons:

1. Django's app registry is a process-global singleton that can't be
   reconfigured with a different app list within one process (established in
   Milestone 10's test suite) — this integration suite exercises several
   different Django fixtures across Scenarios A and C, so in-process
   invocation would make later fixtures fail against the first one's app
   config. A fresh interpreter per invocation sidesteps that entirely.
2. It's the most faithful possible test of "the pipeline running end to end"
   — the actual thing Section 1 says this milestone needs to prove — since
   it exercises the exact code path a real user's terminal invocation does,
   config resolution and all, not just the library functions underneath it.

Every fixture is copied into `tmp_path` before use, never analyzed in place:
`gravitar feedback confirm/dismiss` (Scenario C) creates `.gravitar/
feedback.sqlite` under the repo root, and `tests/integration/full_pipeline/
fixtures/` is a tracked directory that must never get a stray file written
into it by a test run (Section 6: each test needs an isolated, temporary
feedback store, never the developer's real one — copying into tmp_path is
what makes that isolation automatic, the same pattern established in
Milestone 9's test_cli.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def copy_fixture(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


def run_check(repo_path: Path) -> dict[str, Any]:
    """Run `gravitar check --format json` against repo_path and return the
    parsed envelope. Never raises on drift/config/analysis exit codes — the
    envelope's own `status`/`findings` fields carry that information, exactly
    as a real caller would consume them.
    """
    result = subprocess.run(
        [sys.executable, "-m", "gravitar", "check", str(repo_path), "--format", "json"],
        capture_output=True,
        text=True,
    )
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def run_feedback(
    repo_path: Path, verdict: str, finding_id: str
) -> subprocess.CompletedProcess[str]:
    """`verdict` is "confirm" or "dismiss"."""
    return subprocess.run(
        [sys.executable, "-m", "gravitar", "feedback", verdict, finding_id, str(repo_path)],
        capture_output=True,
        text=True,
    )


def run_feedback_stats(repo_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "gravitar", "feedback", "stats", str(repo_path)],
        capture_output=True,
        text=True,
    )
