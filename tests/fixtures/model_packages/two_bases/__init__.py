"""A models package with two independent declarative bases, for base_classes filtering tests."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

BaseA = declarative_base()
BaseB = declarative_base()


class Widget(BaseA):
    __tablename__ = "widgets"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)


class Gadget(BaseB):
    __tablename__ = "gadgets"

    id = Column(Integer, primary_key=True)
    label = Column(String(50), nullable=False)
