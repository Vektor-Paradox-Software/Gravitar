"""A models package using the legacy declarative_base() factory."""

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
