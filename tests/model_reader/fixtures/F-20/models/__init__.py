"""SPEC-17 F-20: star import with and without __all__.

- ``shared`` defines ``__all__ = ["ExportedBase"]``; a star-import
  from it is resolvable through the symbol table.
- ``private`` deliberately has NO ``__all__``; a star-import from it
  must emit a scoped note rather than silently resolving (or missing).
"""

from .private import *  # noqa: F403 — fixture is testing * handling
from .shared import *  # noqa: F403 — fixture is testing * handling
