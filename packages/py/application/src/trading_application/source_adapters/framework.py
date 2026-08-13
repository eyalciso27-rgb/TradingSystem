"""Shared production source-adapter framework (PHASE-6).

Reusable, provider-agnostic primitives every source adapter needs, so each
adapter is a thin mapping from an INJECTED provider-client to the canonical
contracts (``trading_contracts.social`` / ``SignalAssessment``) — never a
network client of its own. Real SDK/clients are injected at owner-run/PoI time;
tests drive deterministic fakes through the same ports.

Hard rules (enforced by adapter tests):

- **Read-only for trading.** No adapter has an order/submit/cancel/execute
  surface, and nothing here imports a broker, execution agent or network client.
- **Fail-closed.** A missing session is ``AUTH_REQUIRED``; repeated failures
  open the circuit; malformed provider data raises ``AdapterError`` and never
  produces a silent default.
- **Deterministic + idempotent.** Dedup keys and cursors are derived from the
  input; a redelivery never produces a duplicate; conflicting content for one id
  fails closed.
- **No credential leakage.** ``redact`` scrubs any configured secret token from
  a string before it can reach a log or evidence line; secrets are never stored
  in these objects.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from trading_contracts.enums import IntegrationHealthStatus


class AdapterError(Exception):
    """Fail-closed source-adapter failure (malformed data, corrupt state)."""


def content_hash(text: str) -> str:
    """Canonical sha256 content hash (matches the social contract's format)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact(text: str, secrets: Iterable[str]) -> str:
    """Scrub any configured secret substring from text before it is logged.

    Defence in depth: adapters never build log lines from raw bodies, but a
    misconfigured message must still never carry a token to a log/evidence file.
    """
    scrubbed = text
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "***")
    return scrubbed


# -- bounded exponential backoff --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Deterministic bounded exponential backoff (no wall clock, no jitter)."""

    base_seconds: float = 1.0
    factor: float = 2.0
    max_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.base_seconds <= 0 or self.factor < 1 or self.max_seconds < self.base_seconds:
            raise AdapterError("invalid backoff policy")

    def delay_for(self, attempt: int) -> float:
        """Delay before retry ``attempt`` (1-based); capped at ``max_seconds``."""
        if attempt < 1:
            return 0.0
        raw = self.base_seconds * (self.factor ** (attempt - 1))
        return min(raw, self.max_seconds)


# -- circuit breaker --------------------------------------------------------------------


@dataclass(slots=True)
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens after a probe.

    Deterministic and clock-free: the caller decides when to ``allow_probe`` (a
    half-open attempt). ``OPEN`` maps to ``IntegrationHealthStatus.CIRCUIT_OPEN``
    so a degraded provider stops hammering and surfaces loudly.
    """

    threshold: int = 5
    _failures: int = 0
    _open: bool = False

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise AdapterError("circuit threshold must be >= 1")

    @property
    def is_open(self) -> bool:
        return self._open

    def record_success(self) -> None:
        self._failures = 0
        self._open = False

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._open = True

    def allow_probe(self) -> None:
        """Half-open: let ONE attempt through without clearing the breaker."""
        self._failures = self.threshold - 1
        self._open = False


# -- fixed-window rate limiter ----------------------------------------------------------


@dataclass(slots=True)
class RateLimiter:
    """Fixed-window limiter: at most ``max_events`` per ``window_seconds``.

    Clock is injected (``now`` passed in), so it is fully deterministic in tests.
    """

    max_events: int
    window_seconds: float
    _window_start: float | None = None
    _count: int = 0

    def __post_init__(self) -> None:
        if self.max_events < 1 or self.window_seconds <= 0:
            raise AdapterError("invalid rate-limiter configuration")

    def allow(self, now: float) -> bool:
        if self._window_start is None or now - self._window_start >= self.window_seconds:
            self._window_start = now
            self._count = 1
            return True
        if self._count < self.max_events:
            self._count += 1
            return True
        return False


# -- idempotent dedup registry ----------------------------------------------------------


@dataclass(slots=True)
class DedupRegistry:
    """Idempotent seen-key registry; conflicting content for one id fails closed."""

    _seen: dict[str, str] = field(default_factory=dict)

    def register(self, key: str, fingerprint: str) -> bool:
        """Return True if newly seen; False if an identical duplicate.

        A key seen before with a DIFFERENT fingerprint is corrupt input and
        raises — a redelivery must be byte-identical or it is not a duplicate.
        """
        previous = self._seen.get(key)
        if previous is None:
            self._seen[key] = fingerprint
            return True
        if previous == fingerprint:
            return False
        raise AdapterError(
            f"id {key!r} redelivered with different content; fail closed"
        )

    def __contains__(self, key: object) -> bool:
        return key in self._seen


# -- health -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    """One health snapshot mapped onto the canonical IntegrationHealthStatus."""

    status: IntegrationHealthStatus
    detail: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise AdapterError("observed_at must be timezone-aware")

    @property
    def is_healthy(self) -> bool:
        return self.status is IntegrationHealthStatus.HEALTHY


__all__ = [
    "AdapterError",
    "AdapterHealth",
    "BackoffPolicy",
    "CircuitBreaker",
    "DedupRegistry",
    "RateLimiter",
    "content_hash",
    "redact",
]
