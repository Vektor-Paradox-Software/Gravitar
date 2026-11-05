"""SPEC-17 F-07: Mapped[PyEnum] uses MEMBER NAMES, not values.

The SA 2.1 semantic the SPEC-17 Section 4 table pins: ``Color.RED =
"r"`` resolves to ``enum(RED, GREEN, BLUE)``, not ``enum(r, g, b)``.
Also Mapped[Literal[...]] which resolves from literal values.
"""

from enum import Enum
from typing import Literal

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Color(Enum):
    RED = "r"
    GREEN = "g"
    BLUE = "b"


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    color: Mapped[Color]
    tier: Mapped[Literal["x", "y"]]
