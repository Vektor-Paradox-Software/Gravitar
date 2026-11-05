# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: SPEC
milestones until 1.0; after 1.0, semver.

## [Unreleased]

Entries added since the last release tag. Promote to a dated section when a
release is cut.

### Added — Benchmark harness
- **R0** — dev-only harness under `benchmarks/` with per-finding TP/FP/FN
  classification, cause-tag attribution, and `--format`-aware report
  generation. Includes a two-project self-test corpus so `python
  benchmarks/run.py` works on a fresh clone. The Postgres oracle runner is
  scaffolded for SPEC-18 v2.

### Added — Output + CI integrations
- **R14 (full)** — SARIF 2.1.0 output (`--format sarif`), baseline/ratchet
  mode (`gravitar baseline set|clear|show`, `--show-all`, `--no-baseline`),
  GitHub Actions workflow-command output (`--format github`). Baselined
  findings don't fail CI; SARIF results carry `baselineState`, JSON findings
  carry `baselined: true`, text output marks them inline.

### Added — Multi-tree + custom types
- **R10 (full)** — ProjectUnit multi-tree analysis: `.gravitar.yml trees:`
  list runs multiple models-plus-migrations pairs per checkout (CTFd, Nautobot,
  Django monorepos). `alembic.versions_dir` key supports Flask-Migrate layouts
  with no `alembic.ini`.
- **R10 (subset)** — `%(here)s` interpolation in `script_location`
  (Alembic 1.20+ default template), recursive `versions/*.py` discovery
  (Prefect-style dialect subdirs), and "zero revisions parsed is a tool
  error" (prevents a silently empty analysis).
- **R13 (full)** — `.gravitar.yml types:` custom-type pin map
  (e.g. `JsonBlob: json`). Applies on both the AST migration side and the
  live-instance model side.

### Added — Walker completeness
- **R2 (full)** — class-form type references (`sa.Integer` with no parens),
  uppercase/alias names (`VARCHAR`, `INTEGER`, `TIMESTAMP`), `.with_variant()`
  passthrough, `TypeDecorator` impl unwrap, first-positional-arg selection
  that skips `ForeignKey`/`Sequence`/`Identity`/`Computed`. Plus
  import-alias resolution (`from alembic import op as al_op` recognizes
  `al_op.create_table(...)`; `from sqlalchemy import String as Str`
  resolves `Str(50)`). Hypothesis-generated parity test asserts AST ↔
  live-instance resolution for every known type.
- **R4 (full)** — loop unroll over literal tuples/lists, module-level
  constant folding, `if`/`else`/`try` branches walked as conditional ops
  (`Operation.conditional=True`), in-file helper inlining with recursion
  guard, `external_call` notes for helpers the walker can't follow.
- **R1** — `with op.batch_alter_table(T) as B: B.add_column(...)` resolves
  to `op.add_column(T, ...)`-equivalent ops. 526 batch blocks across the
  pinned corpus — the dominant source of pre-amendment false positives.

### Added — Transparency contract
- **R3 (subset)** — scoped `Unresolved` notes demote in-scope findings a
  tier ("review"); global notes reduce confidence but don't demote.
  `unknown_type` no longer forces exit `1`; it exits per its tier (warning).
- **R7** — `coverage` struct on every run (revisions_total /
  fully_resolved / partial / skipped); `--strict-coverage` exits 11
  (incomplete) when any revision carries a note.
- **R9** — parses both `revision = "..."` (`Assign`) and `revision: str =
  "..."` (`AnnAssign`), Alembic ≥1.11's default template.
- **R11** — per-file fault isolation: a `SyntaxError` in one revision file
  becomes a `parse_error` note on a placeholder Revision; the run
  continues against the rest. Pre-R11, one unparseable file aborted the
  whole run.
- **R12** — raw-SQL classification: `op.execute(sa.text("ALTER TABLE
  ..."))` unwraps to the DDL string, pattern-matched statements update
  schema state, DML/PRAGMA counted as inert, truly dynamic SQL becomes a
  `dynamic_sql` note (not a 0.3-confidence broken_chain warning).
- **R16** — Django parser: every op returns a result (handled, inert, or
  `django_op_unsupported` note); `RenameField`/`RenameModel`/`AlterModelTable`
  handled; per-file isolation matches the Alembic parser.

### Added — SPEC-12 amendment contracts
- **Scenario E** — accounting identity: `nodes_seen == applied + inert +
  noted`. Every input node is either an Operation, a known-inert, or a
  Note. Enforced across both parsers.
- **Scenario F** — tier/exit contract: no error-tier finding on a tainted
  table; exit code semantics explicit; coverage line present on every run.
- **Corpus-quirk fixtures (G9)** — synthetic mirrors of real-world shapes:
  `%(here)s`, nested versions, ini outside root, multi-tree, annotated
  revision, syntax-error-isolated, booby-trapped migration proving the
  parser never imports.
- **G4** — `pattern_id` includes resolution class
  (`ast:missing_column:resolved` vs `:partial`), so feedback for a partial
  finding doesn't degrade reliability on fully-resolved ones.
- **G5** — parity tolerance 0.05 (strictly less than the 0.15 unresolved
  penalty), with a regression test that fails if widened.
- **G10** — raw-SQL finding ids are per-source-line unique.

### Added — Pre-amendment foundation (initial milestones)
- **M1-M12** — the base tool: AST parser, SQLAlchemy introspection,
  comparator, CLI, output formatters, repository discovery, failure modes,
  confidence engine, raw-SQL classification, feedback recalibration,
  Django parser, live corroboration, full-pipeline integration suite.

See individual commit messages for ticket-level detail.

[Unreleased]: https://github.com/vektor-paradox-software/Gravitar/compare/HEAD...HEAD
