"""A models package with a bare Core Table (no mapped class) alongside a mapped model."""

from sqlalchemy import Column, Integer, String, Table
from sqlalchemy.orm import declarative_base

Base = declarative_base()

widget_tags = Table(
    "widget_tags",
    Base.metadata,
    Column("widget_id", Integer, primary_key=True),
    Column("tag_id", Integer, primary_key=True),
)


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
