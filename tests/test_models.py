from pathlib import Path

import pytest

from gravitar.errors import ConfigError
from gravitar.schema.models import load_model_schema

FIXTURES = Path(__file__).parent / "fixtures" / "model_packages"


@pytest.fixture(autouse=True)
def _fixtures_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES))


def test_legacy_declarative_base_style() -> None:
    schema = load_model_schema(["legacy_declarative"])

    assert set(schema) == {"users"}
    users = schema["users"]
    assert set(users["columns"]) == {"id", "email", "created_at"}
    assert users["columns"]["email"]["type"] == "varchar(255)"
    assert users["columns"]["email"]["nullable"] is False
    assert users["columns"]["created_at"]["type"] == "timestamptz"
    assert users["columns"]["id"]["primary_key"] is True
    assert users["columns"]["email"]["primary_key"] is False


def test_modern_declarative_base_style() -> None:
    schema = load_model_schema(["modern_declarative"])

    assert set(schema) == {"orders"}
    assert schema["orders"]["columns"]["total_cents"]["type"] == "numeric(10,2)"
    assert schema["orders"]["columns"]["total_cents"]["nullable"] is False


def test_multiple_package_paths_are_merged() -> None:
    schema = load_model_schema(["legacy_declarative", "modern_declarative"])

    assert set(schema) == {"users", "orders"}


def test_base_classes_filter_restricts_discovery() -> None:
    schema = load_model_schema(["two_bases"], base_classes=["BaseA"])

    assert set(schema) == {"widgets"}


def test_no_base_classes_filter_discovers_all_bases() -> None:
    schema = load_model_schema(["two_bases"])

    assert set(schema) == {"widgets", "gadgets"}


def test_bare_association_table_falls_back_to_module_source() -> None:
    schema = load_model_schema(["bare_association_table"])

    assert set(schema) == {"widgets", "widget_tags"}

    widgets = schema["widgets"]
    assert widgets["source_line"] is not None
    assert widgets["source_line"] > 0
    assert widgets["source_file"].endswith("__init__.py")

    widget_tags = schema["widget_tags"]
    assert widget_tags["source_line"] is None
    assert widget_tags["source_file"].endswith("__init__.py")


def test_unimportable_package_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="does_not_exist"):
        load_model_schema(["does_not_exist"])


def test_source_file_recorded_for_mapped_table() -> None:
    schema = load_model_schema(["legacy_declarative"])

    assert schema["users"]["source_file"].endswith("__init__.py")
    assert schema["users"]["source_line"] is not None and schema["users"]["source_line"] > 0
