"""SPEC-17 F-18: booby-trapped module.

The point of the fixture is proof-by-absence: a static reader that
only ``ast.parse``s this file never fires the sentinel. S1 collects
+ parses but does not execute; the audit-hook test (§8) checks for
``exec`` events on this file, and the booby trap verifies that
assertion is real — any ``exec`` of this file would:
  - write a sentinel file to disk,
  - open a network socket.

The sentinel path uses an env var the test sets so each test run
gets its own location.
"""

import os
import socket
from pathlib import Path

# Side effects at import time. If a reader ever imports this file,
# the sentinel appears on disk and the test fails loudly.
_sentinel = os.environ.get("GRAVITAR_F18_SENTINEL")
if _sentinel:
    Path(_sentinel).write_text("TRIPPED")

# Attempt to open a socket. Not connecting — opening the fd itself
# is enough to be noticed by audit hooks watching socket.connect.
# (We don't actually connect; that'd flake on sandboxed CI.)
_s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_s.close()

from sqlalchemy import Column, Integer, String  # noqa: E402
from sqlalchemy.orm import declarative_base  # noqa: E402

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    name = Column(String(10), nullable=False)
