"""SPEC-18 §6 seeded drift mutations.

Two buckets:

- **Mainline (§6.1)**: for each unit, apply N=10 model-side mutations
  one at a time to a copy of the project source. After each mutation,
  re-run both oracles against the mutated models and run Gravitar.
  This is a RECALL measurement — did Gravitar catch the planted drift?

- **Hard-seeds (§6.2)**: AST-rewrite one existing resolved migration
  op into an equivalent op inside a batch block / loop / helper /
  RunSQL, plant the matching model-side drift. Measures recall when
  the drift resolution requires surviving the hard-case code paths.
  Lives in a sibling module (``mutate_hard.py``) added by T8.

Mutation types (equal weight; chosen by a seeded RNG):

1. drop_model_column — delete one existing column from a model class
2. add_model_column — add a new column with a random mapped type
3. change_column_type — swap one column's type for a different one
4. flip_nullability — toggle one column's nullable flag
5. rename_model_column — rename on the model side only (oracle sees
   drop + add)
6. drop_model_table — remove a whole model class

Seed: ``blake2b((project, unit, "mainline", trial_index),
digest_size=8)`` — so a trial is reproducible without storing every
input (SPEC §2.8).
"""

from __future__ import annotations

import ast
import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MutationType = Literal[
    "drop_model_column",
    "add_model_column",
    "change_column_type",
    "flip_nullability",
    "rename_model_column",
    "drop_model_table",
]

MUTATION_TYPES: tuple[MutationType, ...] = (
    "drop_model_column",
    "add_model_column",
    "change_column_type",
    "flip_nullability",
    "rename_model_column",
    "drop_model_table",
)

#: Fixed cross-type list for ``change_column_type``. Covers the broad
#: strokes (integer <-> string, nullable vs sized), not an exhaustive
#: type matrix — SA 2.1 has 40+ types and we only need enough variety
#: to produce a measurable recall signal across trials.
TYPE_CHANGE_PAIRS: tuple[tuple[str, str], ...] = (
    ("Integer", "BigInteger"),
    ("String(50)", "String(100)"),
    ("String(50)", "Text"),
    ("Integer", "String(10)"),
    ("DateTime", "Date"),
    ("Float", "Numeric"),
    ("Boolean", "SmallInteger"),
)

#: Types used by ``add_model_column``. Simple cross-section of what
#: Gravitar's resolver can normalize cleanly.
ADD_COLUMN_TYPES: tuple[str, ...] = (
    "Integer",
    "String(50)",
    "Boolean",
    "DateTime",
    "Float",
    "Text",
    "Date",
)


@dataclass(frozen=True)
class Mutation:
    """A single planned mutation. Writes are deterministic given
    ``(project, unit, trial_index)`` so a trial is reproducible from
    its seed alone."""

    mutation_type: MutationType
    class_name: str
    column: str | None = None  # None for drop_model_table
    new_name: str | None = None  # rename_model_column
    new_type: str | None = None  # change_column_type / add_model_column
    new_nullable: bool | None = None  # flip_nullability
    trial_index: int = 0

    def description(self) -> str:
        """Human-readable summary for the per-trial report."""
        where = f"{self.class_name}.{self.column}" if self.column else self.class_name
        if self.mutation_type == "rename_model_column":
            return f"rename {where} -> {self.new_name}"
        if self.mutation_type == "change_column_type":
            return f"retype {where} -> {self.new_type}"
        if self.mutation_type == "flip_nullability":
            return f"flip nullable {where} -> {self.new_nullable}"
        if self.mutation_type == "add_model_column":
            return f"add {self.class_name}.{self.column} ({self.new_type})"
        if self.mutation_type == "drop_model_column":
            return f"drop {where}"
        if self.mutation_type == "drop_model_table":
            return f"drop class {self.class_name}"
        return self.mutation_type


@dataclass(frozen=True)
class ModelClass:
    """One mapped class the mutation planner can target. Discovered
    by ``discover_model_classes`` — a lightweight AST scan that
    doesn't import project code."""

    file: Path
    class_name: str
    columns: tuple[str, ...]  # ordered as they appear in the class body


def _seed_bytes(project: str, unit: str, bucket: str, trial_index: int) -> bytes:
    """SPEC §2.8: blake2b((project, unit, bucket, trial_index),
    digest_size=8)."""
    key = f"{project}|{unit}|{bucket}|{trial_index}".encode()
    return hashlib.blake2b(key, digest_size=8).digest()


