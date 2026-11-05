# Gravitar

Gravitar is a command-line tool and GitHub Actions integration that catches **schema
drift** before it reaches production: the class of bug where your SQLAlchemy models
say one thing about your database schema and your Alembic migration history says
another.

It's easy to introduce this kind of bug without noticing. Someone adds a column to a
model and forgets the migration. Someone hand-edits a migration and the type doesn't
match what the model declares. Two people branch off the same revision and each adds
a conflicting change. None of this trips a linter or a type checker — the code is
syntactically fine, and the tests probably don't touch it — but the two systems have
quietly disagreed about your schema, and the failure shows up as a runtime error
against a real database.

Gravitar checks for that disagreement statically, with no database connection and no
LLM calls, and is designed to run as a required check on every pull request.

## What it checks

Given a repository with SQLAlchemy declarative models and an Alembic migration
history, `gravitar check` detects:

- **Missing table** — a model declares a table no migration creates
- **Missing column** — a model declares a column no migration adds
- **Type mismatch** — a column's type differs between model and migrations
- **Nullability mismatch** — a column's `nullable` differs between model and migrations
- **Orphaned migration column** — a migration adds a table/column no model references
- **Broken migration chain** — gaps, cycles, or disagreeing branches in your revision history

It also flags model types it can't confidently normalize (`unknown_type`) and
migrations containing raw SQL it can't statically analyze, so those get a human look
instead of a false pass.

