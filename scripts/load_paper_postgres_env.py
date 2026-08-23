"""Emit the PAPER PostgreSQL credentials for the probe wrapper to capture.

    username\n
    database\n
    password\n

Three lines, fixed order, on stdout, for exactly one caller:
``scripts/run_execution_probe_paper.sh``. The wrapper exports them into its own
process so that ``docker compose`` and ``build_paper_database_url.py`` see them,
and so the owner never has to type, export or even see the password.

This is the same intentional, single-purpose disclosure that
``build_paper_database_url.py`` already makes with the full DSN: to stdout, for
capture into a variable, never to a log. Values are never echoed by the wrapper,
and ``set -x`` appears nowhere in it.

An environment override, if present, wins -- that is the documented exceptional
path for an owner who deliberately runs against a database the secret store does
not describe. The ordinary path uses the store and requires no exports at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from trading_config.paper_postgres import PaperPostgresError, load

ENV_SECRETS_ROOT = "SECRETS_ROOT"

#: The exceptional override, kept for continuity with the previous contract.
ENV_USER_OVERRIDE = "PAPER_POSTGRES_USER"
ENV_PASSWORD_OVERRIDE = "PAPER_POSTGRES_PASSWORD"
ENV_DB_OVERRIDE = "PAPER_POSTGRES_DB"

FAILED = "PAPER_POSTGRES_LOAD_FAILED"


def resolve(environ: dict[str, str]) -> tuple[str, str, str]:
    """``(username, database, password)`` from the environment, else the store.

    The override is all-or-nothing on the credential pair. Taking a username
    from the environment and a password from the store would pair two values
    that were never chosen together, and PostgreSQL would reject the pair after
    the probe had already been told the database was fine.
    """
    user_override = environ.get(ENV_USER_OVERRIDE, "").strip()
    password_override = environ.get(ENV_PASSWORD_OVERRIDE, "")
    if user_override and password_override.strip():
        database = environ.get(ENV_DB_OVERRIDE, "").strip() or "trading_paper"
        return user_override, database, password_override.strip()
    if user_override or password_override.strip():
        raise PaperPostgresError(
            f"{ENV_USER_OVERRIDE} and {ENV_PASSWORD_OVERRIDE} must be set"
            " together or not at all; one without the other would pair values"
            " that were never chosen together. Unset both to use the canonical"
            " secret store."
        )
    raw_root = environ.get(ENV_SECRETS_ROOT, "").strip()
    if not raw_root:
        raise PaperPostgresError(
            f"{ENV_SECRETS_ROOT} must point at the canonical secret store"
        )
    credentials = load(Path(raw_root))
    return (
        credentials.username,
        credentials.database,
        credentials.password.reveal().strip(),
    )


def _reject_embedded_newlines(values: tuple[str, str, str]) -> None:
    """The transport is three lines. A value containing one would desynchronise
    it, and the caller would export a fragment of a password as a username.

    Generated passwords never contain one. An operator-supplied credential
    could, so this fails closed rather than assuming.
    """
    for label, value in zip(("username", "database", "password"), values, strict=True):
        if chr(10) in value or chr(13) in value:
            raise PaperPostgresError(
                f"the PAPER PostgreSQL {label} contains a line break, which this"
                " transport cannot carry. Store a single-line value."
            )


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        username, database, password = resolve(dict(os.environ))
        _reject_embedded_newlines((username, database, password))
    except PaperPostgresError as error:
        sys.stderr.write(f"{FAILED}: {error}\n")
        return 2
    # stdout, for capture. The password is the third line so that a truncated
    # read cannot silently yield a username where a password was expected.
    sys.stdout.write(f"{username}\n{database}\n{password}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
