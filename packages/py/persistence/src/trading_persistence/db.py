"""Database engine helpers (IMP-0201).

The connection URL comes only from the ``DATABASE_URL`` environment variable
(or an explicit argument in tests). There is no hard-coded production URL and
no credentials in code. PostgreSQL is driven by psycopg 3 (``postgresql+psycopg``).
"""

import os

from sqlalchemy import Engine, create_engine

DATABASE_URL_ENV = "DATABASE_URL"


class DatabaseConfigError(RuntimeError):
    """The database URL is not configured."""


def get_database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise DatabaseConfigError(
            f"{DATABASE_URL_ENV} is not set; provide a PostgreSQL URL "
            "(e.g. postgresql+psycopg://user:pass@host:5432/db)"
        )
    return url


def normalize_url(url: str) -> str:
    """Ensure the psycopg driver is used for plain 'postgresql://' URLs."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def create_sync_engine(url: str | None = None) -> Engine:
    return create_engine(normalize_url(url or get_database_url()), future=True)