Findings are tiered: **error** (clear drift that fails CI), **warning**
(ambiguous — e.g. unresolvable types), and **review** (findings
demoted because a scoped `Unresolved` note explains why Gravitar can't be
fully confident; shown and tracked but doesn't fail CI).

## What it deliberately doesn't do

No live database connection, no auto-fix, no LLM calls, no database other than
PostgreSQL. These are v1 scope boundaries, not oversights. SQLAlchemy +
Alembic is the primary, fully-featured path; Django is supported on a
deliberately bounded subset (see [docs/configuration.md](docs/configuration.md)'s
`django` section) as a proof that the underlying comparator generalizes beyond
one ORM, not as a second production-grade integration.

## Installation

```bash
pip install gravitar
```

Analyzing a Django project instead needs the `django` extra:

```bash
pip install 'gravitar[django]'
```

Requires Python 3.11+.

## Quickstart

From the root of a repository with a standard Alembic + SQLAlchemy layout:

```bash
gravitar check
```

That's it — no configuration file needed if your models live in `models/` or
`app/models/` and Alembic's `script_location` is discoverable from `alembic.ini`. On
a clean repository:

```
gravitar: no drift detected (12 tables, 78 columns analyzed)
```

When drift is found, `gravitar check` exits non-zero and reports exactly what
disagrees and where:

```
gravitar: drift detected — 1 findings

  error  missing_column  widgets.deleted_at
         model declares column, no migration adds it
         → add a migration: alembic revision --autogenerate -m "add widgets.deleted_at"
         id: 23533e5230ea

summary: 1 errors, 0 warnings across 1 tables
```

Machine-readable output is one flag away:

```bash
gravitar check --format json      # the versioned JSON envelope
gravitar check --format sarif     # SARIF 2.1.0 for GitHub Code Scanning
gravitar check --format github    # GitHub Actions ::error / ::warning / ::notice
```

The SARIF output is a drop-in for `gh api repos/O/R/code-scanning/sarifs`
uploads: category rules, per-finding confidence as `rank`, demoted findings
tagged as `kind: review`. The `github` format emits one workflow command per
finding so Gravitar appears inline on PR diffs with no SARIF upload step.

If your repository doesn't match the default conventions, run `gravitar init` to
write a starting `.gravitar.yml`, then see
[docs/configuration.md](docs/configuration.md) for the full reference.

## Feedback

Every finding carries a `confidence` score and an `id` — a signal of how sure
Gravitar is, and something you can act on. If a finding is real drift or a false
positive, tell Gravitar:

```bash
gravitar feedback confirm 23533e5230ea
gravitar feedback dismiss 23533e5230ea
gravitar feedback stats
```

Outcomes are tracked per underlying pattern (not per individual finding) in a
local, gitignored `.gravitar/feedback.sqlite` — nothing leaves your machine.
Once a pattern has real feedback, future confidence scores for that pattern
shift accordingly instead of relying on Gravitar's static defaults alone.

## Baseline / ratchet

Onboarding Gravitar into a repository that has accumulated drift shouldn't
turn CI red on day one. The baseline snapshots which findings are present
right now; later runs only fail on *new* drift:

```bash
gravitar baseline set         # snapshot the current drift findings
gravitar check                # exits 0 — pre-existing drift stays silent
gravitar baseline show        # metadata + count
gravitar baseline clear       # remove the snapshot
```

Snapshot lives at `.gravitar/baseline.json` (gitignore it or check it in,
your call). Add `--show-all` to see baselined findings in the output
without failing CI; `--no-baseline` ignores the file for one run.
Formatters are baseline-aware: SARIF results carry `baselineState`, JSON
findings carry `baselined: true`, text renders a `[baselined]` marker.

## Multi-tree projects

Monorepos and plugin architectures ship multiple models-plus-migrations
pairs per checkout — CTFd (core + plugins), Nautobot (core + apps), any
Django monorepo. Gravitar analyzes every declared tree in one run and
aggregates output:

```yaml
# .gravitar.yml
trees:
  - name: plugin_scoring
    models:
      paths: ["plugins.scoring.models"]
    alembic:
      versions_dir: "plugins/scoring/migrations/versions"
```

Flask-Migrate projects without an `alembic.ini` set
`alembic.versions_dir` directly instead of `config` / `script_location`.

## Custom type pins

Projects routinely ship their own column types — `JsonBlob`,
`PasswordHash`, `ExtendedJSON`. Pre-pin them to their canonical type so
Gravitar stops reading them as `unknown`:

```yaml
# .gravitar.yml
types:
  JsonBlob: json
  PasswordHash: "varchar(60)"
```

Applies to both the AST and live-instance sides.

## Live corroboration (optional)

Gravitar never connects to a database by default — that's the whole point. But
some findings are legitimately ambiguous under static analysis alone (an
unrecognized column type, a raw-SQL reference Gravitar's classifier
couldn't place). If you want, you can hand Gravitar a **read-only** connection
and it will corroborate just those specific low-confidence findings against
the real thing:

```bash
gravitar check --live-db-url "postgresql://gravitar_ro:...@host/db"
```

This is corroboration, not a data source: it never replaces the static
model/migration reconstruction, only resolves ambiguity in findings that
reconstruction already produced, one targeted single-column query per finding
— never a schema dump. A finding it touches gets `live_verified: true` and a
`live_db_corroboration` evidence entry; nothing else changes, and without the
flag, output is byte-for-byte identical to not having this feature at all. The
URL itself is never written to disk, logs, or any output artifact.

**Use a read-only database role.** Gravitar only ever runs schema-reflection
queries, but it can't verify the grants on whatever role you hand it — create
a role with `SELECT` on `information_schema`/`pg_catalog` only (no table data
access needed) and use that, not an application or admin credential.

## GitHub Actions

The whole point of Gravitar is to run automatically on every pull request. The
copy-paste integration lives in [docs/github-actions.md](docs/github-actions.md); the
short version:

```yaml
- name: Install Gravitar
  run: pip install gravitar

- name: Check for schema drift
  run: gravitar check
```

`gravitar check` exits non-zero when drift is found, so adding it as a required
status check blocks the merge automatically.

## Exit codes

CI systems depend on these, so they're a stable contract:

| Code | Meaning |
| ---- | ------- |
| `0`  | No drift found (or every finding is baselined) |
| `1`  | Drift found (at least one error-tier *new* finding) |
| `2`  | Configuration error (missing `alembic.ini`, unparseable `.gravitar.yml`, models package can't be imported) |
| `3`  | Analysis error (migration files unparseable, models raise on import) — distinct from drift: Gravitar couldn't complete the check |
| `10` | Warnings only, with `--warnings-as-nonzero` set (default exit is `0` if only warnings are present) |
| `11` | Incomplete coverage, with `--strict-coverage` set — opt-in gate for CI pipelines that want `0` to mean "every revision fully resolved" |

## Documentation

- [docs/configuration.md](docs/configuration.md) — the full `.gravitar.yml` reference
- [docs/github-actions.md](docs/github-actions.md) — the CI integration guide

## License

MIT — see [LICENSE](LICENSE).

## Contributing

See [CHANGELOG.md](CHANGELOG.md) for what's shipped so far.
Issues and pull requests are welcome — for anything that isn't a small fix,
please open an issue first.
