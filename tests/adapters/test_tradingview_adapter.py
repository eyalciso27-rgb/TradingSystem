"""IMP-0608 TradingView adapter — authenticated webhook, no order path, replay."""

from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime

import pytest
from trading_application.source_adapters.framework import AdapterError, RateLimiter
from trading_application.source_adapters.tradingview import (
    TradingViewAdapter,
    TradingViewSignal,
    verify_signature,
)
from trading_contracts.enums import IntegrationHealthStatus, SignalDirection, SignalFamily

SECRET = "tv-shared-secret"
T0 = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


def body(**over: object) -> bytes:
    payload: dict[str, object] = {
        "alert_id": "a-1",
        "symbol": "AAPL",
        "action": "BUY",
        "strategy": "breakout-v1",
        "fired_at": T0.isoformat(),
    }
    payload.update(over)
    return json.dumps(payload).encode("utf-8")


def sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw, "sha256").hexdigest()


def make(*, secret: str = SECRET, rate: RateLimiter | None = None) -> TradingViewAdapter:
    return TradingViewAdapter(secret=secret, rate_limiter=rate)


def test_valid_signed_alert_is_normalized_with_provenance() -> None:
    adapter = make()
    raw = body()
    signal = adapter.ingest(raw, sign(raw), received_at=T0, now=0.0)
    assert isinstance(signal, TradingViewSignal)
    assert signal.symbol == "AAPL"
    assert signal.direction is SignalDirection.LONG
    assert signal.strategy_id == "breakout-v1"
    assert signal.provenance_hash.startswith("sha256:")
    assert signal.executable is False and signal.read_only_for_trading is True
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.HEALTHY
    fields = adapter.to_assessment_fields(signal)
    assert fields["signal_family"] is SignalFamily.EXTERNAL


def test_sell_action_maps_to_negative_direction() -> None:
    adapter = make()
    raw = body(alert_id="a-sell", action="SELL")
    signal = adapter.ingest(raw, sign(raw), received_at=T0, now=0.0)
    assert signal.direction is SignalDirection.NEGATIVE


def test_missing_secret_is_auth_required() -> None:
    adapter = make(secret="")
    with pytest.raises(AdapterError, match="AUTH_REQUIRED"):
        adapter.ingest(body(), "deadbeef", received_at=T0, now=0.0)
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.AUTH_REQUIRED


def test_invalid_signature_is_refused_and_normalizes_nothing() -> None:
    adapter = make()
    with pytest.raises(AdapterError, match="signature"):
        adapter.ingest(body(), "not-a-valid-signature", received_at=T0, now=0.0)
    assert adapter.health(observed_at=T0).status in (
        IntegrationHealthStatus.AUTH_REQUIRED,
        IntegrationHealthStatus.DEGRADED,
    )


def test_tampered_body_fails_signature() -> None:
    adapter = make()
    raw = body()
    good = sign(raw)
    tampered = body(symbol="TSLA")  # same signature, different body
    with pytest.raises(AdapterError, match="signature"):
        adapter.ingest(tampered, good, received_at=T0, now=0.0)


def test_malformed_payload_is_degraded_fail_closed() -> None:
    adapter = make()
    raw = b"{not json"
    with pytest.raises(AdapterError, match="malformed"):
        adapter.ingest(raw, sign(raw), received_at=T0, now=0.0)
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.DEGRADED


def test_missing_required_field_fails_closed() -> None:
    adapter = make()
    raw = json.dumps({"alert_id": "a-1", "action": "BUY", "fired_at": T0.isoformat()}).encode()
    with pytest.raises(AdapterError, match="symbol"):
        adapter.ingest(raw, sign(raw), received_at=T0, now=0.0)


def test_replayed_identical_alert_is_idempotent() -> None:
    adapter = make()
    raw = body()
    first = adapter.ingest(raw, sign(raw), received_at=T0, now=0.0)
    second = adapter.ingest(raw, sign(raw), received_at=T0, now=1.0)  # replay
    assert second is first  # no duplicate signal
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.HEALTHY


def test_conflicting_duplicate_alert_id_fails_closed() -> None:
    adapter = make()
    raw = body()
    adapter.ingest(raw, sign(raw), received_at=T0, now=0.0)
    conflicting = body(symbol="MSFT")  # same alert_id, different content
    with pytest.raises(AdapterError, match="different content"):
        adapter.ingest(conflicting, sign(conflicting), received_at=T0, now=1.0)


def test_rate_limit_surfaces_rate_limited() -> None:
    adapter = make(rate=RateLimiter(max_events=1, window_seconds=60))
    raw1 = body(alert_id="a-1")
    adapter.ingest(raw1, sign(raw1), received_at=T0, now=0.0)
    raw2 = body(alert_id="a-2")
    with pytest.raises(AdapterError, match="RATE_LIMITED"):
        adapter.ingest(raw2, sign(raw2), received_at=T0, now=1.0)
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.RATE_LIMITED


def test_adapter_has_no_order_or_execution_surface() -> None:
    adapter = make()
    for surface in ("submit", "cancel", "execute", "place_order", "flatten", "authorize"):
        assert not hasattr(adapter, surface)


def test_secret_never_appears_in_repr() -> None:
    adapter = make()
    assert SECRET not in repr(adapter)


def test_verify_signature_is_constant_time_and_fail_closed() -> None:
    raw = body()
    assert verify_signature(raw, sign(raw), SECRET) is True
    assert verify_signature(raw, "", SECRET) is False
    assert verify_signature(raw, sign(raw), "") is False


def test_unverified_signal_cannot_be_constructed() -> None:
    with pytest.raises(AdapterError, match="fail closed"):
        TradingViewSignal(
            alert_id="a", symbol="AAPL", action="BUY", direction=SignalDirection.LONG,
            strategy_id="s", fired_at=T0, received_at=T0, provenance_hash="sha256:x",
            signature_verified=False,
        )
