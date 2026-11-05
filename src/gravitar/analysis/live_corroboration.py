"""Live (read-only) database corroboration.

Strictly optional, off by default: nothing in this module runs unless the
caller (cli.py) passes a `--live-db-url`. This is corroboration, not a data
source — it only resolves ambiguity in findings the static reconstruction
already produced, one targeted read-only column lookup per low-confidence
finding, never a schema dump.

Nothing here ever logs or stores the connection URL itself ("never
persisted... including logs") — failure messages report the exception *type*
only, never its text, since a driver's error message can itself embed the DSN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from gravitar.analysis.confidence import clamp
from gravitar.schema import DriftFinding, Evidence
from gravitar.schema.types import normalize_type

logger = logging.getLogger("gravitar.live_corroboration")

# Only findings the static path was already unsure about are worth a live
# round-trip; anything at or above this is left alone (Section 4).
LIVE_CORROBORATION_THRESHOLD = 0.7
LIVE_CORROBORATION_BONUS = 0.3


@dataclass(frozen=True)
class LiveColumn:
    type: str
    nullable: bool


def corroboration_bonus_live(confidence: float) -> float:
    return clamp(confidence + LIVE_CORROBORATION_BONUS)


def corroborate(findings: list[DriftFinding], live_db_url: str) -> list[DriftFinding]:
    """Corroborate every finding below LIVE_CORROBORATION_THRESHOLD against a
    live database. Connects once, runs the targeted per-finding lookups, and
    closes before returning (Section 5) — never a persistent pool.

    Never raises: a connection failure fails soft (Section 5), logging one
    warning (exception type only — see module docstring) and returning
    `findings` completely unchanged, exactly as if this had never been called.
    """
    try:
        engine = create_engine(live_db_url)
        with engine.connect():
            pass  # fail fast on bad credentials/unreachable host, before touching findings
    except Exception as e:
        logger.warning("live-db corroboration disabled: could not connect (%s)", type(e).__name__)
        return findings

    try:
        return [_corroborate_finding(engine, finding) for finding in findings]
    finally:
        engine.dispose()


def _corroborate_finding(engine: Engine, finding: DriftFinding) -> DriftFinding:
    if finding["confidence"] >= LIVE_CORROBORATION_THRESHOLD:
        return finding
    if finding["table"] is None or finding["column"] is None:
        return finding

    live_column = _introspect_single_column(engine, finding["table"], finding["column"])
    if live_column is None:
        # Section 6: no live value either way (table/column doesn't exist
        # live, or its type can't be normalized) is a neutral result, not
        # evidence for or against — leave the finding exactly as it was.
        return finding

    return _apply_corroboration(finding, live_column)


def _introspect_single_column(engine: Engine, table: str, column: str) -> LiveColumn | None:
    """One targeted, read-only reflection query for a single named column —
    never a blanket schema sweep (Section 2 Non-goals).
    """
    try:
        inspector = inspect(engine)
        if not inspector.has_table(table):
            return None
        for col in inspector.get_columns(table):
            if col["name"] != column:
                continue
            type_str = normalize_type(col["type"])
            if type_str.startswith("unknown:"):
                # Section 6: a live type Gravitar's own canonical vocabulary
                # can't represent doesn't corroborate anything — don't guess.
                return None
            return LiveColumn(type=type_str, nullable=bool(col["nullable"]))
    except Exception as e:
        logger.debug(
            "live-db corroboration: could not introspect %s.%s (%s)",
            table,
            column,
            type(e).__name__,
        )
        return None
    return None


def _apply_corroboration(finding: DriftFinding, live_column: LiveColumn) -> DriftFinding:
    if finding["category"] == "orphaned_reference":
        # migration_value is raw SQL text and model_value is always None for
        # this category — neither is a type string to compare against, so
        # the column's mere existence live is itself the corroborating
        # signal (the reference wasn't a typo/dead end after all).
        return _bump(
            finding,
            corroboration_bonus_live(finding["confidence"]),
            LIVE_CORROBORATION_BONUS,
            "live schema confirms the table/column exists",
        )

    matched_side = _matching_side(finding, live_column.type)
    if matched_side is not None:
        return _bump(
            finding,
            corroboration_bonus_live(finding["confidence"]),
            LIVE_CORROBORATION_BONUS,
            f"live schema matches {matched_side}",
        )
    # Live state contradicts both sides — this is now a confirmed,
    # high-confidence finding, not a low-confidence one (Section 4).
    return _bump(finding, 1.0, 1.0, "live schema matches neither migration nor model")


def _matching_side(finding: DriftFinding, live_type: str) -> str | None:
    if finding["migration_value"] == live_type:
        return "migration"
    if finding["model_value"] == live_type:
        return "model"
    return None


def _bump(finding: DriftFinding, confidence: float, weight: float, detail: str) -> DriftFinding:
    evidence = [
        *finding["evidence"],
        Evidence(signal="live_db_corroboration", weight=weight, detail=detail),
    ]
    return DriftFinding(
        category=finding["category"],
        severity=finding["severity"],
        table=finding["table"],
        column=finding["column"],
        model_value=finding["model_value"],
        migration_value=finding["migration_value"],
        message=finding["message"],
        suggestion=finding["suggestion"],
        confidence=confidence,
        source=finding["source"],
        evidence=evidence,
        live_verified=True,
        kind=finding["kind"],
        demoted_from=finding["demoted_from"],
        explaining_notes=finding["explaining_notes"],
    )
