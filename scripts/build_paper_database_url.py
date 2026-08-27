"""Build the paper DATABASE_URL correctly, from components in the environment.

The wrapper used to interpolate the credentials straight into a string:

    postgresql+psycopg://${PAPER_POSTGRES_USER}:${PAPER_POSTGRES_PASSWORD}@...

Raw credentials are not valid URL components. A perfectly legal PostgreSQL
password containing ``@``, ``:``, ``/``, ``#``, ``%`` or a space produces a URL
that is malformed or -- worse -- well-formed and parsed as something else: an
``@`` splits the authority early, a ``/`` ends it, a ``%`` starts a percent
escape. The right answer is to encode the components, not to constrain the
secret so that string concatenation happens to work.

``URL.create`` does that encoding itself, and it is the same SQLAlchemy the
application uses to consume the result, so the round trip is exact by
construction rather than by our own escaping.

The URL is printed to stdout for the caller to capture. It is never logged.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy.engine import URL

#: The components, and the environment variables the paper stack already uses.
ENV_USER = "PAPER_POSTGRES_USER"
ENV_PASSWORD = "PAPER_POSTGRES_PASSWORD"
ENV_DB = "PAPER_POSTGRES_DB"
ENV_HOST = "PAPER_PROBE_DB_HOST"
ENV_PORT = "PAPER_PROBE_DB_PORT"

DEFAULT_DB = "trading_paper"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 55432
DRIVER = "postgresql+psycopg"


def build_url(environ: dict[str, str] | None = None) -> URL:
    """The paper database URL, with every component safely encoded."""
    env = environ if environ is not None else dict(os.environ)
    username = env.get(ENV_USER, "")
    password = env.get(ENV_PASSWORD, "")
    if not username or not password:
        raise SystemExit(
            f"{ENV_USER} and {ENV_PASSWORD} must be provided from the PAPER"
            " secret store. The probe wrapper loads them itself from"
            " $SECRETS_ROOT/PAPER/postgres/*; if they are not there yet, run:"
            " bash scripts/bootstrap_paper_postgres.sh"
        )
    raw_port = env.get(ENV_PORT, str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError as error:
        raise SystemExit(f"{ENV_PORT} is not a port number") from error
    return URL.create(
        DRIVER,
        username=username,
        password=password,
        host=env.get(ENV_HOST, DEFAULT_HOST) or DEFAULT_HOST,
        port=port,
        database=env.get(ENV_DB, DEFAULT_DB) or DEFAULT_DB,
    )


def main() -> int:
    # ``render_as_string(hide_password=False)`` is the only intentional way the
    # real password leaves this process, and it goes to stdout for the caller to
    # capture into an environment variable -- never to a log.
    sys.stdout.write(build_url().render_as_string(hide_password=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
