"""D-CANCEL-03: the callback router must never lose a broker callback.

The IB API delivers every callback on one reader thread. The old transport
pushed them all onto a single shared queue, so a waiter draining positions
could consume — and discard — the ``orderStatus`` a cancellation was blocked
on. That turns a real cancellation into a false timeout and an UNKNOWN state.

These tests pin the lossless property directly, including the interleavings
that produced the defect. No IBKR connection, no orders.
"""

from __future__ import annotations

import threading

from trading_broker_ibkr.callback_router import (
    DIAGNOSTICS_STREAM,
    OPEN_ORDERS_STREAM,
    POSITIONS_STREAM,
    CallbackRouter,
    classify_error_stream,
    order_stream,
    request_stream,
)

TIMEOUT = 0.5


def test_streams_are_independent() -> None:
    router = CallbackRouter()
    router.publish(order_stream(2), "status", "Cancelled")
    router.publish(POSITIONS_STREAM, "position", "row")

    envelope = router.take(POSITIONS_STREAM, timeout=TIMEOUT)

    assert envelope is not None
    assert envelope.kind == "position"
    # The order status is untouched: a positions drain cannot eat it.
    assert router.pending(order_stream(2)) == 1


def test_order_status_arriving_during_positions_is_not_consumed() -> None:
    """The exact defect interleaving: orderStatus lands mid-positions drain."""
    router = CallbackRouter()
    router.publish(POSITIONS_STREAM, "position", "row-1")
    router.publish(order_stream(2), "status", "Cancelled")  # arrives mid-drain
    router.publish(POSITIONS_STREAM, "position_end", None)

    drained = []
    while True:
        envelope = router.take(POSITIONS_STREAM, timeout=TIMEOUT)
        assert envelope is not None
        if envelope.kind == "position_end":
            break
        drained.append(envelope)

    assert len(drained) == 1
    cancel = router.take(order_stream(2), timeout=TIMEOUT)
    assert cancel is not None
    assert cancel.payload == "Cancelled"


def test_cancel_confirmation_arriving_during_account_summary_survives() -> None:
    router = CallbackRouter()
    router.publish(request_stream(101), "account", ("DU", "NetLiquidation", "1"))
    router.publish(order_stream(5), "status", "Cancelled")
    router.publish(request_stream(101), "account_end", 101)

    assert router.take(request_stream(101), timeout=TIMEOUT) is not None
    assert router.take(request_stream(101), timeout=TIMEOUT) is not None
    survived = router.take(order_stream(5), timeout=TIMEOUT)
    assert survived is not None
    assert survived.payload == "Cancelled"


def test_informational_message_between_pending_cancel_and_cancelled() -> None:
    router = CallbackRouter()
    router.publish(order_stream(6), "status", "PendingCancel")
    router.publish(DIAGNOSTICS_STREAM, "error", (-1, 2104, "farm OK", ""))
    router.publish(order_stream(6), "status", "Cancelled")

    kinds = [
        router.take(order_stream(6), timeout=TIMEOUT),
        router.take(order_stream(6), timeout=TIMEOUT),
    ]
    assert [e.payload for e in kinds if e is not None] == ["PendingCancel", "Cancelled"]
    # The farm notice stayed observable rather than being consumed as an order event.
    assert router.pending(DIAGNOSTICS_STREAM) == 1


def test_two_order_ids_interleaved_never_cross_over() -> None:
    router = CallbackRouter()
    for status in ("Submitted", "PendingCancel", "Cancelled"):
        router.publish(order_stream(7), "status", f"7:{status}")
        router.publish(order_stream(8), "status", f"8:{status}")

    seven = [router.take(order_stream(7), timeout=TIMEOUT) for _ in range(3)]
    eight = [router.take(order_stream(8), timeout=TIMEOUT) for _ in range(3)]

    assert [e.payload for e in seven if e is not None] == [
        "7:Submitted",
        "7:PendingCancel",
        "7:Cancelled",
    ]
    assert [e.payload for e in eight if e is not None] == [
        "8:Submitted",
        "8:PendingCancel",
        "8:Cancelled",
    ]


def test_a_stale_ack_before_and_after_the_cancel_request_is_still_delivered() -> None:
    router = CallbackRouter()
    router.publish(order_stream(9), "status", "Submitted")  # before the cancel
    router.publish(order_stream(9), "status", "Submitted")  # after the cancel
    router.publish(order_stream(9), "status", "Cancelled")

    seen = [router.take(order_stream(9), timeout=TIMEOUT) for _ in range(3)]
    assert [e.payload for e in seen if e is not None] == [
        "Submitted",
        "Submitted",
        "Cancelled",
    ]


def test_a_waiter_is_woken_by_a_late_publish_without_a_false_timeout() -> None:
    """A callback published after the waiter blocked must still arrive."""
    router = CallbackRouter()
    result: list[object] = []

    def wait() -> None:
        envelope = router.take(order_stream(10), timeout=5.0)
        result.append(None if envelope is None else envelope.payload)

    waiter = threading.Thread(target=wait)
    waiter.start()
    router.publish(order_stream(10), "status", "Cancelled")
    waiter.join(timeout=5.0)

    assert not waiter.is_alive()
    assert result == ["Cancelled"]


