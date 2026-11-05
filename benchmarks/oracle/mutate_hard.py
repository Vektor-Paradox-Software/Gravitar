"""SPEC-18 §6.2 hard-seed mutations.

Where mainline seeds drift as a straightforward model edit, hard
seeds plant drift *and* rewrite one of the project's migrations so
that resolving the drift requires surviving a hard code path:

- `batch_alter_table`: a resolved `op.add_column(...)` becomes
  `with op.batch_alter_table(T) as b: b.add_column(...)` (SPEC-16 R1).
- `loop`: `op.create_table("x", ...)` becomes
  `for name in ("x",): op.create_table(name, ...)` (SPEC-16 R4 subset).
- `helper`: `op.add_column(T, Column(...))` becomes a module-level
  helper `def _add(op, t): op.add_column(t, ...)` plus `_add(op, T)`.
- `RunSQL`: the Alembic op becomes an `op.execute("ALTER TABLE ...")`
  so pattern matching has to recover the drift (SPEC-16 R12).

The matching model-side drift is planted the same way the mainline
mutations plant theirs — same `apply_mutation` signatures. The hard-
seed layer wraps the applier and ALSO rewrites one migration file.

The bucket's recall number lives on `summary.json.roll_up.hard_recall`
(SPEC §8). A regression on the hard code paths shows up here first
because mainline seeds would still be caught by Gravitar's "basic"
reader.
"""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HardMutationShape = Literal["batch", "loop", "helper", "run_sql"]

HARD_SHAPES: tuple[HardMutationShape, ...] = (
    "batch",
    "loop",
    "helper",
    "run_sql",
)


@dataclass(frozen=True)
class HardMutationPlan:
    """A hard-seed mutation = where to plant the drift (which migration
    file + line) + what shape the rewrite takes. The model-side drift
    is a sibling ``Mutation`` from ``mutate.py``.
    """

    shape: HardMutationShape
    migration_file: Path
    original_line: int  # 1-indexed line of the op being wrapped
    original_table: str  # table the op mentions, so the model-side drift matches


@dataclass(frozen=True)
class MigrationOp:
    """One recognizable Alembic op the hard-seed scanner found,
    reduced to the identity bits the rewriter needs."""

    kind: str  # "create_table" | "add_column" | "drop_column" | ...
    table: str
    line: int
    end_line: int  # inclusive


