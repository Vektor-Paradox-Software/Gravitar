"""Scenario C: two independent missing_column
findings (widgets.legacy, gadgets.notes), both pattern_id "ast:missing_column"
— dismissing one must lower the other's future confidence."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    legacy = Column(String(20), nullable=True)


class Gadget(Base):
    __tablename__ = "gadgets"

    id = Column(Integer, primary_key=True)
    title = Column(String(50), nullable=False)
    notes = Column(String(200), nullable=True)
