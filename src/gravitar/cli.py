"""Argument parsing and command dispatch."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from gravitar import __version__
from gravitar.analysis.demotion import demote_explained_findings
from gravitar.analysis.live_corroboration import corroborate
from gravitar.baseline import (
    baseline_path_for,
    clear_baseline,
    load_baseline,
    read_baseline_metadata,
    write_baseline,
)
from gravitar.config import DEFAULT_CONFIG_YAML, TreeConfig, resolve_config
from gravitar.errors import (
    EXIT_ANALYSIS_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_DRIFT,
    EXIT_INCOMPLETE,
    EXIT_SUCCESS,
    EXIT_WARNINGS_ONLY,
    ConfigError,
)
from gravitar.feedback import store as feedback_store
from gravitar.feedback.ids import finding_id as compute_finding_id
from gravitar.feedback.ids import pattern_id as pattern_id_for
from gravitar.feedback.recalibrate import apply_reliability
from gravitar.feedback.reliability import reliability_multiplier
from gravitar.output.github import format_github
from gravitar.output.json import format_json, format_json_error
from gravitar.output.sarif import format_sarif
from gravitar.output.text import format_text
from gravitar.schema import Coverage, DriftFinding, Schema, Unresolved, sort_findings
from gravitar.schema.compare import compare_schemas, filter_findings
from gravitar.schema.migrations import (
    build_migration_result,
    build_migration_schema,
    parse_versions_dir,
)
from gravitar.schema.models import load_model_schema
from gravitar.schema.types import custom_types_scope


def _emit_error(message: str, want_json: bool) -> None:
    """Report a ConfigError/AnalysisError. With --format json (or output.format:
    json), the error goes to stdout as the same envelope shape normal results use,
    so a caller doesn't have to special-case stderr text to detect failure.
    """
    if want_json:
        click.echo(format_json_error(message), nl=False)
    else:
        click.echo(f"gravitar: {message}", err=True)


def _ensure_utf8_streams() -> None:
    # Output uses non-ASCII characters (→, —). Some terminals — notably Windows'
    # legacy console codepages — default stdout/stderr to an encoding that can't
    # represent them, which crashes with a raw UnicodeEncodeError instead of the
    # clean exit codes this CLI otherwise guarantees. errors="replace" is a last
    # line of defense: worst case is a substituted glyph, never a crash.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


@click.group()
def main() -> None:
    """Gravitar — schema drift detector for SQLAlchemy models and Alembic migrations."""
    _ensure_utf8_streams()


@main.command("version")
def version_cmd() -> None:
    """Print version and exit."""
    click.echo(f"gravitar {__version__}")
    sys.exit(EXIT_SUCCESS)


@main.command("init")
def init_cmd() -> None:
    """Write a default .gravitar.yml to cwd."""
    target = Path.cwd() / ".gravitar.yml"
    if target.exists():
        click.echo(f"gravitar: {target} already exists, refusing to overwrite", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
    target.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    click.echo(f"gravitar: wrote {target}")
    sys.exit(EXIT_SUCCESS)


def _analyze_tree(
    tree: TreeConfig, repo_path: Path
) -> tuple[list[DriftFinding], list[Unresolved], Coverage, int, int]:
    """SPEC-16 R10 (full): run the full parse → compare → demote → recalibrate
    pipeline against ONE tree, returning (findings, notes, coverage,
    tables_analyzed, columns_analyzed). The caller aggregates across trees.

    Raises ConfigError (bad models path, missing versions dir) and
    AnalysisError-shaped exceptions (zero revisions) up to the CLI, which
    maps them to the right exit code. A zero-revisions tree is a tool
    error for THIS tree; multi-tree runs do not swallow it.
    """
    if tree.dsl == "django":
        assert tree.django_app_path is not None
        model_schema, migrations_dir, app_label = _load_django(tree.django_app_path)
        from gravitar.schema.parsers.django_parser import parse_migrations_dir

        revisions = parse_migrations_dir(migrations_dir, app_label)
        where = f"app '{tree.django_app_path}'"
    else:
        assert tree.versions_dir is not None
        model_schema = load_model_schema(tree.model_paths, tree.base_classes)
        revisions = parse_versions_dir(tree.versions_dir)
        where = str(tree.versions_dir)

    if not revisions:
        raise ConfigError(
            f"zero revisions found under {where}; check the configured migrations directory"
        )

    migration_result = build_migration_result(revisions)
    findings = compare_schemas(model_schema, migration_result.schema) + migration_result.findings
    findings = demote_explained_findings(findings, migration_result.taint)
    findings = apply_reliability(findings, feedback_store.db_path_for(repo_path))
    tables_analyzed = len(model_schema)
    columns_analyzed = sum(len(table["columns"]) for table in model_schema.values())
    return (
        findings,
        migration_result.notes,
        migration_result.coverage,
        tables_analyzed,
        columns_analyzed,
    )


def _merge_coverage(coverages: list[Coverage]) -> Coverage:
    """SPEC-16 R10 (full): across trees, coverage sums per-field. A clean
    repo of two trees with 10 revisions each shows revisions_total=20."""
    merged = Coverage(
        revisions_total=0,
        revisions_fully_resolved=0,
        revisions_partial=0,
        revisions_skipped=0,
    )
    for coverage in coverages:
        merged["revisions_total"] += coverage["revisions_total"]
        merged["revisions_fully_resolved"] += coverage["revisions_fully_resolved"]
        merged["revisions_partial"] += coverage["revisions_partial"]
        merged["revisions_skipped"] += coverage["revisions_skipped"]
    return merged


def _load_django(app_path: str) -> tuple[Schema, Path, str]:
    """Load a Django app's model schema, and resolve its migrations directory
    and app_label for the migration-parsing step that follows (SPEC-
    GENERALIZATION-ENGINE.md Section 4). Deferred import: a pure-Alembic
    install never needs the 'django' package at all.
    """
    try:
        import importlib

        from gravitar.schema.parsers.django_models import load_django_model_schema
    except ImportError as e:
        raise ConfigError(
            "Django support requires the 'django' package: pip install 'gravitar[django]'"
        ) from e

    model_schema = load_django_model_schema([app_path])
    app_module = importlib.import_module(app_path)
    if app_module.__file__ is None:
        raise ConfigError(f"Django app {app_path!r} has no resolvable file location")
    migrations_dir = Path(app_module.__file__).parent / "migrations"
    app_label = app_path.rsplit(".", 1)[-1]
    return model_schema, migrations_dir, app_label


@main.command("check")
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd(),
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override .gravitar.yml location",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "sarif", "github"]),
    default=None,
    help=(
        "Output format: text/json/sarif, or github for GitHub Actions "
        "workflow commands (inline PR annotations). Default: text."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=None,
    help="Suppress summary line (default: output.quiet from .gravitar.yml, or off)",
)
@click.option(
    "--warnings-as-nonzero", is_flag=True, default=False, help="Exit 10 if warnings present"
)
@click.option("--no-color", is_flag=True, default=False, help="Disable ANSI color")
@click.option(
    "--live-db-url",
    default=None,
    help=(
        "Optional read-only database URL to corroborate low-confidence findings "
        "against. Off by default; never persisted."
    ),
)
@click.option(
    "--strict-coverage",
    is_flag=True,
    default=False,
    help=(
        "SPEC-16 R7: exit 11 (incomplete) if any revision wasn't fully resolved. "
        "Off by default; opt-in for CI pipelines that want certainty."
    ),
)
@click.option(
    "--show-all",
    is_flag=True,
    default=False,
    help=(
        "SPEC-16 R14: when a baseline is active, show baselined findings in "
        "addition to new ones (default hides baselined). Exit code still "
        "only reflects new drift."
    ),
)
@click.option(
    "--no-baseline",
    is_flag=True,
    default=False,
    help=(
        "SPEC-16 R14: ignore .gravitar/baseline.json for this run — every "
        "finding counts as new. Useful for ad-hoc runs that shouldn't "
        "ratchet against a stored baseline."
    ),
)
def check_cmd(
    path: Path,
    config_path: Path | None,
    output_format: str | None,
    quiet: bool | None,
    warnings_as_nonzero: bool,
    no_color: bool,
    live_db_url: str | None,
    strict_coverage: bool,
    show_all: bool,
    no_baseline: bool,
) -> None:
    """Run drift analysis; PATH defaults to cwd."""
    repo_path = path.resolve()

    # Model package discovery (in resolve_config) and load_model_schema both need
    # repo_path importable, so it must be on sys.path before either runs.
    sys.path.insert(0, str(repo_path))
    for module_name in ("models", "app", "app.models"):
        sys.modules.pop(module_name, None)

    all_findings: list[DriftFinding] = []
    all_notes: list[Unresolved] = []
    all_coverages: list[Coverage] = []
    total_tables = 0
    total_columns = 0
    resolved = None

    try:
        try:
            resolved = resolve_config(repo_path, config_path)
        except ConfigError as e:
            _emit_error(str(e), output_format == "json")
            sys.exit(EXIT_CONFIG_ERROR)
        except Exception as e:
            # A non-ImportError exception while discovering the default models
            # package (e.g. the module's top-level code raised); fail closed
            # rather than crash with a raw traceback.
            _emit_error(f"analysis error: {e}", output_format == "json")
            sys.exit(EXIT_ANALYSIS_ERROR)

        # Config resolved successfully, so its output.format preference is known now.
        effective_format = output_format if output_format is not None else resolved.output_format
        want_json = effective_format == "json"
        want_sarif = effective_format == "sarif"
        want_github = effective_format == "github"

        # SPEC-16 R13 (full): install the user-provided custom-type map
        # for every parser + model-loader call made under this scope. The
        # map is read via a ContextVar inside normalize_type/normalize_ast_type
        # so handlers don't need to thread it through every signature.
        with custom_types_scope(resolved.custom_types):
            for tree in resolved.all_trees():
                try:
                    findings, notes, coverage, tables, columns = _analyze_tree(tree, repo_path)
                except ConfigError as e:
                    _emit_error(str(e), want_json)
                    sys.exit(EXIT_ANALYSIS_ERROR)
                except Exception as e:
                    # Per-tree parse errors (R11) are already notes inside the tree;
                    # anything bubbling up to here is a genuinely fatal failure
                    # (bad .ini, unreadable versions dir, un-importable models).
                    _emit_error(f"analysis error: {e}", want_json)
                    sys.exit(EXIT_ANALYSIS_ERROR)
                all_findings.extend(findings)
                all_notes.extend(notes)
                all_coverages.append(coverage)
                total_tables += tables
                total_columns += columns
    finally:
        if str(repo_path) in sys.path:
            sys.path.remove(str(repo_path))

    assert resolved is not None  # control flow above exits otherwise

    if live_db_url:
        all_findings = corroborate(all_findings, live_db_url)
    all_findings = sort_findings(
        filter_findings(
            all_findings,
            checks=resolved.checks,
            exclude_tables=resolved.exclude_tables,
            exclude_columns=resolved.exclude_columns,
        )
    )
    merged_coverage = _merge_coverage(all_coverages)

    # SPEC-16 R14 (remainder): baseline/ratchet. If a baseline file exists
    # and --no-baseline wasn't passed, partition findings into "baselined"
    # (already-known drift that doesn't fail CI) and "new" (unseen drift
    # that does). --show-all keeps baselined findings in the output;
    # without it they're filtered to keep CI output focused.
    baselined_ids: frozenset[str] | None = None
    if not no_baseline:
        try:
            loaded = load_baseline(baseline_path_for(repo_path))
        except ValueError as e:
            _emit_error(str(e), want_json)
            sys.exit(EXIT_CONFIG_ERROR)
        if loaded is not None:
            baselined_ids = frozenset(loaded)
    display_findings = all_findings
    if baselined_ids is not None and not show_all:
        display_findings = [f for f in all_findings if compute_finding_id(f) not in baselined_ids]

    # CLI flags override .gravitar.yml's output.format/output.quiet when given;
    # otherwise the config file's values (themselves defaulting to text/false) apply.
    effective_quiet = quiet if quiet is not None else resolved.quiet

    if want_sarif:
        output = format_sarif(
            display_findings,
            total_tables,
            total_columns,
            notes=all_notes,
            coverage=merged_coverage,
            baselined_ids=baselined_ids,
        )
    elif want_json:
        output = format_json(
            display_findings,
            total_tables,
            total_columns,
            notes=all_notes,
            coverage=merged_coverage,
            baselined_ids=baselined_ids,
        )
    elif want_github:
        output = format_github(
            display_findings,
            total_tables,
            total_columns,
            notes=all_notes,
            baselined_ids=baselined_ids,
        )
    else:
        color_disabled = no_color or bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()
        output = format_text(
            display_findings,
            total_tables,
            total_columns,
            notes=all_notes,
            coverage=merged_coverage,
            quiet=effective_quiet,
            no_color=color_disabled,
            baselined_ids=baselined_ids,
        )
    click.echo(output, nl=False)

    # SPEC-16 R14 (remainder): exit code reflects NEW drift only. A baselined
    # finding is pre-existing drift the team has agreed to carry, so it must
    # not fail CI; otherwise ratcheting is impossible. Count by *effective*
    # tier (after R3 demotion), not raw severity.
    def _is_new(f: DriftFinding) -> bool:
        return baselined_ids is None or compute_finding_id(f) not in baselined_ids

    errors = sum(
        1 for f in all_findings if f["kind"] == "fail" and f["severity"] == "error" and _is_new(f)
    )
    warnings = sum(
        1 for f in all_findings if f["kind"] == "fail" and f["severity"] == "warning" and _is_new(f)
    )
    # ``unknown_type`` no longer forces exit 1. It exits per its severity
    # tier (warning). The rationale is that "could not read" isn't drift; failing
    # CI on it turns unreadable input into a red build without surfacing
    # anything the user can act on.
    if errors:
        sys.exit(EXIT_DRIFT)
    if warnings and warnings_as_nonzero:
        sys.exit(EXIT_WARNINGS_ONLY)
    # SPEC-16 Amendment R7 / T5: a clean run with incomplete coverage must
    # not look identical to a fully-covered clean run. --strict-coverage is
    # the opt-in gate for CI pipelines that want "no notes" to fail.
    if strict_coverage and all_notes:
        sys.exit(EXIT_INCOMPLETE)
    sys.exit(EXIT_SUCCESS)


def _analyze_for_feedback(repo_path: Path) -> list[DriftFinding]:
    """Re-run the same analysis `check` uses, unfiltered and unsorted, so
    `feedback confirm`/`dismiss` can locate a finding by id. feedback.sqlite
    stores only ids and pattern_ids (Section 3.1), not full finding data, so the
    finding itself is always looked up fresh against the current repo state
    rather than cached. SPEC-16 R10 (full): iterates every tree so a
    finding in a plugin/extra tree is still resolvable.
    """
    sys.path.insert(0, str(repo_path))
    for module_name in ("models", "app", "app.models"):
        sys.modules.pop(module_name, None)
    try:
        resolved = resolve_config(repo_path, None)
        combined: list[DriftFinding] = []
        with custom_types_scope(resolved.custom_types):
            for tree in resolved.all_trees():
                if tree.dsl == "django":
                    assert tree.django_app_path is not None
                    from gravitar.schema.parsers.django_parser import parse_migrations_dir

                    model_schema, migrations_dir, app_label = _load_django(tree.django_app_path)
                    revisions = parse_migrations_dir(migrations_dir, app_label)
                else:
                    assert tree.versions_dir is not None
                    model_schema = load_model_schema(tree.model_paths, tree.base_classes)
                    revisions = parse_versions_dir(tree.versions_dir)
                migration_schema, chain_findings = build_migration_schema(revisions)
                combined.extend(compare_schemas(model_schema, migration_schema) + chain_findings)
    finally:
        sys.path.remove(str(repo_path))

    return combined


def _resolve_feedback_target(finding_id_arg: str, repo_path: Path) -> DriftFinding:
    try:
        findings = _analyze_for_feedback(repo_path)
    except ConfigError as e:
        click.echo(f"gravitar: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
    except Exception as e:
        click.echo(f"gravitar: analysis error: {e}", err=True)
        sys.exit(EXIT_ANALYSIS_ERROR)

    for finding in findings:
        if compute_finding_id(finding) == finding_id_arg:
            return finding

    click.echo(
        f"gravitar: no finding with id {finding_id_arg!r} in the current analysis "
        "of this repository (run `gravitar check --format json` to see current ids)",
        err=True,
    )
    sys.exit(EXIT_ANALYSIS_ERROR)


@main.group("feedback")
def feedback_group() -> None:
    """Record and inspect drift-finding outcomes that recalibrate confidence."""


def _record_feedback(finding_id_arg: str, path: Path, verdict: str) -> None:
    repo_path = path.resolve()
    finding = _resolve_feedback_target(finding_id_arg, repo_path)
    pattern = pattern_id_for(finding)
    feedback_store.record_outcome(
        feedback_store.db_path_for(repo_path), finding_id_arg, pattern, verdict
    )
    click.echo(f"gravitar: recorded {verdict} for {finding_id_arg} (pattern {pattern!r})")
    sys.exit(EXIT_SUCCESS)


@feedback_group.command("confirm")
@click.argument("finding_id_arg", metavar="FINDING_ID")
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path.cwd()
)
def feedback_confirm_cmd(finding_id_arg: str, path: Path) -> None:
    """Mark FINDING_ID as real drift, reinforcing its pattern's reliability."""
    _record_feedback(finding_id_arg, path, "confirmed")


