"""`.gravitar.yml` loading and repository discovery (see docs/configuration.md).

This module resolves everything `gravitar check` needs to run: which model
package(s) to import, where the Alembic versions/ directory is, which drift
categories are enabled, and which tables/columns to exclude. Unknown keys are
rejected outright — "do not silently ignore" typos.
"""

from __future__ import annotations

import configparser
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from gravitar.errors import ConfigError

_DEFAULT_MODEL_PACKAGES = ("models", "app.models")

_DEFAULT_CHECKS: dict[str, bool] = {
    "missing_table": True,
    "missing_column": True,
    "type_mismatch": True,
    "nullability_mismatch": True,
    "orphaned_column": True,
    "orphaned_reference": True,
    "broken_chain": True,
}

# The full .gravitar.yml schema (Section 2.2): top-level section -> its known keys.
# ``alembic.versions_dir`` is SPEC-16 R10 (full): no-ini support — projects such
# as Flask-Migrate ship no alembic.ini, so users point directly at the versions
# directory. ``trees:`` is R10's ProjectUnit multi-tree key. ``types:`` is
# SPEC-16 R13 (full): project-local custom type → canonical type map so unmapped
# column types stop reading as type_mismatch (readiness D4: Airflow's `TIMESTAMP`
# wrappers, MLflow's `JsonBlob`).
_KNOWN_KEYS: dict[str, set[str]] = {
    "models": {"paths", "base_classes"},
    "alembic": {"config", "script_location", "versions_dir"},
    "django": {"app_path"},
    "checks": set(_DEFAULT_CHECKS),
    "exclude": {"tables", "columns"},
    "output": {"format", "quiet"},
    "trees": set(),  # value is a list; nested keys validated per-tree
    "types": set(),  # value is a mapping; keys are user-defined type names
}

# Keys allowed inside each entry of the top-level ``trees:`` list.
_KNOWN_TREE_KEYS: set[str] = {"name", "models", "alembic", "django"}


@dataclass
class TreeConfig:
    """SPEC-16 Amendment R10 (full): one analysis unit within a repo.

    A ``TreeConfig`` is a models-plus-migrations pair — one DSL, one models
    package (or Django app), one versions directory. Repos such as CTFd
    (core plus plugins), Nautobot (core plus apps), and large monorepos
    ship multiple of these per checkout; R10 analyzes each and aggregates
    the output. Readiness doc D4.
    """

    dsl: str  # "alembic" | "django"
    model_paths: list[str]
    base_classes: list[str] | None
    versions_dir: Path | None  # Alembic only
    django_app_path: str | None  # Django only
    name: str | None = None  # Human-readable label for output; defaults to inferred


@dataclass
class ResolvedConfig:
    dsl: str  # "alembic" | "django"
    model_paths: list[str]
    base_classes: list[str] | None
    versions_dir: Path | None  # Alembic only
    django_app_path: str | None  # Django only
    checks: dict[str, bool] = field(default_factory=lambda: dict(_DEFAULT_CHECKS))
    exclude_tables: list[str] = field(default_factory=list)
    exclude_columns: list[str] = field(default_factory=list)
    output_format: str = "text"
    quiet: bool = False
    # SPEC-16 R10 (full): additional trees analyzed in the same run. The
    # primary tree stays in the top-level fields above for pre-R10
    # back-compat; ``extra_trees`` holds any trees declared under the YAML
    # ``trees:`` key.
    extra_trees: list[TreeConfig] = field(default_factory=list)
    # SPEC-16 R13 (full): user-provided custom-type aliases. Keys are the
    # project-local type names (e.g. ``JsonBlob``, ``PasswordHash``) that
    # appear in migrations/models; values are the canonical type strings
    # Gravitar should treat them as (e.g. ``json``, ``varchar(60)``).
    # Unmapped custom types stay as ``unknown:X`` and produce a note.
    custom_types: dict[str, str] = field(default_factory=dict)

    def primary_tree(self) -> TreeConfig:
        """The top-level tree, mapped onto the common TreeConfig shape so
        the CLI can treat ``[primary] + extra_trees`` uniformly."""
        return TreeConfig(
            dsl=self.dsl,
            model_paths=self.model_paths,
            base_classes=self.base_classes,
            versions_dir=self.versions_dir,
            django_app_path=self.django_app_path,
            name=None,
        )

    def all_trees(self) -> list[TreeConfig]:
        return [self.primary_tree(), *self.extra_trees]


