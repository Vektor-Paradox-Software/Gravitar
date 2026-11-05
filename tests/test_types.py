import ast
import random
import string

from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import types as sqltypes
from sqlalchemy.dialects import postgresql

from gravitar.schema.types import normalize_ast_type, normalize_type


def _expr(source: str) -> ast.expr:
    return ast.parse(source, mode="eval").body


def test_string_with_length() -> None:
    assert normalize_type(sqltypes.String(50)) == "varchar(50)"


def test_string_without_length() -> None:
    assert normalize_type(sqltypes.String()) == "varchar"


def test_text() -> None:
    assert normalize_type(sqltypes.Text()) == "text"


def test_integer() -> None:
    assert normalize_type(sqltypes.Integer()) == "integer"


def test_big_integer() -> None:
    assert normalize_type(sqltypes.BigInteger()) == "bigint"


def test_small_integer() -> None:
    assert normalize_type(sqltypes.SmallInteger()) == "smallint"


def test_numeric_precision_and_scale() -> None:
    assert normalize_type(sqltypes.Numeric(10, 2)) == "numeric(10,2)"


def test_numeric_precision_only() -> None:
    assert normalize_type(sqltypes.Numeric(10)) == "numeric(10)"


def test_numeric_bare() -> None:
    assert normalize_type(sqltypes.Numeric()) == "numeric"


def test_numeric_scale_zero_is_not_treated_as_missing() -> None:
    assert normalize_type(sqltypes.Numeric(10, 0)) == "numeric(10,0)"


def test_float() -> None:
    assert normalize_type(sqltypes.Float()) == "double precision"


def test_boolean() -> None:
    assert normalize_type(sqltypes.Boolean()) == "boolean"


def test_datetime_without_timezone() -> None:
    assert normalize_type(sqltypes.DateTime()) == "timestamp"


def test_datetime_with_timezone() -> None:
    assert normalize_type(sqltypes.DateTime(timezone=True)) == "timestamptz"


def test_date() -> None:
    assert normalize_type(sqltypes.Date()) == "date"


def test_time() -> None:
    assert normalize_type(sqltypes.Time()) == "time"


def test_json() -> None:
    assert normalize_type(sqltypes.JSON()) == "json"


def test_jsonb() -> None:
    assert normalize_type(postgresql.JSONB()) == "jsonb"


def test_uuid_postgresql_dialect() -> None:
    assert normalize_type(postgresql.UUID()) == "uuid"


def test_uuid_generic() -> None:
    assert normalize_type(sqltypes.Uuid()) == "uuid"


def test_large_binary() -> None:
    assert normalize_type(sqltypes.LargeBinary()) == "bytea"


def test_array_of_integer() -> None:
    assert normalize_type(sqltypes.ARRAY(sqltypes.Integer())) == "array(integer)"


def test_array_of_string() -> None:
    assert normalize_type(sqltypes.ARRAY(sqltypes.String(20))) == "array(varchar(20))"


def test_enum_values_sorted_alphabetically() -> None:
    assert normalize_type(sqltypes.Enum("b", "a", "c")) == "enum(a,b,c)"


def test_unknown_type_falls_back_to_class_name() -> None:
    class CustomType(sqltypes.TypeEngine):  # type: ignore[type-arg]
        pass

    assert normalize_type(CustomType()) == "unknown:CustomType"


# --- Subclass-ordering regressions ---
# These types subclass one another in SQLAlchemy; getting the dispatch order wrong
# silently misnormalizes the subclass as its base.


def test_enum_is_not_normalized_as_string() -> None:
    # sqlalchemy.types.Enum subclasses String — must not fall through to varchar(N).
    assert normalize_type(sqltypes.Enum("a", "b")) == "enum(a,b)"


def test_jsonb_is_not_normalized_as_json() -> None:
    assert normalize_type(postgresql.JSONB()) != "json"


def test_float_is_not_normalized_as_numeric() -> None:
    # Float subclasses Numeric — must not fall through to the numeric() branch.
    assert normalize_type(sqltypes.Float()) != "numeric"


def test_big_integer_is_not_normalized_as_integer() -> None:
    assert normalize_type(sqltypes.BigInteger()) != "integer"


def test_small_integer_is_not_normalized_as_integer() -> None:
    assert normalize_type(sqltypes.SmallInteger()) != "integer"


# --- Property tests: identical construction parameters must normalize identically ---


@given(st.one_of(st.none(), st.integers(min_value=1, max_value=2000)))
def test_string_normalization_is_deterministic(length: int | None) -> None:
    a, b = sqltypes.String(length=length), sqltypes.String(length=length)
    assert normalize_type(a) == normalize_type(b)


@given(
    st.one_of(st.none(), st.integers(min_value=1, max_value=38)),
    st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
)
def test_numeric_normalization_is_deterministic(precision: int | None, scale: int | None) -> None:
    a = sqltypes.Numeric(precision=precision, scale=scale)
    b = sqltypes.Numeric(precision=precision, scale=scale)
    assert normalize_type(a) == normalize_type(b)


@given(st.booleans())
def test_datetime_normalization_is_deterministic(timezone: bool) -> None:
    a, b = sqltypes.DateTime(timezone=timezone), sqltypes.DateTime(timezone=timezone)
    assert normalize_type(a) == normalize_type(b)


