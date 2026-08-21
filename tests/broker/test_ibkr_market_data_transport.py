"""The REAL market-data transport, against a stub IBKR SDK.

``tests/integration/test_production_quote_binding.py`` drives the real quote
source, session and raw client -- but it replaces the whole
``MarketDataTransport``, and that replacement builds its own contract. So it
proves the chain *asks* for a con id; it does not prove what
``_build_ibapi_transport`` hands to ``EClient.reqMktData``.

This file closes that gap the same way ``test_ibkr_cancel_transport`` does for
the order path: install a stub ``ibapi``, build the ACTUAL production transport,
and assert on the ``Contract`` the production code constructed. No duplicate
builder exists for tests to check instead.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest


class _StubContract:
    """``ibapi.contract.Contract``: bare attributes, assigned after init."""

    def __init__(self) -> None:
        self.conId = 0  # noqa: N815 - the SDK's own spelling
        self.symbol = ""
        self.secType = ""  # noqa: N815
        self.exchange = ""
        self.currency = ""


class _StubEWrapper:
    def __init__(self) -> None:  # pragma: no cover - trivial
        pass


class _StubEClient:
    """Records exactly what the production transport asked the SDK to do."""

    def __init__(self, wrapper: object) -> None:
        self.calls: list[tuple[str, Any]] = []

    def reqMktData(  # noqa: N802
        self,
        req_id: int,
        contract: object,
        generic_ticks: str,
        snapshot: bool,
        regulatory: bool,
        options: list[object],
    ) -> None:
        self.calls.append(
            ("reqMktData", (req_id, contract, generic_ticks, snapshot, regulatory))
        )

    def cancelMktData(self, req_id: int) -> None:  # noqa: N802
        self.calls.append(("cancelMktData", req_id))

    def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802
        self.calls.append(("reqMarketDataType", market_data_type))

    def reqCurrentTime(self) -> None:  # noqa: N802
        self.calls.append(("reqCurrentTime", ()))

    def reqContractDetails(self, req_id: int, contract: object) -> None:  # noqa: N802
        self.calls.append(("reqContractDetails", (req_id, contract)))

    def reqAccountSummary(self, req_id: int, group: str, tags: str) -> None:  # noqa: N802
        self.calls.append(("reqAccountSummary", (req_id, group, tags)))

    def cancelAccountSummary(self, req_id: int) -> None:  # noqa: N802
        self.calls.append(("cancelAccountSummary", req_id))

    def isConnected(self) -> bool:  # noqa: N802
        return False

    def connect(self, host: str, port: int, client_id: int) -> None:
        self.calls.append(("connect", (host, port, client_id)))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))

    def run(self) -> None:  # pragma: no cover - the transport's own thread
        pass


def _install_stub_ibapi(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("ibapi")
    package.__path__ = []  # marks it as a package so submodules resolve
    modules = {
        "ibapi": package,
        "ibapi.client": types.ModuleType("ibapi.client"),
        "ibapi.contract": types.ModuleType("ibapi.contract"),
        "ibapi.wrapper": types.ModuleType("ibapi.wrapper"),
    }
    for module_name, attribute, stub in (
        ("ibapi.client", "EClient", _StubEClient),
        ("ibapi.contract", "Contract", _StubContract),
        ("ibapi.wrapper", "EWrapper", _StubEWrapper),
    ):
        setattr(modules[module_name], attribute, stub)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The REAL market-data transport, wired to the stub SDK."""
    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.poi_market_data_client import _build_ibapi_transport

    yield _build_ibapi_transport()


def _app(transport: Any) -> Any:
    return transport._app  # noqa: SLF001


def _requests(transport: Any) -> list[Any]:
    return [args for name, args in _app(transport).calls if name == "reqMktData"]


# == the outgoing contract names the resolved security ======================


def test_production_req_mkt_data_uses_the_resolved_con_id(transport: Any) -> None:
    """THE proof, at the SDK boundary the owner host actually calls."""
    transport.request_market_data_for_con_id(9301, 265598)

    (req_id, contract, generic, snapshot, regulatory), = _requests(transport)

    assert req_id == 9301, "the request id the caller allocated"
    assert contract.conId == 265598, "the RESOLVED security"
    assert isinstance(contract, _StubContract), "the SDK's own Contract type"
    # snapshot=True auto-completes via tickSnapshotEnd; regulatorySnapshot=False
    # avoids any paid snapshot fee. Both are load-bearing on a paper account.
    assert snapshot is True
    assert regulatory is False
    assert generic == ""


def test_the_outgoing_contract_carries_no_ticker_identity(transport: Any) -> None:
    """A symbol here would be a SECOND identity for one request.

    Two identities can disagree, and if they did, IBKR -- not us -- would decide
    which one wins. The con id alone is unambiguous.
    """
    transport.request_market_data_for_con_id(9301, 265598)

    (_req_id, contract, *_rest), = _requests(transport)

    assert contract.symbol == "", (
        f"the market-data contract carried a ticker ({contract.symbol!r})"
    )
    assert contract.conId == 265598


def test_each_snapshot_is_requested_and_cancelled_under_its_own_id(
    transport: Any,
) -> None:
    """The transport passes the caller's id straight through, both ways."""
    transport.request_market_data_for_con_id(9301, 265598)
    transport.cancel_market_data(9301)
    transport.request_market_data_for_con_id(9302, 4391)
    transport.cancel_market_data(9302)

    calls = [
        (name, args)
        for name, args in _app(transport).calls
        if name in {"reqMktData", "cancelMktData"}
    ]
    assert [name for name, _args in calls] == [
        "reqMktData", "cancelMktData", "reqMktData", "cancelMktData"
    ]
    assert calls[0][1][0] == 9301
    assert calls[1][1] == 9301
    assert calls[2][1][0] == 9302
    assert calls[3][1] == 9302
    assert [args[1].conId for name, args in calls if name == "reqMktData"] == [
        265598, 4391
    ]


def test_the_legacy_ticker_request_still_names_a_symbol(transport: Any) -> None:
    """The read-only PoI path is preserved, and is visibly the other shape.

    Kept deliberately: tooling that predates contract resolution still uses it.
    The execution path cannot reach it -- the session protocol the quote source
    depends on does not declare it -- and this test documents the difference
    rather than pretending the method is gone.
    """
    transport.request_market_data(9301, "AAPL")

    (_req_id, contract, *_rest), = _requests(transport)

    assert contract.symbol == "AAPL"
    assert contract.conId == 0, "the legacy path never had a security identity"


def test_the_market_data_type_request_reaches_the_sdk(transport: Any) -> None:
    """Asking for realtime is a real request, not an assumption."""
    transport.set_market_data_type(1)

    assert ("reqMarketDataType", 1) in _app(transport).calls
