"""The model and migration disagree on a column's type."""

from sqlalchemy import Column, Integer, Numeric
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    total_cents = Column(Numeric(10, 2), nullable=False)
