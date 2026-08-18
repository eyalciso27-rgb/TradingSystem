"""D-CLOCK-01: the real adapter must stamp evidence with the real UTC clock.

A REAL owner-run on the host at **2026-07-30T17:05:22Z** received adapter
timestamps beginning at **2026-07-10T12:00:01Z**, incrementing one second per
call. ``IbkrPaperAdapter.__init__`` defaulted to a deterministic TEST clock:

    self._now = clock or _deterministic_clock(datetime(2026, 7, 10, 12, 0, tzinfo=UTC))

so every account snapshot, receipt, broker event, reconciliation result and
health check carried a fabricated timestamp ~20 days stale — evidence that
could not be correlated with the Gateway log, with the harness's own UTC
timestamps, or with reality.

These tests pin the production default. They use bounded before/after
comparisons against real UTC rather than exact equality, so they cannot flake.
No IBKR connection and no order: the deterministic fake transport only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from tests.broker.test_ibkr_paper_adapter import (
    ALIASES,
    FakePaperTransport,
    deterministic_clock,
    make_intent,
)
from trading_broker_core.port import BrokerErrorCode, BrokerPortError, CancelOrderRequest
from trading_broker_ibkr.paper_adapter import IbkrPaperAdapter, utc_now

#: The frozen default that shipped in production and must never return.
STALE_DEFAULT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
#: Generous bound: a test machine may be slow, but never 20 days slow.
MAX_SKEW = timedelta(minutes=5)


def production_adapter(
    transport: FakePaperTransport | None = None,
) -> IbkrPaperAdapter:
    """An adapter built exactly as production builds it: NO injected clock."""
    fake = transport if transport is not None else FakePaperTransport()
    return IbkrPaperAdapter(lambda: fake, account_aliases=dict(ALIASES))


def assert_is_current_utc(stamp: datetime, *, before: datetime, after: datetime) -> None:
    """Bounded assertion: `stamp` is tz-aware UTC inside the observed window."""
    assert stamp.tzinfo is not None, "evidence timestamps must be timezone-aware"
    assert stamp.utcoffset() == timedelta(0), "evidence timestamps must be UTC"
    assert before - MAX_SKEW <= stamp <= after + MAX_SKEW, (
        f"{stamp.isoformat()} is not the current UTC time"
    )
    # The exact regression: never the frozen test date again.
    assert abs(stamp - STALE_DEFAULT) > timedelta(days=1), (
        "the deterministic 2026-07-10 test clock is back in production"
    )


# -- the production default --------------------------------------------------


def test_the_default_clock_returns_current_utc() -> None:
    before = datetime.now(tz=UTC)
    adapter = production_adapter()
    stamp = adapter._now()
    after = datetime.now(tz=UTC)

    assert_is_current_utc(stamp, before=before, after=after)


def test_the_default_resolves_through_utc_now_on_every_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must CALL the clock source per reading, not capture one.

    `second >= first` is not enough: an implementation that captured
    `datetime.now(tz=UTC)` once during construction and returned that same
    instant forever would satisfy it — the value would be current, would not
    equal the old 2026-07-10 default, and would be trivially non-decreasing.

    So the production source is monkeypatched to hand out two DISTINCT
    controlled timestamps, the adapter is built with NO clock argument, and the
    two readings must be exactly those two values. That proves:

    1. the default resolves through the production `utc_now` (a hard-coded
       `datetime.now(...)` inline would ignore the patch and fail);
    2. the source is invoked once per reading (the call counter);
    3. nothing is captured at import or construction time (a captured instant
       would repeat the first value);

    ...with no real sleep and no timing race anywhere.
    """
    import trading_broker_ibkr.paper_adapter as adapter_module

    first_stamp = datetime(2026, 7, 30, 17, 5, 22, tzinfo=UTC)
    second_stamp = datetime(2026, 7, 30, 17, 9, 44, tzinfo=UTC)
    readings = iter((first_stamp, second_stamp))
    calls = {"count": 0}

    def fake_utc_now() -> datetime:
        calls["count"] += 1
        return next(readings)

    # Patched BEFORE construction: the adapter must pick the source up from the
    # module, not from a value frozen at import time.
    monkeypatch.setattr(adapter_module, "utc_now", fake_utc_now)
    adapter = production_adapter()

    assert calls["count"] == 0, "construction must not read the clock"
    assert adapter._now() == first_stamp
    assert calls["count"] == 1
    assert adapter._now() == second_stamp
    assert calls["count"] == 2, "each reading must invoke the source separately"


