"""Scenario A: the migration drops "legacy" via a
dedicated drop_column op, but the model still declares it (drift: the model
update was forgotten) — missing_column, exercising drop_column replay."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    legacy = Column(String(20), nullable=True)
