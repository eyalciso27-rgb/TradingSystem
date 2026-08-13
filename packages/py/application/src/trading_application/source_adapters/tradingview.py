"""TradingView adapter (IMP-0608) — authenticated webhook external signal only.

Canonical DoD: "Authenticated webhook external signal only; No direct order
path; replay/idempotency pass." TradingView's OFFICIAL alert delivery is an
outbound webhook, so this adapter is a fail-closed webhook RECEIVER — it does
not log in, scrape or hold a session. It:

  1. verifies an HMAC-SHA256 signature of the exact raw body against a shared
     secret (constant-time; a missing/invalid signature is AUTH-refused and
     nothing is normalized);
  2. normalizes the verified alert into a canonical EXTERNAL ``SignalAssessment``
     with full provenance (raw content hash);
  3. is idempotent — a replayed alert (same alert id + identical body) returns
     the first result and never a second signal; the same id with different
     content fails closed;
  4. has NO order/submit/cancel surface — an external signal is research input,
     never an execution instruction (Traceability TR-007: "signed alert verified
     and rechecked"; IMP-0608: "No direct order path").

The shared secret is supplied at construction (from ``trading_config.secrets``)
and held privately — it never appears in a signal, a repr or a log.
"""

from __future__ import annotations

import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    IntegrationHealthStatus,
    SignalDirection,
    SignalFamily,
)

from trading_application.source_adapters.framework import (
    AdapterError,
    AdapterHealth,
    RateLimiter,
    content_hash,
)

TRADINGVIEW_ADAPTER_VERSION = "tradingview-adapter-1.0.0"

_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-00000000700a")

#: TradingView `action` string -> canonical direction (no SHORT in the enum;
#: a bearish alert is NEGATIVE, an ambiguous one NEUTRAL).
_DIRECTION_BY_ACTION = {
    "BUY": SignalDirection.LONG,
    "LONG": SignalDirection.LONG,
    "SELL": SignalDirection.NEGATIVE,
    "SHORT": SignalDirection.NEGATIVE,
    "EXIT": SignalDirection.NEUTRAL,
    "ALERT": SignalDirection.NEUTRAL,
}


@dataclass(frozen=True, slots=True)
class TradingViewSignal:
    """One verified external signal. Research input only — never executable."""

    alert_id: str
    symbol: str
    action: str
    direction: SignalDirection
    strategy_id: str
    fired_at: datetime
    received_at: datetime
    provenance_hash: str
    signature_verified: bool = True
    signal_family: SignalFamily = SignalFamily.EXTERNAL
    read_only_for_trading: bool = True
    executable: bool = False

    def __post_init__(self) -> None:
        if self.fired_at.tzinfo is None or self.received_at.tzinfo is None:
            raise AdapterError("timestamps must be timezone-aware")
        if not self.signature_verified:
            raise AdapterError("an unverified alert is never a signal; fail closed")
        if self.executable or not self.read_only_for_trading:
            raise AdapterError("a TradingView signal is research-only; fail closed")


