"""Shared SQLAlchemy metadata for current legal-corpus and contract tables."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """All mapped tables are registered here for Alembic."""
