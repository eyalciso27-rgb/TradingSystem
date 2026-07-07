"""Correlation context propagated into every operational log record.

The context travels with the current task/thread via ``contextvars`` so that
services bind identifiers once (per message, per request, per job) and every
log record emitted underneath carries them automatically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Final

# Canonical per-record correlation fields (Technology spec section 15:
# correlation_id, decision_id and order_intent_id enable end-to-end traces;
# high-cardinality values live in the log body, never in labels).
CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "event_id",
    "correlation_id",
    "causation_id",
    "source",
    "symbol",
    "strategy_id",
    "decision_id",
    "order_intent_id",
)

_log_context: ContextVar[MappingProxyType[str, str]] = ContextVar(
    "trading_log_context", default=MappingProxyType({})
)


def new_correlation_id() -> str:
    """Return a fresh correlation identifier."""
    return str(uuid.uuid4())


def current_context() -> MappingProxyType[str, str]:
    """Return the correlation fields bound to the current task (read-only)."""
    return _log_context.get()


@contextmanager
def bind_log_context(**fields: str | None) -> Iterator[None]:
    """Bind correlation fields for the duration of the ``with`` block.

    Nested binds layer on top of the surrounding context; unknown field
    names are rejected so the record shape stays canonical. ``None`` values
    are ignored (field stays unset / inherited).
    """
    unknown = set(fields) - set(CONTEXT_FIELDS)
    if unknown:
        raise ValueError(f"unknown log context fields: {sorted(unknown)}")
    merged = dict(_log_context.get())
    for key, value in fields.items():
        if value is not None:
            merged[key] = value
    token = _log_context.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _log_context.reset(token)
