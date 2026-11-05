"""Type normalization (class form, alias/uppercase names,
with_variant/evaluates_none/copy passthrough, project-local TypeDecorator
unwrap to impl, first-ast.Call-arg selection skipping
ForeignKey/Sequence/Identity/Computed/Index/CheckConstraint).

Dispatch is order-sensitive because several SQLAlchemy types subclass one another:
Enum subclasses String, JSONB subclasses JSON, Text subclasses String, BigInteger and
SmallInteger subclass Integer, and Float subclasses Numeric. Each subclass must be
checked before its base or it will be silently misnormalized as the base type.

Two entry points:
- ``normalize_type(instance)``: live SA type instance (model side). R13:
  ``TypeDecorator`` subclasses unwrap one layer to ``impl`` before dispatch,
  so a project's custom ``class JSONEncoded(TypeDecorator): impl = JSON``
  resolves to ``json`` rather than ``unknown:JSONEncoded``.
- ``normalize_ast_type(node)``: AST expression (migration side). R2: handles
  bare class references (``sa.Integer``, no parens), passthrough chains
  (``BigInteger().with_variant(...)``), and the uppercase/alias names
  (``VARCHAR``, ``INTEGER``, ``TIMESTAMP``, ``DECIMAL``) the readiness doc
  flagged in D3/D4.
"""

from __future__ import annotations

import ast
import contextvars
from collections.abc import Callable
from typing import Any

from sqlalchemy import types as sqltypes
from sqlalchemy.dialects import postgresql

# SPEC-16 R13 (full): a ContextVar lets us thread user-provided custom-type
# aliases from the top of ``parse_versions_dir`` / ``load_model_schema``
# down to every call site of ``normalize_ast_type``/``normalize_type``
# without changing every signature in between. Set via
# :func:`custom_types_scope`.
_CURRENT_CUSTOM_TYPES: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "gravitar_custom_types", default=None
)

# SPEC-16 R2 (full): per-file import alias map set by the parser before
# walking a revision. Resolves ``from sqlalchemy import String as Str``
# so a bare ``Str(50)`` in the AST canonicalizes to String before
# dispatch. Scoped per-file (set via :func:`type_import_aliases_scope`)
# rather than per-run because every migration file defines its own
# imports.
_CURRENT_IMPORT_ALIASES: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "gravitar_type_import_aliases", default=None
)


class custom_types_scope:  # noqa: N801 — context-manager class, lowercase like contextlib.*
    """SPEC-16 R13 (full): set the custom-type map for the duration of a
    ``with`` block. The map is read by ``normalize_type``/``normalize_ast_type``
    when no explicit ``custom_types=`` argument is passed. Nested scopes
    stack (contextvars' token-reset semantics) so a test can override
    without leaking state."""

    def __init__(self, custom_types: dict[str, str] | None) -> None:
        self._custom_types = custom_types
        self._token: contextvars.Token[dict[str, str] | None] | None = None

    def __enter__(self) -> custom_types_scope:
        self._token = _CURRENT_CUSTOM_TYPES.set(self._custom_types)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _CURRENT_CUSTOM_TYPES.reset(self._token)
            self._token = None


def _effective_custom_types(explicit: dict[str, str] | None) -> dict[str, str] | None:
    """An explicit kwarg beats the ambient ContextVar — tests and library
    users with their own map get what they passed; CLI runs get the
    ContextVar their ``parse_versions_dir`` call installed."""
    if explicit is not None:
        return explicit
    return _CURRENT_CUSTOM_TYPES.get()


class type_import_aliases_scope:  # noqa: N801 — context manager, lowercase like contextlib.*
    """SPEC-16 R2 (full): set the per-file import-alias map for the
    duration of a ``with`` block. The map lets ``_canonicalize_name``
    resolve ``Str`` → ``String`` when a migration writes
    ``from sqlalchemy import String as Str``."""

    def __init__(self, aliases: dict[str, str] | None) -> None:
        self._aliases = aliases
        self._token: contextvars.Token[dict[str, str] | None] | None = None

    def __enter__(self) -> type_import_aliases_scope:
        self._token = _CURRENT_IMPORT_ALIASES.set(self._aliases)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _CURRENT_IMPORT_ALIASES.reset(self._token)
            self._token = None


def _format_string(t: Any) -> str:
    return f"varchar({t.length})" if t.length else "varchar"


