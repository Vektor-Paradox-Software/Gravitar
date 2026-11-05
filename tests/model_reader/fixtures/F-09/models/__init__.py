"""F-09: mixin in another file via relative import.

Verifies the class-graph resolver can follow a ``from .mixins import
Timestamped`` edge and attribute Timestamped's columns onto the
mapped class that mixes it in.
"""

from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

from .mixins import Timestamped

Base = declarative_base()


class Widget(Base, Timestamped):
    __tablename__ = "widgets"
    id = Column(Integer, primary_key=True)
