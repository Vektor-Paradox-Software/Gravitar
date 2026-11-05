"""SPEC-12 Amendment v1.1 R16: Django parser notes + isolation.

Pre-R16, Gravitar's Django parser returned None for every operation except
CreateModel/DeleteModel/AddField/RemoveField/AlterField, and
``parse_migration_file`` filtered the Nones out. The S1-style silent-drop
failure mode from the Alembic side had an exact mirror on the Django side
that my SPEC-16 run didn't fix: ``RunSQL``, ``RunPython``, ``RenameField``,
``RenameModel``, ``AlterModelTable``, ``SeparateDatabaseAndState``, and
every constraint op vanished. A ``RenameField`` on the migration side with
a renamed field on the model side read as a false ``missing+orphan`` pair.

R16 closes that:
- RunSQL/RunPython/SeparateDatabaseAndState/unknown → ``Unresolved`` note
  with ``reason="django_op_unsupported"``, scoped to the named model if the
  op exposes one, else global.
- Known-inert ops (AddIndex/RemoveIndex/AlterUniqueTogether/...) → silent
  skip (no Operation, no note — Gravitar's Schema has no index/constraint
  concept, so there's nothing to apply AND nothing to flag).
- RenameField/RenameModel/AlterModelTable → handled, mapped to the
  canonical rename_column/rename_table Operations.
- Per-file isolation at ``parse_migrations_dir``: one broken file becomes
  a ``parse_error`` note, the run continues.
"""

from __future__ import annotations

from pathlib import Path

from gravitar.schema.parsers.django_parser import parse_migration_file, parse_migrations_dir


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- R16a: unhandled ops become notes, not silent drops ---


def test_run_sql_becomes_a_django_op_unsupported_note(tmp_path: Path) -> None:
    path = tmp_path / "0001_run_sql.py"
    _write(
        path,
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.RunSQL(sql="UPDATE blog_post SET title = title"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.operations == []
    assert len(revision.notes) == 1
    note = revision.notes[0]
    assert note["reason"] == "django_op_unsupported"
    assert note["detail"] == "RunSQL"
    assert note["source"]["file"] == str(path)


def test_run_python_becomes_a_django_op_unsupported_note(tmp_path: Path) -> None:
    path = tmp_path / "0002_run_python.py"
    _write(
        path,
        """from django.db import migrations

def forward(apps, schema_editor):
    pass

class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(forward)]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.operations == []
    assert len(revision.notes) == 1
    assert revision.notes[0]["reason"] == "django_op_unsupported"
    assert revision.notes[0]["detail"] == "RunPython"


def test_separate_database_and_state_becomes_a_note(tmp_path: Path) -> None:
    path = tmp_path / "0003_separate.py"
    _write(
        path,
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.SeparateDatabaseAndState(database_operations=[], state_operations=[]),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.operations == []
    assert revision.notes[0]["reason"] == "django_op_unsupported"
    assert revision.notes[0]["detail"] == "SeparateDatabaseAndState"


def test_unsupported_op_with_model_name_is_scoped_to_that_model(tmp_path: Path) -> None:
    """R16a: when the unsupported op exposes a model_name, scope the note
    to that table so R3's taint-based demotion can act on it. Without
    this, every Django unsupported op would taint globally, demoting
    unrelated findings."""
    path = tmp_path / "0004_run_sql_with_model.py"
    # RunSQL has no model_name — but AlterOrderWithRespectTo does when
    # it's handled as unsupported. Use a plugin-shaped op by subclassing
    # the base Operation class with a model_name.
    _write(
        path,
        """from django.db.migrations.operations.base import Operation

class CustomUnhandled(Operation):
    model_name = "post"
    def __init__(self):
        pass
    def state_forwards(self, app_label, state):
        pass
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        pass
    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = [CustomUnhandled()]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert len(revision.notes) == 1
    note = revision.notes[0]
    assert note["reason"] == "django_op_unsupported"
    assert note["tables"] == ["blog_post"]


# --- R16a: known-inert ops don't produce notes OR operations ---


def test_add_index_is_silently_inert(tmp_path: Path) -> None:
    path = tmp_path / "0001_add_index.py"
    _write(
        path,
        """from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.AddIndex(model_name="post", index=models.Index(fields=["title"], name="ix_t")),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert revision.operations == []
    assert revision.notes == []


# --- R16b: RenameField / RenameModel / AlterModelTable are handled ---


def test_rename_field_produces_a_rename_column_operation(tmp_path: Path) -> None:
    """Pre-R16, RenameField was silently dropped — a renamed field read as
    a false missing_column + orphaned_column pair."""
    path = tmp_path / "0001_rename.py"
    _write(
        path,
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.RenameField(model_name="post", old_name="title", new_name="headline"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert len(revision.operations) == 1
    assert revision.notes == []
    op = revision.operations[0]
    assert op.op == "rename_column"
    assert op.table == "blog_post"
    assert op.column_name == "title"
    assert op.new_column_name == "headline"


def test_rename_model_produces_a_rename_table_operation(tmp_path: Path) -> None:
    path = tmp_path / "0001_rename_model.py"
    _write(
        path,
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.RenameModel(old_name="Post", new_name="Article"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert len(revision.operations) == 1
    op = revision.operations[0]
    assert op.op == "rename_table"
    assert op.table == "blog_post"
    assert op.new_table == "blog_article"


def test_alter_model_table_produces_a_rename_table_operation(tmp_path: Path) -> None:
    """AlterModelTable changes Meta.db_table — treat as a rename from the
    default-name to the new one."""
    path = tmp_path / "0001_alter_table.py"
    _write(
        path,
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.AlterModelTable(name="Post", table="legacy_posts"),
    ]
""",
    )

    revision = parse_migration_file(path, app_label="blog")

    assert len(revision.operations) == 1
    op = revision.operations[0]
    assert op.op == "rename_table"
    assert op.table == "blog_post"
    assert op.new_table == "legacy_posts"


# --- R16c: per-file isolation ---


def test_one_broken_file_doesnt_abort_the_run(tmp_path: Path) -> None:
    _write(
        tmp_path / "0001_good.py",
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )
    _write(tmp_path / "0002_broken.py", "class Migration(:\n    pass\n")
    _write(
        tmp_path / "0003_also_good.py",
        """from django.db import migrations

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_good")]
    operations = []
""",
    )

    revisions = parse_migrations_dir(tmp_path, app_label="blog")

    assert len(revisions) == 3
    good = [r for r in revisions if r.revision is not None]
    assert {r.revision for r in good} == {"0001_good", "0003_also_good"}
    broken = next(r for r in revisions if r.source_file.endswith("0002_broken.py"))
    assert broken.revision is None
    assert len(broken.notes) == 1
    assert broken.notes[0]["reason"] == "parse_error"


def test_runtime_exception_in_migration_module_is_also_isolated(tmp_path: Path) -> None:
    """R16c isn't just about SyntaxError — a Django migration whose
    import-time code raises (e.g. ``from missing_app import X``) should
    also become a parse_error note, not an abort."""
    _write(tmp_path / "0001_bad_import.py", "from nonexistent_pkg import something\n")

    revisions = parse_migrations_dir(tmp_path, app_label="blog")

    assert len(revisions) == 1
    assert revisions[0].revision is None
    assert revisions[0].notes[0]["reason"] == "parse_error"