def _format_numeric(t: Any) -> str:
    if t.precision and t.scale is not None:
        return f"numeric({t.precision},{t.scale})"
    if t.precision:
        return f"numeric({t.precision})"
    return "numeric"


def _format_datetime(t: Any) -> str:
    return "timestamptz" if t.timezone else "timestamp"


def _format_enum(t: Any) -> str:
    values = ",".join(sorted(t.enums))
    return f"enum({values})"


def _format_array(t: Any) -> str:
    return f"array({normalize_type(t.item_type)})"


_Formatter = Callable[[Any], str]

# Order matters: subclasses before their base classes.
_DISPATCH: list[tuple[type, _Formatter]] = [
    (sqltypes.Enum, _format_enum),
    (postgresql.JSONB, lambda t: "jsonb"),
    (sqltypes.JSON, lambda t: "json"),
    (sqltypes.Text, lambda t: "text"),
    (sqltypes.String, _format_string),
    (sqltypes.BigInteger, lambda t: "bigint"),
    (sqltypes.SmallInteger, lambda t: "smallint"),
    (sqltypes.Integer, lambda t: "integer"),
    (sqltypes.Float, lambda t: "double precision"),
    (sqltypes.Numeric, _format_numeric),
    (sqltypes.Boolean, lambda t: "boolean"),
    # SPEC-16 R2: Interval subclasses DateTime — must come before DateTime
    # in the dispatch order or an Interval instance resolves to "timestamp".
    (sqltypes.Interval, lambda t: "interval"),
    (sqltypes.DateTime, _format_datetime),
    (sqltypes.Date, lambda t: "date"),
    (sqltypes.Time, lambda t: "time"),
    (postgresql.UUID, lambda t: "uuid"),
    (sqltypes.Uuid, lambda t: "uuid"),
    (sqltypes.LargeBinary, lambda t: "bytea"),
    (sqltypes.ARRAY, _format_array),
]


def normalize_type(t: Any, custom_types: dict[str, str] | None = None) -> str:
    """Normalize a SQLAlchemy type instance to its canonical string representation.

    SPEC-16 Amendment R13: a ``TypeDecorator`` subclass unwraps one layer to
    its ``impl`` before dispatch (``class JSONEncoded(TypeDecorator): impl =
    JSON`` resolves to ``json``). ``TypeDecorator.impl`` is a class on the
    class itself but gets instantiated on an instance — handle both.

    SPEC-16 R13 (full): ``custom_types`` is the user-provided project-local
    type map from ``.gravitar.yml``. When a type doesn't resolve through the
    built-in dispatch, the class's ``__name__`` is looked up in
    ``custom_types`` before falling to ``unknown:`` — a project that pins
    ``JsonBlob: json`` in its config stops reading as type_mismatch against
    model-side ``JSON`` without touching source.

    Types that cannot be normalized (custom SQLAlchemy types outside the
    allowlist, dialect types not listed above, custom TypeDecorators whose
    impl is itself custom) are returned as ``unknown:<ClassName>``.
    """
    effective = _effective_custom_types(custom_types)
    # Check the explicit dispatch list FIRST. Several built-in SA types are
    # TypeDecorator subclasses under the hood (``Interval``, ``PickleType``);
    # their canonical shape is their own, not their impl's. Only fall through
    # to the TypeDecorator unwrap when the dispatch couldn't match.
    for type_class, formatter in _DISPATCH:
        if isinstance(t, type_class):
            return formatter(t)

    class_name = type(t).__name__
    # Custom-type pin applies BEFORE the TypeDecorator impl unwrap: a
    # project that explicitly says ``PasswordHash: varchar(60)`` means it
    # ever to resolve to varchar(60), even if the TypeDecorator's impl
    # would otherwise expand to something else.
    if effective and class_name in effective:
        return effective[class_name]

    if isinstance(t, sqltypes.TypeDecorator):
        impl = getattr(t, "impl_instance", None) or getattr(t, "impl", None)
        if isinstance(impl, type):
            # impl is still a class — instantiate the no-arg form to feed
            # the dispatch below. If this ever raises for a type that needs
            # args, the except falls back to "unknown:" and we don't crash.
            try:
                impl = impl()
            except Exception:  # noqa: BLE001
                impl = None
        if impl is not None and not isinstance(impl, sqltypes.TypeDecorator):
            # Guard against infinite recursion if impl is itself a decorator
            return normalize_type(impl, custom_types=effective)

    return f"unknown:{class_name}"


