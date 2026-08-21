"""POI-IBKR market-data read-only session (POI-MD-001 pre-check).

Provides ``create_session()`` for ``scripts/poi_ibkr_market_data_readonly.py``:
a session object exposing exactly the harness allowlist — ``connect``,
``req_current_time``, ``req_account_summary``, ``resolve_contract``,
``req_market_data``, ``disconnect`` — plus ONE addition for the canonical
execution path, ``req_market_data_for_instrument``, which quotes a security by
the broker's own resolved reference rather than by ticker. Nothing else. There
is deliberately no order-placing, cancellation, modification or global-cancel
surface here at all; this is a market-data pre-check, NOT the IMP-1002 adapter
and NOT POI-IBKR-001 execution.

The two market-data operations are not interchangeable. ``req_market_data``
takes a ticker and predates contract resolution; it is kept for the read-only
PoI tooling that still uses it. The execution path may only use
``req_market_data_for_instrument``, because asking for a price by ticker after a
security has been resolved lets IBKR resolve the string a second time, with
nobody comparing the two answers.

The shared safety gates are REUSED from the merged POI-IBKR-READONLY session
(``_validate_endpoint``, ``mask_account_id``, ``PoiSessionError``,
``ReadOnlyAccountSummary``, ``PAPER_ACCOUNT_PREFIX``) — not re-implemented — so
the localhost-only, paper-port-4002-only and DU-account gates behave
identically. Market-data specifics (contract resolution, ``reqMarketDataType``,
``reqMktData`` snapshot and tick handling) live in the sibling
``poi_market_data_client`` and its lazily imported IBKR transport; the merged
read-only modules are not modified.

Safety (ADR-0014; PoI plan v2):

- READ-ONLY: the six allowlisted methods are the entire public surface.
- PAPER ONLY: host 127.0.0.1/localhost only; port 4002 only (live 4001
  rejected); account id must start with ``DU`` or the session fails closed and
  disconnects immediately.
- NO SECRETS: no credentials, no ENV access, no prompts — login happens in the
  Gateway UI, by the owner. Account ids are masked; the session prints nothing.
- ISOLATION: ``ibapi`` is imported lazily inside the client, so this module
  imports and is fully unit-tested without the SDK and without any connection.
- NO AUTOMATIC RETRY / FAIL CLOSED: any error or unknown response raises
  ``PoiSessionError``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from trading_broker_ibkr.poi_market_data_client import ContractResolution, MarketDataQuote
from trading_broker_ibkr.poi_readonly_session import (
    ALLOWED_HOSTS,
    LIVE_API_PORT,
    PAPER_ACCOUNT_PREFIX,
    PAPER_API_PORT,
    PoiSessionError,
    ReadOnlyAccountSummary,
    _validate_endpoint,
    mask_account_id,
)

_T = TypeVar("_T")

MARKET_DATA_SESSION_VERSION = "poi-ibkr-market-data-session-1.0.0"

__all__ = [
    "ALLOWED_HOSTS",
    "LIVE_API_PORT",
    "MARKET_DATA_SESSION_VERSION",
    "PAPER_ACCOUNT_PREFIX",
    "PAPER_API_PORT",
    "IbkrMarketDataSession",
    "PoiSessionError",
    "RawMarketDataClient",
    "create_session",
    "mask_account_id",
]


class RawMarketDataClient(Protocol):
    """The minimal read-only market-data transport the session needs.

    The concrete implementation wraps the IBKR SDK and is built only inside
    ``create_session`` at PoI run time; unit tests inject a fake.
    """

    def open(self, host: str, port: int) -> None: ...

    def current_time(self) -> str: ...

    def account_summary(self) -> ReadOnlyAccountSummary: ...

    def resolve_contract(self, symbol: str) -> ContractResolution: ...

    def market_data(
        self, symbol: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote: ...

    def market_data_for_instrument(
        self, broker_instrument_ref: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote: ...

    def close(self) -> None: ...


class IbkrMarketDataSession:
    """Read-only market-data session with the harness allowlist + safety gates."""

    def __init__(self, client: RawMarketDataClient) -> None:
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

    def resolve_contract(self, symbol: str) -> ContractResolution:
        self._require_connected()
        return self._guarded(lambda: self._client.resolve_contract(symbol))

    def req_market_data(
        self, symbol: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote:
        """Ticker-shaped, read-only. Kept for the PoI tooling that predates
        contract resolution; the execution path uses
        ``req_market_data_for_instrument``.
        """
        self._require_connected()
        return self._guarded(
            lambda: self._client.market_data(symbol, requested_market_data_type)
        )

    def req_market_data_for_instrument(
        self, broker_instrument_ref: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote:
        """A quote for THE resolved security, by its opaque reference.

        The execution path's only market-data operation. The reference is
        decoded inside the raw client; an unusable one raises there, before any
        request reaches the gateway.
        """
        self._require_connected()
        return self._guarded(
            lambda: self._client.market_data_for_instrument(
                broker_instrument_ref, requested_market_data_type
            )
        )

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


def _build_market_data_client() -> RawMarketDataClient:
    """Construct the SDK-backed read-only market-data client (run time only).

    Imported lazily from ``poi_market_data_client`` so this module stays
    importable and unit-testable without the SDK and without any network.
    """
    from trading_broker_ibkr.poi_market_data_client import build_market_data_client

    return build_market_data_client()


def create_session(client: RawMarketDataClient | None = None) -> IbkrMarketDataSession:
    """Return a read-only market-data session for the harness.

    ``client`` is injectable for tests and for a hand-wired PoI transport; when
    omitted, the SDK-backed read-only client is built (its IBKR SDK is imported
    only when it actually connects, so it fails closed on a host without
    ``ibapi`` rather than pretending).
    """
    raw = client if client is not None else _build_market_data_client()
    return IbkrMarketDataSession(raw)