@given(
    st.lists(
        st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_enum_normalization_is_deterministic_regardless_of_declared_order(
    values: list[str],
) -> None:
    shuffled = values[:]
    random.shuffle(shuffled)
    assert normalize_type(sqltypes.Enum(*values)) == normalize_type(sqltypes.Enum(*shuffled))


@given(st.integers(min_value=1, max_value=500))
def test_array_normalization_is_deterministic(length: int) -> None:
    a = sqltypes.ARRAY(sqltypes.String(length))
    b = sqltypes.ARRAY(sqltypes.String(length))
    assert normalize_type(a) == normalize_type(b)


# --- normalize_ast_type: reconstructing types from unexecuted migration AST ---


def test_ast_string_with_length() -> None:
    assert normalize_ast_type(_expr("sa.String(50)")) == "varchar(50)"


def test_ast_string_without_length() -> None:
    assert normalize_ast_type(_expr("sa.String()")) == "varchar"


def test_ast_string_length_as_keyword() -> None:
    assert normalize_ast_type(_expr("sa.String(length=50)")) == "varchar(50)"


def test_ast_text() -> None:
    assert normalize_ast_type(_expr("sa.Text()")) == "text"


def test_ast_bare_name_without_module_prefix() -> None:
    assert normalize_ast_type(_expr("String(50)")) == "varchar(50)"


def test_ast_big_integer() -> None:
    assert normalize_ast_type(_expr("sa.BigInteger()")) == "bigint"


def test_ast_small_integer() -> None:
    assert normalize_ast_type(_expr("sa.SmallInteger()")) == "smallint"


def test_ast_integer() -> None:
    assert normalize_ast_type(_expr("sa.Integer()")) == "integer"


def test_ast_numeric_precision_and_scale() -> None:
    assert normalize_ast_type(_expr("sa.Numeric(10, 2)")) == "numeric(10,2)"


def test_ast_numeric_keyword_args() -> None:
    assert normalize_ast_type(_expr("sa.Numeric(precision=10, scale=2)")) == "numeric(10,2)"


def test_ast_numeric_precision_only() -> None:
    assert normalize_ast_type(_expr("sa.Numeric(10)")) == "numeric(10)"


def test_ast_numeric_bare() -> None:
    assert normalize_ast_type(_expr("sa.Numeric()")) == "numeric"


def test_ast_numeric_scale_zero_is_not_treated_as_missing() -> None:
    assert normalize_ast_type(_expr("sa.Numeric(10, 0)")) == "numeric(10,0)"


def test_ast_float() -> None:
    assert normalize_ast_type(_expr("sa.Float()")) == "double precision"


def test_ast_boolean() -> None:
    assert normalize_ast_type(_expr("sa.Boolean()")) == "boolean"


def test_ast_datetime_without_timezone() -> None:
    assert normalize_ast_type(_expr("sa.DateTime()")) == "timestamp"


def test_ast_datetime_with_timezone() -> None:
    assert normalize_ast_type(_expr("sa.DateTime(timezone=True)")) == "timestamptz"


def test_ast_date() -> None:
    assert normalize_ast_type(_expr("sa.Date()")) == "date"


def test_ast_time() -> None:
    assert normalize_ast_type(_expr("sa.Time()")) == "time"


def test_ast_json() -> None:
    assert normalize_ast_type(_expr("sa.JSON()")) == "json"


def test_ast_jsonb() -> None:
    assert normalize_ast_type(_expr("postgresql.JSONB()")) == "jsonb"


def test_ast_uuid() -> None:
    assert normalize_ast_type(_expr("postgresql.UUID()")) == "uuid"


def test_ast_generic_uuid() -> None:
    assert normalize_ast_type(_expr("sa.Uuid()")) == "uuid"


def test_ast_large_binary() -> None:
    assert normalize_ast_type(_expr("sa.LargeBinary()")) == "bytea"


def test_ast_array_of_integer() -> None:
    assert normalize_ast_type(_expr("sa.ARRAY(sa.Integer())")) == "array(integer)"


def test_ast_array_of_string() -> None:
    assert normalize_ast_type(_expr("sa.ARRAY(sa.String(20))")) == "array(varchar(20))"


def test_ast_enum_values_sorted_alphabetically() -> None:
    assert normalize_ast_type(_expr("sa.Enum('b', 'a', 'c')")) == "enum(a,b,c)"


def test_ast_enum_is_not_normalized_as_string() -> None:
    assert normalize_ast_type(_expr("sa.Enum('a', 'b')")) != "varchar(a,b)"


def test_ast_non_call_expression_is_unknown() -> None:
    assert normalize_ast_type(_expr('"just a string"')).startswith("unknown:")


def test_ast_unrecognized_call_falls_back_to_type_name() -> None:
    assert normalize_ast_type(_expr("MyCustomType(50)")) == "unknown:MyCustomType"


def test_ast_matches_live_normalize_type_for_equivalent_types() -> None:
    """The two normalizers must agree — this is what makes comparing model and
    migration types meaningful."""
    cases = [
        ("sa.String(50)", sqltypes.String(50)),
        ("sa.Text()", sqltypes.Text()),
        ("sa.Integer()", sqltypes.Integer()),
        ("sa.BigInteger()", sqltypes.BigInteger()),
        ("sa.Numeric(10, 2)", sqltypes.Numeric(10, 2)),
        ("sa.Boolean()", sqltypes.Boolean()),
        ("sa.DateTime(timezone=True)", sqltypes.DateTime(timezone=True)),
        ("sa.Enum('b', 'a')", sqltypes.Enum("b", "a")),
        ("sa.ARRAY(sa.Integer())", sqltypes.ARRAY(sqltypes.Integer())),
    ]
    for source, live_type in cases:
        assert normalize_ast_type(_expr(source)) == normalize_type(live_type)
