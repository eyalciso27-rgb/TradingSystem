"""Durable halt / kill-switch wiring for the runtime (A4).

The runtime must react to a halt an operator wrote AFTER it started, so this
never caches: every ``active_halts`` / ``entry_halt_active`` call opens a short
connection and replays the append-only emergency log via the persistence
``load_active_halts``. The result is converted to the application's
``HaltRecord`` and matched with the canonical ``match_halt`` gate, so the runtime
uses exactly the same deterministic halt semantics the execution path does.

Two consumers share this one source:

* the runtime's own per-tick gate calls ``entry_halt_active`` BEFORE it pulls
  any entry intent, so an operator halt blocks the very next entry attempt;
* the ``ExecutionService`` is constructed with ``active_halts`` as its ``halts``
  callable, so the mandatory pre-submit halt check re-reads the same durable
  store at the submission boundary.

A store failure is fail-closed by construction: ``entry_halt_active`` returns
True (an entry is blocked) when the halt state cannot be read, because a runtime
that cannot prove there is NO halt must not enter. ``active_halts`` re-raises so
the execution path's own fail-closed handling sees the failure rather than an
empty (falsely 'no halt') list.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from trading_application.emergency import EntryContext, HaltRecord, match_halt
from trading_contracts.enums import Environment
from trading_persistence.runtime_ops import read_active_halts

LOGGER = logging.getLogger("trading_execution_agent.runtime.halts")


class HaltStoreUnavailableError(RuntimeError):
    """The durable halt store could not be read (fail closed)."""


class DurableHaltSource:
    """Reads active halts from the durable emergency store, never cached."""

    def __init__(self, engine: object, environment: Environment) -> None:
        self._engine = engine
        self._environment = environment

    def active_halts(self) -> Sequence[HaltRecord]:
        """Every active halt for this environment, freshly replayed.

        Raises on a store failure rather than returning an empty list: an empty
        list means 'no halt', and inventing that from a failed read is the one
        thing a kill switch must never do.
        """
        try:
            active = read_active_halts(self._engine, self._environment)  # type: ignore[arg-type]
        except Exception as error:  # noqa: BLE001 - normalised, fail closed
            LOGGER.error(
                "durable halt store is unreadable; failing closed",
                exc_info=True,
            )
            raise HaltStoreUnavailableError(
                f"could not read the halt store: {type(error).__name__}"
            ) from error
        return tuple(
            HaltRecord(
                scope=halt.scope,
                environment=halt.environment,
                target_ref=halt.target_ref,
                reason_code=halt.reason_code,
                keep_protection=halt.keep_protection,
            )
            for halt in active
        )

    def entry_halt_active(self, context: EntryContext) -> bool:
        """Whether a NEW entry in ``context`` is halted right now (fail closed).

        A store failure returns True -- blocked -- because a runtime that cannot
        prove there is no halt must not enter.
        """
        try:
            halts = self.active_halts()
        except HaltStoreUnavailableError:
            return True
        return match_halt(halts, context) is not None


__all__ = ["DurableHaltSource", "HaltStoreUnavailableError"]
