"""Model matches the migration exactly; the only finding should be the raw-SQL
warning from the op.execute() call that Gravitar can't analyze."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
