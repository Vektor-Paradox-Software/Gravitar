"""Django field type normalization tests.

Field instances are constructed bare (never attached to a model, never needing
`django.setup()`) — verified in django_parser.py's own module docstring; this
mirrors that same fact for a standalone field.
"""

from django.db import models

from gravitar.schema.parsers.django_types import normalize_django_field


def test_char_field_with_length() -> None:
    assert normalize_django_field(models.CharField(max_length=100)) == "varchar(100)"


def test_char_field_without_length() -> None:
    assert normalize_django_field(models.CharField()) == "varchar"


def test_text_field() -> None:
    assert normalize_django_field(models.TextField()) == "text"


def test_integer_field() -> None:
    assert normalize_django_field(models.IntegerField()) == "integer"


def test_big_integer_field() -> None:
    assert normalize_django_field(models.BigIntegerField()) == "bigint"


def test_small_integer_field() -> None:
    assert normalize_django_field(models.SmallIntegerField()) == "smallint"


def test_positive_integer_field_is_still_integer() -> None:
    assert normalize_django_field(models.PositiveIntegerField()) == "integer"


def test_positive_big_integer_field_is_still_bigint() -> None:
    assert normalize_django_field(models.PositiveBigIntegerField()) == "bigint"


def test_auto_field_is_integer() -> None:
    assert normalize_django_field(models.AutoField()) == "integer"


def test_big_auto_field_is_bigint() -> None:
    assert normalize_django_field(models.BigAutoField()) == "bigint"


def test_small_auto_field_is_smallint() -> None:
    assert normalize_django_field(models.SmallAutoField()) == "smallint"


def test_decimal_field_with_precision_and_scale() -> None:
    field = models.DecimalField(max_digits=10, decimal_places=2)
    assert normalize_django_field(field) == "numeric(10,2)"


def test_decimal_field_with_only_max_digits() -> None:
    field = models.DecimalField(max_digits=10, decimal_places=None)
    assert normalize_django_field(field) == "numeric(10)"


def test_decimal_field_with_neither_max_digits_nor_decimal_places() -> None:
    field = models.DecimalField(max_digits=None, decimal_places=None)
    assert normalize_django_field(field) == "numeric"


def test_float_field() -> None:
    assert normalize_django_field(models.FloatField()) == "double precision"


def test_boolean_field() -> None:
    assert normalize_django_field(models.BooleanField()) == "boolean"


def test_datetime_field_is_not_normalized_as_date() -> None:
    assert normalize_django_field(models.DateTimeField()) == "timestamp"


def test_date_field() -> None:
    assert normalize_django_field(models.DateField()) == "date"


def test_time_field() -> None:
    assert normalize_django_field(models.TimeField()) == "time"


def test_uuid_field() -> None:
    assert normalize_django_field(models.UUIDField()) == "uuid"


def test_binary_field() -> None:
    assert normalize_django_field(models.BinaryField()) == "bytea"


def test_json_field() -> None:
    assert normalize_django_field(models.JSONField()) == "jsonb"


def test_unrecognized_field_falls_back_to_unknown() -> None:
    assert normalize_django_field(models.DurationField()) == "unknown:DurationField"


def test_foreign_key_falls_back_to_unknown() -> None:
    # Relations are a Milestone 10 non-goal (see django_types.py docstring).
    result = normalize_django_field(models.ForeignKey("self", on_delete=models.CASCADE))
    assert result == "unknown:ForeignKey"
