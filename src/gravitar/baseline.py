"""Baseline/ratchet storage. SPEC-16 Amendment R14 (remainder).

A baseline is a snapshot of the finding ids present at a known-good point
in time. Each subsequent ``gravitar check`` compares against it: findings
in the baseline are "unchanged" and do not fail CI, findings not in the
baseline are "new" and do. The pattern lets a drifty project onboard
Gravitar without turning its CI red on day one — teams accept the
current state as the floor, then ratchet from there.

Stored as JSON at ``<repo>/.gravitar/baseline.json`` by default. Finding
identity is :func:`gravitar.feedback.ids.finding_id` (stable across
runs; see Section 3.1), so a baseline survives refactors that don't
move the drift itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASELINE_RELATIVE_PATH = Path(".gravitar") / "baseline.json"
BASELINE_VERSION = 1


def baseline_path_for(repo_path: Path) -> Path:
    return repo_path / BASELINE_RELATIVE_PATH


def load_baseline(baseline_path: Path) -> set[str] | None:
    """Read the baseline's finding ids, or None if no baseline exists.

    Missing file is the common case (no baseline configured) — never an
    error. A malformed baseline is loud: a project should not silently
    behave as if it had no baseline when the file is actually corrupt,
    because baselined findings would suddenly fail CI.
    """
    if not baseline_path.exists():
        return None
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{baseline_path}: malformed JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{baseline_path}: expected an object, got {type(data).__name__}")
    finding_ids = data.get("finding_ids")
    if not isinstance(finding_ids, list):
        raise ValueError(f"{baseline_path}: 'finding_ids' must be a list")
    return {str(fid) for fid in finding_ids}


def write_baseline(baseline_path: Path, finding_ids: list[str]) -> None:
    """Write the baseline atomically. Creates ``.gravitar/`` on first use.

    The ids are sorted so repeat runs that produce the same set yield a
    byte-identical file — a `git diff` on the baseline shows only genuine
    drift additions/removals rather than key reorderings.
    """
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": BASELINE_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "finding_ids": sorted(set(finding_ids)),
    }
    tmp = baseline_path.with_suffix(baseline_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(baseline_path)


def clear_baseline(baseline_path: Path) -> bool:
    """Remove the baseline file. Returns True if a baseline was removed,
    False if there was nothing to remove."""
    if not baseline_path.exists():
        return False
    baseline_path.unlink()
    return True


def read_baseline_metadata(baseline_path: Path) -> dict[str, Any] | None:
    """Return the full baseline payload (version, created_at, finding_ids)
    for ``baseline show``. None if no baseline exists."""
    if not baseline_path.exists():
        return None
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{baseline_path}: expected an object")
    return data