def scan_migration_ops(path: Path) -> list[MigrationOp]:
    """SPEC-18 §6.2 preface: a hard seed needs an existing resolved
    op to rewrite. This scanner finds the minimal set — ``op.<kind>(
    "<table>", ...)`` calls at the top level of ``upgrade()``, with
    both kind and table as literals. More complex shapes aren't
    rewritable because the rewriter would have to reason about
    identifiers it can't fold."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []

    upgrade = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )
    if upgrade is None:
        return []

    out: list[MigrationOp] = []
    for stmt in upgrade.body:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        func = call.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "op":
            continue
        if not call.args:
            continue
        first = call.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        out.append(
            MigrationOp(
                kind=func.attr,
                table=first.value,
                line=stmt.lineno,
                end_line=stmt.end_lineno or stmt.lineno,
            )
        )
    return out


def plan_hard_mutation(
    rng: random.Random, ops: list[MigrationOp], migration_file: Path
) -> HardMutationPlan | None:
    """Pick a shape + an existing op to rewrap. Returns None if the
    migration has no ops the rewriter can target."""
    if not ops:
        return None
    shape = rng.choice(HARD_SHAPES)
    target = rng.choice(ops)
    return HardMutationPlan(
        shape=shape,
        migration_file=migration_file,
        original_line=target.line,
        original_table=target.table,
    )


class HardApplyError(RuntimeError):
    """Raised when the hard-seed rewriter can't realize a plan.
    SPEC §2.7 — the harness turns this into an ``unrunnable`` trial
    rather than a silent skip."""


def apply_hard_mutation(plan: HardMutationPlan) -> None:
    """Rewrite ``plan.migration_file`` in-place, wrapping the op at
    ``plan.original_line`` in the planned hard-case shape. The file
    must be pre-copied by the caller — the hard-seed mutator never
    touches the committed source."""
    text = plan.migration_file.read_text(encoding="utf-8")
    lines = text.split("\n")
    idx = plan.original_line - 1
    if idx < 0 or idx >= len(lines):
        raise HardApplyError(f"{plan.migration_file}: line {plan.original_line} out of range")
    original = lines[idx]
    indent = original[: len(original) - len(original.lstrip())]

    if plan.shape == "batch":
        lines[idx] = _wrap_batch(original, indent, plan.original_table)
    elif plan.shape == "loop":
        lines[idx] = _wrap_loop(original, indent, plan.original_table)
    elif plan.shape == "helper":
        lines[idx] = _wrap_helper_call(original, indent)
        _append_helper_def(lines, plan.original_table)
    elif plan.shape == "run_sql":
        lines[idx] = _wrap_run_sql(original, indent)
    else:  # pragma: no cover
        raise HardApplyError(f"unknown shape {plan.shape!r}")

    plan.migration_file.write_text("\n".join(lines), encoding="utf-8")


# --- Shape rewriters ---


def _wrap_batch(line: str, indent: str, table: str) -> str:
    """``op.add_column(T, Column(...))`` →
    ``with op.batch_alter_table(T) as b: b.add_column(Column(...))``.

    One-line form keeps the file close to its original shape; the
    hard-seed test cares about the walker's recognition path, not
    about pretty formatting.
    """
    body = line.strip()
    if body.startswith("op.add_column("):
        inner = body[len("op.add_column(") : -1]
        parts = _split_top_level_args(inner)
        if len(parts) >= 2:
            col_expr = ",".join(parts[1:]).strip()
            return f"{indent}with op.batch_alter_table({table!r}) as b: b.add_column({col_expr})"
    # Fallback: wrap the whole op body inside a batch block even if
    # we can't precisely split the args. The walker won't resolve
    # the inner call either way, which is exactly the kind of regression
    # the hard-seed bucket is supposed to catch.
    return f"{indent}with op.batch_alter_table({table!r}) as b: {body}"


def _wrap_loop(line: str, indent: str, table: str) -> str:
    body = line.strip()
    body_with_var = body.replace(f'"{table}"', "name", 1).replace(f"'{table}'", "name", 1)
    return f"{indent}for name in ({table!r},): {body_with_var}"


def _wrap_helper_call(line: str, indent: str) -> str:
    """Replace the op line with a call to a module-level helper that
    will be appended at EOF. The helper is a one-shot: ``_hard_seed_
    helper(op)`` — the walker's external-call detector should emit
    an ``external_call`` note instead of resolving the inner op."""
    return f"{indent}_hard_seed_helper(op)"


def _append_helper_def(lines: list[str], table: str) -> None:
    """Append the helper definition the ``_wrap_helper_call`` call
    references. Lives at module scope so R4's in-file helper
    inlining would see it (that's the point — if inlining works,
    recall stays high; if it breaks, recall drops first on this bucket)."""
    lines.append("")
    lines.append("def _hard_seed_helper(op):")
    lines.append(f'    op.add_column({table!r}, sa.Column("_hard_seed", sa.Integer()))')


def _wrap_run_sql(line: str, indent: str) -> str:
    """Replace a resolved op with an ``op.execute("ALTER TABLE
    ...")`` so pattern matching (R12) has to recover it."""
    body = line.strip()
    if body.startswith("op.add_column("):
        return f'{indent}op.execute("ALTER TABLE widgets ADD COLUMN _hard_seed INTEGER")'
    return f'{indent}op.execute("-- hard seed: {body}")'


def _split_top_level_args(src: str) -> list[str]:
    """Split a comma-separated arg string at the top level, honoring
    nested parens/brackets. Used by ``_wrap_batch`` to tease apart
    ``T, Column(...)`` safely."""
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in src:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [p.strip() for p in out]
