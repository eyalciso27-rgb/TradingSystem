"""Prove the paper database is actually usable, from the host, before the probe.

``pg_isready`` run *inside* the container proves the PostgreSQL server is
accepting connections. It proves none of the things that actually stand between
the wrapper and a working probe:

* that ``127.0.0.1:<port>`` is reachable from the owner host at all -- the
  loopback publish is created seconds earlier and could have failed to bind;
* that the generated ``DATABASE_URL`` parses in the consuming process;
* that ``PAPER_POSTGRES_USER`` / ``PAPER_POSTGRES_PASSWORD`` authenticate.

The last one is the trap. The PostgreSQL image only initialises credentials on
an EMPTY data directory: recreating the container against an existing volume
does not reset the role's password. So the environment can hold one password
while the database still expects an older one, ``pg_isready`` passes happily --
it does not authenticate -- and the first genuine failure arrives inside the
probe, after two IBKR sessions are open. That is exactly the failure class this
whole round exists to move earlier.

So: connect for real, with the exact URL the probe will use, run ``SELECT 1``,
dispose. Same SQLAlchemy/psycopg stack the application uses, because a preflight
that proves a *different* stack works proves nothing.

Output is a stable code and an exception type. Never the URL, never the
password, never the driver's message -- driver text routinely embeds the DSN.
"""

from __future__ import annotations

import os
import sys

DATABASE_URL_ENV = "DATABASE_URL"

#: Stable owner-visible codes. Diagnosable without printing anything we do not
#: control.
OK = "PAPER_DB_PREFLIGHT_OK"
NO_URL = "PAPER_DB_PREFLIGHT_NO_URL"
UNUSABLE = "PAPER_DB_PREFLIGHT_FAILED"

EXIT_OK = 0
EXIT_FAILED = 1


def check(database_url: str) -> tuple[int, str]:
    """Connect, ``SELECT 1``, dispose. Returns (exit code, owner-safe line)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
    except Exception as error:  # noqa: BLE001 - the message would carry the DSN
        return EXIT_FAILED, f"{UNUSABLE}: {type(error).__name__} (url parse)"

    engine = None
    try:
        # Short timeouts: this is a preflight, and a hang here is the same
        # problem as a failure -- the probe must not start either way.
        engine = create_engine(
            url,
            connect_args={"connect_timeout": 10},
            pool_pre_ping=False,
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
    except Exception as error:  # noqa: BLE001 - driver text embeds the DSN
        return EXIT_FAILED, f"{UNUSABLE}: {type(error).__name__}"
    finally:
        if engine is not None:
            engine.dispose()
    return EXIT_OK, OK


def main(argv: list[str] | None = None) -> int:
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        print(NO_URL)
        return EXIT_FAILED
    status, line = check(database_url)
    print(line)
    return status


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv[1:]))
