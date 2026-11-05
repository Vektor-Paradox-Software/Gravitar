from pathlib import Path

from gravitar.schema.migrations import (
    RevisionFile,
    build_migration_schema,
    parse_revision_file,
    parse_versions_dir,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_versions_dir_returns_full_chain() -> None:
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")

    assert [r.revision for r in revisions] == ["0001", "0002", "0003"]
    assert [r.down_revision for r in revisions] == [None, "0001", "0002"]


def test_create_table_operation_extracted() -> None:
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")
    first = revisions[0]

    assert len(first.operations) == 1
    op = first.operations[0]
    assert op.op == "create_table"
    assert op.table == "users"
    assert len(op.columns) == 3
    assert op.line > 0


def test_add_column_operation_extracted() -> None:
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")
    second = revisions[1]

    assert len(second.operations) == 1
    assert second.operations[0].op == "add_column"


def test_multiple_operations_in_one_revision() -> None:
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")
    third = revisions[2]

    ops = [op.op for op in third.operations]
    assert ops == ["create_table", "alter_column"]


def test_source_file_recorded() -> None:
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")
    for revision in revisions:
        assert revision.source_file.endswith(".py")


def test_downgrade_operations_are_not_collected() -> None:
    """upgrade() and downgrade() both call op.*; only the forward path counts."""
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")
    first = revisions[0]

    assert [op.op for op in first.operations] == ["create_table"]


def test_merge_revision_down_revision_is_tuple() -> None:
    revision = parse_revision_file(FIXTURES / "merge_revision_sample.py")

    assert revision.revision == "0004"
    assert revision.down_revision == ["0002", "0003"]


def test_init_py_skipped(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "0001_only.py").write_text('revision = "0001"\ndown_revision = None\n')

    revisions = parse_versions_dir(tmp_path)

    assert len(revisions) == 1
    assert revisions[0].revision == "0001"


# --- build_migration_schema: operation application ---


def test_build_migration_schema_on_clean_fixture() -> None:
    revisions = parse_versions_dir(FIXTURES / "clean" / "versions")

    schema, findings = build_migration_schema(revisions)

    assert findings == []
    assert set(schema) == {"users", "orders"}
    assert set(schema["users"]["columns"]) == {"id", "email", "created_at", "deleted_at"}
    assert schema["users"]["columns"]["email"]["type"] == "varchar(255)"
    assert schema["users"]["columns"]["email"]["nullable"] is False
    assert schema["users"]["columns"]["deleted_at"]["nullable"] is True
    assert schema["orders"]["columns"]["total_cents"]["type"] == "numeric(10,2)"


def _write_revision(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(body, encoding="utf-8")


def test_full_operation_sequence(tmp_path: Path) -> None:
    """create_table -> alter_column(type) -> drop_column -> add_column ->
    rename_column -> rename_table -> create/drop a second table."""
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "t",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("legacy_col", sa.Integer(), nullable=False),
    )
""",
    )
    _write_revision(
        tmp_path,
        "0002_widen_name.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.alter_column("t", "name", type_=sa.String(length=100))
""",
    )
    _write_revision(
        tmp_path,
        "0003_drop_legacy.py",
        """
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.drop_column("t", "legacy_col")
""",
    )
    _write_revision(
        tmp_path,
        "0004_add_new_col.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.add_column("t", sa.Column("new_col", sa.Boolean(), nullable=True))
""",
    )
    _write_revision(
        tmp_path,
        "0005_rename_col.py",
        """
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.rename_column("t", "new_col", "renamed_col")
""",
    )
    _write_revision(
        tmp_path,
        "0006_rename_table.py",
        """
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.rename_table("t", "t2")
""",
    )
    _write_revision(
        tmp_path,
        "0007_temp_table.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.create_table("temp", sa.Column("id", sa.Integer(), primary_key=True))
    op.drop_table("temp")
""",
    )

    revisions = parse_versions_dir(tmp_path)
    schema, findings = build_migration_schema(revisions)

    assert findings == []
    assert set(schema) == {"t2"}
    columns = schema["t2"]["columns"]
    assert set(columns) == {"id", "name", "renamed_col"}
    assert columns["name"]["type"] == "varchar(100)"
    assert columns["renamed_col"]["type"] == "boolean"
    assert columns["renamed_col"]["nullable"] is True