def test_a_clock_captured_once_would_fail_this_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the guard: the capture-once defect must actually be detectable.

    Builds an adapter whose clock captures one instant (the defect shape the
    weaker `second >= first` assertion could not catch) and shows the readings
    repeat — which the assertion above would reject.
    """
    captured = datetime(2026, 7, 30, 17, 5, 22, tzinfo=UTC)
    adapter = IbkrPaperAdapter(
        FakePaperTransport,
        account_aliases=dict(ALIASES),
        clock=lambda: captured,  # a "current" but frozen instant
    )
    assert adapter._now() == adapter._now() == captured
    # It is current and it is not the old default — yet it is still frozen.
    assert abs(captured - STALE_DEFAULT) > timedelta(days=1)


def test_the_real_default_advances_between_readings() -> None:
    """End-to-end sanity on the unpatched default (no sleep, no exact time)."""
    adapter = production_adapter()
    first = adapter._now()
    second = adapter._now()

    assert second >= first
    assert (first - STALE_DEFAULT) > timedelta(days=1)
    assert (second - first) < timedelta(seconds=5), (
        "consecutive readings must be real elapsed time, not a +1s per call tick"
    )


def test_utc_now_is_timezone_aware_utc() -> None:
    before = datetime.now(tz=UTC)
    stamp = utc_now()
    after = datetime.now(tz=UTC)
    assert_is_current_utc(stamp, before=before, after=after)


def test_an_injected_deterministic_clock_is_still_honoured() -> None:
    """Tests must keep full control of time."""
    adapter = IbkrPaperAdapter(
        FakePaperTransport,
        account_aliases=dict(ALIASES),
        clock=deterministic_clock(STALE_DEFAULT),
    )
    assert adapter._now() == STALE_DEFAULT + timedelta(seconds=1)
    assert adapter._now() == STALE_DEFAULT + timedelta(seconds=2)


def test_a_naive_clock_fails_closed() -> None:
    """An ambiguous timestamp must never reach the evidence."""
    adapter = IbkrPaperAdapter(
        FakePaperTransport,
        account_aliases=dict(ALIASES),
        clock=lambda: datetime(2026, 7, 30, 17, 5),  # noqa: DTZ001 - the point
    )
    with pytest.raises(BrokerPortError) as error:
        adapter._now()
    assert error.value.code is BrokerErrorCode.VALIDATION_FAILED
    assert "timezone-aware" in str(error.value)


def test_a_non_utc_clock_is_normalized_to_utc() -> None:
    """No dependency on the host's local timezone."""
    local = datetime(2026, 7, 30, 19, 5, tzinfo=timezone(timedelta(hours=2)))
    adapter = IbkrPaperAdapter(
        FakePaperTransport, account_aliases=dict(ALIASES), clock=lambda: local
    )
    stamp = adapter._now()
    assert stamp.utcoffset() == timedelta(0)
    assert stamp == local  # same instant, expressed in UTC


# -- every production evidence timestamp -------------------------------------


def test_account_snapshot_observed_at_is_current_utc() -> None:
    adapter = production_adapter()
    adapter.connect()

    before = datetime.now(tz=UTC)
    snapshot = adapter.get_account_snapshot("paper-main")
    after = datetime.now(tz=UTC)

    assert_is_current_utc(snapshot.observed_at, before=before, after=after)


def test_health_check_timestamp_is_current_utc() -> None:
    adapter = production_adapter()
    adapter.connect()

    before = datetime.now(tz=UTC)
    health = adapter.health_check()
    after = datetime.now(tz=UTC)

    assert_is_current_utc(health.checked_at, before=before, after=after)


