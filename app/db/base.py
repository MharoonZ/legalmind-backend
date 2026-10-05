"""Shared SQLAlchemy metadata for legacy and new corpus tables."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """All mapped tables are registered here for Alembic."""
