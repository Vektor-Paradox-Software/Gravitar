# Gravitar — Production Readiness Review

Prepared for Chad Neipling · 2026-10-07 · Evidence: installed Gravitar (M12 code) probed against five pinned open-source projects.

## 0. Bottom line

1. Gravitar's core idea survives contact with real projects. Its *parser* is not the scale risk: 550 real migration files parse in 0.47 s total (0.5–1.7 ms/file).
2. Its *coverage* is the risk, and the failure mode is the dangerous one: **silence**. In probes, batch blocks, loops, helpers, annotated revision IDs, nested version dirs and class-form types all produce either zero findings or false errors, with no signal to the user that analysis was incomplete.
3. The fix is one contract, not eleven patches: **Gravitar must always tell the user what it could not see, and may only say `error` about things it fully saw.** Everything below follows from that.
4. Reaching "industry standard" is a measurable bar (section 3), not a slogan. Bring the benchmark to it before claiming parity with `alembic check`.

## 1. Scenarios to plan for (Q1)

Status key: **P** = reproduced by probe on installed code · **C** = confirmed in corpus census · **R** = reasoned from code, not yet exercised.

| # | Scenario | Status | What happens today | Ticket |
|---|---|---|---|---|
| S1 | Alembic ≥1.11 default template (`revision: str = "..."`, AnnAssign) | P | Revision ignored; empty schema, no error. Corpus has 0 annotated files, so this did not cause the benchmark, but every project created from a current template is exposed. | R9 |
| S2 | `batch_alter_table` | P,C | Invisible. 526 blocks across MLflow/Airflow/Mealie/Prefect. | R1 |
| S3 | Loops, `if`, in-file helpers, external helpers receiving `op` | P,C | Ops dropped, nothing emitted. Airflow has 13 external calls passing `op`. | R4 |
| S4 | Class-form / aliased / custom types (`sa.Integer`, `StringID`, `UtcDateTime`, `GUID`) | P,C | `unknown:*` or ignored (so `alter_column(type_=sa.BigInteger)` leaves the old type). 23–292 unsupported type refs per project. | R2, R13 |
| S5 | Custom `TypeDecorator`, `Interval`, `PickleType`, `with_variant` | P | `unknown:*` on one side only → false type mismatch. | R2, R13 |
| S6 | `%(here)s` in `script_location` (default `alembic init` output on 1.20) | P | Config crash. | R10 |
| S7 | Non-root `alembic.ini`, package-resource `script_location`, no ini at all (Flask-Migrate) | C | 4 of 5 projects lack a root `alembic.ini`. Six distinct `script_location` forms across five projects. | R10 |
| S8 | Multiple migration trees / multiple metadatas in one repo | C | Airflow 3 trees, MLflow 2, CTFd core+plugin, Prefect dual-dialect dirs. A single (models, migrations) pair is the wrong unit. | R10 |
| S9 | Nested `versions/<subdir>` | P | Parses zero revisions silently → every model table reported `missing_table`. | R10 |
| S10 | One unparseable file (incl. newer-Python syntax; Mealie ships PEP 758 `except A, B:`) | P,C | Whole run aborts (exit 3). | R11 |
| S11 | Raw SQL: DML, `sa.text()` DDL, plain-string DDL | P,C | DML → 0.3-confidence "any table may be affected" noise; `sa.text` DDL unclassified; plain DDL classified but not applied → false `missing_column`. Prefect 113 DDL-ish executes. | R12 |
| S12 | Model import failure / import side effects | C(code) | Aborts run; also executes project code. | R6, R15 |
| S13 | Untrusted PR code in CI | R | Import-based model loading is an RCE vector on fork PRs. A hosted tier cannot import customer code at all. | R15 |
| S14 | SQLAlchemy 1.4 projects vs `sqlalchemy>=2.0` pin | R | Cannot be installed alongside. Static reader removes the constraint. | R15 |
| S15 | Raw-SQL findings in one revision share an ID | C(code) | Baseline/dismissal collisions. | R12, R14 |
| S16 | Schemas (`schema=`) | C | Rare (MLflow 1, Airflow 10, Mealie 1). Lower priority than first thought. | R5 |
| S17 | Live-inspector migrations (`inspect(bind)`, `has_table`) | C | Airflow has 9. Statically undecidable → must surface as a note, never be guessed. | R3 |
| S18 | `env.py` filtering (`include_object`, `include_name`) | R | Gravitar compares objects Alembic is configured to ignore → false errors vs the oracle. | R10 |
| S19 | Branches/merge heads, multiple heads | R | Graph code exists; needs the multi-tree unit to be correct. | R10 |

