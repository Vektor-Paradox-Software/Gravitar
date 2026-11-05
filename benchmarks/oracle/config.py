"""SPEC-18 T0: oracle harness configuration loader.

Reads ``benchmarks/manifest.yml``'s ``oracle_units:`` section into
``ProjectUnit`` dataclasses. The oracle_units list is the authoritative
corpus for the Postgres oracle harness; the pre-SPEC-18 ``projects:``
list under the same manifest continues to feed the simpler static-oracle
harness (``benchmarks/run.py``). They're decoupled on purpose: the two
harnesses answer different questions and shouldn't force the other to
change shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass(frozen=True)
class ProjectUnit:
    """SPEC-18 §3.1 ProjectUnit.

    Immutable per-unit record. The harness host NEVER imports project
    code — ``metadata_import`` is a string the per-unit container will
    resolve inside its own venv (T2+); here it's just carried through.
    """

    project: str
    unit: str
    tree_path: str
    metadata_import: str
    env_var: str
    dialects: tuple[str, ...]
    python: str
    lockfile: str
    extras: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        """Report filename and sort key: ``project__unit``."""
        return f"{self.project}__{self.unit}"


def _parse_unit(idx: int, entry: dict[str, Any]) -> ProjectUnit:
    missing = [
        k for k in ("project", "unit", "tree_path", "metadata_import", "env_var") if k not in entry
    ]
    if missing:
        raise ValueError(f"oracle_units[{idx}]: missing required key(s): {', '.join(missing)}")
    dialects_raw = entry.get("dialects") or ["postgres"]
    if not isinstance(dialects_raw, list) or not all(isinstance(d, str) for d in dialects_raw):
        raise ValueError(f"oracle_units[{idx}].dialects must be a list of strings")
    extras = entry.get("extras") or {}
    if not isinstance(extras, dict):
        raise ValueError(f"oracle_units[{idx}].extras must be a mapping")
    return ProjectUnit(
        project=str(entry["project"]),
        unit=str(entry["unit"]),
        tree_path=str(entry["tree_path"]),
        metadata_import=str(entry["metadata_import"]),
        env_var=str(entry["env_var"]),
        dialects=tuple(dialects_raw),
        python=str(entry.get("python", "3.11")),
        lockfile=str(entry.get("lockfile", "")),
        extras=dict(extras),
    )


def load_oracle_units(manifest_path: Path) -> list[ProjectUnit]:
    """Parse ``oracle_units:`` from the manifest. Returns ``[]`` when
    the key is missing — the simpler ``projects:`` list is still valid
    without SPEC-18 ever being configured."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"{manifest_path}: manifest not found")
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{manifest_path}: expected a top-level mapping")
    raw = data.get("oracle_units") or []
    if not isinstance(raw, list):
        raise ValueError(f"{manifest_path}: 'oracle_units' must be a list")
    units: list[ProjectUnit] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{manifest_path}: oracle_units[{idx}] must be a mapping")
        units.append(_parse_unit(idx, cast("dict[str, Any]", entry)))
    # Deterministic order: SPEC-18 §2.8 — reports sort by
    # (project, unit, category, table, column); unit order flows from here.
    units.sort(key=lambda u: (u.project, u.unit))
    return units