def resolve_config(repo_path: Path, config_override: Path | None) -> ResolvedConfig:
    """Resolve model import paths and the migration directory, applying
    .gravitar.yml overrides if present. Raises ConfigError (exit code 2) if neither
    the config file nor the default repository conventions resolve successfully,
    or if the config file itself is malformed or contains unknown keys.

    A `django:` section switches the whole repository to Django mode (Section 4);
    absent, Gravitar assumes Alembic, unchanged from pre-Milestone-10 behavior.
    """
    config_path = config_override or (repo_path / ".gravitar.yml")
    user_config: dict[str, Any] = {}
    if config_path.exists():
        try:
            user_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{config_path}: malformed YAML: {e}") from e
        _validate_config_keys(user_config)

    checks_section = user_config.get("checks") or {}
    exclude_section = user_config.get("exclude") or {}
    output_section = user_config.get("output") or {}
    checks = {**_DEFAULT_CHECKS, **checks_section}
    exclude_tables = exclude_section.get("tables") or []
    exclude_columns = exclude_section.get("columns") or []
    output_format = output_section.get("format") or "text"
    quiet = bool(output_section.get("quiet", False))

    extra_trees = _resolve_extra_trees(repo_path, user_config.get("trees") or [])
    custom_types = _resolve_custom_types(user_config.get("types") or {})

    django_section = user_config.get("django")
    if django_section:
        app_path = django_section.get("app_path")
        if not app_path:
            raise ConfigError("django.app_path is required when a 'django:' section is present")
        return ResolvedConfig(
            dsl="django",
            model_paths=[],
            base_classes=None,
            versions_dir=None,
            django_app_path=app_path,
            checks=checks,
            exclude_tables=exclude_tables,
            exclude_columns=exclude_columns,
            output_format=output_format,
            quiet=quiet,
            extra_trees=extra_trees,
            custom_types=custom_types,
        )

    models_section = user_config.get("models") or {}
    alembic_section = user_config.get("alembic") or {}

    model_paths = models_section.get("paths") or _discover_default_model_paths()
    base_classes = models_section.get("base_classes") or None

    versions_dir = _resolve_versions_dir(repo_path, alembic_section)

    return ResolvedConfig(
        dsl="alembic",
        model_paths=model_paths,
        base_classes=base_classes,
        versions_dir=versions_dir,
        django_app_path=None,
        checks=checks,
        exclude_tables=exclude_tables,
        exclude_columns=exclude_columns,
        output_format=output_format,
        quiet=quiet,
        extra_trees=extra_trees,
        custom_types=custom_types,
    )


def _resolve_custom_types(types_section: dict[str, Any]) -> dict[str, str]:
    """SPEC-16 R13 (full): parse the top-level ``types:`` mapping into a
    plain ``dict[str, str]``. Keys are project-local type names as they
    appear in source (e.g. ``JsonBlob``); values are the canonical type
    strings Gravitar normalizes to (e.g. ``json``). Non-string values
    are rejected rather than silently stringified."""
    if not isinstance(types_section, dict):
        raise ConfigError("'types:' must be a mapping of type-name → canonical-type")
    resolved: dict[str, str] = {}
    for name, value in types_section.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ConfigError(f"'types:' entries must be string → string, got {name!r}: {value!r}")
        resolved[name] = value
    return resolved


