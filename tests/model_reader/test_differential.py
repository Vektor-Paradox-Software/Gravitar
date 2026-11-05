"""SPEC-17 S0 tests: differential harness + zero-execution audit.

Two kinds of test here:

1. **Self-check**: run the differential harness against the S0 stub
   (which just defers to the import loader). Truth and reader both
   come from the same loader, so every fixture must pass with zero
   silent mismatches. If this test ever fails, the harness itself
   has a bug.

2. **Negative control**: deliberately inject a mismatch into the
   reader result and confirm the harness fails loudly. Protects
   against a future "harness always passes" regression.

Zero-execution tests (§8) live in ``test_zero_execution.py`` as a
separate file so the audit hook state can't contaminate anything else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.model_reader.differential import (
    FixtureSpec,
    _compare_tables,
    assert_differential_passes,
    run_differential,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# S3+S5 pass set: F-01…F-05 and F-08 from S3; F-06 (type_annotation_map)
# and F-07 (Enum / Literal) added by S5.
_S3_FIXTURES = [
    ("F-01", ["models"]),
    ("F-02", ["models"]),
    ("F-03", ["models"]),
    ("F-04", ["models"]),
    ("F-05", ["models"]),
    ("F-06", ["models"]),  # S5
    ("F-07", ["models"]),  # S5
    ("F-08", ["models"]),
]


@pytest.mark.parametrize("name,packages", _S3_FIXTURES)
def test_s3_fixture_passes_via_static_reader(name: str, packages: list[str]) -> None:
    """S3+S5 pass rule: for every column, either static-reader output
    equals truth OR a scoped note explains the mismatch. A silent
    mismatch is a P0."""
    fixture = FixtureSpec(name=name, root=FIXTURES_DIR / name, packages=packages)
    assert_differential_passes(fixture)


def test_harness_detects_a_silent_mismatch_in_reader_output() -> None:
    """Negative control: construct a truth dict and a reader schema
    that differ in one column, with NO notes; the harness must flag
    it as a silent mismatch (P0). If this test ever stops failing,
    the harness is no longer a gate — it's a snapshot."""
    truth: dict[str, dict[str, dict[str, dict[str, object]]]] = {
        "widgets": {
            "columns": {
                "id": {
                    "name": "id",
                    "type": "integer",
                    "nullable": False,
                    "primary_key": True,
                }
            },
        },
    }
    reader_schema = {
        "widgets": {
            "name": "widgets",
            "source_file": "x",
            "source_line": 1,
            "columns": {
                # Nullable flipped vs truth; no note explains it.
                "id": {
                    "name": "id",
                    "type": "integer",
                    "nullable": True,  # mismatch
                    "primary_key": True,
                    "source_file": "x",
                    "source_line": 1,
                },
            },
        }
    }

    outcome = _compare_tables(truth, reader_schema, reader_notes=[])  # type: ignore[arg-type]

    assert not outcome.passed()
    assert len(outcome.silent_mismatches) == 1
    assert outcome.silent_mismatches[0].field == "nullable"


def test_harness_allows_mismatch_when_a_note_scopes_it() -> None:
    """SPEC §10.1: a mismatch IS allowed when the reader emits a note
    scoped to the mismatching table/column. The note is the honest
    admission "I couldn't fully resolve this." """
    from gravitar.schema import SourceLocation, make_unresolved

    truth = {
        "widgets": {
            "columns": {
                "id": {
                    "name": "id",
                    "type": "integer",
                    "nullable": False,
                    "primary_key": True,
                }
            }
        }
    }
    reader_schema = {
        "widgets": {
            "name": "widgets",
            "source_file": "x",
            "source_line": 1,
            "columns": {
                "id": {
                    "name": "id",
                    "type": "integer",
                    "nullable": True,  # mismatch — but a note covers it
                    "primary_key": True,
                    "source_file": "x",
                    "source_line": 1,
                }
            },
        }
    }
    reader_notes = [
        make_unresolved(
            "unresolved",
            "could not resolve nullability",
            SourceLocation(file="x", line=1, revision_id=None),
            tables=["widgets"],
            columns=["id"],
        )
    ]

    outcome = _compare_tables(truth, reader_schema, reader_notes)  # type: ignore[arg-type]

    assert outcome.passed()
    assert len(outcome.explained_mismatches) == 1


def test_run_differential_returns_reader_result_too() -> None:
    """The harness runs both sides and returns the reader's full
    ModelResult, so downstream tests can check notes/coverage beyond
    the pass rule — reader confidence depends on more than schema
    equality."""
    fixture = FixtureSpec(name="F-01", root=FIXTURES_DIR / "F-01", packages=["models"])
    outcome, reader_result = run_differential(fixture)

    assert outcome.passed()
    assert reader_result.reader == "static"  # S3 provenance
    assert reader_result.coverage.classes_total >= 1
