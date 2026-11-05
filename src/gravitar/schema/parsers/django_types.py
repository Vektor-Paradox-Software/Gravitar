"""Django field type normalization, mirroring `schema/types.py`'s role for
SQLAlchemy types: both dispatch on a live field/type *instance*, not source
text, since Django field objects (unlike Alembic's AST) are already fully
resolved by the time this module ever sees them.

Dispatch is order-sensitive for the same reason `types.py`'s is: Django's
auto-PK fields subclass their plain integer counterparts (`BigAutoField` <
`BigIntegerField` < `IntegerField`), the "positive" variants subclass their
signed counterparts, and `DateTimeField` subclasses `DateField`. Each subclass
must be checked before its base or it's silently misnormalized as the base.

Imports `django` at module level — safe because this module is only ever
imported from `django_parser.py`, itself only imported once Django mode is
actually selected (see cli.py), so a pure-Alembic install never needs Django
installed at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db import models as django_models


def _format_char(f: Any) -> str:
    return f"varchar({f.max_length})" if f.max_length else "varchar"


def _format_decimal(f: Any) -> str:
    if f.max_digits and f.decimal_places is not None:
        return f"numeric({f.max_digits},{f.decimal_places})"
    if f.max_digits:
        return f"numeric({f.max_digits})"
    return "numeric"


_Formatter = Callable[[Any], str]

# Order matters — see module docstring.
_DISPATCH: list[tuple[type, _Formatter]] = [
    (django_models.BigAutoField, lambda f: "bigint"),
    (django_models.SmallAutoField, lambda f: "smallint"),
    (django_models.AutoField, lambda f: "integer"),
    (django_models.PositiveBigIntegerField, lambda f: "bigint"),
    (django_models.PositiveSmallIntegerField, lambda f: "smallint"),
    (django_models.PositiveIntegerField, lambda f: "integer"),
    (django_models.BigIntegerField, lambda f: "bigint"),
    (django_models.SmallIntegerField, lambda f: "smallint"),
    (django_models.IntegerField, lambda f: "integer"),
    (django_models.DecimalField, _format_decimal),
    (django_models.FloatField, lambda f: "double precision"),
    (django_models.BooleanField, lambda f: "boolean"),
    (django_models.TextField, lambda f: "text"),
    (django_models.CharField, _format_char),
    # DateTimeField subclasses DateField — must precede it.
    (django_models.DateTimeField, lambda f: "timestamp"),
    (django_models.DateField, lambda f: "date"),
    (django_models.TimeField, lambda f: "time"),
    (django_models.UUIDField, lambda f: "uuid"),
    (django_models.BinaryField, lambda f: "bytea"),
    (django_models.JSONField, lambda f: "jsonb"),
]


def normalize_django_field(field: Any) -> str:
    """Normalize a Django model field instance to Gravitar's canonical type
    string (the same vocabulary `types.normalize_type` produces for SQLAlchemy).

    Fields with no DB-level type equivalent in Gravitar's Schema (relations —
    `ForeignKey`, `ManyToManyField`, `OneToOneField` — are Milestone 10
    Non-goals) and any field type not listed above fall back to
    ``unknown:<ClassName>``, the same honest, non-crashing fallback
    `types.normalize_type` uses.
    """
    for field_class, formatter in _DISPATCH:
        if isinstance(field, field_class):
            return formatter(field)
    return f"unknown:{type(field).__name__}"
