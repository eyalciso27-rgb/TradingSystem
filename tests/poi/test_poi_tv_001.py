"""POI-TV-001 drill — TradingView webhook contract and security (spike).

The webhook contract and its security behavior are exercised for real (the
validation logic below IS the spike under test); what remains blocked is
firing a live alert from a TradingView account.

Pass criteria (POI_BACKLOG): alert creates external signal only; duplicates
ignored; no broker path. Fail criteria: direct-to-order or an
unauthenticated/replayable endpoint.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tradingview"

# Canonical webhook payload contract (schema-versioned; unknown/extra fields
# rejected). The payload intentionally has NO order/side/quantity semantics:
# a TradingView alert is an external signal that is revalidated internally —
# never a trading command (Integration spec: revalidated internally;
# no direct trading).
REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "alert_id",
        "symbol",
        "exchange",
        "condition",
        "triggered_at",
        "interval",
        "price",
        "strategy_ref",
    }
)
FORBIDDEN_FIELDS = frozenset({"side", "quantity", "action", "order_type", "account"})


class WebhookRejected(Exception):
    pass


@dataclass(frozen=True)
class ExternalSignal:
    """The ONLY artifact a webhook may produce (never an order intent)."""

    alert_id: str
    symbol: str
    condition: str
    triggered_at: str


class TradingViewWebhookSpike:
    """Minimal authenticated, idempotent webhook receiver (PoI spike)."""

    def __init__(self, shared_secret: str) -> None:
        self._secret = shared_secret
        self._seen: set[str] = set()
        self.signals: list[ExternalSignal] = []

    def receive(self, *, secret_header: str | None, body: bytes) -> ExternalSignal | None:
        # 1. authentication: constant-time compare of the shared secret header
        if secret_header is None or not hmac.compare_digest(secret_header, self._secret):
            raise WebhookRejected("unauthenticated webhook call")
        # 2. schema validation: strict field set, no extras, no order semantics
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WebhookRejected(f"malformed payload: {error}") from error
        fields = set(payload)
        if fields & FORBIDDEN_FIELDS:
            raise WebhookRejected(f"order semantics rejected: {sorted(fields & FORBIDDEN_FIELDS)}")
        if fields != REQUIRED_FIELDS:
            raise WebhookRejected(
                f"schema mismatch: missing {sorted(REQUIRED_FIELDS - fields)}, "
                f"extra {sorted(fields - REQUIRED_FIELDS)}"
            )
        if payload["schema_version"] != "v1":
            raise WebhookRejected("unsupported schema_version")
        # 3. idempotency / replay: alert_id is the dedup key
        if payload["alert_id"] in self._seen:
            return None  # duplicate/replay ignored, no second signal
        self._seen.add(payload["alert_id"])
        signal = ExternalSignal(
            alert_id=payload["alert_id"],
            symbol=payload["symbol"],
            condition=payload["condition"],
            triggered_at=payload["triggered_at"],
        )
        self.signals.append(signal)
        return signal


SECRET = "poi-drill-shared-secret"  # drill value, not a real credential


def _valid_body() -> bytes:
    return (FIXTURES / "valid_alert.json").read_bytes()


def test_valid_alert_creates_external_signal_only() -> None:
    spike = TradingViewWebhookSpike(SECRET)
    signal = spike.receive(secret_header=SECRET, body=_valid_body())
    assert isinstance(signal, ExternalSignal)
    # no broker path: the produced artifact carries no order semantics at all
    assert not any(hasattr(signal, f) for f in FORBIDDEN_FIELDS)


def test_unauthenticated_call_rejected() -> None:
    spike = TradingViewWebhookSpike(SECRET)
    for header in (None, "", "wrong-secret"):
        with pytest.raises(WebhookRejected, match="unauthenticated"):
            spike.receive(secret_header=header, body=_valid_body())
    assert spike.signals == []


def test_duplicate_and_replay_ignored() -> None:
    spike = TradingViewWebhookSpike(SECRET)
    assert spike.receive(secret_header=SECRET, body=_valid_body()) is not None
    # exact replay of the same authenticated request
    assert spike.receive(secret_header=SECRET, body=_valid_body()) is None
    assert len(spike.signals) == 1


def test_order_semantics_rejected() -> None:
    spike = TradingViewWebhookSpike(SECRET)
    body = (FIXTURES / "malformed_alert.json").read_bytes()
    with pytest.raises(WebhookRejected, match="order semantics rejected"):
        spike.receive(secret_header=SECRET, body=body)
    assert spike.signals == []


def test_malformed_and_extra_fields_rejected() -> None:
    spike = TradingViewWebhookSpike(SECRET)
    with pytest.raises(WebhookRejected, match="malformed"):
        spike.receive(secret_header=SECRET, body=b"not json at all")
    payload = json.loads(_valid_body())
    payload["surprise"] = "extra"
    with pytest.raises(WebhookRejected, match="schema mismatch"):
        spike.receive(secret_header=SECRET, body=json.dumps(payload).encode())
    del payload["surprise"], payload["price"]
    with pytest.raises(WebhookRejected, match="schema mismatch"):
        spike.receive(secret_header=SECRET, body=json.dumps(payload).encode())
