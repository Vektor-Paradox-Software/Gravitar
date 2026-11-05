"""SPEC-15 M20: tests for the real-world validation harness.

The harness is a wrapper around ``gravitar check`` plus report
generation. These tests exercise the wrapper mechanics — repo
resolution, seed-drift application, report shape — against local
synthetic repos so the suite stays fast and offline. Real cloning +
real repos belong in M21 integration runs (SPEC-15 §4.1), not here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load the harness as a module even though it lives under scripts/
# (which isn't a package). Python 3.14's dataclass decorator looks up
# cls.__module__ via sys.modules at class-creation time, so we must
# register the module there BEFORE exec_module runs.
_HARNESS_PATH = REPO_ROOT / "scripts" / "validate_real_world.py"
_SPEC = importlib.util.spec_from_file_location("validate_real_world", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_HARNESS = importlib.util.module_from_spec(_SPEC)
sys.modules["validate_real_world"] = _HARNESS
_SPEC.loader.exec_module(_HARNESS)


# --- URL detection ---


def test_url_detection_recognizes_https(tmp_path: Path) -> None:
    assert _HARNESS._looks_like_url("https://github.com/org/repo.git")
    assert _HARNESS._looks_like_url("git@github.com:org/repo.git")
    assert not _HARNESS._looks_like_url(str(tmp_path))


def test_url_detection_prefers_path_when_both_could_match(tmp_path: Path) -> None:
    """A directory called ``https-whatever`` happens to exist on disk —
    treat it as a path, not a URL."""
    (tmp_path / "https-whatever").mkdir()
    assert not _HARNESS._looks_like_url(str(tmp_path / "https-whatever"))


# --- Mutation loader ---


def test_load_mutations_parses_scenario(tmp_path: Path) -> None:
    scenario = tmp_path / "s.yml"
    scenario.write_text(
        "mutations:\n"
        "  - type: add_model_column\n"
        "    model_file: app/models/widget.py\n"
        "    after_line: 'name = Column(String())'\n"
        "    column: 'flag = Column(Boolean(), nullable=False)'\n"
        "    description: 'seed: flag drift'\n",
        encoding="utf-8",
    )

    mutations = _HARNESS._load_mutations(scenario)

    assert len(mutations) == 1
    assert mutations[0].mutation_type == "add_model_column"
    assert mutations[0].description == "seed: flag drift"


def test_load_mutations_rejects_missing_keys(tmp_path: Path) -> None:
    scenario = tmp_path / "s.yml"
    scenario.write_text("mutations:\n  - type: add_model_column\n", encoding="utf-8")

    with pytest.raises(ValueError, match="type.*model_file"):
        _HARNESS._load_mutations(scenario)


# --- Mutation application ---


def test_apply_mutation_add_model_column_inserts_after_anchor(tmp_path: Path) -> None:
    model = tmp_path / "models.py"
    model.write_text(
        "from sqlalchemy import Column, Integer, String\n"
        "class Widget:\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String())\n",
        encoding="utf-8",
    )

    mutation = _HARNESS.Mutation(
        mutation_type="add_model_column",
        model_file="models.py",
        after_line="    name = Column(String())",
        column="flag = Column(Boolean(), nullable=False)",
        description="seed: flag drift",
    )
    _HARNESS._apply_mutation(tmp_path, mutation)

    out = model.read_text(encoding="utf-8")
    assert "flag = Column(Boolean(), nullable=False)" in out
    # Indentation of the anchor line (4 spaces) is preserved on the new line.
    assert "    flag = Column" in out


def test_apply_mutation_rejects_unimplemented_type(tmp_path: Path) -> None:
    model = tmp_path / "x.py"
    model.write_text("", encoding="utf-8")

    mutation = _HARNESS.Mutation(
        mutation_type="change_column_type", model_file="x.py", description="x"
    )
    with pytest.raises(NotImplementedError, match="not implemented in-harness"):
        _HARNESS._apply_mutation(tmp_path, mutation)


def test_apply_mutation_errors_if_target_file_missing(tmp_path: Path) -> None:
    mutation = _HARNESS.Mutation(
        mutation_type="add_model_column",
        model_file="does/not/exist.py",
        column="x = Column(String())",
        description="x",
    )
    with pytest.raises(FileNotFoundError):
        _HARNESS._apply_mutation(tmp_path, mutation)


# --- Report shape ---


def test_confidence_bucket_bands() -> None:
    assert _HARNESS._confidence_bucket(0.95) == ">=0.9"
    assert _HARNESS._confidence_bucket(0.9) == ">=0.9"
    assert _HARNESS._confidence_bucket(0.85) == "0.7-0.9"
    assert _HARNESS._confidence_bucket(0.5) == "<0.7"


def test_build_report_captures_envelope_distribution() -> None:
    result = _HARNESS.RunResult(
        exit_code=1,
        duration_s=1.5,
        envelope={
            "findings": [
                {
                    "category": "missing_column",
                    "severity": "error",
                    "confidence": 0.95,
                    "kind": "fail",
                    "table": "widgets",
                    "column": "flag",
                    "finding_id": "abc",
                    "message": "model declares; migration doesn't add",
                },
                {
                    "category": "unknown_type",
                    "severity": "warning",
                    "confidence": 0.6,
                    "kind": "fail",
                    "table": "widgets",
                    "column": "data",
                    "finding_id": "def",
                },
            ],
            "notes": [{"reason": "dynamic_sql", "tables": None, "source": {"file": "v/0001.py"}}],
            "coverage": {"revisions_total": 3},
            "summary": {"errors": 1, "warnings": 1, "reviews": 0},
        },
    )

    report = _HARNESS._build_report("repo-x", None, [], result)

    assert report["findings_count"] == 2
    assert report["notes_count"] == 1
    assert report["distribution"]["by_category"] == {
        "missing_column": 1,
        "unknown_type": 1,
    }
    assert report["distribution"]["by_confidence_bucket"] == {">=0.9": 1, "<0.7": 1}


def test_build_report_includes_seed_drift_metadata() -> None:
    mutation = _HARNESS.Mutation(
        mutation_type="add_model_column",
        model_file="app/models.py",
        description="seed: flag",
    )
    result = _HARNESS.RunResult(exit_code=0, duration_s=0.1, envelope={})

    report = _HARNESS._build_report("repo-y", "seeds.yml", [mutation], result)

    assert report["seed_drift"]["scenario_file"] == "seeds.yml"
    assert report["seed_drift"]["mutations_applied"] == [
        {
            "type": "add_model_column",
            "model_file": "app/models.py",
            "description": "seed: flag",
        }
    ]


def test_render_markdown_lists_findings_as_review_rows() -> None:
    """SPEC-15 §4.1: the harness surfaces findings for manual review, so
    the markdown must list every finding — never suppress one."""
    report = _HARNESS._build_report(
        "repo-x",
        None,
        [],
        _HARNESS.RunResult(
            exit_code=1,
            duration_s=0.5,
            envelope={
                "findings": [
                    {
                        "category": "missing_column",
                        "severity": "error",
                        "confidence": 0.9,
                        "kind": "fail",
                        "table": "widgets",
                        "column": "flag",
                        "finding_id": "abc",
                        "message": "model declares; migration missing",
                    }
                ],
                "notes": [],
                "coverage": {},
                "summary": {},
            },
        ),
    )

    md = _HARNESS._render_markdown(report)
    assert "abc" in md
    assert "widgets.flag" in md
    assert "manual TP/FP review" in md


# --- End-to-end against a local synthetic repo ---


def _write_synthetic_repo(root: Path) -> None:
    (root / "models").mkdir()
    (root / "models" / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(50), nullable=False)\n",
        encoding="utf-8",
    )
    (root / "alembic.ini").write_text("[alembic]\nscript_location = .\n")
    (root / "versions").mkdir()
    (root / "versions" / "0001.py").write_text(
        '"""create"""\n'
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        'revision = "0001"\n'
        "down_revision = None\n\n"
        "def upgrade():\n"
        '    op.create_table("widgets",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("name", sa.String(length=50), nullable=False),\n'
        "    )\n"
    )


def test_main_end_to_end_against_local_repo(tmp_path: Path) -> None:
    """Clean synthetic repo + no seed-drift → the harness still writes
    both outputs and the markdown lists zero findings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_synthetic_repo(repo)
    report_out = tmp_path / "out" / "report.md"

    exit_code = _HARNESS.main(
        [
            "--repo",
            str(repo),
            "--report-out",
            str(report_out),
        ]
    )

    assert exit_code == 0
    assert report_out.exists()
    sidecar = report_out.with_suffix(report_out.suffix + ".json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    # Clean repo → zero drift findings.
    assert data["findings_count"] == 0


def test_main_applies_seed_drift_and_reports_the_added_finding(tmp_path: Path) -> None:
    """Seed-drift adds a model column; gravitar check must surface the
    corresponding missing_column finding; the report must mention the
    seeded column."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_synthetic_repo(repo)
    seed = tmp_path / "seed.yml"
    seed.write_text(
        "mutations:\n"
        "  - type: add_model_column\n"
        "    model_file: models/__init__.py\n"
        "    after_line: '    name = Column(String(50), nullable=False)'\n"
        "    column: 'flag = Column(String(10), nullable=True)'\n"
        "    description: 'seed: flag drift'\n",
        encoding="utf-8",
    )
    report_out = tmp_path / "report.md"

    exit_code = _HARNESS.main(
        [
            "--repo",
            str(repo),
            "--seed-drift",
            str(seed),
            "--report-out",
            str(report_out),
        ]
    )

    assert exit_code == 0
    data = json.loads(
        report_out.with_suffix(report_out.suffix + ".json").read_text(encoding="utf-8")
    )
    categories = [f["category"] for f in data["findings"]]
    assert "missing_column" in categories, data
    assert any(f.get("column") == "flag" for f in data["findings"])
    # And the local source file was NOT mutated (harness copies into tmp first).
    source_text = (repo / "models" / "__init__.py").read_text(encoding="utf-8")
    assert "flag" not in source_text


def test_harness_exit_zero_even_on_drift(tmp_path: Path) -> None:
    """SPEC-15 §6: the harness produces a report for review, not a CI
    gate — its own exit code is 0 whether the check found drift or not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_synthetic_repo(repo)
    # Create model-only drift by editing a copy before passing to harness.
    (repo / "models" / "__init__.py").write_text(
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class Widget(Base):\n"
        '    __tablename__ = "widgets"\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(50), nullable=False)\n"
        "    extra = Column(String(50), nullable=True)\n",  # unmigrated
        encoding="utf-8",
    )
    report_out = tmp_path / "r.md"

    exit_code = _HARNESS.main(["--repo", str(repo), "--report-out", str(report_out)])

    assert exit_code == 0
    data = json.loads(report_out.with_suffix(report_out.suffix + ".json").read_text())
    assert data["run"]["exit_code"] != 0  # gravitar check saw drift
    assert data["findings_count"] >= 1


def test_harness_errors_cleanly_on_missing_repo(tmp_path: Path) -> None:
    report_out = tmp_path / "r.md"
    exit_code = _HARNESS.main(
        [
            "--repo",
            str(tmp_path / "does-not-exist"),
            "--report-out",
            str(report_out),
        ]
    )
    assert exit_code == 2
