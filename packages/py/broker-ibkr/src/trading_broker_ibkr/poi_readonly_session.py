"""POI-IBKR-READONLY session module (read-only; not the IMP-1002 adapter).

This is the ONLY concrete broker glue for the read-only PoI. It provides
``create_session()`` for ``scripts/poi_ibkr_readonly.py``: a session object
exposing exactly the harness allowlist — ``connect``, ``req_current_time``,
``req_account_summary``, ``req_positions``, ``disconnect`` — and nothing
else. There is deliberately no order-placing, cancellation or modification
surface here at all; the real IBKR Paper adapter (IMP-1002) is separate and
blocked until the PoI passes.

Safety (ADR-0014; PoI plan v2):

- READ-ONLY: the five allowlisted methods are the entire public surface.
- PAPER ONLY: host is 127.0.0.1/localhost only; port is 4002 only (the live
  API port 4001 is rejected); the account id must start with ``DU`` or the
  session fails closed and disconnects.
- NO SECRETS: no credentials, no ENV access, no prompts — login is
  performed by the owner in the IB Gateway UI. Account ids are masked; the
  session prints nothing.
- ISOLATION: ``ibapi`` is imported lazily inside ``create_session`` so this
  module imports (and is fully unit-tested) without the SDK present and
  without any connection. The SDK stays inside this ``trading_broker_ibkr``
  package (guardrails forbid it elsewhere). Tests inject a fake raw client.
- FAIL CLOSED: any error or unknown response raises ``PoiSessionError``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

_T = TypeVar("_T")

READONLY_SESSION_VERSION = "poi-ibkr-readonly-session-1.0.0"

ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
LIVE_API_PORT = 4001
PAPER_ACCOUNT_PREFIX = "DU"


class PoiSessionError(Exception):
    """Fail-closed read-only session failure."""


@dataclass(frozen=True, slots=True)
class ReadOnlyAccountSummary:
    """Duck-typed to what the harness reads; values kept as plain strings."""

    account_id: str
    net_liquidation: str
    total_cash: str
    buying_power: str


def mask_account_id(account_id: str) -> str:
    """``DU1234567`` -> ``DU*****67``; never expose the full id."""
    if len(account_id) <= 4:
        return account_id[:2] + "**"
    return account_id[:2] + "*" * (len(account_id) - 4) + account_id[-2:]


def _validate_endpoint(host: str, port: int) -> None:
    if host not in ALLOWED_HOSTS:
        raise PoiSessionError(
            f"host {host!r} refused: only 127.0.0.1/localhost is allowed in this PoI"
        )
    if port == LIVE_API_PORT:
        raise PoiSessionError(
            f"port {LIVE_API_PORT} is the LIVE API port and is never allowed"
        )
    if port != PAPER_API_PORT:
        raise PoiSessionError(
            f"port {port} refused: only the paper API port {PAPER_API_PORT} is allowed"
        )


class RawReadOnlyClient(Protocol):
    """The minimal read-only transport the session needs.

    The concrete implementation wraps the IBKR SDK and is built only inside
    ``create_session`` at PoI run time; unit tests inject a fake.
    """

    def open(self, host: str, port: int) -> None: ...

    def current_time(self) -> str: ...

    def account_summary(self) -> ReadOnlyAccountSummary: ...

    def positions(self) -> Sequence[object]: ...

    def close(self) -> None: ...


class IbkrReadOnlySession:
    """Read-only session with exactly the harness allowlist and safety gates."""

    def __init__(self, client: RawReadOnlyClient) -> None:
        self._client = client
        self._connected = False

    def connect(self, host: str, port: int) -> None:
        _validate_endpoint(host, port)
        try:
            self._client.open(host, port)
        except PoiSessionError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise PoiSessionError(f"connect failed: {type(error).__name__}") from error
        self._connected = True

    def req_current_time(self) -> str:
        self._require_connected()
        value = self._guarded(self._client.current_time)
        if not str(value).strip():
            raise PoiSessionError("empty current-time response")
        return str(value)

    def req_account_summary(self) -> ReadOnlyAccountSummary:
        self._require_connected()
        summary = self._guarded(self._client.account_summary)
        account_id = summary.account_id.strip()
        if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
            # Not a paper account: fail closed and disconnect immediately.
            self.disconnect()
            raise PoiSessionError(
                f"account {mask_account_id(account_id)} is not a paper account"
                f" (expected prefix {PAPER_ACCOUNT_PREFIX}); disconnected"
            )
        return summary

    def req_positions(self) -> Sequence[object]:
        self._require_connected()
        return self._guarded(self._client.positions)

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self._client.close()
        finally:
            self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise PoiSessionError("session is not connected")

    @staticmethod
    def _guarded(call: Callable[[], _T]) -> _T:
        try:
            return call()
        except PoiSessionError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise PoiSessionError(
                f"read-only request failed, failing closed: {type(error).__name__}"
            ) from error


def _build_ibapi_client() -> RawReadOnlyClient:
    """Construct the SDK-backed read-only client (PoI run time only).

    The IBKR Python SDK is imported here, lazily, so this module stays
    importable and unit-testable without the SDK and without any network.
    This function is exercised only during the manual PoI on the host where
    IB Gateway runs; it performs no login (the owner logs in via the Gateway
    UI) and uses only read-only request calls.
    """
    try:
        import ibapi  # type: ignore[import-not-found]  # noqa: F401  # SDK confined here
    except ImportError as error:  # pragma: no cover - depends on host install
        raise PoiSessionError(
            "the IBKR Python API (ibapi) is not installed on this host; install it"
            " inside the broker-ibkr environment before running the PoI"
        ) from error
    # The concrete ibapi EClient/EWrapper read-only wiring is intentionally
    # deferred to the real adapter work (IMP-1002); this PoI session is not a
    # full adapter. Until that wiring lands, fail closed rather than pretend.
    raise PoiSessionError(
        "read-only ibapi client wiring is provided by the IBKR adapter task"
        " (IMP-1002); supply a RawReadOnlyClient explicitly for the PoI"
    )


def create_session(client: RawReadOnlyClient | None = None) -> IbkrReadOnlySession:
    """Return a read-only session for the harness.

    ``client`` is injectable for tests and for a hand-wired PoI transport;
    when omitted, the SDK-backed client is built lazily (host-only).
    """
    raw = client if client is not None else _build_ibapi_client()
    return IbkrReadOnlySession(raw)


__all__ = [
    "ALLOWED_HOSTS",
    "LIVE_API_PORT",
    "PAPER_ACCOUNT_PREFIX",
    "PAPER_API_PORT",
    "READONLY_SESSION_VERSION",
    "IbkrReadOnlySession",
    "PoiSessionError",
    "RawReadOnlyClient",
    "ReadOnlyAccountSummary",
    "create_session",
    "mask_account_id",
]
