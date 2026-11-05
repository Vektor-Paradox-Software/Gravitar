"""The migration adds a column no model references."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Gadget(Base):
    __tablename__ = "gadgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
