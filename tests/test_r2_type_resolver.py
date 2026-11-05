"""SPEC-16 Amendment R2 + R13 (subset): type resolver.

Readiness doc D2–D5 and D10 — the pattern-shaped type-side failures
responsible for most non-batch false positives on the pinned corpus:

- D2: class-form types (``sa.Integer``, no parens) resolved to
  ``unknown:missing`` in ``Column(...)``, and were silently ignored in
  ``alter_column(type_=sa.BigInteger)``, leaving the column at its old
  type.
- D3: uppercase/alias names (``Unicode``, ``VARCHAR``, ``INTEGER``,
  ``TIMESTAMP``, ``DECIMAL``, ...) returned ``unknown:<Name>`` on the
  migration side even though the model side normalized them correctly
  through isinstance dispatch.
- D4: ``BigInteger().with_variant(X, "mysql")`` chains resolved to
  ``unknown:with_variant`` on the migration side.
- D5: custom ``TypeDecorator`` instances on the model side returned
  ``unknown:<Name>`` even when their ``impl`` was a supported type.
- D10: ``Column("uid", sa.ForeignKey("users.id"))`` resolved its type
  to ``unknown:ForeignKey`` because the walker took the first
  ``ast.Call`` — but ``ForeignKey`` is a constraint, not a type.

The full R2 (import-following TypeDecorator across files, Hypothesis
parity property test, full ``type_resolver.py`` registry introspection,
``R13`` config ``types:`` mapping) stays deferred; this subset covers
the readiness doc's concrete D-labeled defects, which per the gate are
what gets measured.
"""

from __future__ import annotations

import ast

from sqlalchemy import types as sqltypes
from sqlalchemy.types import TypeDecorator

from gravitar.schema.types import normalize_ast_type, normalize_type


def _parse(source: str) -> ast.expr:
    """Parse a single expression and return its AST node."""
    return ast.parse(source, mode="eval").body


# --- D2: class-form types ---


def test_class_form_integer_resolves_to_integer() -> None:
    """``sa.Integer`` without parentheses was ``unknown:missing``."""
    assert normalize_ast_type(_parse("sa.Integer")) == "integer"


def test_class_form_big_integer_resolves_to_bigint() -> None:
    assert normalize_ast_type(_parse("sa.BigInteger")) == "bigint"


def test_class_form_bare_identifier_resolves() -> None:
    """``Integer`` imported without a module prefix also works."""
    assert normalize_ast_type(_parse("Integer")) == "integer"


def test_class_form_string_without_length_resolves_to_varchar() -> None:
    assert normalize_ast_type(_parse("sa.String")) == "varchar"


# --- D3: uppercase/alias names ---


def test_uppercase_varchar_alias_resolves() -> None:
    """``VARCHAR`` is SQLAlchemy's uppercase alias for ``String`` on the
    canonical representation path."""
    assert normalize_ast_type(_parse("sa.VARCHAR(255)")) == "varchar(255)"


def test_uppercase_integer_alias_resolves() -> None:
    assert normalize_ast_type(_parse("sa.INTEGER")) == "integer"


def test_uppercase_timestamp_alias_resolves() -> None:
    assert normalize_ast_type(_parse("sa.TIMESTAMP")) == "timestamp"


def test_uppercase_timestamp_timezone_kwarg() -> None:
    assert normalize_ast_type(_parse("sa.TIMESTAMP(timezone=True)")) == "timestamptz"


def test_decimal_alias_maps_to_numeric() -> None:
    assert normalize_ast_type(_parse("sa.DECIMAL(10, 2)")) == "numeric(10,2)"


def test_unicode_alias_maps_to_varchar() -> None:
    """SQLAlchemy's ``Unicode`` is a String subclass — same canonical shape."""
    assert normalize_ast_type(_parse("sa.Unicode(100)")) == "varchar(100)"


def test_dialect_prefixed_timestamp_alias() -> None:
    """``postgresql.TIMESTAMP`` is a dialect-prefixed alias; the ``_call_type_name``
    helper picks the trailing identifier regardless of module path."""
    assert normalize_ast_type(_parse("postgresql.TIMESTAMP(timezone=True)")) == "timestamptz"


# --- D4: with_variant / evaluates_none / copy passthrough ---


def test_with_variant_unwraps_to_receiver() -> None:
    """``BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")``
    resolves to ``bigint`` on the base dialect."""
    assert (
        normalize_ast_type(_parse('sa.BigInteger().with_variant(mysql.BIGINT(), "mysql")'))
        == "bigint"
    )


def test_class_form_with_variant_unwraps() -> None:
    """Edge case: ``.with_variant`` on a class-form base — Python would
    reject this at runtime, but the AST walker handles it gracefully by
    unwrapping to the class."""
    assert normalize_ast_type(_parse('sa.Integer.with_variant(X, "mysql")')) == "integer"


def test_evaluates_none_unwraps() -> None:
    assert normalize_ast_type(_parse("sa.String(50).evaluates_none()")) == "varchar(50)"


