# GitHub Actions integration

This is the copy-paste path to running Gravitar as a required check on every pull
request. It should work verbatim in any repository that already has SQLAlchemy
models at a conventional package path (`models/` or `app/models/`) and an
Alembic migration history discoverable from an `alembic.ini`. If your layout
differs, use a `.gravitar.yml` — see [configuration.md](configuration.md).

## 1. Add the workflow file

Create `.github/workflows/gravitar.yml` in your repository with the following
content (also available as a standalone file at
[`examples/github-action.yml`](../examples/github-action.yml)):

```yaml
name: schema drift check

on:
  pull_request:
  push:
    branches: [main]

jobs:
  gravitar:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # Gravitar imports your models package directly (to read SQLAlchemy's
      # metadata), so your project's own dependencies must be installed too —
      # not just Gravitar itself.
      - name: Install project dependencies
        run: pip install -r requirements.txt

      - name: Install Gravitar
        run: pip install gravitar

      - name: Check for schema drift
        run: gravitar check
```

If your project uses `pyproject.toml`/Poetry/uv instead of `requirements.txt`,
replace the "Install project dependencies" step with whatever your project
normally uses to install itself — the only hard requirement is that your models
package is importable by the time `gravitar check` runs.

## 2. Adjust the branch, working directory, and Python version if needed

- Change `branches: [main]` if your default branch is named something else.
- If your models/migrations live in a subdirectory rather than the repo root, add
  `working-directory: your-subdir` to the last step, or pass a path directly:
  `gravitar check your-subdir`.
- Any Python 3.11+ works; match whatever your project already targets.

## 3. Commit and open a pull request

Push the workflow file and open a PR. The `gravitar` check will run automatically.
On a clean repository it exits `0` and the check passes; if the PR introduces
drift, it exits `1` with the specific findings printed in the job log.

## 4. Make it a required status check (recommended)

Without this step, Gravitar runs and reports but doesn't block anything. To make
it a real gate:

1. Go to **Settings → Branches** in your GitHub repository.
2. Add (or edit) a branch protection rule for your default branch.
3. Under **Require status checks to pass before merging**, add the `gravitar` job.

Now a PR that introduces schema drift cannot be merged until it's fixed.

## Useful flags for CI

- `gravitar check --format json` — machine-readable output if you want to parse
  results in a later step (see [configuration.md](configuration.md) for the JSON
  shape).
- `gravitar check --warnings-as-nonzero` — also fail the build on warning-only
  findings (unrecognized types, raw SQL in migrations) instead of the default of
  only failing on errors.

Color is disabled automatically in CI (Gravitar detects a non-TTY stdout), so
there's no need to pass `--no-color` explicitly, though it's there if you want to
be explicit about it.

## Troubleshooting

- **Exit code 2, "could not import either of ('models', 'app.models')"** — your
  models package has a different name or location. Run `gravitar init` locally
  and set `models.paths` in the generated `.gravitar.yml`.
- **Exit code 2, "alembic.ini not found"** — same idea, but for the Alembic side;
  set `alembic.config` or `alembic.script_location`.
- **Exit code 3** — Gravitar couldn't complete the analysis (an unparseable
  migration file, or an exception raised while importing your models package).
  The error message includes the file and line where it happened.

For the full list of failure modes and exit codes, see the "Exit codes"
section of the main [README](../README.md).
