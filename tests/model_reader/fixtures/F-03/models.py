"""SPEC-17 F-03: Mapped[Optional[str]], str | None, Union[str, None].

Three equivalent nullability expressions. Each should resolve to
nullable=True on both sides.
"""

from __future__ import annotations

from typing import Optional, Union

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    a: Mapped[Optional[str]]  # noqa: UP007, UP045 — testing both annotation styles
    b: Mapped[str | None]
    c: Mapped[Union[str, None]]  # noqa: UP007, UP045
