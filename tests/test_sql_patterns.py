"""Signature-based raw-SQL classification tests."""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gravitar.analysis.sql_patterns import SqlMatch, match, tokenize

FIXTURES = Path(__file__).parent / "fixtures" / "sql_patterns"


def _sql(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- One fixture per pattern in the Section 5 library ---


def test_add_column_pattern() -> None:
    assert match(_sql("add_column.sql")) == SqlMatch(
        "sql:add_column", table="accounts", column="nickname"
    )


def test_drop_column_pattern() -> None:
    assert match(_sql("drop_column.sql")) == SqlMatch(
        "sql:drop_column", table="accounts", column="nickname"
    )


def test_alter_column_type_pattern() -> None:
    assert match(_sql("alter_column_type.sql")) == SqlMatch(
        "sql:alter_column_type", table="accounts", column="name"
    )


def test_create_index_pattern() -> None:
    assert match(_sql("create_index.sql")) == SqlMatch("sql:create_index", table="accounts")


def test_create_unique_index_pattern() -> None:
    assert match(_sql("create_unique_index.sql")) == SqlMatch(
        "sql:create_unique_index", table="accounts"
    )


# --- At least 3 no-match cases ---


@pytest.mark.parametrize(
    "fixture_name", ["no_match_update.sql", "no_match_delete.sql", "no_match_malformed.sql"]
)
def test_no_match_cases(fixture_name: str) -> None:
    assert match(_sql(fixture_name)) is None


# --- Case-insensitivity and formatting variations ---


def test_keywords_are_case_insensitive() -> None:
    assert match("alter table accounts add column nickname varchar(50)") == SqlMatch(
        "sql:add_column", table="accounts", column="nickname"
    )


def test_identifier_casing_is_preserved() -> None:
    result = match("ALTER TABLE Accounts ADD COLUMN Nickname VARCHAR(50)")
    assert result is not None
    assert result.table == "Accounts"
    assert result.column == "Nickname"


def test_create_unique_index_matched_before_create_index() -> None:
    # A fixed-position CREATE INDEX check would misread "UNIQUE" as the index name
    # if tried first; confirm the more specific pattern wins.
    result = match("CREATE UNIQUE INDEX idx ON accounts (name)")
    assert result is not None
    assert result.pattern_id == "sql:create_unique_index"


# --- Section 6 failure mode: the tokenizer never crashes, no-match is the fallback ---


def test_empty_string_does_not_match() -> None:
    assert match("") is None


@given(st.text(max_size=200))
def test_tokenizer_never_raises_on_arbitrary_text(sql: str) -> None:
    tokenize(sql)  # must not raise


@given(st.text(max_size=200))
def test_match_never_raises_on_arbitrary_text(sql: str) -> None:
    match(sql)  # must not raise