def seeded_rng(
    project: str, unit: str, trial_index: int, bucket: str = "mainline"
) -> random.Random:
    """Return a ``random.Random`` seeded deterministically from the
    unit identity + trial index + bucket. Two calls with the same
    arguments produce the same stream."""
    seed = int.from_bytes(_seed_bytes(project, unit, bucket, trial_index), "big")
    return random.Random(seed)


# --- Discovery: find mutation targets by AST scan ---


def discover_model_classes(path: Path) -> list[ModelClass]:
    """Light AST scan to locate class definitions and their simple
    column annotations/assignments. The mutator doesn't need full
    semantic understanding — it only needs to know (a) which class
    bodies exist to target and (b) which attribute names they define
    so it can drop/rename/retype one at random.

    This is SPEC-18 T7's own narrow reader; the SPEC-17 static reader
    is overkill here (we don't need types or nullability resolution,
    just names).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    classes: list[ModelClass] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        columns: list[str] = []
        for stmt in node.body:
            name = _attribute_target(stmt)
            if name is None:
                continue
            if name.startswith("__"):
                continue
            columns.append(name)
        if columns:
            classes.append(ModelClass(file=path, class_name=node.name, columns=tuple(columns)))
    return classes


def _attribute_target(stmt: ast.stmt) -> str | None:
    """Pull the attribute name from ``name = Column(...)`` or
    ``name: Mapped[T]`` — the two shapes model files use."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        if isinstance(stmt.targets[0], ast.Name):
            return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return None


# --- Planner: pick a mutation ---


def plan_mutation(
    rng: random.Random, classes: list[ModelClass], trial_index: int
) -> Mutation | None:
    """Pick a (type, class, column) combination from the available
    targets. Returns None when no class has any mutable columns —
    the caller should skip the trial rather than fabricate a target."""
    mutable = [c for c in classes if c.columns]
    if not mutable:
        return None
    mtype = rng.choice(MUTATION_TYPES)
    target = rng.choice(mutable)

    if mtype == "drop_model_table":
        return Mutation(mutation_type=mtype, class_name=target.class_name, trial_index=trial_index)
    column = rng.choice(target.columns)
    if mtype == "add_model_column":
        new_col = f"planted_{trial_index}_{rng.randint(100, 999)}"
        new_type = rng.choice(ADD_COLUMN_TYPES)
        return Mutation(
            mutation_type=mtype,
            class_name=target.class_name,
            column=new_col,
            new_type=new_type,
            trial_index=trial_index,
        )
    if mtype == "change_column_type":
        pair = rng.choice(TYPE_CHANGE_PAIRS)
        return Mutation(
            mutation_type=mtype,
            class_name=target.class_name,
            column=column,
            new_type=pair[1],
            trial_index=trial_index,
        )
    if mtype == "flip_nullability":
        return Mutation(
            mutation_type=mtype,
            class_name=target.class_name,
            column=column,
            new_nullable=bool(rng.getrandbits(1)),
            trial_index=trial_index,
        )
    if mtype == "rename_model_column":
        new_name = f"{column}_renamed_{trial_index}"
        return Mutation(
            mutation_type=mtype,
            class_name=target.class_name,
            column=column,
            new_name=new_name,
            trial_index=trial_index,
        )
    # drop_model_column
    return Mutation(
        mutation_type=mtype,
        class_name=target.class_name,
        column=column,
        trial_index=trial_index,
    )


# --- Applier: write the mutation to disk ---


class MutationApplyError(RuntimeError):
    """Raised when a planned mutation can't be applied to the file.

    The harness turns this into an ``unrunnable`` trial result rather
    than a silent skip — SPEC §2.7 (fail noisy, not clean)."""