def test_submit_receipt_and_event_timestamps_are_current_utc() -> None:
    adapter = production_adapter()
    adapter.connect()

    before = datetime.now(tz=UTC)
    receipt = adapter.submit_order(make_intent("ord-clock-1"))
    events = tuple(adapter.order_events())
    after = datetime.now(tz=UTC)

    assert_is_current_utc(receipt.received_at, before=before, after=after)
    assert events
    for event in events:
        assert_is_current_utc(event.observed_at, before=before, after=after)


def test_cancel_receipt_timestamps_are_current_utc() -> None:
    adapter = production_adapter()
    adapter.connect()
    adapter.submit_order(make_intent("ord-clock-2"))

    before = datetime.now(tz=UTC)
    cancelled = adapter.cancel_order(
        CancelOrderRequest(client_order_id="ord-clock-2", correlation_id="c")
    )
    after = datetime.now(tz=UTC)

    assert_is_current_utc(cancelled.received_at, before=before, after=after)


def test_reconciliation_timestamps_are_current_utc() -> None:
    adapter = production_adapter()
    adapter.connect()

    before = datetime.now(tz=UTC)
    result = adapter.reconcile("paper-main")
    after = datetime.now(tz=UTC)

    assert_is_current_utc(result.reconciled_at, before=before, after=after)
    assert_is_current_utc(result.snapshot.observed_at, before=before, after=after)


def test_recovered_open_order_observations_are_current_utc() -> None:
    """Restart-recovered receipts are evidence too, and must be real-time."""
    transport = FakePaperTransport()
    adapter = production_adapter(transport)
    adapter.connect()
    adapter.submit_order(make_intent("ord-clock-3"))

    before = datetime.now(tz=UTC)
    observed = adapter.get_open_orders("paper-main")
    recovered = adapter.recover_cancellable_orders("paper-main")
    after = datetime.now(tz=UTC)

    assert observed
    for receipt in (*observed, *recovered):
        assert_is_current_utc(receipt.received_at, before=before, after=after)


def test_unknown_state_evidence_is_current_utc() -> None:
    """An UNKNOWN broker status still produces real-time evidence."""
    transport = FakePaperTransport(submit_status="NotAKnownStatus")
    adapter = production_adapter(transport)
    adapter.connect()

    with pytest.raises(BrokerPortError) as error:
        adapter.submit_order(make_intent("ord-clock-4"))
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE

    before = datetime.now(tz=UTC)
    result = adapter.reconcile("paper-main")
    after = datetime.now(tz=UTC)
    assert "ord-clock-4" in result.unresolved_client_order_ids
    assert_is_current_utc(result.reconciled_at, before=before, after=after)


# -- no frozen calendar date may return to production ------------------------


def test_no_hardcoded_production_date_remains_in_broker_ibkr() -> None:
    """A guardrail, not a snapshot: catch any frozen default reintroduced."""
    repo_root = Path(__file__).resolve().parents[2]
    package = repo_root / "packages/py/broker-ibkr/src/trading_broker_ibkr"
    assert package.is_dir(), package
    offenders: list[str] = []
    for module in sorted(package.glob("*.py")):
        for number, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]
            if "datetime(20" in code:
                offenders.append(f"{module.name}:{number}: {line.strip()}")
    assert not offenders, (
        "broker-ibkr production code must not construct a fixed calendar date"
        f" (use utc_now or an injected clock): {offenders}"
    )


def test_the_adapter_has_no_deterministic_clock_helper() -> None:
    """The frozen-tick helper belonged to tests, never to the adapter."""
    import trading_broker_ibkr.paper_adapter as adapter_module

    assert not hasattr(adapter_module, "_deterministic_clock")


def test_the_fake_broker_keeps_its_deterministic_default() -> None:
    """Only the REAL adapter changed; fake-broker tests stay reproducible."""
    from trading_broker_core.fake import FakeBroker

    first = FakeBroker(accounts={}).health_check().checked_at
    second = FakeBroker(accounts={}).health_check().checked_at
    assert first == second
    assert first.tzinfo is not None
