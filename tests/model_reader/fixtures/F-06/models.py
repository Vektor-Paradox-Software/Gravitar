"""SPEC-17 F-06: type_annotation_map custom mapping (str → String(50))."""

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {str: String(50)}


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]  # should resolve to varchar(50) via type_annotation_map
