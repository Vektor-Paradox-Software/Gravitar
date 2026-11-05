"""Django declarative-model introspection (ModelSchema builder), mirroring
`schema/models.py`'s role for SQLAlchemy models. Not formalized by SPEC-
GENERALIZATION-ENGINE.md Section 4 (which only generalizes the migration-
parsing side) but required for `gravitar check` to run end-to-end against a
Django project at all — there's no drift to detect without a model-side
schema to compare against. Scoped identically to the migration side: a single
app, the field types `django_types.py` recognizes, no custom-field
resolution.

Unlike Alembic migrations, Django model classes genuinely require Django's
app registry to be configured (`django.setup()`) before they can even be
*defined* — verified empirically: importing a plain `models.Model` subclass
without it raises `ImproperlyConfigured`. That's a real, load-bearing
difference from the migration side (`django_parser.py`), not an oversight.
"""

from __future__ import annotations

import importlib

import django
from django.conf import settings as django_settings
from django.db.models import Model

from gravitar.errors import ConfigError
from gravitar.schema import Column, Schema, Table
from gravitar.schema.parsers.django_types import normalize_django_field


def ensure_django_configured(app_paths: list[str]) -> None:
    """Configure Django's app registry once per process.

    Django's registry is a process-global singleton — safe for `gravitar
    check` (one process per invocation) but not for reconfiguring with a
    *different* app list within one process (raises RuntimeError). Only
    matters for tests, which isolate Django-dependent runs in subprocesses
    instead (see test_cli.py) rather than fighting the global state.
    """
    if django_settings.configured:
        return
    django_settings.configure(
        INSTALLED_APPS=list(app_paths), DEFAULT_AUTO_FIELD="django.db.models.AutoField"
    )
    django.setup()


def load_django_model_schema(app_paths: list[str]) -> Schema:
    ensure_django_configured(app_paths)

    tables: Schema = {}
    for app_path in app_paths:
        module_name = f"{app_path}.models"
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            raise ConfigError(f"cannot import Django models module '{module_name}': {e}") from e

        module_file = getattr(module, "__file__", None) or f"<{module_name}>"
        for name in dir(module):
            obj = getattr(module, name)
            if not _is_concrete_model(obj):
                continue
            table = _build_table(obj, module_file)
            tables[table["name"]] = table

    return tables


def _is_concrete_model(obj: object) -> bool:
    # mypy can't narrow `obj` past `type` here (Model resolves to Any without
    # django-stubs), so ._meta is genuinely unknown to it — real at runtime.
    return (
        isinstance(obj, type)
        and issubclass(obj, Model)
        and not obj._meta.abstract  # type: ignore[attr-defined]
        and not obj._meta.proxy  # type: ignore[attr-defined]
    )


def _build_table(model: type[Model], module_file: str) -> Table:
    columns: dict[str, Column] = {}
    for field in model._meta.get_fields():
        # concrete=False (reverse relations) and many_to_many (through-table
        # backed, no column on this table) aren't represented in Gravitar's
        # Schema, same as relations generally (see django_types.py).
        if not getattr(field, "concrete", False) or getattr(field, "many_to_many", False):
            continue
        columns[field.attname] = Column(
            name=field.attname,
            type=normalize_django_field(field),
            nullable=bool(getattr(field, "null", False)),
            primary_key=bool(getattr(field, "primary_key", False)),
            source_file=module_file,
            source_line=None,
        )
    return Table(
        name=model._meta.db_table, columns=columns, source_file=module_file, source_line=None
    )
