"""Scenario A: the migration widens "name" via a
dedicated alter_column op, but the model still declares the original,
narrower length — type_mismatch, exercising alter_column replay."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
