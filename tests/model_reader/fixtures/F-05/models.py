"""SPEC-17 F-05: Annotated[int, mapped_column(primary_key=True)] alias.

The annotation itself carries the column config. This is the
`intpk`-style idiom in 2.0 projects — see SPEC-17 Section 4 and
Airflow's models.
"""

from typing import Annotated

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

intpk = Annotated[int, mapped_column(primary_key=True)]


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[intpk]
    name: Mapped[str]
