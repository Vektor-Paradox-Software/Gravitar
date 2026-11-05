"""No models — this fixture demonstrates a broken revision chain, not a model/migration
disagreement."""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
