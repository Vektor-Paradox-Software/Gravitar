import sys
from pathlib import Path

import pytest

from gravitar.config import DEFAULT_CONFIG_YAML, resolve_config
from gravitar.errors import ConfigError

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).parent.parent


def test_gravitar_yml_example_matches_default_config_yaml() -> None:
    """The repo's documented example must stay byte-identical to what `gravitar
    init` actually writes, or the docs and the tool will drift apart."""
    example = (REPO_ROOT / ".gravitar.yml.example").read_text(encoding="utf-8")

    assert example == DEFAULT_CONFIG_YAML


def _clear_model_module_cache() -> None:
    for name in ("models", "app", "app.models", "custom_models"):
        sys.modules.pop(name, None)


def test_default_discovery_finds_models_and_versions_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES / "clean"))
    _clear_model_module_cache()

    resolved = resolve_config(FIXTURES / "clean", None)

    assert resolved.model_paths == ["models"]
    assert resolved.base_classes is None
    assert resolved.versions_dir == FIXTURES / "clean" / "versions"


def test_missing_alembic_ini_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    with pytest.raises(ConfigError, match="alembic.ini"):
        resolve_config(tmp_path, None)


def test_missing_versions_dir_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    with pytest.raises(ConfigError, match="versions"):
        resolve_config(tmp_path, None)


def test_gravitar_yml_overrides_model_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "custom_models").mkdir()
    (tmp_path / "custom_models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    (tmp_path / ".gravitar.yml").write_text('models:\n  paths:\n    - "custom_models"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, None)

    assert resolved.model_paths == ["custom_models"]


def test_gravitar_yml_overrides_script_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alt_location" / "versions").mkdir(parents=True)
    (tmp_path / ".gravitar.yml").write_text('alembic:\n  script_location: "alt_location"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, None)

    assert resolved.versions_dir == tmp_path / "alt_location" / "versions"


def test_config_override_path_used_instead_of_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "versions").mkdir()
    custom_config = tmp_path / "custom.yml"
    custom_config.write_text('alembic:\n  script_location: "."\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, custom_config)

    assert resolved.versions_dir == tmp_path / "versions"


def test_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / ".gravitar.yml").write_text("models: [unterminated")

    with pytest.raises(ConfigError, match="malformed YAML"):
        resolve_config(tmp_path, None)


def test_unimportable_default_models_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()
    _clear_model_module_cache()

    with pytest.raises(ConfigError, match="models"):
        resolve_config(tmp_path, None)


# --- unknown-key validation (Section 5) ---


def _minimal_repo(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "__init__.py").write_text("")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (tmp_path / "versions").mkdir()


def test_unknown_top_level_key_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _minimal_repo(tmp_path)
    (tmp_path / ".gravitar.yml").write_text("modles:\n  paths: []\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    with pytest.raises(ConfigError, match="modles"):
        resolve_config(tmp_path, None)


def test_unknown_nested_key_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _minimal_repo(tmp_path)
    (tmp_path / ".gravitar.yml").write_text('models:\n  pathz:\n    - "models"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    with pytest.raises(ConfigError, match="models.pathz"):
        resolve_config(tmp_path, None)


def test_unknown_keys_lists_every_offender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_repo(tmp_path)
    (tmp_path / ".gravitar.yml").write_text("bogus_top: 1\nmodels:\n  bogus_nested: 2\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    with pytest.raises(ConfigError) as exc_info:
        resolve_config(tmp_path, None)

    message = str(exc_info.value)
    assert "bogus_top" in message
    assert "models.bogus_nested" in message


# --- checks toggles and exclude patterns ---


def test_checks_defaults_all_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_repo(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, None)

    assert all(resolved.checks.values())
    assert resolved.exclude_tables == []
    assert resolved.exclude_columns == []
    assert resolved.output_format == "text"
    assert resolved.quiet is False


def test_output_section_resolved_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _minimal_repo(tmp_path)
    (tmp_path / ".gravitar.yml").write_text('output:\n  format: "json"\n  quiet: true\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, None)

    assert resolved.output_format == "json"
    assert resolved.quiet is True


def test_checks_yaml_overrides_merge_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _minimal_repo(tmp_path)
    (tmp_path / ".gravitar.yml").write_text("checks:\n  missing_table: false\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, None)

    assert resolved.checks["missing_table"] is False
    assert resolved.checks["missing_column"] is True


def test_exclude_patterns_resolved_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _minimal_repo(tmp_path)
    (tmp_path / ".gravitar.yml").write_text(
        'exclude:\n  tables:\n    - "tmp_*"\n  columns:\n    - "*.legacy_*"\n'
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_model_module_cache()

    resolved = resolve_config(tmp_path, None)

    assert resolved.exclude_tables == ["tmp_*"]
    assert resolved.exclude_columns == ["*.legacy_*"]


# --- django: section ---
#
# resolve_config never imports the 'django' package itself (only django_models.py
# / django_parser.py do, lazily, once a Django repo is actually being analyzed —
# see cli.py), so these run safely with no Django install and no app registry.


def test_django_section_switches_dsl(tmp_path: Path) -> None:
    (tmp_path / ".gravitar.yml").write_text('django:\n  app_path: "blog"\n')

    resolved = resolve_config(tmp_path, None)

    assert resolved.dsl == "django"
    assert resolved.django_app_path == "blog"
    assert resolved.versions_dir is None
    assert resolved.model_paths == []


def test_django_section_without_app_path_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / ".gravitar.yml").write_text("django:\n  app_path: null\n")

    with pytest.raises(ConfigError, match="django.app_path"):
        resolve_config(tmp_path, None)


def test_absent_django_section_keeps_alembic_dsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(FIXTURES / "clean"))
    _clear_model_module_cache()

    resolved = resolve_config(FIXTURES / "clean", None)

    assert resolved.dsl == "alembic"
    assert resolved.django_app_path is None
