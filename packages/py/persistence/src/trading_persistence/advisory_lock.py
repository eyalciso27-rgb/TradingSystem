"""Session-level PostgreSQL advisory lock for single-instance ownership.

The autonomous runtime must be the ONLY process doing trading work. A
session-level ``pg_advisory_lock`` is the smallest durable primitive that gives
that guarantee on the MVP's single-host Postgres: the lock is held by a database
SESSION, so it is released automatically the instant that session ends -- a
clean shutdown, a crash, a killed process or a dropped network connection all
free it, and a second instance can then acquire it. There is no lease to renew
and no stale lock to reap.

Two properties are enforced deliberately:

* **acquisition is non-blocking** -- ``try_acquire`` uses ``pg_try_advisory_lock``
  and returns False immediately if another session holds the lock, so a second
  instance refuses to start rather than hanging;
* **ownership is re-verified, never assumed** -- ``is_held`` pings the owning
  connection. A session-level advisory lock is released only by an explicit
  unlock or by the session ending, so an alive owning session that acquired and
  has not released still holds the lock. If the connection has dropped, the ping
  raises and ``is_held`` returns False: the process learns it lost ownership
  instead of continuing to believe it still owns execution.

This holds its OWN dedicated connection for the lock's whole lifetime, separate
from any pooled connection the application uses for queries, precisely so the
lock's fate is tied to nothing but the runtime's own liveness.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

LOGGER = logging.getLogger("trading_persistence.advisory_lock")

#: The canonical ownership key namespace. A stable string per protected
#: resource, hashed to the signed 64-bit integer ``pg_advisory_lock`` wants.
PAPER_RUNTIME_LOCK_NAME = "trading:autonomous-paper-runtime:PAPER"


def advisory_key(name: str) -> int:
    """Deterministically map a name to a signed 64-bit advisory-lock key.

    Postgres advisory locks are keyed by a ``bigint``. A blake2b digest gives a
    stable key from a human-readable name, folded into the signed 64-bit range
    so the same name always locks the same key across processes and restarts.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    unsigned = int.from_bytes(digest, "big", signed=False)
    # Fold into the signed 64-bit range pg_advisory_lock accepts.
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


class AdvisoryLockError(RuntimeError):
    """The advisory lock could not be operated (connection/driver failure)."""


class AdvisoryLock:
    """A single session-level advisory lock over one dedicated connection."""

    def __init__(self, engine: Engine, *, name: str = PAPER_RUNTIME_LOCK_NAME) -> None:
        self._engine = engine
        self._name = name
        self._key = advisory_key(name)
        self._connection: Connection | None = None
        self._acquired = False

    @property
    def key(self) -> int:
        return self._key

    def try_acquire(self) -> bool:
        """Attempt to take the lock without blocking. Idempotent while held.

        Opens a dedicated connection and calls ``pg_try_advisory_lock``. Returns
        True if this process now holds the lock, False if another session does.
        A failure to even reach the database is fail-closed: it raises, and the
        caller must treat "could not prove ownership" as "do not run".
        """
        if self._acquired:
            return True
        try:
            connection = self._engine.connect()
        except Exception as error:  # noqa: BLE001 - normalised, fail closed
            raise AdvisoryLockError(
                f"could not open a connection to acquire the ownership lock:"
                f" {type(error).__name__}"
            ) from error
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
                ).scalar()
            )
        except Exception as error:  # noqa: BLE001 - normalised, fail closed
            connection.close()
            raise AdvisoryLockError(
                f"could not acquire the ownership lock: {type(error).__name__}"
            ) from error
        if not acquired:
            # Another instance owns execution. Release the connection; we hold
            # nothing.
            connection.close()
            return False
        self._connection = connection
        self._acquired = True
        LOGGER.info("acquired single-instance ownership lock %s", self._name)
        return True

    def is_held(self) -> bool:
        """Re-verify ownership against the live session. Never assumes.

        Returns True only when this process acquired the lock AND its owning
        session is still alive. A dropped connection -- which would have released
        the lock server-side and let another instance take it -- makes the ping
        raise, and this returns False so the runtime stops believing it owns
        execution.
        """
        if not self._acquired or self._connection is None:
            return False
        try:
            self._connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - a dead session is a lost lock
            LOGGER.warning(
                "ownership lock connection is no longer usable; treating"
                " ownership as LOST"
            )
            self._acquired = False
            return False
        return True

    def release(self) -> None:
        """Release the lock and close the owning connection. Safe to repeat."""
        connection = self._connection
        self._connection = None
        was_acquired = self._acquired
        self._acquired = False
        if connection is None:
            return
        try:
            if was_acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                )
        except Exception:  # noqa: BLE001 - closing the session frees it anyway
            LOGGER.warning("explicit advisory unlock failed; closing the session")
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - best effort
                LOGGER.warning("could not close the ownership lock connection")
        LOGGER.info("released single-instance ownership lock %s", self._name)


def advisory_lock_held(engine: Engine, *, name: str = PAPER_RUNTIME_LOCK_NAME) -> bool:
    """Read-only probe: is SOME other session holding this lock right now?

    Diagnostic only (the status CLI): it briefly ``pg_try_advisory_lock``s on a
    throwaway connection and, if it succeeds, unlocks immediately and reports the
    lock as free. A failure to acquire means another session holds it. The hold,
    when it happens at all, lasts microseconds and is always released. This is
    never used to gate trading work -- only ``AdvisoryLock`` does that.
    """
    with engine.connect() as connection:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": advisory_key(name)}
            ).scalar()
        )
        if acquired:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": advisory_key(name)}
            )
            return False
        return True


__all__ = [
    "PAPER_RUNTIME_LOCK_NAME",
    "AdvisoryLock",
    "AdvisoryLockError",
    "advisory_key",
    "advisory_lock_held",
]
