"""The model uses a custom SQLAlchemy type Gravitar cannot normalize."""

from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import TypeEngine

Base = declarative_base()


class MoneyType(TypeEngine):
    """A hypothetical custom type — not one of the SQLAlchemy builtins Gravitar
    knows how to normalize."""


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True)
    balance = Column(MoneyType(), nullable=False)