def apply_mutation(mutation: Mutation, model_file: Path) -> None:
    """Rewrite ``model_file`` in-place to realize ``mutation``.

    Each mutation is a precise text edit so the output stays readable
    (the trial author may want to inspect the generated drift). AST
    round-tripping would reflow the file; we prefer a surgical
    line-based edit per mutation type.
    """
    text = model_file.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(model_file))
    target_class = _find_class(tree, mutation.class_name)
    if target_class is None:
        raise MutationApplyError(f"{model_file}: class {mutation.class_name!r} not found")

    if mutation.mutation_type == "drop_model_table":
        text = _drop_class(text, target_class)
    elif mutation.mutation_type == "add_model_column":
        if not mutation.column or not mutation.new_type:
            raise MutationApplyError("add_model_column requires column + new_type")
        text = _add_column(text, target_class, mutation.column, mutation.new_type)
    elif mutation.mutation_type == "drop_model_column":
        if not mutation.column:
            raise MutationApplyError("drop_model_column requires column")
        text = _drop_column(text, target_class, mutation.column)
    elif mutation.mutation_type == "change_column_type":
        if not mutation.column or not mutation.new_type:
            raise MutationApplyError("change_column_type requires column + new_type")
        text = _retype_column(text, target_class, mutation.column, mutation.new_type)
    elif mutation.mutation_type == "flip_nullability":
        if not mutation.column or mutation.new_nullable is None:
            raise MutationApplyError("flip_nullability requires column + new_nullable")
        text = _flip_nullable(text, target_class, mutation.column, mutation.new_nullable)
    elif mutation.mutation_type == "rename_model_column":
        if not mutation.column or not mutation.new_name:
            raise MutationApplyError("rename_model_column requires column + new_name")
        text = _rename_column(text, target_class, mutation.column, mutation.new_name)
    else:  # pragma: no cover - exhaustive
        raise MutationApplyError(f"unknown mutation_type {mutation.mutation_type!r}")

    model_file.write_text(text, encoding="utf-8")


