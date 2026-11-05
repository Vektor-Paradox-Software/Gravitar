"""Mixin module imported from the main models package."""

from sqlalchemy import Column, DateTime


class Timestamped:
    created_at = Column(DateTime(), nullable=False)
    updated_at = Column(DateTime(), nullable=True)
