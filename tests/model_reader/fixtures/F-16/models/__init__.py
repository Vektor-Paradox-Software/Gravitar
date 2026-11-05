"""F-16: external base (``SomeLibMixin``) — scoped S3 note, no error
possible.

Simulates a project that mixes in a mixin from a third-party library.
Gravitar can't see what columns the external base contributes, so
the mapped class carries a scoped note; the comparator cannot emit
an error on this table because the finding would be unprovable.
"""

from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

# Pretend this came from a third-party package. The class name
# ``SomeLibMixin`` is not in Gravitar's known-columnless allowlist,
# so the class graph marks ``Widget`` as external_base=True.
try:
    from some_third_party import SomeLibMixin  # pragma: no cover - not installed
except ImportError:

    class SomeLibMixin:
        pass


Base = declarative_base()


class Widget(Base, SomeLibMixin):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
