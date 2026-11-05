# Configuration reference

Gravitar is zero-config by default: `gravitar check` works with no `.gravitar.yml`
at all if your repository follows standard conventions (models in `models/` or
`app/models/`, an `alembic.ini` at the repository root). This document covers what
those conventions are, and every field available if your repository doesn't match
them.

## Where the config file lives

Gravitar looks for `.gravitar.yml` at the root of the path you run `gravitar check`
against. Use `--config PATH` to point at a config file somewhere else instead.

Run `gravitar init` to write an annotated starting file (the same content shown
below, and kept in sync with it at
[`.gravitar.yml.example`](../.gravitar.yml.example)) into the current directory.
It refuses to overwrite an existing `.gravitar.yml`.

**Every key below is optional.** Only set what you need to override — omitted
sections fall back to their defaults. An unrecognized key anywhere in the file
(a typo'd section name, a misspelled field) is a hard error (exit code `2`) rather
than being silently ignored, so a typo can't quietly disable a check you thought
was running.

## `models`

Controls which SQLAlchemy declarative models Gravitar reads.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `models.paths` | `list[str]` | tries `"models"`, then `"app.models"` | Python import path(s) to the package(s) containing your declarative models. Gravitar imports each one directly — no virtualenv is created for you, so it runs in whatever Python environment invoked it. |
| `models.base_classes` | `list[str]` | `[]` (discover all) | Name(s) of specific declarative base variables to inspect (both the legacy `declarative_base()` style and SQLAlchemy 2.0's `class Base(DeclarativeBase)` style are supported). Leave empty to auto-discover every declarative base found in the imported module(s). |

```yaml
models:
  paths:
    - "myapp.db.models"
  base_classes:
    - "Base"
```

## `alembic`

Controls where Gravitar finds your migration history.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `alembic.config` | `str` | `"alembic.ini"` | Path to `alembic.ini`, relative to the repository root. Read only to extract `script_location`. |
| `alembic.script_location` | `str \| null` | `null` (read from `alembic.ini`) | Overrides `script_location` directly, skipping `alembic.ini` entirely. The `versions/` subdirectory under this path is what gets parsed. |

```yaml
alembic:
  config: "backend/alembic.ini"
```

If neither `alembic.ini` nor `alembic.script_location` resolves to a real
`versions/` directory, `gravitar check` exits `2` rather than silently treating an
empty/missing directory as "no migrations" (which would make every model table
look like drift).

## `django`

Analyzes a Django app instead of a SQLAlchemy + Alembic repository. Presence of
this section — even empty — switches the whole repository to Django mode:
`models`/`alembic` are ignored, and Gravitar reads the app's `models.py` and
`<app>/migrations/*.py` instead. Requires the `django` package (`pip install
'gravitar[django]'`); scoped to a single app (see the note below).

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `django.app_path` | `str` | *(required)* | Python import path to a single Django app package — e.g. `"blog"`, or `"myproject.blog"` for a nested app. Must contain `models.py` and a `migrations/` directory. |

```yaml
django:
  app_path: "blog"
```

Milestone 10's scope is intentionally bounded: `AddField`, `RemoveField`,
`AlterField`, `CreateModel`, `DeleteModel` are recognized (`AddIndex`/
`RemoveIndex` are recognized but produce no finding — Gravitar's schema has no
index concept, same as `op.create_index` on the Alembic side); relation fields
(`ForeignKey`, `ManyToManyField`, `OneToOneField`) aren't resolved and fall back
to `unknown:<FieldType>`; and only one app is analyzed at a time — a
`dependencies` entry naming a different app is silently ignored rather than
followed.

## `checks`

Enable or disable individual drift categories. All default to enabled; this is
the escape hatch for a category that's structurally noisy in your repository
(for example, a codebase mid-migration to Gravitar where one category needs to
stay off temporarily).

| Field | Type | Default |
| --- | --- | --- |
| `checks.missing_table` | `bool` | `true` |
| `checks.missing_column` | `bool` | `true` |
| `checks.type_mismatch` | `bool` | `true` |
| `checks.nullability_mismatch` | `bool` | `true` |
| `checks.orphaned_column` | `bool` | `true` |
| `checks.orphaned_reference` | `bool` | `true` |
| `checks.broken_chain` | `bool` | `true` |

`unknown_type` findings (an unrecognized column type) aren't in this list — they
aren't a category you'd normally want to silence, since they mean Gravitar
genuinely can't verify that column at all.

```yaml
checks:
  orphaned_column: false
```

## `exclude`

Skip specific tables or columns entirely — they won't produce findings in any
category. Patterns are glob-style (`fnmatch`), not regex.

| Field | Type | Default | Format |
| --- | --- | --- | --- |
| `exclude.tables` | `list[str]` | `[]` | Table name glob patterns, e.g. `"tmp_*"` |
| `exclude.columns` | `list[str]` | `[]` | `"table.column"` glob patterns, e.g. `"*.legacy_*"` or `"users.internal_notes"` |

```yaml
exclude:
  tables:
    - "tmp_*"
    - "celery_*"
  columns:
    - "*.legacy_id"
```

## `output`

Sets the default output mode. Both fields can still be overridden per-invocation
by the matching CLI flag (`--format` / `--quiet`), which takes precedence when
given.

| Field | Type | Default |
| --- | --- | --- |
| `output.format` | `"text"` \| `"json"` | `"text"` |
| `output.quiet` | `bool` | `false` |

```yaml
output:
  format: "json"
```

## Full example

```yaml
# .gravitar.yml — all fields optional; shown values are defaults

models:
  paths:
    - "models"
    - "app.models"
  base_classes: []

alembic:
  config: "alembic.ini"
  script_location: null

checks:
  missing_table: true
  missing_column: true
  type_mismatch: true
  nullability_mismatch: true
  orphaned_column: true
  orphaned_reference: true
  broken_chain: true

exclude:
  tables: []
  columns: []

output:
  format: "text"
  quiet: false
```

## JSON output shape

`gravitar check --format json` (or `output.format: "json"`) produces a stable,
versioned envelope:

```json
{
  "gravitar_version": "0.1.0",
  "schema_version": 2,
  "status": "drift",
  "summary": {
    "tables_analyzed": 12,
    "columns_analyzed": 78,
    "errors": 1,
    "warnings": 0,
    "reviews": 0
  },
  "coverage": {
    "revisions_total": 7,
    "revisions_fully_resolved": 7,
    "revisions_partial": 0,
    "revisions_skipped": 0
  },
  "findings": [
    {
      "category": "missing_column",
      "severity": "error",
      "table": "users",
      "column": "deleted_at",
      "model_value": { "type": "timestamp", "nullable": true },
      "migration_value": null,
      "message": "model declares column, no migration adds it",
      "suggestion": "add a migration: alembic revision --autogenerate -m \"add users.deleted_at\"",
      "confidence": 1.0,
      "source": {
        "file": "src/models/user.py",
        "line": 42,
        "revision_id": null
      },
      "evidence": [
        {
          "signal": "ast_fully_resolved",
          "weight": 1.0,
          "detail": "model declaration fully resolved from source"
        }
      ],
      "live_verified": false,
      "kind": "fail",
      "demoted_from": null,
      "explaining_notes": [],
      "finding_id": "23533e5230ea"
    }
  ],
  "notes": []
}
```

`schema_version` is the compatibility contract for anything parsing this
output — it only changes on a breaking change to the shape above. `status` is
one of `"clean"` (no findings), `"drift"` (at least one finding), or
`"error"` (Gravitar couldn't complete the analysis).

`--strict-coverage` signals incomplete coverage **through the exit code
(11)**, not through the `status` field: the JSON `status` continues to
reflect findings presence, so a consumer parsing `status` alone sees the
clean/drift classification unchanged. The exit code is the authoritative
signal for the incomplete case; see the main [README](../README.md)'s Exit
codes table.

Each finding carries:

- `category` / `severity` / `kind` — the tier contract (SPEC-16 R3). `kind` is
  `"fail"` (counts toward CI failure) or `"review"` (demoted; shown but
  non-blocking). `demoted_from` is `"error"` / `"warning"` when `kind` was
  demoted from a higher tier by a scoped `Unresolved` note, else `null`.
- `confidence` — 0.0 to 1.0.
- `source` — `{file, line, revision_id}`. For model-side findings `line` is
  the model's declaration line; for migration-side findings `revision_id` is
  the Alembic revision.
- `evidence` — the signals that fed the confidence score, each a `{signal,
  weight, detail}` object.
- `live_verified` — `true` only when `--live-db-url` was passed and the
  finding was corroborated against the real schema.
- `explaining_notes` — note ids that justify a demotion.
- `finding_id` — a stable hash across repeated runs; feed it to
  `gravitar feedback confirm`/`dismiss`.

`coverage` reports how many revisions Gravitar fully resolved, had partial
coverage on (one or more scoped notes), or skipped (a `parse_error` note
replaced the revision). `notes` is the full list of `Unresolved` observations
the parser emitted — see the SARIF export's `toolExecutionNotifications`
for the same material in the SARIF format.

On `"error"`, `findings` and `notes` are empty and `summary`/`coverage` are
all zeros, but an extra `error` field carries the message — so a caller
doesn't need to fall back to parsing stderr text to tell success, drift, and
failure apart:

```json
{
  "gravitar_version": "0.1.0",
  "schema_version": 2,
  "status": "error",
  "summary": { "tables_analyzed": 0, "columns_analyzed": 0, "errors": 0, "warnings": 0, "reviews": 0 },
  "coverage": { "revisions_total": 0, "revisions_fully_resolved": 0, "revisions_partial": 0, "revisions_skipped": 0 },
  "findings": [],
  "notes": [],
  "error": "alembic.ini not found; create a .gravitar.yml with alembic.config or alembic.script_location set"
}
```

### Baseline-enabled output

When `.gravitar/baseline.json` exists and `--no-baseline` wasn't passed, the
envelope carries two additional pieces so consumers can tell new drift from
pre-existing drift:

- A top-level `"baseline_active": true` flag.
- Each finding in `findings` gains a `"baselined": bool` field — `true` when
  the finding was already in the baseline snapshot, `false` when it's new.

Without a baseline (or with `--no-baseline`), neither field is present and the
envelope shape is identical to the examples above. See the main
[README](../README.md)'s "Baseline / ratchet" section for the workflow.