def _resolve_versions_dir(repo_path: Path, alembic_section: dict[str, Any]) -> Path:
    """SPEC-16 R10 (full): three ways to resolve the versions dir, in order:

    1. ``alembic.versions_dir`` set — no-ini mode (Flask-Migrate style).
       The user points directly at the versions directory; no ini is read.
    2. ``alembic.script_location`` set — override the ini and resolve
       ``<script_location>/versions`` relative to the repo root.
    3. Default — read ``alembic.config`` (default ``alembic.ini``) and
       derive the script_location + versions subdir from it.
    """
    versions_dir_override = alembic_section.get("versions_dir")
    if versions_dir_override:
        versions_dir = repo_path / str(versions_dir_override)
        if not versions_dir.is_dir():
            raise ConfigError(
                f"{versions_dir} not found; check alembic.versions_dir in .gravitar.yml"
            )
        return versions_dir

    return _discover_versions_dir(
        repo_path,
        alembic_config=alembic_section.get("config", "alembic.ini"),
        script_location_override=alembic_section.get("script_location"),
    )


def _resolve_extra_trees(repo_path: Path, trees_list: list[Any]) -> list[TreeConfig]:
    """SPEC-16 R10 (full): parse the top-level ``trees:`` list into
    TreeConfig instances. Each entry has the same shape as a top-level
    config: ``models``/``alembic`` for Alembic, or ``django`` for Django,
    plus an optional ``name`` for output. Unknown keys inside a tree
    entry are rejected the same way the top level rejects unknown keys.
    """
    if not isinstance(trees_list, list):
        raise ConfigError("'trees:' must be a list of tree entries, see docs/configuration.md")

    resolved: list[TreeConfig] = []
    for idx, entry in enumerate(trees_list):
        if not isinstance(entry, dict):
            raise ConfigError(f"trees[{idx}]: each tree entry must be a mapping")
        unknown = sorted(set(entry) - _KNOWN_TREE_KEYS)
        if unknown:
            raise ConfigError(
                f"trees[{idx}]: unknown key(s) {', '.join(unknown)}; "
                "allowed: name, models, alembic, django"
            )
        name = entry.get("name")
        django_section = entry.get("django")
        if django_section:
            app_path = django_section.get("app_path")
            if not app_path:
                raise ConfigError(
                    f"trees[{idx}].django.app_path is required when 'django:' is present"
                )
            resolved.append(
                TreeConfig(
                    dsl="django",
                    model_paths=[],
                    base_classes=None,
                    versions_dir=None,
                    django_app_path=app_path,
                    name=name,
                )
            )
            continue
        models_section = entry.get("models") or {}
        alembic_section = entry.get("alembic") or {}
        model_paths = models_section.get("paths")
        if not model_paths:
            raise ConfigError(f"trees[{idx}].models.paths is required for an Alembic tree entry")
        base_classes = models_section.get("base_classes") or None
        try:
            versions_dir = _resolve_versions_dir(repo_path, alembic_section)
        except ConfigError as e:
            raise ConfigError(f"trees[{idx}]: {e}") from e
        resolved.append(
            TreeConfig(
                dsl="alembic",
                model_paths=list(model_paths),
                base_classes=base_classes,
                versions_dir=versions_dir,
                django_app_path=None,
                name=name,
            )
        )
    return resolved


# Top-level keys whose nested keys are USER-defined (not a fixed schema) and
# therefore should not be validated against a known-keys set — ``types:``
# names arbitrary project-local type names, ``trees:`` is a list.
_OPEN_SCHEMA_KEYS: frozenset[str] = frozenset({"types", "trees"})


def _validate_config_keys(user_config: dict[str, Any]) -> None:
    unknown: list[str] = []
    for top_key, value in user_config.items():
        if top_key not in _KNOWN_KEYS:
            unknown.append(top_key)
            continue
        if top_key in _OPEN_SCHEMA_KEYS:
            continue
        if isinstance(value, dict):
            for nested_key in value:
                if nested_key not in _KNOWN_KEYS[top_key]:
                    unknown.append(f"{top_key}.{nested_key}")
    if unknown:
        raise ConfigError(
            f"unknown .gravitar.yml key(s): {', '.join(sorted(unknown))}; "
            "see docs/configuration.md for the full schema"
        )


def _discover_default_model_paths() -> list[str]:
    for candidate in _DEFAULT_MODEL_PACKAGES:
        try:
            importlib.import_module(candidate)
        except ImportError:
            continue
        else:
            return [candidate]
    raise ConfigError(
        f"could not import either of {_DEFAULT_MODEL_PACKAGES!r}; create a "
        ".gravitar.yml with models.paths set to your models package"
    )


