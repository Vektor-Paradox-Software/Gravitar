"""feedback.sqlite read/write tests."""

from pathlib import Path

from gravitar.feedback.store import db_path_for, pattern_counts, record_outcome


def test_db_path_for_is_dotgravitar_feedback_sqlite_under_repo_root(tmp_path: Path) -> None:
    assert db_path_for(tmp_path) == tmp_path / ".gravitar" / "feedback.sqlite"


# --- Section 5 failure mode: missing/corrupt DB never blocks a run ---


def test_pattern_counts_on_missing_database_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    assert pattern_counts(db_path) == {}


def test_reading_a_missing_database_does_not_create_a_file(tmp_path: Path) -> None:
    """A plain `gravitar check` must never leave a stray .gravitar/ behind in a
    repo nobody has run `gravitar feedback` in."""
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    pattern_counts(db_path)

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_pattern_counts_on_corrupt_database_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"not a sqlite database")

    assert pattern_counts(db_path) == {}


# --- record_outcome / pattern_counts round trip ---


def test_record_outcome_creates_the_database(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    record_outcome(db_path, "abc123", "sql:add_column", "confirmed")

    assert db_path.exists()


def test_recorded_outcome_is_reflected_in_pattern_counts(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    record_outcome(db_path, "abc123", "sql:add_column", "confirmed")

    assert pattern_counts(db_path) == {"sql:add_column": (1, 0)}


def test_confirm_and_dismiss_counts_accumulate_independently(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    record_outcome(db_path, "a", "sql:add_column", "confirmed")
    record_outcome(db_path, "b", "sql:add_column", "confirmed")
    record_outcome(db_path, "c", "sql:add_column", "false_positive")

    assert pattern_counts(db_path) == {"sql:add_column": (2, 1)}


def test_different_patterns_accumulate_into_separate_buckets(tmp_path: Path) -> None:
    """Section 5: two pattern_ids must never collapse into the same bucket."""
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    record_outcome(db_path, "a", "sql:add_column", "confirmed")
    record_outcome(db_path, "b", "sql:drop_column", "false_positive")

    counts = pattern_counts(db_path)
    assert counts == {"sql:add_column": (1, 0), "sql:drop_column": (0, 1)}
    assert counts["sql:add_column"] != counts["sql:drop_column"]


def test_record_outcome_returns_the_finding_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / ".gravitar" / "feedback.sqlite"

    outcome = record_outcome(db_path, "abc123", "sql:add_column", "confirmed")

    assert outcome.finding_id == "abc123"
    assert outcome.pattern_id == "sql:add_column"
    assert outcome.verdict == "confirmed"
    assert outcome.timestamp
