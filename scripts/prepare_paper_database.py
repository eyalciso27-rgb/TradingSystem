"""Bring the paper database to the canonical schema, before any broker socket.

Run by ``scripts/run_execution_probe_paper.sh`` after the authenticated
host-side check and before the probe. It uses the exact ``DATABASE_URL`` the
probe will use.

Why the authenticated ``SELECT 1`` was not enough: a PostgreSQL cluster created
moments ago authenticates and answers ``SELECT 1`` while holding none of the
application tables. On the owner host -- no container, no volume, no network,
no Compose project -- the next run is a true first initialisation, so without
this the first evidence of an empty schema would come from the durable audit
with two IBKR sessions already open.

Output is a stable code, a revision and an exception type. Never the URL, never
the password, never a traceback: Alembic and SQLAlchemy exception text embeds
the DSN.
"""

from __future__ import annotations

import os
import sys

from trading_persistence.schema_preparation import PreparationResult, prepare_database

ENV_DATABASE_URL = "DATABASE_URL"

OK = "PAPER_DB_SCHEMA_OK"
NO_URL = "PAPER_DB_SCHEMA_NO_URL"
FAILED = "PAPER_DB_SCHEMA_FAILED"


def _describe(result: PreparationResult) -> str:
    if result.first_run:
        state = "initialised (first run)"
    elif result.upgraded:
        state = f"upgraded from {result.previous_revision}"
    else:
        state = "already at head"
    return f"{OK}: schema {state}; revision {result.revision}"


def main(argv: list[str] | None = None) -> int:
    del argv
    if not os.environ.get(ENV_DATABASE_URL, "").strip():
        sys.stderr.write(
            f"{NO_URL}: {ENV_DATABASE_URL} is not set; the wrapper builds it"
            " before calling this\n"
        )
        return 2
    try:
        result = prepare_database()
    except Exception as error:  # noqa: BLE001 - the boundary is the point
        # Type only. Alembic and psycopg messages routinely carry the DSN, and
        # a traceback carries the whole call stack with it.
        sys.stderr.write(f"{FAILED}: {type(error).__name__}\n")
        return 2
    sys.stdout.write(_describe(result) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
