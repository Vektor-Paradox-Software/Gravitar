"""SPEC-16 Amendment R13 (full): config ``types:`` mapping.

Projects routinely ship their own column types — Airflow's ``ExtendedJSON``,
MLflow's ``JsonBlob``, every SaaS codebase's ``PasswordHash`` or
``TimestampTz``. Pre-R13, each read as ``unknown:X`` on the migration side
while the model side resolved to its underlying type, which the comparator
reported as a type_mismatch false positive.

R13 (full) adds a ``.gravitar.yml types:`` mapping so a user can pin
``JsonBlob: json`` without touching code. Unmapped custom types still
produce an ``unknown:`` type, visible via the unknown_type check.

The ``types:`` map applies to both sides:
- AST side (migration): a bare ``JsonBlob()`` or class-form ``JsonBlob``
  resolves to the pinned value before anything else.
- Live instance side (model): a ``TypeDecorator`` whose class name is in
  the map resolves to the pinned value, overriding the ``.impl`` unwrap.
"""

from __future__ import annotations

import ast
from pathlib import Path

from click.testing import CliRunner

from gravitar.cli import main
from gravitar.config import resolve_config
from gravitar.errors import EXIT_SUCCESS
from gravitar.schema.types import custom_types_scope, normalize_ast_type, normalize_type


class _JsonBlobLike:
    """Stand-in for a project-local TypeDecorator — matched by class name."""

    class __class__:  # pragma: no cover - never reached; __name__ only
        pass


def _write_models_with_table(root: Path, pkg: str, table: str, column_type: str) -> None:
    """Minimal SA models package where one column uses a specified type."""
    (root / pkg).mkdir(parents=True, exist_ok=True)
    (root / pkg / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer, JSON\n"
        "from sqlalchemy.types import TypeDecorator\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class JsonBlob(TypeDecorator):\n"
        "    impl = JSON\n"
        "    cache_ok = True\n"
        "class Row(Base):\n"
        f'    __tablename__ = "{table}"\n'
        "    id = Column(Integer, primary_key=True)\n"
        f"    data = Column({column_type})\n",
        encoding="utf-8",
    )


# --- Config parsing ---


def test_resolve_config_parses_types_mapping(tmp_path: Path) -> None:
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / ".gravitar.yml").write_text(
        "models:\n  paths: ['pkg_that_does_not_need_to_import']\n"
        "types:\n  JsonBlob: json\n  PasswordHash: 'varchar(60)'\n"
    )

    resolved = resolve_config(tmp_path, None)

    assert resolved.custom_types == {"JsonBlob": "json", "PasswordHash": "varchar(60)"}


def test_resolve_config_rejects_non_string_type_values(tmp_path: Path) -> None:
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / ".gravitar.yml").write_text("types:\n  JsonBlob: 42\n")

    runner = CliRunner()
    result = runner.invoke(main, ["check", str(tmp_path)])

    assert result.exit_code != EXIT_SUCCESS
    assert "types" in result.output or "types" in result.stderr_bytes.decode("utf-8", "replace")


# --- normalize_ast_type with custom_types ---


def test_normalize_ast_type_resolves_custom_class_form() -> None:
    """``JsonBlob`` (no parens) with a pinned mapping resolves to the
    pinned value, not ``unknown:JsonBlob``."""
    node = ast.parse("JsonBlob", mode="eval").body

    result = normalize_ast_type(node, custom_types={"JsonBlob": "json"})

    assert result == "json"


def test_normalize_ast_type_resolves_custom_call_form() -> None:
    """``JsonBlob()`` resolves identically to class form — the call args
    are ignored when a pin is present (the user accepted that in the
    config)."""
    node = ast.parse("JsonBlob(42)", mode="eval").body

    result = normalize_ast_type(node, custom_types={"JsonBlob": "json"})

    assert result == "json"


def test_normalize_ast_type_without_mapping_stays_unknown() -> None:
    """Unmapped custom types stay as ``unknown:X`` — the unknown_type
    check surfaces them for the user to add to their config."""
    node = ast.parse("JsonBlob", mode="eval").body

    result = normalize_ast_type(node, custom_types={})

    assert result.startswith("unknown:")


def test_normalize_ast_type_custom_type_beats_alias() -> None:
    """A user's pin wins over the built-in alias map — pinning
    ``TIMESTAMP: timestamptz`` must give timestamptz even though
    ``TIMESTAMP`` is in the alias map (→ DateTime → timestamp)."""
    node = ast.parse("TIMESTAMP()", mode="eval").body

    result = normalize_ast_type(node, custom_types={"TIMESTAMP": "timestamptz"})

    assert result == "timestamptz"


# --- normalize_type (live instance side) ---


