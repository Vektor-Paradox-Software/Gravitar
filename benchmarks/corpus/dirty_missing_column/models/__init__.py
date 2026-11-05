"""Self-test corpus project: one known missing_column drift."""

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    # Declared in the model but no migration adds it → missing_column drift.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