Not compared by design (state this in docs and output): indexes, constraints, server defaults, comments, views, triggers, enums at the DB level. A schema tool that does not say what it omits is hiding drift by omission.

## 2. Transparency contract (Q2)

You are right that this is the product. Make it an enforced invariant, tested like a feature:

- **T1 No silent skips.** Every op, file, tree or model Gravitar could not fully interpret yields an `Unresolved` note with reason, file, line and scope. A test fails if any input node is neither applied nor noted.
- **T2 No `error` from incomplete signal.** `error` requires both sides fully resolved for that table/column. Otherwise the finding is `warning` or `review`.
- **T3 Demotion never deletes.** A demoted finding stays in JSON/SARIF output with `demoted_from`, `reason` and the note IDs that explain it. The text summary always prints counts per tier and per demotion reason, e.g. `3 error · 5 warning · 12 review (9 demoted: unresolved loop in 0042)`.
- **T4 Coverage line on every run.** `Coverage: 548/550 revisions fully resolved · 2 partial · 0 skipped`. Zero revisions parsed or zero tables found is itself an error-level *tool* failure, never a clean pass.
- **T5 Clean means clean-and-covered.** Exit 0 is only emitted when coverage meets the configured floor; below it the exit is a distinct "incomplete" code. A green check must never mean "I saw nothing".
- **T6 Policy visible.** Output header names the active policy (built-in or org profile + version). Calibration may never be invisible.
- **T7 Real drift hiding hazard.** The failure you are guarding against is a real drift demoted to `review` and ignored. Mitigations: `--fail-on review` available; baseline ratchet fails when review count *rises*; coverage line makes the demotion cause visible; benchmark gate includes a *hard-seed* bucket (real drift planted inside batch/loop/helper code) and measures how often it surfaces at ≥ warning.

