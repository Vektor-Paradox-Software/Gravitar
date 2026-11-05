"""Postgres-backed Alembic autogenerate oracle. SPEC-16 R0 (scaffold).

v1 ships as a stub: calling it raises ``NotImplementedError`` with a
pointer to the SPEC section describing the procedure. v2 will:

1. Provision a fresh Postgres database (benchmark-only; Gravitar never
   connects itself).
2. Run the project's migrations to head using
   ``project.migration_runner`` (default: ``alembic upgrade head``).
3. Call ``alembic.autogenerate.compare_metadata`` with
   ``compare_type=True, compare_server_default=False``.
4. Keep only diffs in categories Gravitar supports and map them per
   the SPEC-16 §Alembic-diff-to-category table.

Deferred because provisioning Postgres is sysadmin work and the static
YAML runner already covers the self-test corpus. Static oracle files
can be the output of a hand-run of this procedure, re-committed and
replayed deterministically.
"""

from __future__ import annotations

from benchmarks.schema import OracleFinding, Project


def load_postgres_oracle(project: Project) -> list[OracleFinding]:  # pragma: no cover
    raise NotImplementedError(
        f"{project.name}: oracle_runner='postgres' is not implemented in v1. "
        "Use oracle_runner='static' with a hand-authored oracle.yml for now; "
        "see benchmarks/README.md."
    )
