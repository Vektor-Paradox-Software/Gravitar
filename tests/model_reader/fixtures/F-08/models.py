"""SPEC-17 F-08: FK-only mapped_column(ForeignKey(...)) — type inherited.

A column declared only via ForeignKey (no explicit type arg) inherits
the referenced column's type at mapper configuration time. Section 4:
``mapped_column(ForeignKey("parent.id"))`` with no type → integer
(inherited from the target column).
"""

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "parents"

    id: Mapped[int] = mapped_column(primary_key=True)


class Child(Base):
    __tablename__ = "children"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parents.id"))
