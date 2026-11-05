"""SPEC-17 F-02: bare Mapped[str] annotation → implicit column.

A ``Mapped[T]`` annotation with no RHS creates a column; the reader
that only looks at ``Column(...)``/``mapped_column(...)`` calls
silently drops it (SPEC-17 §2 finding 3; Prefect: 80 cases).
"""

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]  # implicit mapped_column; not null (not Optional)
