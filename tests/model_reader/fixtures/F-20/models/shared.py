"""Module with an explicit ``__all__`` — star-importable."""

__all__ = ["ExportedBase", "Widget"]

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

ExportedBase = declarative_base()


class Widget(ExportedBase):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
    name = Column(String(10), nullable=False)