@feedback_group.command("dismiss")
@click.argument("finding_id_arg", metavar="FINDING_ID")
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path.cwd()
)
def feedback_dismiss_cmd(finding_id_arg: str, path: Path) -> None:
    """Mark FINDING_ID as a false positive, weakening its pattern's reliability."""
    _record_feedback(finding_id_arg, path, "false_positive")


@feedback_group.command("stats")
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path.cwd()
)
def feedback_stats_cmd(path: Path) -> None:
    """Show recorded confirm/dismiss counts and reliability per pattern."""
    repo_path = path.resolve()
    counts = feedback_store.pattern_counts(feedback_store.db_path_for(repo_path))
    if not counts:
        click.echo("gravitar: no feedback recorded yet")
        sys.exit(EXIT_SUCCESS)

    for pattern in sorted(counts):
        confirm_count, dismiss_count = counts[pattern]
        score = reliability_multiplier(confirm_count, dismiss_count)
        click.echo(
            f"{pattern}: {confirm_count} confirmed, {dismiss_count} dismissed, "
            f"reliability {score:.2f}"
        )
    sys.exit(EXIT_SUCCESS)


@main.group("baseline")
def baseline_group() -> None:
    """SPEC-16 R14 (remainder): manage the baseline/ratchet file. The baseline
    records which drift findings are known as of a point in time so later
    runs can distinguish new drift from pre-existing drift."""


