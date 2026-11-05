"""SPEC-17 F-23: cyclic imports — a and b import each other.

Only the ``__init__`` is here; actual cycle lives in ``a.py`` and
``b.py``. Cycle detection must break and emit a scoped note rather
than hang.
"""
