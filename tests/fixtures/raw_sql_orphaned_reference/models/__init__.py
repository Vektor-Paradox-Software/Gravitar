"""The raw SQL below matches the ALTER TABLE ADD COLUMN pattern (Section 5), but
names a table ("ghost_table") no migration or model ever creates — Section 6's
"orphaned reference" failure mode."""

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
