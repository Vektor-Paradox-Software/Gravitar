"""SPEC-17 F-21: try/except ImportError fallback.

The classic SA pattern: prefer the dialect-specific type when
available, fall back to a generic one. SPEC §5.2 — first binding
wins and the resulting symbol carries ``conditional=True`` so the
column's table taints accordingly.
"""

try:
    from sqlalchemy.dialects.postgresql import JSONB as JsonType  # noqa: N811
except ImportError:
    from sqlalchemy import JSON as JsonType

from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    data = Column(JsonType, nullable=True)