def test_alter_column_nullable_change(tmp_path: Path) -> None:
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "t", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("optional", sa.String(length=10), nullable=False),
    )
""",
    )
    _write_revision(
        tmp_path,
        "0002_make_nullable.py",
        """
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.alter_column("t", "optional", nullable=True)
""",
    )

    schema, findings = build_migration_schema(parse_versions_dir(tmp_path))

    assert findings == []
    assert schema["t"]["columns"]["optional"]["nullable"] is True


# --- build_migration_schema: broken-chain detection ---


def test_gap_produces_broken_chain_finding() -> None:
    revisions = [
        RevisionFile(revision="0001", down_revision="missing", operations=[], source_file="a.py")
    ]

    schema, findings = build_migration_schema(revisions)

    assert schema == {}
    assert len(findings) == 1
    assert findings[0]["category"] == "broken_chain"
    assert "missing" in findings[0]["message"]


def test_two_revision_cycle_produces_broken_chain_findings() -> None:
    revisions = [
        RevisionFile(revision="a", down_revision="b", operations=[], source_file="a.py"),
        RevisionFile(revision="b", down_revision="a", operations=[], source_file="b.py"),
    ]

    schema, findings = build_migration_schema(revisions)

    assert schema == {}
    categories = {f["category"] for f in findings}
    assert categories == {"broken_chain"}
    mentioned = {f["migration_value"] for f in findings}
    assert mentioned == {"a", "b"}


def test_valid_chain_downstream_of_cycle_is_unaffected() -> None:
    """A cycle among b/c shouldn't stop a well-formed, independently-based revision
    from contributing to the schema."""
    revisions = [
        RevisionFile(revision="base", down_revision=None, operations=[], source_file="base.py"),
        RevisionFile(revision="b", down_revision="c", operations=[], source_file="b.py"),
        RevisionFile(revision="c", down_revision="b", operations=[], source_file="c.py"),
    ]

    _, findings = build_migration_schema(revisions)

    categories = [f["category"] for f in findings]
    assert categories == ["broken_chain", "broken_chain"]


def test_merge_revision_applies_both_parents_before_child(tmp_path: Path) -> None:
    _write_revision(
        tmp_path,
        "0001_base.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    _write_revision(
        tmp_path,
        "0002_branch_a.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("t", sa.Column("from_a", sa.Integer(), nullable=True))
""",
    )
    _write_revision(
        tmp_path,
        "0003_branch_b.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("t", sa.Column("from_b", sa.Integer(), nullable=True))
""",
    )
    _write_revision(
        tmp_path,
        "0004_merge.py",
        """
from alembic import op

revision = "0004"
down_revision = ("0002", "0003")


def upgrade() -> None:
    pass
""",
    )

    schema, findings = build_migration_schema(parse_versions_dir(tmp_path))

    assert findings == []
    assert set(schema["t"]["columns"]) == {"id", "from_a", "from_b"}


def test_schema_build_is_deterministic_across_runs(tmp_path: Path) -> None:
    _write_revision(
        tmp_path,
        "0001_base.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    _write_revision(
        tmp_path,
        "0002_a.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("t", sa.Column("a", sa.Integer(), nullable=True))
""",
    )
    _write_revision(
        tmp_path,
        "0003_b.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("t", sa.Column("b", sa.Integer(), nullable=True))
""",
    )

    revisions = parse_versions_dir(tmp_path)
    first, _ = build_migration_schema(revisions)
    second, _ = build_migration_schema(list(reversed(revisions)))

    assert first == second


# --- multi-head disagreement (Section 5) ---


def test_two_heads_touching_different_columns_is_not_a_disagreement(tmp_path: Path) -> None:
    _write_revision(
        tmp_path,
        "0001_base.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("t", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    _write_revision(
        tmp_path,
        "0002_branch_a.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("t", sa.Column("a", sa.Integer(), nullable=True))
""",
    )
    _write_revision(
        tmp_path,
        "0003_branch_b.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("t", sa.Column("b", sa.Integer(), nullable=True))
""",
    )

    _, findings = build_migration_schema(parse_versions_dir(tmp_path))

    assert findings == []


def test_two_heads_disagreeing_on_shared_column_is_flagged(tmp_path: Path) -> None:
    _write_revision(
        tmp_path,
        "0001_base.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "t",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("total", sa.Integer(), nullable=True),
    )
""",
    )
    _write_revision(
        tmp_path,
        "0002_branch_a.py",
        """
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.alter_column("t", "total", type_=sa.Numeric(10, 2))
""",
    )
    _write_revision(
        tmp_path,
        "0003_branch_b.py",
        """
from alembic import op

revision = "0003"
down_revision = "0001"


def upgrade() -> None:
    op.alter_column("t", "total", nullable=False)
""",
    )

    _, findings = build_migration_schema(parse_versions_dir(tmp_path))

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "broken_chain"
    assert finding["severity"] == "error"
    assert finding["table"] == "t"
    assert finding["column"] == "total"
    assert finding["migration_value"] == {
        "0002": {"type": "numeric(10,2)", "nullable": True},
        "0003": {"type": "integer", "nullable": False},
    }