Code changes needed for scale beyond the R-tickets: per-file fault isolation (R11), stable unique finding IDs (R12), SARIF + baseline (R14), coverage report (R7), a static model reader (R15), discovery producing `ProjectUnit(metadata, migration_tree)` pairs from `env.py` `target_metadata` (all five env.py files set one; CTFd's is not statically resolvable, so a config override is required).

## 3. False-error standard (Q3)

**There is no single published numeric standard for schema-drift tools.** The defensible reference points are from static analysis generally:

| Source | Standard |
|---|---|
| Google Tricorder | Effective false-positive rate < 10% ("developers did not take positive action"); analyzers above 10% are disabled until fixed. |
| Error Prone | ERROR-level checks should have essentially no false positives; WARNING-level should be right ≥ 90% of the time. |
| Coverity (Bessey et al.) | Aim < 20% for stable checkers; > 30% and "people ignore the tool"; the first few reports matter disproportionately. |
| SARIF 2.1.0 | `level` none/note/warning/error; `rank` 0–100 confidence; `kind` including `review` ("warrants human review"). |

**Adopt a tiered contract that maps onto these:**

| Tier | SARIF | Rule | Target |
|---|---|---|---|
| error | level=error | Both sides fully resolved | ~0 false positives (pre-1.0: ≤1 per benchmark project; GA: 0) |
| warning | level=warning | Partial resolution, strong evidence | ≤10% effective FP |
| review | level=warning/note, kind=review | Cannot be determined | Not scored as FP; coverage-gated, always counted and shown |

Confidence 0–1 maps to SARIF `rank` (×100). The M9 feedback dismissal rate *is* Google's effective-FP rate, so a pattern whose reliability falls below ~0.9 auto-demotes one tier (and the demotion is shown per T3).

Also gate on **recall**: an error-tier recall floor and ≥95% seeded recall, so the system can't "win" by demoting everything. (Your seeded-recommendation is accepted: the gate is FP ceiling *and* recall floor together.) Current state: 113 false errors on 5 projects, i.e. far outside any of these bars.

## 4. Calibration by clients (Q3 cont.)

Yes, clients will want it, and it should exist, in three layers with guardrails:

1. **Built-in tiers** (above) — fixed, not configurable below the floor.
2. **Project config:** per-category severity overrides, `min_confidence`, baseline/ratchet (accept existing drift, fail on new).
3. **Org policy profiles** (the Stage 4 governance on-ramp): versioned, signed-off policy; *loosening* requires a recorded justification and is visible in output (T6). Tightening is free.

Rule: a client may move findings between warning/review/ignore; they may not make Gravitar claim `error` on unresolved input, and the tool never reports a lowered tier as if it were the default.

## 5. Marketability beyond drift

Ranked by pull ÷ effort, given the existing AST parser and zero-connection posture:

1. **Migration-safety linting** (lock-heavy ops, non-null add without default, type narrowing) — a proven paid category (Squawk, strong_migrations, Atlas lint); reuses the parser and runs in CI with no DB.
2. **Multi-head / merge-race PR guard** — graph code exists; high-frequency real pain for teams with parallel branches.
3. **SQL-migration frontend** (sqlglot) for Flyway/golang-migrate — widens beyond Python shops.
4. **Schema catalog / ERD from code** — natural output of the canonical `Schema`; entry to the governance layer.
5. **Policy-as-code** (PII columns, naming, required audit columns) — the Stage 4 product.
6. **Optional live-environment drift** via the M11 bridge — only as opt-in; the zero-connection invariant stays for the core.
7. More ORMs/DSLs (the DSL-agnostic design makes this a story, not just a feature).

Note the vocabulary problem: the industry says "drift" for *environment* drift (live DB vs desired state). Position Gravitar as "code-vs-code drift, no database needed" explicitly.

## 6. Benchmark corpus (Q4)

Four public Alembic projects plus MLflow, cloned and pinned (see `manifest.yml`). Atlas is excluded (proprietary).

| Project | Revisions | Batch blocks / inner calls | Loops / ifs | Notable |
|---|---|---|---|---|
| MLflow | 74 | 50 / 91 | 14 / 33 | 23/454 type refs unsupported; 2 trees |
| Airflow | 151 | 228 / 432 | 27 / 100 | 276/739 types unsupported; 3 trees; raw execute 39 DML, 14 DDL-ish, 21 sa.text, 9 dynamic |
| Mealie | 53 | 56 / 147 | 1 / 11 | 173/534 types unsupported; 1 file with Python 3.14-only syntax |
| CTFd | 37 | 0 / 0 | 6 / 25 | core + plugin trees; no root-level ini (Flask-Migrate) |
| Prefect | 231 | 192 / 571 | 56 / 52 | 113 DDL-ish executes; postgresql + sqlite sibling dirs |

Census is read-only AST analysis. I have **not** yet run the full oracle baseline on these; that waits for the test environment you want to set up. Parse timing above is real; accuracy numbers are not claimed here.

## 7. Test strategy outline (deferred, per your note)

Corpus tiers (pinned real projects → synthetic fixtures) · seeded mutation with a hard-seeds bucket · generative/differential tests (random op sequences rendered in several syntactic styles, ground truth by executing in SQLite) · git-history replay · oracle = `alembic check` honoring `env.py` (policy mode) plus strict `compare_metadata` mode, Postgres for type fidelity (SQLite reflection loses types). The oracle may use a database; Gravitar's runtime may not.

## Sources

- [Lessons from Building Static Analysis Tools at Google](https://cacm.acm.org/research/lessons-from-building-static-analysis-tools-at-google/)
- [A Few Billion Lines of Code Later](https://cacm.acm.org/research/a-few-billion-lines-of-code-later)
- [Error Prone: bug pattern criteria](https://errorprone.info/docs/criteria)
- [SARIF 2.1.0 (OASIS)](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
