"""Django model introspection tests (django_models.py) — required for
`gravitar check` to run end-to-end against a Django project
(see django_models.py's docstring).

Django's app registry is a process-global singleton configured exactly once
(`ensure_django_configured`) — this module therefore uses a single tmp_path
app and a single call to `load_django_model_schema`, asserting everything it
needs to in one pass, rather than many independent tests each trying to
reconfigure Django with a different app list (which raises). Broader
end-to-end coverage across scenarios comes from the subprocess-isolated
Django fixture suite (test_django_fixtures.py) instead.
"""

import pytest

from gravitar.schema.parsers.django_models import load_django_model_schema


@pytest.fixture(scope="module")
def schema(tmp_path_factory: pytest.TempPathFactory) -> dict:
    app_dir = tmp_path_factory.mktemp("django_models_app") / "shop"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("")
    (app_dir / "models.py").write_text(
        """
from django.db import models


class AbstractBase(models.Model):
    class Meta:
        abstract = True


class Product(models.Model):
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    tags = models.ManyToManyField("Tag")


class Tag(models.Model):
    label = models.CharField(max_length=50)


class ProductProxy(Product):
    class Meta:
        proxy = True
"""
    )

    import sys

    sys.path.insert(0, str(app_dir.parent))
    try:
        yield load_django_model_schema(["shop"])
    finally:
        sys.path.remove(str(app_dir.parent))


def test_concrete_model_becomes_a_table(schema: dict) -> None:
    assert "shop_product" in schema


def test_abstract_model_is_not_a_table(schema: dict) -> None:
    assert "shop_abstractbase" not in schema


def test_proxy_model_is_not_a_separate_table(schema: dict) -> None:
    assert "shop_productproxy" not in schema
    assert set(schema) == {"shop_product", "shop_tag"}


def test_fields_are_normalized_and_carry_nullability(schema: dict) -> None:
    columns = schema["shop_product"]["columns"]
    assert columns["name"]["type"] == "varchar(100)"
    assert columns["name"]["nullable"] is False
    assert columns["price"]["type"] == "numeric(10,2)"
    assert columns["price"]["nullable"] is True


def test_auto_pk_is_included(schema: dict) -> None:
    columns = schema["shop_product"]["columns"]
    assert columns["id"]["primary_key"] is True
    assert columns["id"]["type"] == "integer"


def test_source_file_is_recorded(schema: dict) -> None:
    assert schema["shop_product"]["source_file"].endswith("models.py")


def test_many_to_many_field_is_not_a_column(schema: dict) -> None:
    assert "tags" not in schema["shop_product"]["columns"]


def test_reconfiguring_with_the_same_apps_is_a_harmless_no_op(schema: dict) -> None:
    from gravitar.schema.parsers.django_models import (
        ensure_django_configured,
        load_django_model_schema,
    )

    ensure_django_configured(["shop"])  # already configured — must not raise
    assert load_django_model_schema(["shop"]) == schema


def test_unimportable_app_raises_config_error() -> None:
    from gravitar.errors import ConfigError
    from gravitar.schema.parsers.django_models import load_django_model_schema

    with pytest.raises(ConfigError, match="cannot import"):
        load_django_model_schema(["not_a_real_django_app_xyz"])