# --- raw SQL (Section 5) ---


def test_op_execute_dml_is_inert_under_r12(tmp_path: Path) -> None:
    """SPEC-16 amendment R12: ``op.execute("UPDATE ...")`` is DML — classified
    as inert, no finding and no note. Pre-R12 this produced a broken_chain
    warning at 0.3 confidence (the "any table may be affected" noise)."""
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("accounts", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("UPDATE accounts SET id = id")
""",
    )

    from gravitar.schema.migrations import build_migration_result

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert set(result.schema) == {"accounts"}
    assert result.findings == []
    assert not any(n["reason"] == "dynamic_sql" for n in result.notes)


def test_op_execute_with_non_literal_argument_becomes_a_dynamic_sql_note(tmp_path: Path) -> None:
    """SPEC-16 amendment R12: ``op.execute(variable)`` becomes a ``dynamic_sql``
    note (lowering overall confidence), not a 0.3-confidence broken_chain
    finding — the latter was false noise, since Gravitar genuinely can't see
    into the string."""
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("accounts", sa.Column("id", sa.Integer(), primary_key=True))
    statement = "UPDATE accounts SET id = id"
    op.execute(statement)
""",
    )

    from gravitar.schema.migrations import build_migration_result

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert set(result.schema) == {"accounts"}
    assert result.findings == []
    assert len(result.notes) == 1
    assert result.notes[0]["reason"] == "dynamic_sql"


def test_op_execute_matching_add_column_applies_the_ddl(tmp_path: Path) -> None:
    """SPEC-16 amendment R12: a recognized DDL pattern now actually mutates
    the schema state (previously only emitted a warning finding). The
    column ends up in the schema with ``unknown:raw_sql`` type since R12
    doesn't parse column types out of raw SQL."""
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("accounts", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("ALTER TABLE accounts ADD COLUMN nickname VARCHAR(50)")
""",
    )

    from gravitar.schema.migrations import build_migration_result

    result = build_migration_result(parse_versions_dir(tmp_path))

    assert set(result.schema) == {"accounts"}
    assert "nickname" in result.schema["accounts"]["columns"]
    assert result.schema["accounts"]["columns"]["nickname"]["type"] == "unknown:raw_sql"
    assert result.findings == []


def test_op_execute_matching_pattern_with_orphaned_table_is_distinct_category(
    tmp_path: Path,
) -> None:
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute("ALTER TABLE ghost ADD COLUMN nickname VARCHAR(50)")
""",
    )

    schema, findings = build_migration_schema(parse_versions_dir(tmp_path))

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "orphaned_reference"
    assert finding["table"] == "ghost"
    assert finding["column"] == "nickname"
    assert finding["confidence"] == 0.2


def test_op_execute_drop_column_pattern_requires_existing_column(tmp_path: Path) -> None:
    """DROP COLUMN presupposes the column already exists — unlike ADD COLUMN, a
    missing column here is the orphaned-reference case, not the happy path."""
    _write_revision(
        tmp_path,
        "0001_create.py",
        """
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table("accounts", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("ALTER TABLE accounts DROP COLUMN nickname")
""",
    )

    schema, findings = build_migration_schema(parse_versions_dir(tmp_path))

    assert len(findings) == 1
    finding = findings[0]
    assert finding["category"] == "orphaned_reference"
    assert finding["table"] == "accounts"
    assert finding["column"] == "nickname"
    assert finding["migration_value"] == "ALTER TABLE accounts DROP COLUMN nickname"