def _collect_current_finding_ids(repo_path: Path) -> list[str]:
    """Re-run the same pipeline ``check`` uses (unfiltered, pre-baseline)
    and return every finding's stable id."""
    try:
        findings = _analyze_for_feedback(repo_path)
    except ConfigError as e:
        click.echo(f"gravitar: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)
    except Exception as e:
        click.echo(f"gravitar: analysis error: {e}", err=True)
        sys.exit(EXIT_ANALYSIS_ERROR)
    return [compute_finding_id(f) for f in findings]


@baseline_group.command("set")
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path.cwd()
)
def baseline_set_cmd(path: Path) -> None:
    """Snapshot current findings as the baseline. Subsequent `check` runs
    exit 0 for any finding in this snapshot and exit 1 only for new drift."""
    repo_path = path.resolve()
    finding_ids = _collect_current_finding_ids(repo_path)
    baseline_file = baseline_path_for(repo_path)
    write_baseline(baseline_file, finding_ids)
    click.echo(f"gravitar: wrote {baseline_file} with {len(set(finding_ids))} finding(s)")
    sys.exit(EXIT_SUCCESS)


@baseline_group.command("clear")
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path.cwd()
)
def baseline_clear_cmd(path: Path) -> None:
    """Remove the baseline file. All drift findings will fail CI again."""
    repo_path = path.resolve()
    baseline_file = baseline_path_for(repo_path)
    if clear_baseline(baseline_file):
        click.echo(f"gravitar: removed {baseline_file}")
    else:
        click.echo(f"gravitar: no baseline at {baseline_file}")
    sys.exit(EXIT_SUCCESS)


@baseline_group.command("show")
@click.argument(
    "path", type=click.Path(exists=True, file_okay=False, path_type=Path), default=Path.cwd()
)
def baseline_show_cmd(path: Path) -> None:
    """Print baseline metadata (location, timestamp, finding count)."""
    repo_path = path.resolve()
    baseline_file = baseline_path_for(repo_path)
    metadata = read_baseline_metadata(baseline_file)
    if metadata is None:
        click.echo(f"gravitar: no baseline at {baseline_file}")
        sys.exit(EXIT_SUCCESS)
    created_at = metadata.get("created_at", "unknown")
    finding_ids = metadata.get("finding_ids") or []
    click.echo(f"gravitar: baseline at {baseline_file}")
    click.echo(f"  created_at: {created_at}")
    click.echo(f"  findings: {len(finding_ids)}")
    sys.exit(EXIT_SUCCESS)


if __name__ == "__main__":
    main()
