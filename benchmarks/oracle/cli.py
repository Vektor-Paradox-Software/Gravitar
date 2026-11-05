"""SPEC-18 T0: CLI entry point for the oracle harness.

Usage:
    python -m benchmarks.oracle list                 # enumerate ProjectUnits
    python -m benchmarks.oracle run --out <dir>      # T0 skeleton: emit empty summary.json
    python -m benchmarks.oracle run --config <path>  # alternate manifest

The ``run`` subcommand is the T0 skeleton — it emits a summary.json with
the SPEC-18 §8 top-level keys populated with placeholder values and an
``unrunnable: {reason: T0_skeleton_only}`` per unit. T1+ fill in the
actual oracle output.

Separate from the pre-SPEC-18 ``benchmarks/run.py`` harness: both can
coexist during the transition. ``run.py`` runs static-oracle flows
against the self-test corpus; ``python -m benchmarks.oracle`` is the
Postgres-oracle harness scaffold.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

from benchmarks.oracle.config import ProjectUnit, load_oracle_units
from benchmarks.oracle.run_report import empty_summary, write_summary

HARNESS_ROOT = Path(__file__).resolve().parent.parent  # benchmarks/
DEFAULT_MANIFEST = HARNESS_ROOT / "manifest.yml"
DEFAULT_REPORTS = HARNESS_ROOT / "reports"


def _print_unit(unit: ProjectUnit) -> str:
    dialects = ",".join(unit.dialects)
    return f"{unit.project}/{unit.unit}  [{dialects}]  tree={unit.tree_path}  py={unit.python}"


def cmd_list(args: argparse.Namespace) -> int:
    units = load_oracle_units(args.config)
    if not units:
        print(
            f"oracle: no oracle_units in {args.config}; see SPEC-18 §3 for the shape",
            file=sys.stderr,
        )
        return 0
    for unit in units:
        print(_print_unit(unit))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    units = load_oracle_units(args.config)
    run_id = args.run_id or _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = args.out / run_id
    summary = empty_summary(units, run_id=run_id)
    write_summary(summary, out_dir / "summary.json")
    print(f"oracle: wrote T0 skeleton summary to {out_dir / 'summary.json'}", file=sys.stderr)
    print(
        "oracle: T0 is the skeleton only — no Postgres oracle yet. "
        "Each unit is marked unrunnable(reason='T0_skeleton_only'). "
        "T1 stands up Postgres; see SPEC-18 §12.",
        file=sys.stderr,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.oracle", description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"manifest path (default: {DEFAULT_MANIFEST.relative_to(HARNESS_ROOT.parent)})",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)

    subs.add_parser("list", help="enumerate ProjectUnits from the manifest")

    run = subs.add_parser(
        "run",
        help="run the oracle (T0: emit empty summary.json; T1+ run the real oracle)",
    )
    run.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORTS,
        help=f"reports directory (default: {DEFAULT_REPORTS.relative_to(HARNESS_ROOT.parent)})",
    )
    run.add_argument(
        "--run-id",
        default=None,
        help="label for the run-id subdirectory (default: current UTC timestamp)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "run":
        return cmd_run(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
