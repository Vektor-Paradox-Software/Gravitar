"""SPEC-17 F-19: file with newer-Python syntax.

Mealie ships files using PEP-758 ``except A, B:`` under a project
that pins a Python version supporting it; running Gravitar under an
older interpreter must NOT abort the run — the single file becomes
a ``parse_error`` note (SPEC §6.2 Phase 1), and the rest of the
run continues.

The actual trigger syntax here (match statement with pattern body)
is only parseable on 3.10+; on an older interpreter this file fails
``ast.parse``. On 3.14 (our current runner) it parses fine; the
``parse_error_detail`` path is covered by a separate test that
hand-crafts a SyntaxError.
"""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    status = Column(String(10), nullable=False)


def _match_example(x: object) -> str:
    """3.10+ match statement — fine today, flagged by F-19 on older."""
    match x:
        case str():
            return "s"
        case _:
            return "?"
