"""Signature-based raw-SQL classification.

Recovers structured schema-operation intent from the string argument of an
`op.execute("<raw sql>")` call, entirely statically: a lightweight, non-validating
tokenizer (whitespace/keyword split, not a grammar parser — see Non-goals) followed
by matching against a fixed library of common schema-mutating SQL shapes. A string
that doesn't match any shape is not an error: it stays unresolved and the caller
(migrations.py) keeps scoring it at the "raw_sql_unresolved" base confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches, in order of preference: a single-quoted string literal (with SQL's ''
# escape for an embedded quote), a double-quoted identifier, a parenthesis or comma
# as its own token, a bare identifier/keyword, or — the non-validating fallback —
# any other single non-space character. Every character in the input is consumed by
# some alternative, so this never raises and never loops without making progress;
# SQL it can't make sense of just degrades into tokens that fail every pattern
# below (Section 6's first failure mode: no-match, never a crash).
_TOKEN_RE = re.compile(r"""'(?:[^']|'')*'|"[^"]*"|[(),;]|[A-Za-z_][A-Za-z0-9_.]*|\S""", re.VERBOSE)


def tokenize(sql: str) -> list[str]:
    return _TOKEN_RE.findall(sql)


@dataclass(frozen=True)
class SqlMatch:
    pattern_id: str
    table: str
    column: str | None = None


def _keyword(token: str, expected: str) -> bool:
    return token.upper() == expected


def _identifier(token: str) -> str:
    return token.strip("\"'")


def _strip_trailing_semicolon(tokens: list[str]) -> list[str]:
    return tokens[:-1] if tokens and tokens[-1] == ";" else tokens


def _match_add_column(tokens: list[str]) -> SqlMatch | None:
    # ALTER TABLE <t> ADD COLUMN <c> <type...>
    if len(tokens) < 6:
        return None
    if not (
        _keyword(tokens[0], "ALTER")
        and _keyword(tokens[1], "TABLE")
        and _keyword(tokens[3], "ADD")
        and _keyword(tokens[4], "COLUMN")
    ):
        return None
    return SqlMatch("sql:add_column", table=_identifier(tokens[2]), column=_identifier(tokens[5]))


def _match_drop_column(tokens: list[str]) -> SqlMatch | None:
    # ALTER TABLE <t> DROP COLUMN <c>
    if len(tokens) != 6:
        return None
    if not (
        _keyword(tokens[0], "ALTER")
        and _keyword(tokens[1], "TABLE")
        and _keyword(tokens[3], "DROP")
        and _keyword(tokens[4], "COLUMN")
    ):
        return None
    return SqlMatch("sql:drop_column", table=_identifier(tokens[2]), column=_identifier(tokens[5]))


def _match_alter_column_type(tokens: list[str]) -> SqlMatch | None:
    # ALTER TABLE <t> ALTER COLUMN <c> TYPE <type...>
    if len(tokens) < 8:
        return None
    if not (
        _keyword(tokens[0], "ALTER")
        and _keyword(tokens[1], "TABLE")
        and _keyword(tokens[3], "ALTER")
        and _keyword(tokens[4], "COLUMN")
        and _keyword(tokens[6], "TYPE")
    ):
        return None
    return SqlMatch(
        "sql:alter_column_type", table=_identifier(tokens[2]), column=_identifier(tokens[5])
    )


def _match_create_index(tokens: list[str]) -> SqlMatch | None:
    # CREATE INDEX <idx> ON <t> ( <cols> )
    if len(tokens) < 7:
        return None
    if not (
        _keyword(tokens[0], "CREATE") and _keyword(tokens[1], "INDEX") and _keyword(tokens[3], "ON")
    ):
        return None
    return SqlMatch("sql:create_index", table=_identifier(tokens[4]))


def _match_create_unique_index(tokens: list[str]) -> SqlMatch | None:
    # CREATE UNIQUE INDEX <idx> ON <t> ( <cols> )
    if len(tokens) < 8:
        return None
    if not (
        _keyword(tokens[0], "CREATE")
        and _keyword(tokens[1], "UNIQUE")
        and _keyword(tokens[2], "INDEX")
        and _keyword(tokens[4], "ON")
    ):
        return None
    return SqlMatch("sql:create_unique_index", table=_identifier(tokens[5]))


# Order matters: CREATE UNIQUE INDEX must be tried before CREATE INDEX, since the
# latter's fixed-position checks would otherwise misread "UNIQUE" as the index name.
_MATCHERS = [
    _match_add_column,
    _match_drop_column,
    _match_alter_column_type,
    _match_create_unique_index,
    _match_create_index,
]


def match(sql: str) -> SqlMatch | None:
    """Match a raw SQL string against the Section 5 pattern library.

    Returns None on no match — including for SQL the tokenizer can't make sense
    of — never raises.
    """
    tokens = _strip_trailing_semicolon(tokenize(sql))
    for matcher in _MATCHERS:
        result = matcher(tokens)
        if result is not None:
            return result
    return None
