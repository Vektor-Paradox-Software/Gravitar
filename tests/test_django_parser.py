"""Django migration parsing tests.

Migration files are real Python files written to tmp_path and parsed through
the actual importlib path (not hand-constructed operation objects) — this
also exercises that importing them never requires `django.setup()`.
"""

from pathlib import Path

from gravitar.schema.parsers import ColumnSpec
from gravitar.schema.parsers.django_parser import parse_migration_file, parse_migrations_dir


def _write_migration(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_create_model_becomes_create_table(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "0001_initial.py",
        """
from django.db import migrations, models

class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Post",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("title", models.CharField(max_length=200)),
            ],
        ),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.revision == "0001_initial"
    assert revision.down_revision == []
    assert len(revision.operations) == 1
    op = revision.operations[0]
    assert op.op == "create_table"
    assert op.table == "blog_post"
    assert {c.name for c in op.columns} == {"id", "title"}
    title = next(c for c in op.columns if c.name == "title")
    assert title.type == "varchar(200)"
    assert title.primary_key is False


def test_add_field_becomes_add_column(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "0002_add_body.py",
        """
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        migrations.AddField(model_name="post", name="body", field=models.TextField(null=True)),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.down_revision == ["0001_initial"]
    op = revision.operations[0]
    assert op.op == "add_column"
    assert op.table == "blog_post"
    assert op.columns == [ColumnSpec(name="body", type="text", nullable=True)]


def test_remove_field_becomes_drop_column(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "0003_remove_body.py",
        """
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("blog", "0002_add_body")]
    operations = [
        migrations.RemoveField(model_name="post", name="body"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    op = revision.operations[0]
    assert op.op == "drop_column"
    assert op.table == "blog_post"
    assert op.column_name == "body"


def test_alter_field_becomes_alter_column(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "0004_widen_title.py",
        """
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        migrations.AlterField(
            model_name="post", name="title", field=models.CharField(max_length=300)
        ),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    op = revision.operations[0]
    assert op.op == "alter_column"
    assert op.table == "blog_post"
    assert op.column_name == "title"
    assert op.type == "varchar(300)"
    assert op.nullable is False


def test_delete_model_becomes_drop_table(tmp_path: Path) -> None:
    path = _write_migration(
        tmp_path,
        "0005_delete_post.py",
        """
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        migrations.DeleteModel(name="Post"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    op = revision.operations[0]
    assert op.op == "drop_table"
    assert op.table == "blog_post"


def test_add_index_and_remove_index_produce_no_operation(tmp_path: Path) -> None:
    """Gravitar's Schema has no index concept — same as Alembic's
    op.create_index today."""
    path = _write_migration(
        tmp_path,
        "0006_index.py",
        """
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        migrations.AddIndex(
            model_name="post", index=models.Index(fields=["title"], name="idx_title")
        ),
        migrations.RemoveIndex(model_name="post", name="idx_title"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.operations == []


def test_cross_app_dependency_is_dropped(tmp_path: Path) -> None:
    """Non-goal: resolving another app's migration graph (module docstring)."""
    path = _write_migration(
        tmp_path,
        "0007_cross_app.py",
        """
from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial"), ("otherapp", "0003_something")]
    operations = []
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.down_revision == ["0001_initial"]


def test_parse_migrations_dir_skips_init_and_sorts_by_filename(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text("")
    _write_migration(
        tmp_path,
        "0002_second.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("blog", "0001_first")]
    operations = []
""",
    )
    _write_migration(
        tmp_path,
        "0001_first.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )

    revisions = parse_migrations_dir(tmp_path, app_label="blog")

    assert [r.revision for r in revisions] == ["0001_first", "0002_second"]
