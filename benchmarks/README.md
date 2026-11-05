# Benchmarks — SPEC-16 R0

Reproducible real-world accuracy harness for Gravitar. Replaces hand-held
spot checks with per-project TP/FP/FN classification + mandatory cause
attribution, so every miss and false positive gets a root-cause tag.

## Run

```bash
python benchmarks/run.py
```

Writes `benchmarks/results/<YYYY-MM-DD-HHMM>/REPORT.md` plus
`<project>.json` per project in the manifest. The exit code is non-zero
if any TP/FP/FN was not attributable from the taxonomy and not covered
by `triage.yml` — a run is never allowed to produce untagged items.

## Add a project

1. Append an entry to `manifest.yml`:
   ```yaml
   - name: example_app
     oracle: corpus/example_app/oracle.yml   # local YAML oracle
     project_path: corpus/example_app        # path to the gravitar-analyzable repo
     notes: "example project with a known missing_column"
     oracle_scope:
       types: true     # pass false for SQLite-reflected projects (SPEC §Type-comparison scope)
   ```
2. If the project needs human-assigned cause tags for FP/FN items the
   taxonomy can't auto-assign, add entries to `triage.yml`.

## Oracle formats

- **Static YAML** (the only format v1 ships): hand-authored list of
  expected findings. Deterministic; no DB required. Use for the
  self-test corpus and for projects where you've captured the oracle
  once from a Postgres run and don't want to re-provision each run.
- **Postgres autogenerate** (planned, scaffold in `oracle/alembic_oracle.py`):
  provision a fresh DB, run the project's migrations to head, call
  `alembic.autogenerate.compare_metadata`, map diffs to Gravitar
  categories per the amendment table.

Pick with `oracle_runner: static` (default) or `oracle_runner: postgres`.

## Cause taxonomy

Every FP and FN **must** carry a `cause`. Auto-assigned when derivable
from the finding; otherwise set in `triage.yml`:

| Cause | Meaning |
|---|---|
| `batch_op` | FP/FN caused by a `batch_alter_table` the walker didn't recognize (R1 territory) |
| `class_form_type` | class-form type reference (`sa.Integer`, no parens) misread |
| `type_alias` | uppercase/alias name (`VARCHAR`) misread |
| `type_variant` | `.with_variant()` or similar passthrough misread |
| `custom_type` | project-local TypeDecorator Gravitar couldn't resolve (R13) |
| `loop_or_helper` | missed op inside a loop or external helper (R4) |
| `schema_qualified` | `schema="x"` kwarg caused Gravitar's compare to skip |
| `pk_constraint_nullability` | PK-column nullability vs model-declared mismatch |
| `fk_typed_column` | ForeignKey column where the type comes from the referenced table |
| `import_failure` | project's models package couldn't be imported |
| `unresolved_taint` | finding on a table listed in `coverage.tainted_tables` (auto) |
| `oracle_artifact` | the "drift" is an oracle quirk (reflection stripped something), not real |
| `other` | genuine new cause — **must** carry a free-text `note:` |

The report lists every `other` so new causes become visible as tickets.

## Not in v1 scope

- Running the Postgres oracle (scaffolded, not implemented)
- Seeded-drift generation (recall harness — SPEC §Seeded drift)
- Atlas comparator plug-in (interface left open)
- Cloning external repos (users point `project_path` at a local checkout)

These extend the harness without changing its contract; v1 is enough
to produce an attributed baseline against local corpuses.
