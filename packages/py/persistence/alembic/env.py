"""Alembic environment (IMP-0201).

Target metadata is the core schema in ``trading_persistence.models``. The URL
comes from ``DATABASE_URL`` (via trading_persistence.db); no URL lives here.
Only online migrations are supported (a real PostgreSQL connection).
"""

from alembic import context
from trading_persistence.db import create_sync_engine
from trading_persistence.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_sync_engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():  # pragma: no cover - offline mode is not used
    raise RuntimeError("offline migrations are not supported; set DATABASE_URL")
run_migrations_online()
