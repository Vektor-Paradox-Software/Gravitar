"""Scenario D: one migration exercising a
fully-resolved op (create_table, mismatched against the model -> missing_column
at confidence 1.0), a pattern-matched raw SQL op (-> broken_chain at 0.5), and
a genuinely unresolvable raw SQL op (a non-literal argument the AST walker
can't see into at all -> broken_chain at 0.3) — three findings at three
distinct confidence levels from a single migration file."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    extra = Column(String(50), nullable=True)