# SPEC-16 Amendment R2: uppercase/alias names (D3/D4). Migrations in the wild
# write ``sa.VARCHAR``, ``postgresql.TIMESTAMP``, ``sa.INTEGER`` — these are
# genuine SQLAlchemy types that resolve to the same canonical shape as their
# proper-case siblings. Mapping here keeps the AST dispatch below honest about
# what it understood, rather than returning ``unknown:VARCHAR``.
_TYPE_ALIASES: dict[str, str] = {
    "VARCHAR": "String",
    "CHAR": "String",
    "NVARCHAR": "String",
    "NCHAR": "String",
    "Unicode": "String",
    "UnicodeText": "Text",
    "TEXT": "Text",
    "CLOB": "Text",
    "INTEGER": "Integer",
    "INT": "Integer",
    "BIGINT": "BigInteger",
    "SMALLINT": "SmallInteger",
    "FLOAT": "Float",
    "REAL": "Float",
    "DOUBLE_PRECISION": "Float",
    "NUMERIC": "Numeric",
    "DECIMAL": "Numeric",
    "BOOLEAN": "Boolean",
    "BOOL": "Boolean",
    "TIMESTAMP": "DateTime",
    "DATETIME": "DateTime",
    "DATE": "Date",
    "TIME": "Time",
    "INTERVAL": "Interval",
    "BLOB": "LargeBinary",
    "BYTEA": "LargeBinary",
    "BINARY": "LargeBinary",
    "VARBINARY": "LargeBinary",
}

# SPEC-16 Amendment R2 (D10): first-positional-ast.Call selection in a
# ``sa.Column(...)`` must skip these — they're constraints, defaults, or
# generators that happen to be ast.Call nodes but aren't the column *type*.
NON_TYPE_CALL_NAMES: frozenset[str] = frozenset(
    {
        "ForeignKey",
        "Sequence",
        "Identity",
        "Computed",
        "Index",
        "CheckConstraint",
        "UniqueConstraint",
        "PrimaryKeyConstraint",
        "ForeignKeyConstraint",
        "Constraint",
    }
)

# R2: ``BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")``
# resolves to the receiver's base type on dialects other than the variant.
# ``.evaluates_none()`` and ``.copy()`` are similar passthroughs. The resolver
# unwraps as many of these as it finds, then dispatches on the inner node.
_PASSTHROUGH_METHODS: frozenset[str] = frozenset({"with_variant", "evaluates_none", "copy"})


