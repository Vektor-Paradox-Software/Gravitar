"""SPEC-17 §6 shared constant folder.

The spec is explicit (§3 principle 3): the model reader uses the
SAME constant folder as the Alembic parser. Any improvement to the
fold rules benefits both sides.

This module re-exports the Alembic parser's existing const-fold
primitives so the model reader never grows a parallel implementation.
Only the entry points the reader needs are exposed; the parser's
internal walker stays internal.
"""

from __future__ import annotations

from gravitar.schema.parsers.alembic_parser import (
    _collect_module_constants as collect_module_constants,
)
from gravitar.schema.parsers.alembic_parser import (
    _ConstFoldRewriter as ConstFoldRewriter,  # noqa: N811
)

__all__ = ["ConstFoldRewriter", "collect_module_constants"]
