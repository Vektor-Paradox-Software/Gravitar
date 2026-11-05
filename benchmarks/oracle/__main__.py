"""Entrypoint for ``python -m benchmarks.oracle``. SPEC-18 §16."""

from __future__ import annotations

import sys

from benchmarks.oracle.cli import main

if __name__ == "__main__":
    sys.exit(main())
