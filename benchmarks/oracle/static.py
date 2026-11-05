"""Static YAML oracle runner. SPEC-16 R0.

Loads a hand-authored list of expected drifts from a YAML file.
Format:

```yaml
findings:
  - category: missing_column
    table: widgets
    column: deleted_at
    note: "model declares; no migration adds"
  - category: type_mismatch
    table: widgets
    column: price
```

The file lists what the oracle (a real Postgres run against this
project's migrations + models, authored once by a human) determined is
genuine drift. Gravitar's run is scored against it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from benchmarks.schema import OracleFinding


def load_static_oracle(oracle_path: Path) -> list[OracleFinding]:
    """Parse a static oracle YAML file. Raises FileNotFoundError or
    yaml.YAMLError up to the harness; a malformed oracle must stop
    the run, not silently produce an empty list."""
    data = yaml.safe_load(oracle_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{oracle_path}: expected a top-level mapping")
    findings_raw = data.get("findings", [])
    if not isinstance(findings_raw, list):
        raise ValueError(f"{oracle_path}: 'findings' must be a list")

    out: list[OracleFinding] = []
    for idx, entry in enumerate(findings_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{oracle_path}: findings[{idx}] must be a mapping")
        entry_typed = cast("dict[str, Any]", entry)
        category = entry_typed.get("category")
        if not isinstance(category, str):
            raise ValueError(f"{oracle_path}: findings[{idx}].category required")
        out.append(
            OracleFinding(
                category=category,  # type: ignore[arg-type]
                table=entry_typed.get("table"),
                column=entry_typed.get("column"),
                note=entry_typed.get("note"),
            )
        )
    return out
