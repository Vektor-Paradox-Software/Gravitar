"""Oracle runners for the benchmark harness. SPEC-16 R0.

An oracle produces the ground-truth list of drifts for one project.
Each runner is a plain function ``load_oracle(project) -> list[OracleFinding]``.

v1 ships the ``static`` runner only; the ``postgres`` runner is a
scaffold placeholder — it fails loudly if called so the acceptance
test ``run.py`` never silently falls through to an empty oracle.
"""

from __future__ import annotations

from benchmarks.schema import OracleFinding, Project

from .static import load_static_oracle

__all__ = ["OracleFinding", "load_oracle"]


def load_oracle(project: Project) -> list[OracleFinding]:
    """Dispatch to the right runner for this project. Raises
    NotImplementedError for runners not yet implemented."""
    if project.oracle_runner == "static":
        if project.oracle_path is None:
            raise ValueError(f"{project.name}: oracle_runner='static' requires oracle_path")
        return load_static_oracle(project.oracle_path)
    if project.oracle_runner == "postgres":
        from .alembic_oracle import load_postgres_oracle

        return load_postgres_oracle(project)
    raise ValueError(f"{project.name}: unknown oracle_runner {project.oracle_runner!r}")
