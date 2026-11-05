"""Models that exactly match tests/fixtures/clean/versions/ — the zero-findings case."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False)
    created_at = Column(DateTime(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    total_cents = Column(Numeric(10, 2), nullable=False)
