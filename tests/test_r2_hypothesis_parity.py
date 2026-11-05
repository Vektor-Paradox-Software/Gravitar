"""SPEC-16 Amendment R2 (full): Hypothesis parity test.

The invariant: for every SQLAlchemy type Gravitar knows about, parsing
its source form via ``normalize_ast_type`` must produce the same
canonical string as inspecting the live instance via ``normalize_type``.
If the two paths disagree, every type_mismatch finding against that
type becomes a false positive.

The pre-R2-full subset already covered a hand-picked matrix in
``test_r2_type_resolver.py``; this file uses Hypothesis to generate
*combinations* — alias names, args, kwargs, passthrough chains —
across every type at once, which is where cross-DSL skew historically
hid (readiness doc D3/D4).

The generators are deliberately narrow: they only produce shapes
``normalize_type`` can actually resolve on a live instance. A fuzzer
that generated random nonsense (``String(scale=4)``) would just be
testing SQLAlchemy's constructor, not the parity invariant.
"""

from __future__ import annotations

import ast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from gravitar.schema.types import normalize_ast_type, normalize_type


def _ast_for(expr: str) -> ast.expr:
    return ast.parse(expr, mode="eval").body


# --- Simple round-trips: live instance → canonical ↔ source form → canonical ---


@given(length=st.one_of(st.none(), st.integers(min_value=1, max_value=255)))
@settings(max_examples=30, deadline=None)
def test_string_parity(length: int | None) -> None:
    """String(N) and String() must resolve identically whether built as
    a live instance or parsed from source."""
    live = normalize_type(String(length) if length else String())
    src = normalize_ast_type(_ast_for(f"String({length})" if length else "String()"))
    assert live == src, (live, src)


@given(
    precision=st.one_of(st.none(), st.integers(min_value=1, max_value=38)),
    scale=st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
)
@settings(max_examples=30, deadline=None)
def test_numeric_parity(precision: int | None, scale: int | None) -> None:
    """Numeric with every valid combination of precision/scale."""
    live = normalize_type(Numeric(precision, scale))
    if precision is not None and scale is not None:
        src = normalize_ast_type(_ast_for(f"Numeric({precision}, {scale})"))
    elif precision is not None:
        src = normalize_ast_type(_ast_for(f"Numeric({precision})"))
    else:
        src = normalize_ast_type(_ast_for("Numeric()"))
    assert live == src, (live, src)


@given(timezone=st.booleans())
@settings(max_examples=5, deadline=None)
def test_datetime_timezone_parity(timezone: bool) -> None:
    """DateTime(timezone=True) / (timezone=False) parity. Pre-R2 the
    AST side missed the kwarg entirely and both resolved to timestamp."""
    live = normalize_type(DateTime(timezone=timezone))
    src = normalize_ast_type(_ast_for(f"DateTime(timezone={timezone})"))
    assert live == src, (live, src)


# --- Full-matrix parity: pick a type, build both forms, compare ---

_PARAMETERLESS_TYPES = [
    ("Integer", Integer),
    ("BigInteger", BigInteger),
    ("SmallInteger", SmallInteger),
    ("Boolean", Boolean),
    ("Date", Date),
    ("Time", Time),
    ("Text", Text),
    ("Float", Float),
    ("JSON", JSON),
    ("JSONB", JSONB),
    ("UUID", UUID),
    ("LargeBinary", LargeBinary),
]


@pytest.mark.parametrize("name,cls", _PARAMETERLESS_TYPES)
def test_parameterless_type_parity(name: str, cls: type) -> None:
    """Every parameter-less type: call-form, class-form, and live
    instance all land on the same canonical string."""
    live = normalize_type(cls())
    call = normalize_ast_type(_ast_for(f"{name}()"))
    bare = normalize_ast_type(_ast_for(name))

    assert live == call == bare, (name, live, call, bare)


# --- Alias parity: live sa.VARCHAR() etc. should match canonical ---


@given(st.sampled_from(["VARCHAR", "CHAR", "NVARCHAR"]))
@settings(max_examples=10, deadline=None)
def test_uppercase_string_alias_parity(alias: str) -> None:
    """Uppercase SQL-standard aliases resolve to the same canonical
    varchar as plain ``String()``. (The live-instance side imports
    these as subclasses of String; the AST side uses _TYPE_ALIASES.)"""
    from sqlalchemy import String as _String

    live = normalize_type(_String(50))
    src = normalize_ast_type(_ast_for(f"{alias}(50)"))
    assert live == src, (alias, live, src)


# --- Import-alias parity via type_import_aliases_scope ---


@given(
    original=st.sampled_from(["String", "Integer", "DateTime", "JSON", "JSONB"]),
    alias=st.sampled_from(["Str", "Int", "Dt", "J", "JB"]),
)
@settings(max_examples=20, deadline=None)
def test_import_alias_parity(original: str, alias: str) -> None:
    """A user-import-aliased type (``from sqlalchemy import X as Y``)
    must resolve identically to the unaliased form."""
    from gravitar.schema.types import type_import_aliases_scope

    unaliased = normalize_ast_type(_ast_for(f"{original}()"))
    with type_import_aliases_scope({alias: original}):
        aliased = normalize_ast_type(_ast_for(f"{alias}()"))
    assert unaliased == aliased, (original, alias, unaliased, aliased)


# --- Passthrough chain parity ---


@given(dialect=st.sampled_from(["mysql", "sqlite", "postgresql"]))
@settings(max_examples=10, deadline=None)
def test_with_variant_parity(dialect: str) -> None:
    """``.with_variant(...)`` unwraps to the receiver — the AST side
    and the live side should match."""
    live = normalize_type(BigInteger())
    src = normalize_ast_type(_ast_for(f"BigInteger().with_variant(SmallInteger(), '{dialect}')"))
    assert live == src == "bigint", (dialect, live, src)


# --- Enum (sorted-values parity) ---


@given(
    values=st.lists(st.sampled_from(["a", "b", "c", "d", "e"]), min_size=1, max_size=5, unique=True)
)
@settings(max_examples=20, deadline=None)
def test_enum_parity(values: list[str]) -> None:
    """``Enum(a, b, c)`` sorts its values deterministically on both
    sides — pre-R2, this could skew between AST (source order) and
    live (constructor-arg order), triggering spurious mismatches."""
    import sqlalchemy as sa

    live = normalize_type(sa.Enum(*values))
    args = ", ".join(repr(v) for v in values)
    src = normalize_ast_type(_ast_for(f"Enum({args})"))
    assert live == src, (values, live, src)