def _discover_versions_dir(
    repo_path: Path, alembic_config: str, script_location_override: str | None
) -> Path:
    """SPEC-16 Amendment R10 (subset): resolve the versions directory, with
    support for ``%(here)s`` interpolation and non-root ``alembic.ini``.

    - ``%(here)s`` expands to the directory containing the ``alembic.ini``
      file (readiness doc S6: default ``alembic init`` output on 1.20 uses
      ``script_location = %(here)s/alembic`` which pre-R10 crashed the run).
    - A path that resolves outside the repo but exists is accepted (user may
      have the ini pointing to a sibling package); the versions dir is still
      the ``versions/`` subdir of the resolved script_location.
    """
    if script_location_override:
        versions_dir = repo_path / script_location_override / "versions"
    else:
        alembic_config_path = repo_path / alembic_config
        if not alembic_config_path.exists():
            raise ConfigError(
                f"{alembic_config_path} not found; create a .gravitar.yml with "
                "alembic.config or alembic.script_location set"
            )

        # ConfigParser's %(name)s interpolation substitutes DEFAULT values; we
        # need %(here)s to resolve to the ini's directory. Register it as a
        # default *before* reading so interpolation lands on the real path.
        parser = configparser.ConfigParser(
            defaults={"here": str(alembic_config_path.parent.resolve())}
        )
        parser.read(alembic_config_path)
        script_location = parser.get("alembic", "script_location", fallback="alembic")
        script_path = Path(script_location)
        if not script_path.is_absolute():
            # Relative script_location is relative to the ini's directory,
            # matching Alembic's own behavior — not to the repo root.
            script_path = alembic_config_path.parent / script_path
        versions_dir = script_path / "versions"

    if not versions_dir.is_dir():
        # A missing versions/ directory would otherwise parse as an empty migration
        # history (an empty glob, not an error) — every model table would then look
        # like drift instead of a misconfiguration. Fail closed instead.
        raise ConfigError(f"{versions_dir} not found; check alembic.script_location")

    return versions_dir


DEFAULT_CONFIG_YAML = """\
# .gravitar.yml — all fields optional; shown values are defaults

models:
  # Python import path(s) to package(s) containing SQLAlchemy declarative models
  paths:
    - "models"
    - "app.models"
  # SQLAlchemy declarative_base variable name(s) to inspect
  # If unspecified, Gravitar discovers all DeclarativeMeta subclasses
  base_classes: []

alembic:
  # Path to alembic.ini, relative to repo root
  config: "alembic.ini"
  # Override the script_location from alembic.ini if set
  script_location: null
  # No-ini mode: point directly at the versions directory. Overrides both
  # 'config' and 'script_location'. Useful for Flask-Migrate projects that
  # ship no alembic.ini.
  versions_dir: null

checks:
  # Enable/disable individual drift categories (all enabled by default)
  missing_table: true
  missing_column: true
  type_mismatch: true
  nullability_mismatch: true
  orphaned_column: true
  orphaned_reference: true
  broken_chain: true

exclude:
  # Tables to skip entirely (glob patterns)
  tables: []
  # Columns to skip (format: "table.column", glob patterns)
  columns: []

output:
  # Default output format: "text", "json", or "sarif"
  format: "text"
  # Suppress the summary line at the end of text output
  quiet: false

# Optional ProjectUnit multi-tree analysis (SPEC-16 R10). The top-level
# models/alembic/django above still runs as the primary tree; each entry
# below is analyzed in addition and findings are aggregated. Useful for
# repos such as CTFd (core + plugins) or Django monorepos (one Django
# app per tree).
# trees:
#   - name: plugin_scoring
#     models:
#       paths: ["plugins.scoring.models"]
#     alembic:
#       versions_dir: "plugins/scoring/migrations/versions"

# Optional custom-type aliases (SPEC-16 R13). Pin a project-local type
# name (TypeDecorator class or AST reference) to its canonical type
# string so unmapped custom types stop reading as type_mismatch.
# types:
#   JsonBlob: json
#   PasswordHash: "varchar(60)"
"""
