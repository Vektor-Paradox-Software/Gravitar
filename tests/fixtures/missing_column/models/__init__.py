"""The model declares a column no migration adds."""

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Widget(Base):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