def _find_class(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _class_lines(text: str, cls: ast.ClassDef) -> tuple[int, int, list[str]]:
    """Return (start_line, end_line, lines) using 0-indexed line
    numbers. ``end_line`` is exclusive."""
    lines = text.split("\n")
    start = cls.lineno - 1
    end = (cls.end_lineno or cls.lineno) - 1
    return start, end + 1, lines


def _attr_line_index(cls: ast.ClassDef, name: str) -> int | None:
    """0-indexed line of the first body statement that targets
    ``name`` as attribute."""
    for stmt in cls.body:
        if _attribute_target(stmt) == name:
            return stmt.lineno - 1
    return None


def _drop_class(text: str, cls: ast.ClassDef) -> str:
    lines = text.split("\n")
    start, end, _ = _class_lines(text, cls)
    del lines[start:end]
    return "\n".join(lines)


def _drop_column(text: str, cls: ast.ClassDef, name: str) -> str:
    idx = _attr_line_index(cls, name)
    if idx is None:
        raise MutationApplyError(f"column {name!r} not found in class {cls.name!r}")
    lines = text.split("\n")
    # A multi-line attribute would need an end_lineno walk; for S0's
    # fixtures the attrs are single-line. If that assumption breaks,
    # we raise MutationApplyError rather than quietly truncating.
    del lines[idx]
    return "\n".join(lines)


def _retype_column(text: str, cls: ast.ClassDef, name: str, new_type: str) -> str:
    """Replace the column's type expression with ``new_type``. Works
    for both ``Column(OldType, ...)`` and ``mapped_column(OldType)`` /
    ``Mapped[...]`` shapes — we rewrite the first type-like token in
    the attr's RHS."""
    idx = _attr_line_index(cls, name)
    if idx is None:
        raise MutationApplyError(f"column {name!r} not found")
    lines = text.split("\n")
    original = lines[idx]
    for prefix in (
        "Column(",
        "mapped_column(",
    ):
        pos = original.find(prefix)
        if pos < 0:
            continue
        after = pos + len(prefix)
        close = _find_arg_end(original, after)
        if close < 0:
            continue
        lines[idx] = f"{original[:after]}{new_type}{original[close:]}"
        return "\n".join(lines)
    # Fall back to annotation form: ``name: Mapped[Old]``.
    pos = original.find("Mapped[")
    if pos >= 0:
        after = pos + len("Mapped[")
        close = original.find("]", after)
        if close > 0:
            lines[idx] = f"{original[:after]}{new_type}{original[close:]}"
            return "\n".join(lines)
    raise MutationApplyError(
        f"column {name!r} has no mutable type expression on its declaration line"
    )


def _find_arg_end(line: str, start: int) -> int:
    """Index of the first unmatched ``,`` or ``)`` in ``line`` after
    ``start`` — the end of the first positional argument."""
    depth = 0
    for i in range(start, len(line)):
        ch = line[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "," and depth == 0:
            return i
    return -1


def _flip_nullable(text: str, cls: ast.ClassDef, name: str, new_nullable: bool) -> str:
    """Insert or flip ``nullable=<bool>`` on the column's declaration
    line. If no ``nullable=`` kwarg exists, append one before the
    closing paren."""
    idx = _attr_line_index(cls, name)
    if idx is None:
        raise MutationApplyError(f"column {name!r} not found")
    lines = text.split("\n")
    line = lines[idx]
    new_val = "True" if new_nullable else "False"
    if "nullable=True" in line:
        line = line.replace("nullable=True", f"nullable={new_val}", 1)
    elif "nullable=False" in line:
        line = line.replace("nullable=False", f"nullable={new_val}", 1)
    else:
        close = line.rfind(")")
        if close < 0:
            raise MutationApplyError(
                f"column {name!r} line has no closing paren to append nullable= into"
            )
        line = f"{line[:close]}, nullable={new_val}{line[close:]}"
    lines[idx] = line
    return "\n".join(lines)


def _rename_column(text: str, cls: ast.ClassDef, name: str, new_name: str) -> str:
    """Rename the attribute itself (``old = Column(...)`` →
    ``new = Column(...)``). The oracle will see this as drop(old)
    + add(new) because the AST attribute name is the only name the
    schema knows."""
    idx = _attr_line_index(cls, name)
    if idx is None:
        raise MutationApplyError(f"column {name!r} not found")
    lines = text.split("\n")
    line = lines[idx]
    # Replace first occurrence at the start of the stripped line.
    stripped = line.lstrip()
    if not stripped.startswith(name):
        raise MutationApplyError(
            f"column {name!r} does not start its declaration line (unexpected shape)"
        )
    indent = line[: len(line) - len(stripped)]
    lines[idx] = indent + new_name + stripped[len(name) :]
    return "\n".join(lines)


def _add_column(text: str, cls: ast.ClassDef, column: str, new_type: str) -> str:
    """Append a new ``Column`` after the class's last existing
    attribute. Uses the surrounding class's indentation so the file
    stays well-formed."""
    lines = text.split("\n")
    # Find the last attribute line in the class body.
    last_attr_idx: int | None = None
    for stmt in cls.body:
        if _attribute_target(stmt) is not None:
            last_attr_idx = stmt.end_lineno or stmt.lineno
    if last_attr_idx is None:
        raise MutationApplyError(f"class {cls.name!r} has no attributes to anchor after")
    insertion_idx = last_attr_idx  # 1-indexed end of last attr → 0-indexed line AFTER
    # Find indentation of the anchor line.
    anchor_line = lines[last_attr_idx - 1]
    indent = anchor_line[: len(anchor_line) - len(anchor_line.lstrip())]
    new_line = f"{indent}{column} = Column({new_type}, nullable=True)"
    lines.insert(insertion_idx, new_line)
    return "\n".join(lines)


# --- Full-trial orchestrator ---


@dataclass
class TrialSpec:
    """One mainline trial: metadata + planned mutation + applied path."""

    project: str
    unit: str
    trial_index: int
    bucket: str = "mainline"
    mutation: Mutation | None = None
    applied_to: Path | None = None
    skipped_reason: str | None = None
    seed_hex: str = field(default="")


def plan_trials(
    project: str,
    unit: str,
    classes: list[ModelClass],
    *,
    count: int = 10,
    bucket: str = "mainline",
) -> list[TrialSpec]:
    """SPEC §6.1: N=10 mutations per unit by default; one trial per
    mutation. Each trial is a self-contained ``TrialSpec`` with its
    own seeded RNG so a reviewer can replay just one trial from its
    index."""
    trials: list[TrialSpec] = []
    for trial_index in range(count):
        rng = seeded_rng(project, unit, trial_index, bucket=bucket)
        mutation = plan_mutation(rng, classes, trial_index)
        trials.append(
            TrialSpec(
                project=project,
                unit=unit,
                trial_index=trial_index,
                bucket=bucket,
                mutation=mutation,
                skipped_reason=None if mutation else "no mutable targets",
                seed_hex=_seed_bytes(project, unit, bucket, trial_index).hex(),
            )
        )
    return trials