def _call_type_name(node: ast.Call) -> str | None:
    """The bare identifier a type constructor call was written with, e.g. "String"
    for both `sa.String(50)` and a bare `String(50)`. Import aliases (`String as Str`)
    are not resolved — the literal source text is what's matched."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _bare_type_name(node: ast.expr) -> str | None:
    """R2: the identifier for a class-form reference — ``sa.Integer`` or just
    ``Integer`` with no parentheses. Pre-R2 these resolved to ``unknown:...``
    because the dispatch only handled ``ast.Call``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _unwrap_passthroughs(node: ast.expr) -> ast.expr:
    """R2: unwrap ``.with_variant(...)``, ``.evaluates_none(...)``, and
    ``.copy()`` chains to their receiver. ``BigInteger().with_variant(X,
    "mysql")`` becomes ``BigInteger()``; the receiver is what counts for
    the canonical type on this dialect."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PASSTHROUGH_METHODS
    ):
        node = node.func.value
    return node


def _literal_call_args(node: ast.Call) -> tuple[list[Any], dict[str, Any]]:
    args = []
    for arg in node.args:
        try:
            args.append(ast.literal_eval(arg))
        except ValueError:
            args.append(None)
    kwargs = {}
    for kw in node.keywords:
        if kw.arg is None:
            continue
        try:
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        except ValueError:
            kwargs[kw.arg] = None
    return args, kwargs


def _canonicalize_name(type_name: str) -> str:
    """Canonicalize a type name through:
    1. SPEC-16 R2 import aliases (``from sqlalchemy import String as Str``
       → ``Str`` canonicalizes to ``String``).
    2. SPEC-16 R2 subset uppercase aliases (``VARCHAR`` → ``String``).

    Import aliases apply first so a user's own name wins over a built-in
    alias collision (unlikely in practice but defined behavior)."""
    imports = _CURRENT_IMPORT_ALIASES.get()
    if imports and type_name in imports:
        type_name = imports[type_name]
    return _TYPE_ALIASES.get(type_name, type_name)


def _dispatch_name(type_name: str, args: list[Any], kwargs: dict[str, Any]) -> str:
    """Dispatch on the resolved (post-alias) type name, with the parsed
    positional/keyword args. ``args``/``kwargs`` are empty for class-form
    references (no call), so parameterized types fall through to their
    default form (``String`` → ``varchar``, ``Numeric`` → ``numeric``)."""
    if type_name == "Enum":
        values = sorted(v for v in args if isinstance(v, str))
        return f"enum({','.join(values)})"
    if type_name == "String":
        length = args[0] if args else kwargs.get("length")
        return f"varchar({length})" if length else "varchar"
    if type_name == "Text":
        return "text"
    if type_name == "BigInteger":
        return "bigint"
    if type_name == "SmallInteger":
        return "smallint"
    if type_name == "Integer":
        return "integer"
    if type_name == "Float":
        return "double precision"
    if type_name == "Numeric":
        precision = args[0] if len(args) > 0 else kwargs.get("precision")
        scale = args[1] if len(args) > 1 else kwargs.get("scale")
        if precision and scale is not None:
            return f"numeric({precision},{scale})"
        if precision:
            return f"numeric({precision})"
        return "numeric"
    if type_name == "Boolean":
        return "boolean"
    if type_name == "DateTime":
        return "timestamptz" if kwargs.get("timezone") else "timestamp"
    if type_name == "Date":
        return "date"
    if type_name == "Time":
        return "time"
    if type_name == "Interval":
        return "interval"
    if type_name == "JSONB":
        return "jsonb"
    if type_name == "JSON":
        return "json"
    if type_name in ("UUID", "Uuid"):
        return "uuid"
    if type_name == "LargeBinary":
        return "bytea"
    return f"unknown:{type_name}"


def normalize_ast_type(node: ast.expr, custom_types: dict[str, str] | None = None) -> str:
    """Normalize a type expression from an unexecuted migration AST into the
    same canonical string ``normalize_type`` produces for live instances.

    Accepts both forms:
    - **Call form**: ``sa.String(50)``, ``Numeric(10, 2)``, ``Enum("a", "b")``.
    - **Class form** (R2): ``sa.Integer`` or bare ``Integer`` — no parentheses.
    - **Passthrough chain** (R2): ``BigInteger().with_variant(X, "mysql")``
      unwraps to ``BigInteger()``.
    - **Alias/uppercase names** (R2): ``VARCHAR``, ``INTEGER``, ``TIMESTAMP``,
      ``DECIMAL`` etc. map to the canonical camelcase name through
      ``_TYPE_ALIASES`` before dispatch.

    SPEC-16 R13 (full): ``custom_types`` is consulted BEFORE dispatch so a
    user-pinned name (``JsonBlob``) wins over the resolver's own
    ``unknown:`` fallback and over any accidental alias collision.
    """
    effective = _effective_custom_types(custom_types)
    node = _unwrap_passthroughs(node)

    # ARRAY needs special-case recursion before anything else: its item type is
    # itself a type expression, not a literal, so ``_literal_call_args`` can't
    # handle it directly.
    if isinstance(node, ast.Call):
        name = _call_type_name(node)
        if name == "ARRAY":
            if not node.args:
                return "unknown:ARRAY()"
            return f"array({normalize_ast_type(node.args[0], custom_types=effective)})"

    # Class form: just a reference, no call. Dispatches with empty args/kwargs.
    bare_name = _bare_type_name(node) if not isinstance(node, ast.Call) else None
    if bare_name is not None:
        if effective and bare_name in effective:
            return effective[bare_name]
        return _dispatch_name(_canonicalize_name(bare_name), args=[], kwargs={})

    if not isinstance(node, ast.Call):
        return f"unknown:{ast.dump(node)[:60]}"

    type_name = _call_type_name(node)
    if type_name is None:
        return f"unknown:{ast.dump(node)[:60]}"

    if effective and type_name in effective:
        return effective[type_name]
    args, kwargs = _literal_call_args(node)
    return _dispatch_name(_canonicalize_name(type_name), args, kwargs)
