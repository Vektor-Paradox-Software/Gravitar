"""feedback.sqlite read/write.

Local-first: one SQLite file per repo at `<repo>/.gravitar/feedback.sqlite`,
gitignored by default, no network calls. Reading never creates the file —
`sqlite3.connect()` would otherwise create it lazily even for a read-only
lookup — so a plain `gravitar check` never leaves a stray file behind in a repo
nobody has run `gravitar feedback` in yet (Section 5: "missing... never blocks
a run"). Only `record_outcome` (used by `gravitar feedback confirm`/`dismiss`)
creates `.gravitar/` and the database.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from gravitar.feedback import FindingOutcome

DB_RELATIVE_PATH = Path(".gravitar") / "feedback.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_outcomes (
    finding_id TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('confirmed', 'false_positive')),
    timestamp TEXT NOT NULL
)
"""


def db_path_for(repo_path: Path) -> Path:
    return repo_path / DB_RELATIVE_PATH


def record_outcome(db_path: Path, finding_id: str, pattern_id: str, verdict: str) -> FindingOutcome:
    """Append a FindingOutcome, creating `.gravitar/` and the database on first use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    outcome = FindingOutcome(
        finding_id=finding_id,
        pattern_id=pattern_id,
        verdict=verdict,
        timestamp=datetime.now(UTC).isoformat(),
    )
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT INTO finding_outcomes (finding_id, pattern_id, verdict, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (outcome.finding_id, outcome.pattern_id, outcome.verdict, outcome.timestamp),
        )
    return outcome


def pattern_counts(db_path: Path) -> dict[str, tuple[int, int]]:
    """pattern_id -> (confirm_count, dismiss_count).

    A missing or corrupt database isn't an error (Section 5) — it's read as "no
    feedback recorded yet" for every pattern.
    """
    if not db_path.exists():
        return {}
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT pattern_id, verdict, COUNT(*) FROM finding_outcomes "
                "GROUP BY pattern_id, verdict"
            ).fetchall()
    except sqlite3.DatabaseError:
        return {}

    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for pattern, verdict, count in rows:
        counts[pattern][0 if verdict == "confirmed" else 1] += count
    return {pattern: (confirmed, dismissed) for pattern, (confirmed, dismissed) in counts.items()}