def verify_signature(raw_body: bytes, provided_signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check of the EXACT raw body. Fail closed."""
    if not secret or not provided_signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, "sha256").hexdigest()
    return hmac.compare_digest(expected, provided_signature.strip().lower())


class TradingViewAdapter:
    """Fail-closed TradingView webhook receiver. Read-only for trading."""

    def __init__(
        self,
        *,
        secret: str,
        connector_id: str = "tradingview-webhook",
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if not connector_id.strip():
            raise AdapterError("connector_id is mandatory")
        self._secret = secret  # held privately; never logged or exposed
        self._connector_id = connector_id
        self._rate_limiter = rate_limiter
        self._signals: dict[str, TradingViewSignal] = {}
        self._seen: dict[str, str] = {}  # alert_id -> provenance hash
        self._verify_failures = 0
        self._last_status = (
            IntegrationHealthStatus.AUTH_REQUIRED
            if not secret
            else IntegrationHealthStatus.INITIALIZING
        )
        self._last_detail = "no secret configured" if not secret else "initialized"

    # -- ingestion ----------------------------------------------------------------
    def ingest(
        self,
        raw_body: bytes,
        signature: str,
        *,
        received_at: datetime,
        now: float,
    ) -> TradingViewSignal:
        """Verify + normalize one webhook delivery. Idempotent; fail-closed."""
        if not self._secret:
            self._set(IntegrationHealthStatus.AUTH_REQUIRED, "no secret configured")
            raise AdapterError("AUTH_REQUIRED: no webhook secret configured")
        if not verify_signature(raw_body, signature, self._secret):
            self._verify_failures += 1
            self._set(
                IntegrationHealthStatus.AUTH_REQUIRED
                if self._verify_failures < 3
                else IntegrationHealthStatus.DEGRADED,
                "signature verification failed",
            )
            raise AdapterError("AUTH_REQUIRED: webhook signature verification failed")
        if self._rate_limiter is not None and not self._rate_limiter.allow(now):
            self._set(IntegrationHealthStatus.RATE_LIMITED, "webhook rate limit exceeded")
            raise AdapterError("RATE_LIMITED: webhook rate limit exceeded")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            self._set(IntegrationHealthStatus.DEGRADED, "malformed payload")
            raise AdapterError(f"malformed webhook payload: {type(error).__name__}") from error
        if not isinstance(payload, dict):
            self._set(IntegrationHealthStatus.DEGRADED, "malformed payload")
            raise AdapterError("webhook payload must be a JSON object")

        provenance = content_hash(raw_body.decode("utf-8"))
        alert_id = self._require(payload, "alert_id")

        # Idempotency / replay: identical redelivery returns the first signal;
        # the same id with different content is corrupt input and fails closed.
        previous = self._seen.get(alert_id)
        if previous is not None:
            if previous == provenance:
                self._verify_failures = 0
                self._set(IntegrationHealthStatus.HEALTHY, "duplicate alert deduplicated")
                return self._signals[alert_id]
            self._set(IntegrationHealthStatus.DEGRADED, "conflicting duplicate alert")
            raise AdapterError(
                f"alert {alert_id!r} replayed with different content; fail closed"
            )

        action = str(payload.get("action", "ALERT")).upper()
        fired_raw = self._require(payload, "fired_at")
        try:
            fired_at = datetime.fromisoformat(fired_raw)
        except ValueError as error:
            self._set(IntegrationHealthStatus.DEGRADED, "malformed fired_at")
            raise AdapterError("malformed fired_at timestamp") from error

        signal = TradingViewSignal(
            alert_id=alert_id,
            symbol=self._require(payload, "symbol"),
            action=action,
            direction=_DIRECTION_BY_ACTION.get(action, SignalDirection.NEUTRAL),
            strategy_id=str(payload.get("strategy") or "tradingview"),
            fired_at=fired_at,
            received_at=received_at,
            provenance_hash=provenance,
        )
        self._seen[alert_id] = provenance
        self._signals[alert_id] = signal
        self._verify_failures = 0
        self._set(IntegrationHealthStatus.HEALTHY, "alert verified and normalized")
        return signal

    def to_assessment_fields(self, signal: TradingViewSignal) -> dict[str, object]:
        """The canonical EXTERNAL SignalAssessment fields for a verified signal.

        Kept as a plain mapping so this adapter has NO dependency on the decision
        contract's constructor surface; the fusion path builds the assessment.
        """
        return {
            "instrument_id": uuid.uuid5(_NAMESPACE, signal.symbol),
            "signal_family": SignalFamily.EXTERNAL,
            "model_id": f"tradingview:{signal.strategy_id}",
            "model_version": TRADINGVIEW_ADAPTER_VERSION,
            "direction": signal.direction,
            "status": AssessmentStatus.TRIGGERED,
            "score": Decimal(0),  # external alerts carry no calibrated score
            "calibration_status": CalibrationStatus.UNVERIFIED,
            "evidence_refs": (f"tradingview:{signal.alert_id}", signal.provenance_hash),
        }

    # -- health -------------------------------------------------------------------
    def health(self, *, observed_at: datetime) -> AdapterHealth:
        return AdapterHealth(
            status=self._last_status, detail=self._last_detail, observed_at=observed_at
        )

    # -- internals ----------------------------------------------------------------
    def _require(self, payload: dict[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AdapterError(f"webhook payload missing required field {key!r}")
        return value

    def _set(self, status: IntegrationHealthStatus, detail: str) -> None:
        self._last_status = status
        self._last_detail = detail

    def __repr__(self) -> str:  # never leak the secret
        return (
            f"TradingViewAdapter(connector_id={self._connector_id!r}, "
            f"status={self._last_status.value})"
        )


__all__ = [
    "TRADINGVIEW_ADAPTER_VERSION",
    "TradingViewAdapter",
    "TradingViewSignal",
    "verify_signature",
]
