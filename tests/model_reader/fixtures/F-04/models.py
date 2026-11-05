"""SPEC-17 F-04: explicit nullable overrides annotation, both ways.

- ``Mapped[str]`` + ``nullable=True`` → nullable (explicit wins over
  annotation).
- ``Mapped[Optional[str]]`` + ``nullable=False`` → not null.
"""

from typing import Optional

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    overridden_true: Mapped[str] = mapped_column(nullable=True)
    overridden_false: Mapped[Optional[str]] = mapped_column(nullable=False)  # noqa: UP007, UP045
