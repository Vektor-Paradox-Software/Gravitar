"""SQLAlchemy declarative-model introspection (ModelSchema builder)."""

from __future__ import annotations

import importlib
import inspect

from sqlalchemy import MetaData
from sqlalchemy import Table as SATable
from sqlalchemy.orm import registry as Registry

from gravitar.errors import ConfigError
from gravitar.schema import Column, Schema, Table
from gravitar.schema.types import normalize_type


def load_model_schema(package_paths: list[str], base_classes: list[str] | None = None) -> Schema:
    """Import model packages and extract schema from SQLAlchemy metadata.

    Declarative bases are detected structurally — an object with both a `.metadata`
    (MetaData) and a `.registry` (registry) — rather than by isinstance-checking a
    specific metaclass. Legacy `declarative_base()` bases and SQLAlchemy 2.0-style
    `DeclarativeBase` subclasses both satisfy this without sharing a common type.

    If `base_classes` is given (matching the `.gravitar.yml` `models.base_classes`
    field), only module attributes with those names are considered; otherwise every
    declarative base found is used.
    """
    tables: Schema = {}
    seen_metadata: set[int] = set()

    for package_path in package_paths:
        try:
            module = importlib.import_module(package_path)
        except ImportError as e:
            raise ConfigError(f"cannot import models package '{package_path}': {e}") from e

        module_file = getattr(module, "__file__", None) or f"<{package_path}>"

        for name in dir(module):
            if base_classes is not None and name not in base_classes:
                continue
            obj = getattr(module, name)
            if not _is_declarative_base(obj):
                continue

            metadata: MetaData = obj.metadata
            if id(metadata) in seen_metadata:
                continue
            seen_metadata.add(id(metadata))

            table_sources = _table_source_locations(obj)

            for table_name, sa_table in metadata.tables.items():
                source_file, source_line = table_sources.get(table_name, (module_file, None))
                tables[table_name] = Table(
                    name=table_name,
                    columns={
                        col.name: Column(
                            name=col.name,
                            type=normalize_type(col.type),
                            nullable=bool(col.nullable),
                            primary_key=bool(col.primary_key),
                            source_file=source_file,
                            source_line=None,
                        )
                        for col in sa_table.columns
                    },
                    source_file=source_file,
                    source_line=source_line,
                )

    return tables


def _is_declarative_base(obj: object) -> bool:
    return (
        isinstance(obj, type)
        and isinstance(getattr(obj, "metadata", None), MetaData)
        and isinstance(getattr(obj, "registry", None), Registry)
    )


def _table_source_locations(base: type) -> dict[str, tuple[str, int | None]]:
    """Best-effort table_name -> (source_file, class definition line) via the mapper registry.

    Tables not backed by a mapped class (bare association tables declared via Core
    `Table(...)`) are absent from the result; callers fall back to the module file
    with no line number.
    """
    locations: dict[str, tuple[str, int | None]] = {}
    registry: Registry = base.registry  # type: ignore[attr-defined]
    for mapper in registry.mappers:
        table = mapper.local_table
        if not isinstance(table, SATable):
            continue
        try:
            source_file = inspect.getsourcefile(mapper.class_)
            _, line = inspect.getsourcelines(mapper.class_)
        except (OSError, TypeError):
            continue
        if source_file is not None:
            locations[table.name] = (source_file, line)
    return locations
