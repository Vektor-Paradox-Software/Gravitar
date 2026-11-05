"""Model matches the migration exactly; the only finding should be the raw-SQL
warning from the op.execute() call — but this one matches a recognized SPEC-
CONFIDENCE-ENGINE.md Section 5 pattern (ALTER TABLE ADD COLUMN), so it should
score higher than an unrecognized raw-SQL call (see raw_sql fixture)."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
