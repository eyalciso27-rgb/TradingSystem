"""Shared source-adapter framework primitives (PHASE-6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from trading_application.source_adapters.framework import (
    AdapterError,
    AdapterHealth,
    BackoffPolicy,
    CircuitBreaker,
    DedupRegistry,
    RateLimiter,
    content_hash,
    redact,
)
from trading_contracts.enums import IntegrationHealthStatus


def test_content_hash_is_stable_and_prefixed() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc").startswith("sha256:")
    assert content_hash("abc") != content_hash("abd")


def test_redact_scrubs_every_secret_substring() -> None:
    out = redact("token=SEKRET user=owner key=API123", ["SEKRET", "API123"])
    assert "SEKRET" not in out and "API123" not in out
    assert "owner" in out


def test_backoff_is_bounded_exponential() -> None:
    policy = BackoffPolicy(base_seconds=1.0, factor=2.0, max_seconds=10.0)
    assert policy.delay_for(0) == 0.0  # no retry yet
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(50) == 10.0  # capped


def test_circuit_breaker_opens_after_threshold_and_recovers() -> None:
    breaker = CircuitBreaker(threshold=3)
    assert breaker.is_open is False
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open is True
    breaker.allow_probe()  # half-open: one attempt allowed through
    assert breaker.is_open is False
    breaker.record_success()
    assert breaker.is_open is False


def test_rate_limiter_fixed_window() -> None:
    limiter = RateLimiter(max_events=2, window_seconds=60)
    assert limiter.allow(0.0) is True
    assert limiter.allow(1.0) is True
    assert limiter.allow(2.0) is False  # third within window
    assert limiter.allow(61.0) is True  # new window


def test_dedup_registry_identical_duplicate_and_conflict() -> None:
    reg = DedupRegistry()
    assert reg.register("k1", "fp-a") is True
    assert reg.register("k1", "fp-a") is False  # identical duplicate
    assert "k1" in reg
    with pytest.raises(AdapterError):
        reg.register("k1", "fp-b")  # same key, conflicting fingerprint -> fail closed


def test_adapter_health_requires_tz_aware_and_reports_healthy() -> None:
    h = AdapterHealth(
        status=IntegrationHealthStatus.HEALTHY, detail="ok",
        observed_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    assert h.is_healthy is True
    degraded = AdapterHealth(
        status=IntegrationHealthStatus.DEGRADED, detail="bad",
        observed_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    assert degraded.is_healthy is False
