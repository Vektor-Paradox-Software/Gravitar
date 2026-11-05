"""A models package using SQLAlchemy 2.0-style DeclarativeBase + mapped_column."""

from sqlalchemy import Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    total_cents: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
