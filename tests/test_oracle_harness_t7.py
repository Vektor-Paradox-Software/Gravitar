"""SPEC-18 T7 tests: mainline mutations.

Each mutation type gets its own test exercising both the planner
(which mutation got chosen, with what args) and the applier (did
the file actually change the expected way). Determinism is
asserted explicitly — same seed must produce same mutation + same
output bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.oracle.mutate import (
    ADD_COLUMN_TYPES,
    MUTATION_TYPES,
    MutationApplyError,
    apply_mutation,
    discover_model_classes,
    plan_mutation,
    plan_trials,
    seeded_rng,
)


def _write_models(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.py"
    path.write_text(body, encoding="utf-8")
    return path


# --- Discovery ---


def test_discover_model_classes_returns_columns_in_order(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "from sqlalchemy import Column, Integer, String\n"
        "class Widget:\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(50), nullable=False)\n"
        "    note = Column(String(100))\n",
    )

    classes = discover_model_classes(path)

    assert len(classes) == 1
    assert classes[0].class_name == "Widget"
    assert classes[0].columns == ("id", "name", "note")


def test_discover_model_classes_skips_dunder_attrs(tmp_path: Path) -> None:
    """``__tablename__`` is not a mutation target."""
    path = _write_models(
        tmp_path,
        'class Widget:\n    __tablename__ = "widgets"\n    id = 1\n',
    )

    classes = discover_model_classes(path)

    assert classes[0].columns == ("id",)


def test_discover_returns_empty_for_syntax_error(tmp_path: Path) -> None:
    path = _write_models(tmp_path, "def :\n")
    assert discover_model_classes(path) == []


# --- Seeded RNG ---


def test_seeded_rng_is_deterministic() -> None:
    """Same identity + bucket + trial_index → identical stream."""
    r1 = seeded_rng("proj", "u", 3)
    r2 = seeded_rng("proj", "u", 3)
    seq1 = [r1.random() for _ in range(5)]
    seq2 = [r2.random() for _ in range(5)]
    assert seq1 == seq2


def test_seeded_rng_differs_by_trial_index() -> None:
    r1 = seeded_rng("proj", "u", 1)
    r2 = seeded_rng("proj", "u", 2)
    assert r1.random() != r2.random()


def test_seeded_rng_differs_by_bucket() -> None:
    r_main = seeded_rng("proj", "u", 1, bucket="mainline")
    r_hard = seeded_rng("proj", "u", 1, bucket="hard")
    assert r_main.random() != r_hard.random()


# --- Planner ---


def test_plan_mutation_returns_none_when_no_columns(tmp_path: Path) -> None:
    """Empty classes aren't valid targets — planner refuses to
    fabricate one."""
    path = _write_models(tmp_path, "class Empty:\n    pass\n")
    rng = seeded_rng("proj", "u", 0)

    assert plan_mutation(rng, discover_model_classes(path), 0) is None


def test_plan_mutation_hits_every_type_across_many_trials(tmp_path: Path) -> None:
    """Fairness check: across enough trials, every mutation type
    should appear. If one is impossible to pick, the type list is
    out of sync with the planner's dispatch."""
    path = _write_models(
        tmp_path,
        "from sqlalchemy import Column, Integer\n"
        "class Widget:\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(Integer)\n",
    )
    classes = discover_model_classes(path)
    seen: set[str] = set()
    for i in range(200):
        rng = seeded_rng("proj", "unit", i)
        plan = plan_mutation(rng, classes, i)
        if plan:
            seen.add(plan.mutation_type)
    assert seen == set(MUTATION_TYPES)


def test_plan_mutation_type_change_uses_cross_type_pair(tmp_path: Path) -> None:
    """When the planner picks change_column_type, new_type must be a
    valid-looking SA type (string from the fixed pair list)."""
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = Column(Integer)\n",
    )
    classes = discover_model_classes(path)
    for i in range(50):
        rng = seeded_rng("proj", "unit", i)
        plan = plan_mutation(rng, classes, i)
        if plan and plan.mutation_type == "change_column_type":
            assert plan.new_type
            break
    else:
        pytest.skip("no change_column_type in 50 trials (seed bias)")


# --- Appliers ---