def test_chained_passthroughs_all_unwrap() -> None:
    """Multiple passthroughs in a row all unwrap."""
    source = 'sa.BigInteger().with_variant(mysql.BIGINT(), "mysql").copy()'
    assert normalize_ast_type(_parse(source)) == "bigint"


# --- Still-unknown types remain reported as unknown (no silent misreading) ---


def test_unmapped_type_name_still_returns_unknown() -> None:
    """R2 doesn't silently accept everything — a genuinely unknown
    class name still reports ``unknown:...`` so the comparator can
    emit an ``unknown_type`` warning."""
    assert (
        normalize_ast_type(_parse("sa.CustomTypeThatDoesNotExist"))
        == "unknown:CustomTypeThatDoesNotExist"
    )


# --- D5: model-side TypeDecorator unwrap (R13 piece) ---


def test_type_decorator_unwraps_to_impl() -> None:
    """Pre-R13: a project's ``class JSONEncoded(TypeDecorator): impl = JSON``
    resolved to ``unknown:JSONEncoded`` even though its impl was a
    supported type. R13 unwraps one layer."""

    class JSONEncoded(TypeDecorator):
        impl = sqltypes.JSON
        cache_ok = True

    assert normalize_type(JSONEncoded()) == "json"


def test_type_decorator_wrapping_string_unwraps() -> None:
    class EncryptedString(TypeDecorator):
        impl = sqltypes.String
        cache_ok = True

        def __init__(self) -> None:
            super().__init__(length=100)

    assert normalize_type(EncryptedString()) == "varchar(100)"


# --- D5: Interval was unknown on the model side ---


def test_interval_resolves_on_model_side() -> None:
    assert normalize_type(sqltypes.Interval()) == "interval"


def test_interval_resolves_on_ast_side() -> None:
    assert normalize_ast_type(_parse("sa.Interval()")) == "interval"


# --- D10: first-ast.Call-arg selection skips ForeignKey/Sequence/etc. ---


def test_column_with_foreign_key_as_second_arg_still_picks_the_type() -> None:
    """``Column("uid", sa.Integer, sa.ForeignKey("users.id"))`` must
    resolve ``uid`` to ``integer``, not ``unknown:ForeignKey``. Tested
    via the parser's own column-call resolution path."""
    from gravitar.schema.parsers.alembic_parser import _resolve_column_call

    call = _parse('Column("uid", sa.Integer, sa.ForeignKey("users.id"))')
    assert isinstance(call, ast.Call)
    spec = _resolve_column_call(call)

    assert spec is not None
    assert spec.name == "uid"
    assert spec.type == "integer"


def test_column_with_only_foreign_key_leaves_type_unresolved() -> None:
    """A column with no explicit type AND a ForeignKey leaves the type
    unresolved (``unknown:missing``) — the SPEC-16 v1.0 ticket's
    expectation. Pre-R2 this reported ``unknown:ForeignKey``, which
    looked like a type-mismatch failure instead of an unresolved one."""
    from gravitar.schema.parsers.alembic_parser import _resolve_column_call

    call = _parse('Column("uid", sa.ForeignKey("users.id"))')
    assert isinstance(call, ast.Call)
    spec = _resolve_column_call(call)

    assert spec is not None
    assert spec.type == "unknown:missing"


def test_column_with_sequence_skipped_when_resolving_type() -> None:
    """``Column("id", sa.Integer, sa.Sequence("foo_seq"))`` — Sequence
    is a default generator, not a type."""
    from gravitar.schema.parsers.alembic_parser import _resolve_column_call

    call = _parse('Column("id", sa.Integer, sa.Sequence("foo_seq"))')
    assert isinstance(call, ast.Call)
    spec = _resolve_column_call(call)

    assert spec is not None
    assert spec.type == "integer"


# --- D2 (alter_column): class-form type_= must apply ---


def test_alter_column_with_class_form_type_applies() -> None:
    """Pre-R2 fix: ``op.alter_column("t", "c", type_=sa.BigInteger)`` was
    silently ignored because of the ``isinstance(ast.Call)`` gate — the
    column kept its old type."""
    from gravitar.schema.parsers.alembic_parser import _resolve_call

    call = _parse('op.alter_column("t", "c", type_=sa.BigInteger)')
    assert isinstance(call, ast.Call)
    op = _resolve_call(call)

    assert op.op == "alter_column"
    assert op.table == "t"
    assert op.column_name == "c"
    assert op.type == "bigint"


def test_alter_column_with_call_form_type_still_applies() -> None:
    """Regression: the call-form path must keep working after fixing the
    class-form path."""
    from gravitar.schema.parsers.alembic_parser import _resolve_call

    call = _parse('op.alter_column("t", "c", type_=sa.String(length=200))')
    assert isinstance(call, ast.Call)
    op = _resolve_call(call)

    assert op.type == "varchar(200)"
