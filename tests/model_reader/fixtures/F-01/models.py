"""SPEC-17 F-01: classic Column(String(10)), primary_key, defaults.

The simplest possible fixture. If the differential harness can't
agree between a static reader and real SA on this, nothing further
works.
"""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(10), nullable=False)
    note = Column(String(50))  # default nullable
