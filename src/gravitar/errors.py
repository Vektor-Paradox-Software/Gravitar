"""Exit codes and exception types. The exit-code set is a stable public contract."""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_DRIFT = 1
EXIT_CONFIG_ERROR = 2
EXIT_ANALYSIS_ERROR = 3
EXIT_WARNINGS_ONLY = 10
# SPEC-16 Amendment R7 / T5: "Clean means clean-and-covered." Exit 0 only
# when coverage meets the floor; below floor exits a distinct "incomplete"
# code so a green check never means "I saw nothing".
EXIT_INCOMPLETE = 11


class GravitarError(Exception):
    """Base class for errors that map to a specific process exit code."""

    exit_code: int = EXIT_ANALYSIS_ERROR


class ConfigError(GravitarError):
    """Raised for .gravitar.yml problems, missing alembic.ini, or unimportable models."""

    exit_code = EXIT_CONFIG_ERROR


class AnalysisError(GravitarError):
    """Raised when Gravitar cannot complete analysis (unparseable migration, etc.)."""

    exit_code = EXIT_ANALYSIS_ERROR
