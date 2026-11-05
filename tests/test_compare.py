from gravitar.schema import Column, Schema, Table
from gravitar.schema.compare import compare_schemas, filter_findings


def _column(name: str, type_: str = "integer", nullable: bool = False, pk: bool = False) -> Column:
    return Column(
        name=name, type=type_, nullable=nullable, primary_key=pk, source_file="x.py", source_line=1
    )


def _table(name: str, *columns: Column) -> Table:
    return Table(
        name=name,
        columns={c["name"]: c for c in columns},
        source_file="x.py",
        source_line=1,
    )


def test_identical_schemas_produce_no_findings() -> None:
    schema: Schema = {"users": _table("users", _column("id", pk=True))}

    assert compare_schemas(schema, schema) == []


def test_missing_table() -> None:
    model: Schema = {"products": _table("products", _column("id", pk=True))}
    migration: Schema = {}

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    assert findings[0]["category"] == "missing_table"
    assert findings[0]["table"] == "products"
    assert findings[0]["column"] is None
    assert findings[0]["severity"] == "error"


def test_orphaned_table() -> None:
    model: Schema = {}
    migration: Schema = {"ghost": _table("ghost", _column("id", pk=True))}

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    assert findings[0]["category"] == "orphaned_column"
    assert findings[0]["table"] == "ghost"
    assert findings[0]["column"] is None


def test_missing_column() -> None:
    model: Schema = {
        "users": _table("users", _column("id", pk=True), _column("deleted_at", "timestamp", True))
    }
    migration: Schema = {"users": _table("users", _column("id", pk=True))}

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "missing_column"
    assert finding["table"] == "users"
    assert finding["column"] == "deleted_at"
    assert finding["model_value"] == {"type": "timestamp", "nullable": True}
    assert finding["migration_value"] is None


def test_orphaned_column() -> None:
    model: Schema = {"users": _table("users", _column("id", pk=True))}
    migration: Schema = {
        "users": _table("users", _column("id", pk=True), _column("internal_flag", "boolean"))
    }

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "orphaned_column"
    assert finding["table"] == "users"
    assert finding["column"] == "internal_flag"
    assert finding["model_value"] is None
    assert finding["migration_value"] == {"type": "boolean", "nullable": False}


def test_type_mismatch() -> None:
    model: Schema = {"invoices": _table("invoices", _column("total_cents", "numeric(10,2)"))}
    migration: Schema = {"invoices": _table("invoices", _column("total_cents", "integer"))}

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "type_mismatch"
    assert finding["model_value"] == "numeric(10,2)"
    assert finding["migration_value"] == "integer"


def test_nullability_mismatch() -> None:
    model: Schema = {"customers": _table("customers", _column("middle_name", "varchar(50)", True))}
    migration: Schema = {
        "customers": _table("customers", _column("middle_name", "varchar(50)", False))
    }

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "nullability_mismatch"
    assert finding["model_value"] is True
    assert finding["migration_value"] is False


def test_type_and_nullability_mismatch_both_reported() -> None:
    model: Schema = {"t": _table("t", _column("c", "varchar(50)", True))}
    migration: Schema = {"t": _table("t", _column("c", "integer", False))}

    findings = compare_schemas(model, migration)

    categories = {f["category"] for f in findings}
    assert categories == {"type_mismatch", "nullability_mismatch"}


def test_findings_sorted_by_category_then_table_then_column() -> None:
    model: Schema = {
        "b_table": _table("b_table", _column("id", pk=True), _column("z_col", "integer")),
        "a_table": _table("a_table", _column("id", pk=True)),
    }
    migration: Schema = {
        "b_table": _table("b_table", _column("id", pk=True)),
        "a_table": _table("a_table", _column("id", pk=True), _column("extra", "integer")),
    }

    findings = compare_schemas(model, migration)

    # missing_column (b_table.z_col) sorts before orphaned_column (a_table.extra)
    assert [(f["category"], f["table"], f["column"]) for f in findings] == [
        ("missing_column", "b_table", "z_col"),
        ("orphaned_column", "a_table", "extra"),
    ]


# --- unknown_type (Section 5) ---


def test_unknown_type_on_model_side() -> None:
    model: Schema = {"t": _table("t", _column("c", "unknown:MoneyType"))}
    migration: Schema = {"t": _table("t", _column("c", "numeric(12,2)"))}

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "unknown_type"
    assert finding["severity"] == "warning"
    assert finding["model_value"] == "unknown:MoneyType"
    assert finding["migration_value"] == "numeric(12,2)"


def test_unknown_type_on_migration_side() -> None:
    model: Schema = {"t": _table("t", _column("c", "integer"))}
    migration: Schema = {"t": _table("t", _column("c", "unknown:SomeCustomType"))}

    findings = compare_schemas(model, migration)

    assert len(findings) == 1
    assert findings[0]["category"] == "unknown_type"


def test_unknown_type_suppresses_redundant_type_mismatch() -> None:
    """The differing 'unknown:X' string would also trigger type_mismatch if not
    special-cased — only unknown_type should be reported, not both."""
    model: Schema = {"t": _table("t", _column("c", "unknown:MoneyType"))}
    migration: Schema = {"t": _table("t", _column("c", "integer"))}

    findings = compare_schemas(model, migration)

    categories = [f["category"] for f in findings]
    assert categories == ["unknown_type"]


# --- filter_findings: checks toggles and exclude patterns ---


def test_filter_findings_disables_category() -> None:
    findings = compare_schemas({"products": _table("products", _column("id", pk=True))}, {})

    filtered = filter_findings(findings, checks={"missing_table": False})

    assert filtered == []


def test_filter_findings_defaults_to_enabled() -> None:
    findings = compare_schemas({"products": _table("products", _column("id", pk=True))}, {})

    assert filter_findings(findings, checks={}) == findings


def test_filter_findings_unknown_type_not_toggleable() -> None:
    findings = compare_schemas(
        {"t": _table("t", _column("c", "unknown:X"))},
        {"t": _table("t", _column("c", "integer"))},
    )

    filtered = filter_findings(findings, checks={"type_mismatch": False})

    assert len(filtered) == 1
    assert filtered[0]["category"] == "unknown_type"


def test_filter_findings_excludes_matching_table_glob() -> None:
    findings = compare_schemas({"tmp_cache": _table("tmp_cache", _column("id", pk=True))}, {})

    assert filter_findings(findings, exclude_tables=["tmp_*"]) == []
    assert filter_findings(findings, exclude_tables=["other_*"]) == findings


def test_filter_findings_excludes_matching_column_glob() -> None:
    model: Schema = {
        "users": _table("users", _column("id", pk=True), _column("legacy_flag", "boolean"))
    }
    migration: Schema = {"users": _table("users", _column("id", pk=True))}

    findings = compare_schemas(model, migration)
    assert len(findings) == 1

    filtered = filter_findings(findings, exclude_columns=["users.legacy_*"])

    assert filtered == []