def test_publishing_to_another_stream_does_not_wake_a_waiter_with_wrong_data() -> None:
    router = CallbackRouter()
    router.publish(POSITIONS_STREAM, "position", "row")
    assert router.take(order_stream(11), timeout=0.05) is None
    assert router.pending(POSITIONS_STREAM) == 1


def test_release_archives_late_callbacks_instead_of_dropping_them() -> None:
    router = CallbackRouter()
    router.publish(OPEN_ORDERS_STREAM, "open_order", "late-row")

    leftover = router.release(OPEN_ORDERS_STREAM)

    assert [e.payload for e in leftover] == ["late-row"]
    assert [e.payload for e in router.archived()] == ["late-row"]
    assert router.pending(OPEN_ORDERS_STREAM) == 0


def test_close_wakes_waiters_deterministically() -> None:
    router = CallbackRouter()
    router.close()
    assert router.take(order_stream(12), timeout=5.0) is None


def test_drain_takes_only_its_own_stream() -> None:
    router = CallbackRouter()
    router.publish(DIAGNOSTICS_STREAM, "error", "a")
    router.publish(DIAGNOSTICS_STREAM, "error", "b")
    router.publish(order_stream(13), "status", "Cancelled")

    assert len(router.drain(DIAGNOSTICS_STREAM)) == 2
    assert router.pending(order_stream(13)) == 1


def test_concurrent_publishers_lose_nothing() -> None:
    router = CallbackRouter()
    streams = [order_stream(i) for i in range(4)]

    def publish(stream: str) -> None:
        for index in range(50):
            router.publish(stream, "status", index)

    threads = [threading.Thread(target=publish, args=(s,)) for s in streams]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert [router.pending(s) for s in streams] == [50, 50, 50, 50]


# -- error attribution ------------------------------------------------------


def test_unattributable_errors_go_to_diagnostics() -> None:
    for request_id in (None, -1, 0):
        assert (
            classify_error_stream(request_id, order_ids={5}, request_ids={101})
            == DIAGNOSTICS_STREAM
        )


def test_an_id_we_never_issued_goes_to_diagnostics() -> None:
    assert (
        classify_error_stream(777, order_ids={5}, request_ids={101})
        == DIAGNOSTICS_STREAM
    )


def test_order_attribution_wins_over_request_attribution() -> None:
    """Order and request ids share a numeric space; the order is the risk."""
    assert (
        classify_error_stream(5, order_ids={5}, request_ids={5}) == order_stream(5)
    )


def test_request_errors_reach_the_request_stream() -> None:
    assert (
        classify_error_stream(101, order_ids={5}, request_ids={101})
        == request_stream(101)
    )


# == retired streams: a late callback cannot recreate a live waiter ==========


def test_a_callback_after_release_is_archived_not_re_buffered() -> None:
    """release() retires a stream; a later publish to it is archived, so it can
    never recreate a live-looking waiter deque."""
    router = CallbackRouter()
    stream = request_stream(9301)
    router.publish(stream, "pnl", 12.5)
    first = router.take(stream, timeout=TIMEOUT)
    assert first is not None and first.payload == 12.5

    router.release(stream)  # waiter done
    router.publish(stream, "pnl", 999.9)  # a late callback for the finished req

    assert router.pending(stream) == 0, "the retired stream was recreated"
    assert any(env.payload == 999.9 for env in router.archived()), (
        "the late callback must be archived, not lost"
    )


def test_a_new_request_stream_is_never_treated_as_retired() -> None:
    """reqIds are unique and monotonic, so a fresh request's stream is live."""
    router = CallbackRouter()
    first = request_stream(9301)
    router.publish(first, "pnl", 1.0)
    router.take(first, timeout=TIMEOUT)
    router.release(first)

    second = request_stream(9302)  # a genuinely new request
    router.publish(second, "pnl", 2.0)

    assert router.pending(second) == 1, "a new request's stream must stay live"
    taken = router.take(second, timeout=TIMEOUT)
    assert taken is not None and taken.payload == 2.0


def test_retired_stream_memory_is_bounded() -> None:
    """The retired set never grows without bound: old keys fall off, and a key
    that has aged out is treated as new again (still inert -- nothing waits)."""
    from trading_broker_ibkr.callback_router import RETIRED_STREAM_MEMORY

    router = CallbackRouter()
    for req_id in range(RETIRED_STREAM_MEMORY + 50):
        router.release(request_stream(req_id))

    # The very first key has aged out of the bounded window.
    assert request_stream(0) not in router._retired_set
    # A recent key is still remembered.
    assert request_stream(RETIRED_STREAM_MEMORY + 40) in router._retired_set
    assert len(router._retired) <= RETIRED_STREAM_MEMORY