def test_normalize_type_resolves_custom_type_decorator_via_pin() -> None:
    """A project's own TypeDecorator — class name only — resolves to
    the pinned value, overriding the impl-unwrap chain."""
    # Set up a TypeDecorator subclass with the name we're pinning.
    from sqlalchemy import JSON
    from sqlalchemy.types import TypeDecorator

    class CustomBlob(TypeDecorator):  # type: ignore[type-arg]
        impl = JSON
        cache_ok = True

    result = normalize_type(CustomBlob(), custom_types={"CustomBlob": "varchar(60)"})

    assert result == "varchar(60)"


def test_normalize_type_falls_through_to_impl_unwrap_without_pin() -> None:
    """Without a pin, a TypeDecorator still unwraps to its impl (the
    pre-R13-full behavior) — a project using ``JsonBlob(JSON)`` with no
    config still gets ``json``."""
    from sqlalchemy import JSON
    from sqlalchemy.types import TypeDecorator

    class AnotherBlob(TypeDecorator):  # type: ignore[type-arg]
        impl = JSON
        cache_ok = True

    result = normalize_type(AnotherBlob(), custom_types={})

    assert result == "json"


# --- custom_types_scope ---


def test_custom_types_scope_sets_contextvar_for_nested_calls() -> None:
    """The scope context manager installs the map so inner calls that
    don't pass ``custom_types=`` explicitly still see it. This is how
    the walker's deep handlers pick up the user's pin."""
    node = ast.parse("Pinned", mode="eval").body

    with custom_types_scope({"Pinned": "varchar(10)"}):
        result = normalize_ast_type(node)

    assert result == "varchar(10)"


def test_custom_types_scope_restores_prior_state() -> None:
    """Nested scopes must restore cleanly — a test harness setting a
    scope must not leak into unrelated tests."""
    node = ast.parse("Pinned", mode="eval").body

    with custom_types_scope({"Pinned": "a"}):
        with custom_types_scope({"Pinned": "b"}):
            assert normalize_ast_type(node) == "b"
        assert normalize_ast_type(node) == "a"
    assert normalize_ast_type(node).startswith("unknown:")


def test_explicit_arg_beats_contextvar() -> None:
    """A caller that passes ``custom_types=`` explicitly wins over
    the ambient map — lets tests and library users override."""
    node = ast.parse("Pinned", mode="eval").body

    with custom_types_scope({"Pinned": "ambient"}):
        result = normalize_ast_type(node, custom_types={"Pinned": "explicit"})

    assert result == "explicit"


# --- End-to-end via CLI ---


def test_cli_types_map_eliminates_type_mismatch(tmp_path: Path) -> None:
    """Headline case: model declares a TypeDecorator-backed column,
    migration adds it with the same TypeDecorator reference. Pre-R13
    this reported type_mismatch (model: json, migration:
    unknown:JsonBlob). With a pin, the mismatch disappears."""
    _write_models_with_table(tmp_path, "models", "widgets", "JsonBlob()")

    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        '"""create widgets"""\n\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        'revision = "0001"\n'
        "down_revision = None\n\n"
        "class JsonBlob(sa.types.TypeDecorator):\n"
        "    impl = sa.JSON\n"
        "    cache_ok = True\n\n"
        "def upgrade():\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("data", JsonBlob()),\n'
        "    )\n"
    )
    (tmp_path / ".gravitar.yml").write_text("types:\n  JsonBlob: json\n")

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--no-color"])

    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "no drift detected" in result.output


def test_cli_without_types_map_shows_type_mismatch(tmp_path: Path) -> None:
    """Confirms the pre-R13-full baseline: without the pin, the same
    setup surfaces a drift finding on the data column — so the test
    above (with the pin) is actually exercising the R13 fix.

    Note: ``unknown_type`` is warning-tier (Decision #1), so a bare
    exit code check isn't sufficient; use JSON output to see findings.
    """
    import json

    _write_models_with_table(tmp_path, "models", "widgets", "JsonBlob()")

    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "alembic" / "versions" / "0001.py").write_text(
        '"""create widgets"""\n\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        'revision = "0001"\n'
        "down_revision = None\n\n"
        "class JsonBlob(sa.types.TypeDecorator):\n"
        "    impl = sa.JSON\n"
        "    cache_ok = True\n\n"
        "def upgrade():\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("data", JsonBlob()),\n'
        "    )\n"
    )
    # No .gravitar.yml → no types: map

    result = CliRunner().invoke(main, ["check", str(tmp_path), "--format", "json", "--no-color"])

    data = json.loads(result.output)
    # Without the pin, the data column's type reads as unknown:JsonBlob,
    # which the comparator reports (as type_mismatch or unknown_type).
    data_findings = [f for f in data["findings"] if f.get("column") == "data"]
    assert data_findings, data