def test_apply_drop_model_table_removes_class(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = 1\n\nclass Other:\n    y = 1\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(Mutation(mutation_type="drop_model_table", class_name="Widget"), path)

    text = path.read_text(encoding="utf-8")
    assert "class Widget" not in text
    assert "class Other" in text


def test_apply_drop_model_column_removes_line(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = 1\n    name = 2\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(
        Mutation(mutation_type="drop_model_column", class_name="Widget", column="name"),
        path,
    )

    text = path.read_text(encoding="utf-8")
    assert "name" not in text
    assert "id" in text


def test_apply_change_column_type_rewrites_type(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "from sqlalchemy import Column, Integer\n"
        "class Widget:\n    id = Column(Integer, primary_key=True)\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(
        Mutation(
            mutation_type="change_column_type",
            class_name="Widget",
            column="id",
            new_type="BigInteger",
        ),
        path,
    )

    text = path.read_text(encoding="utf-8")
    assert "Column(BigInteger, primary_key=True)" in text


def test_apply_flip_nullability_adds_kwarg_when_absent(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "from sqlalchemy import Column, Integer\n"
        "class Widget:\n    id = Column(Integer, primary_key=True)\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(
        Mutation(
            mutation_type="flip_nullability",
            class_name="Widget",
            column="id",
            new_nullable=True,
        ),
        path,
    )

    assert "nullable=True" in path.read_text(encoding="utf-8")


def test_apply_flip_nullability_toggles_existing_kwarg(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = Column(Integer, nullable=True)\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(
        Mutation(
            mutation_type="flip_nullability",
            class_name="Widget",
            column="id",
            new_nullable=False,
        ),
        path,
    )

    text = path.read_text(encoding="utf-8")
    assert "nullable=False" in text
    assert "nullable=True" not in text


def test_apply_rename_model_column_rewrites_lhs(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "class Widget:\n    name = Column(String(50))\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(
        Mutation(
            mutation_type="rename_model_column",
            class_name="Widget",
            column="name",
            new_name="label",
        ),
        path,
    )

    text = path.read_text(encoding="utf-8")
    assert "    label = Column(String(50))" in text
    assert "    name = Column" not in text


def test_apply_add_model_column_inserts_after_last_attr(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = Column(Integer, primary_key=True)\n",
    )
    from benchmarks.oracle.mutate import Mutation

    apply_mutation(
        Mutation(
            mutation_type="add_model_column",
            class_name="Widget",
            column="planted_1_123",
            new_type="String(10)",
        ),
        path,
    )

    text = path.read_text(encoding="utf-8")
    assert "planted_1_123 = Column(String(10), nullable=True)" in text


def test_apply_errors_cleanly_on_missing_class(tmp_path: Path) -> None:
    path = _write_models(tmp_path, "class Other:\n    x = 1\n")
    from benchmarks.oracle.mutate import Mutation

    with pytest.raises(MutationApplyError, match="not found"):
        apply_mutation(Mutation(mutation_type="drop_model_table", class_name="Missing"), path)


# --- End-to-end: plan_trials + apply ---


def test_plan_trials_returns_n_trials_each_with_a_seed(tmp_path: Path) -> None:
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = Column(Integer)\n    name = Column(String())\n",
    )
    classes = discover_model_classes(path)

    trials = plan_trials("proj", "u", classes, count=5)

    assert len(trials) == 5
    assert all(t.seed_hex for t in trials)
    assert all(t.trial_index == i for i, t in enumerate(trials))
    assert all(t.mutation is not None for t in trials)


def test_plan_trials_is_deterministic_byte_for_byte(tmp_path: Path) -> None:
    """SPEC §10 reproducibility: two runs with the same inputs
    produce identical trial plans."""
    path = _write_models(
        tmp_path,
        "class Widget:\n    id = Column(Integer)\n    name = Column(String())\n",
    )
    classes = discover_model_classes(path)

    r1 = plan_trials("proj", "u", classes, count=10)
    r2 = plan_trials("proj", "u", classes, count=10)

    for a, b in zip(r1, r2, strict=True):
        assert a.seed_hex == b.seed_hex
        assert a.mutation == b.mutation


def test_plan_trials_skipped_when_no_mutable_targets(tmp_path: Path) -> None:
    """Classes with no columns → trial carries ``skipped_reason``
    rather than silently producing a no-op or a fabricated target."""
    path = _write_models(tmp_path, "class Empty:\n    pass\n")

    trials = plan_trials("proj", "u", discover_model_classes(path), count=3)

    assert all(t.skipped_reason == "no mutable targets" for t in trials)
    assert all(t.mutation is None for t in trials)


# --- Guard: constant tables stay stable ---


def test_mutation_types_match_spec_section_6_1() -> None:
    """SPEC-18 §6.1 enumerates exactly six mutation types. If this
    tuple drifts without a spec amendment, the test flags it."""
    assert MUTATION_TYPES == (
        "drop_model_column",
        "add_model_column",
        "change_column_type",
        "flip_nullability",
        "rename_model_column",
        "drop_model_table",
    )


def test_add_column_types_are_resolver_friendly() -> None:
    """All types used for ``add_model_column`` must be ones Gravitar's
    resolver can normalize cleanly — otherwise the recall measurement
    is confounded by ``unknown_type`` warnings on the planted column."""
    import ast as _ast

    from gravitar.schema.types import normalize_ast_type

    for type_expr in ADD_COLUMN_TYPES:
        node = _ast.parse(type_expr, mode="eval").body
        normalized = normalize_ast_type(node)
        assert not normalized.startswith("unknown:"), type_expr
