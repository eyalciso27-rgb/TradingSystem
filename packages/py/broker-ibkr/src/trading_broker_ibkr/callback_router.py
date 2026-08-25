"""Lossless callback router for the IBKR API reader thread (D-CANCEL-03).

Authority: Integration_Specification_v0.6 section 9.1 (adapter contract), 9.4
(append-only, idempotent broker events); Decision_Contract 12.5 (UNKNOWN is
resolved only by reconciliation, never by guessing).

**Why this exists.** The IB API delivers every callback — order status, error,
open order, positions, account summary, contract details — on ONE reader
thread. The original transport pushed all of them onto a single shared
``queue.Queue`` and every waiter popped from it. That is lossy by
construction: a waiter draining positions could consume (and discard) the
``orderStatus`` that a cancellation was blocked on, producing a false timeout
and an UNKNOWN cancellation while the order was, in fact, cancelled.

``CallbackRouter`` replaces the shared queue with independent, per-key
streams. A consumer only ever removes envelopes from ITS OWN stream, so:

- no consumer can discard a callback intended for another operation;
- a callback arriving while an unrelated request is in flight stays buffered
  for its real waiter (including one that has not started waiting yet);
- releasing a finished waiter's stream is explicit and returns whatever was
  still buffered.

**Loss guarantees, precisely.** Callbacks for an ACTIVE waiter stream are
lossless: nothing is ever overwritten or dropped while a request is in flight.
A callback that arrives for a RETIRED stream -- one whose waiter has already
been released -- is moved to a BOUNDED diagnostic archive
(``ARCHIVE_MEMORY`` entries) so it stays inspectable instead of recreating a
live-looking waiter. That archive can evict its oldest entries once full; that
eviction is diagnostic-only and can never affect an active request or a
decision, because a retired stream has, by definition, no waiter.

The router is deliberately transport-agnostic and free of any ``ibapi``
import, so the demultiplexing logic is fully unit-testable without a broker.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

#: Stream carrying every open-order snapshot callback (``openOrder`` /
#: ``openOrderEnd``), for both the global and the same-client requests.
OPEN_ORDERS_STREAM = "open-orders"

#: Stream carrying account-position callbacks (``position`` / ``positionEnd``),
#: which the IB API delivers without any request id.
POSITIONS_STREAM = "positions"

#: Stream carrying connectivity/farm notices and any error that cannot be
#: attributed to a specific in-flight order or request. Never consumed by an
#: operation waiter — it exists so diagnostics stay observable.
DIAGNOSTICS_STREAM = "diagnostics"

#: Most-recently-retired stream keys remembered, so a callback that arrives
#: shortly after its waiter was released is archived rather than recreating a
#: live-looking stream. Bounded: the router never grows an unbounded registry
#: of every request id it has ever seen. A late callback beyond this window is
#: rare (it would have to arrive after this many later requests) and, lacking a
#: waiter, is inert either way.
RETIRED_STREAM_MEMORY = 1024

#: Cap on archived late callbacks kept for inspection. Generous but bounded.
ARCHIVE_MEMORY = 4096


def order_stream(broker_order_id: int) -> str:
    """Stream key for callbacks scoped to one broker order id."""
    return f"order:{broker_order_id}"


def request_stream(request_id: int) -> str:
    """Stream key for callbacks scoped to one request id."""
    return f"request:{request_id}"


def classify_error_stream(
    request_id: int | None,
    *,
    order_ids: AbstractSet[int],
    request_ids: AbstractSet[int],
) -> str:
    """Decide which stream an ``error()`` callback belongs to.

    IB order ids and request ids share one numeric space, so an error id is
    attributed ONLY against the ids this transport actually issued. An
    unattributable error (``reqId`` of -1/0/None, or an id we never issued)
    goes to diagnostics — it is never allowed to fail an unrelated operation.
    Order attribution wins over request attribution because an order-scoped
    error is the safety-critical one.
    """
    if request_id is None or request_id <= 0:
        return DIAGNOSTICS_STREAM
    if request_id in order_ids:
        return order_stream(request_id)
    if request_id in request_ids:
        return request_stream(request_id)
    return DIAGNOSTICS_STREAM


@dataclass(frozen=True, slots=True)
class CallbackEnvelope:
    """One broker callback, tagged with the stream that owns it."""

    stream: str
    kind: str
    payload: object


class CallbackRouter:
    """Thread-safe demultiplexer of broker callbacks.

    One publisher (the API reader thread) and one waiter per stream. An ACTIVE
    stream is an unbounded FIFO: nothing on it is overwritten or dropped, and
    only ``release`` / ``drain`` remove its envelopes. A callback for a RETIRED
    stream is diverted to a bounded diagnostic archive rather than buffered, so
    a late callback cannot recreate a waiter for a request that is already over.
    """

    def __init__(self, *, monotonic: Callable[[], float] | None = None) -> None:
        self._condition = threading.Condition()
        self._streams: dict[str, deque[CallbackEnvelope]] = {}
        # Both bounded, so the router's memory is lifecycle-owned rather than
        # growing for the life of the process. Archived late callbacks stay
        # inspectable up to a generous cap; the very oldest fall off first.
        self._archived: deque[CallbackEnvelope] = deque(maxlen=ARCHIVE_MEMORY)
        # Streams that have been released. A callback that arrives AFTER its
        # waiter finished must NOT recreate a live-looking stream (publish used
        # ``setdefault``), so it is archived instead. Bounded and in a fixed
        # order, with a mirror set for O(1) lookup kept in step with the deque.
        self._retired: deque[str] = deque(maxlen=RETIRED_STREAM_MEMORY)
        self._retired_set: set[str] = set()
        self._monotonic = monotonic or time.monotonic
        self._closed = False

    def _retire(self, stream: str) -> None:
        """Record ``stream`` as retired, evicting the oldest if the cap is hit.

        Called under the condition lock. reqIds are unique and monotonic within
        a session, so a retired key is never reused by a later request -- a new
        request always has a new stream key that is not in this set.
        """
        if stream in self._retired_set:
            return
        if len(self._retired) == self._retired.maxlen:
            evicted = self._retired[0]  # about to be pushed out by the append
            self._retired_set.discard(evicted)
        self._retired.append(stream)
        self._retired_set.add(stream)

    # -- producer side -------------------------------------------------------
    def publish(self, stream: str, kind: str, payload: object) -> None:
        """Append one callback to its stream and wake any waiter.

        A callback for a RETIRED stream -- one whose waiter has already been
        released -- is archived, never buffered, so it can neither recreate a
        live-looking waiter deque nor be mistaken for a current request's data.
        """
        with self._condition:
            envelope = CallbackEnvelope(stream=stream, kind=kind, payload=payload)
            if stream in self._retired_set:
                self._archived.append(envelope)
                return
            self._streams.setdefault(stream, deque()).append(envelope)
            self._condition.notify_all()

    # -- consumer side -------------------------------------------------------
    def take(self, stream: str, *, timeout: float) -> CallbackEnvelope | None:
        """Pop the next envelope from ``stream``; ``None`` on timeout/close.

        Blocks only on the requested stream. Callbacks published to any other
        stream while this call is waiting are untouched.
        """
        deadline = self._monotonic() + timeout
        with self._condition:
            while True:
                pending = self._streams.get(stream)
                if pending:
                    return pending.popleft()
                if self._closed:
                    return None
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def drain(self, stream: str) -> tuple[CallbackEnvelope, ...]:
        """Remove and return everything currently buffered on ``stream``."""
        with self._condition:
            pending = self._streams.get(stream)
            if not pending:
                return ()
            drained = tuple(pending)
            pending.clear()
            return drained

    def pending(self, stream: str) -> int:
        """How many envelopes are buffered on ``stream`` (diagnostics only)."""
        with self._condition:
            return len(self._streams.get(stream, ()))

    def release(self, stream: str) -> tuple[CallbackEnvelope, ...]:
        """Retire a finished waiter's stream deterministically.

        Anything still buffered is ARCHIVED (and returned) rather than
        discarded, so a broker event that arrived just after the waiter
        finished remains inspectable instead of vanishing.
        """
        with self._condition:
            pending = self._streams.pop(stream, None)
            self._retire(stream)
            if not pending:
                return ()
            leftover = tuple(pending)
            self._archived.extend(leftover)
            return leftover

    def archived(self) -> tuple[CallbackEnvelope, ...]:
        """Late callbacks for retired streams, kept in a BOUNDED archive.

        Retained for inspection up to ``ARCHIVE_MEMORY`` entries; once full, the
        oldest are evicted. This is diagnostic only -- a retired stream has no
        waiter, so an evicted entry can never affect a request or a decision.
        """
        with self._condition:
            return tuple(self._archived)

    def close(self) -> None:
        """Wake every waiter; subsequent ``take`` calls return ``None``."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()


__all__ = [
    "DIAGNOSTICS_STREAM",
    "OPEN_ORDERS_STREAM",
    "POSITIONS_STREAM",
    "CallbackEnvelope",
    "CallbackRouter",
    "classify_error_stream",
    "order_stream",
    "request_stream",
]
