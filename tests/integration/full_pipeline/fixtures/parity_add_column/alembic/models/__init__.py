"""Scenario A: model doesn't declare a column the
migration adds via a dedicated add_column op — orphaned_column, exercising
the add_column replay path specifically (not just create_table)."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
