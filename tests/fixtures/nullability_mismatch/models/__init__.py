"""The model and migration disagree on a column's nullability."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    middle_name = Column(String(50), nullable=True)
