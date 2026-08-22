"""The PRODUCTION quote leg, bound end to end and driven by identity.

This file exists because the previous round moved the *contract*
(``QuoteSource.current_quote(BrokerInstrument)``) and the *fakes* to the
resolved security, and left the real implementation behind: ``IbkrQuoteSource``
still took a ticker, ``IbkrMarketDataSession.req_market_data`` still took a
ticker, and the SDK transport still built ``reqMktData`` from a symbol-shaped
stock contract. Every test passed, because every test used a fake.

So nothing here is a fake quote source. The chain under test is:

    IbkrQuoteSource            (the production QuoteSource implementation)
      -> IbkrMarketDataSession (the real session, with its safety gates)
      -> IbkrMarketDataRawClient
      -> transport seam        (a stub SDK, so the assertion lands on the
                                Contract the production code actually builds)

The one thing stubbed is the SDK itself, which is exactly where the identity
has to be observed.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from trading_application.readiness_guarded_broker import QuoteSource
from trading_application.trading_readiness_runtime import MarketDataState
from trading_broker_core.port import BrokerInstrument
from trading_broker_ibkr.poi_market_data_client import (
    IbkrMarketDataRawClient,
    MarketDataSink,
)
from trading_broker_ibkr.poi_market_data_session import IbkrMarketDataSession
from trading_execution_agent.ibkr_quote_source import IbkrQuoteSource

CAPTURED_AT = "2026-08-19T15:30:01+00:00"

BID, ASK, LAST = 1, 2, 4
BID_SIZE, ASK_SIZE, LAST_SIZE = 0, 3, 5

#: The security. Resolved by the broker port in production; stated here.
AAPL = BrokerInstrument(
    broker_instrument_ref="conid:265598",
    symbol="AAPL",
    exchange="SMART",
    currency="USD",
)


class _StubContract:
    """Shaped like ``ibapi.contract.Contract``: attributes, set after init."""

    def __init__(self) -> None:
        self.conId = 0  # noqa: N815 - the SDK's own spelling
        self.symbol = ""
        self.secType = ""  # noqa: N815
        self.exchange = ""
        self.currency = ""


class RecordingMarketData:
    """The SDK seam. Records the CONTRACT production built, then replays ticks."""

    def __init__(self) -> None:
        self.contracts: list[_StubContract] = []
        self.calls: list[str] = []
        self._sink: MarketDataSink | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, host: str, port: int, client_id: int, sink: MarketDataSink) -> None:
        self.calls.append(f"start:{host}:{port}:{client_id}")
        self._sink = sink
        sink.on_ready()

    def stop(self) -> None:
        self._sink = None

    def request_current_time(self) -> None:
        assert self._sink is not None
        self._sink.on_current_time(0)

    def request_account_summary(self, req_id: int, tags: str) -> None:
        raise NotImplementedError

    def cancel_account_summary(self, req_id: int) -> None:
        raise NotImplementedError

    def request_contract_details(self, req_id: int, symbol: str) -> None:
        raise NotImplementedError

    def set_market_data_type(self, market_data_type: int) -> None:
        self.calls.append(f"set_market_data_type:{market_data_type}")

    # -- the two market-data requests --------------------------------------
    def request_market_data(self, req_id: int, symbol: str) -> None:
        """The legacy ticker path. The execution chain must never reach it."""
        self.calls.append(f"reqMktData_by_symbol:{symbol}")
        raise AssertionError(
            "the production execution quote path requested market data by"
            " ticker after a security had already been resolved"
        )

    def request_market_data_for_con_id(self, req_id: int, con_id: int) -> None:
        contract = _StubContract()
        contract.conId = con_id
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        self.contracts.append(contract)
        self.calls.append(f"reqMktData_by_con_id:{con_id}")
        self._replay(req_id)

    def cancel_market_data(self, req_id: int) -> None:
        self.calls.append("cancel_market_data")

    def _replay(self, req_id: int) -> None:
        """Every callback carries its request id, as the real wrapper does."""
        assert self._sink is not None
        self._sink.on_market_data_type(1, req_id)
        self._sink.on_tick_price(BID, 100.00, req_id)
        self._sink.on_tick_price(ASK, 100.05, req_id)
        self._sink.on_tick_price(LAST, 100.02, req_id)
        self._sink.on_tick_size(BID_SIZE, 500, req_id)
        self._sink.on_tick_size(ASK_SIZE, 400, req_id)
        self._sink.on_tick_size(LAST_SIZE, 100, req_id)
        self._sink.on_snapshot_end(req_id)


def _production_chain() -> tuple[IbkrQuoteSource, RecordingMarketData]:
    """The REAL components, with only the SDK stubbed."""
    transport = RecordingMarketData()
    client = IbkrMarketDataRawClient(
        lambda: transport,
        now=lambda: CAPTURED_AT,
        timeout_seconds=1.0,
    )
    session = IbkrMarketDataSession(client)
    session.connect("127.0.0.1", 4002)
    return IbkrQuoteSource(session), transport


# == the request names the resolved security ================================


def test_the_production_quote_path_requests_the_resolved_con_id() -> None:
    """THE regression. On the previous head this asked for "AAPL"."""
    source, transport = _production_chain()

    state = source.current_quote(AAPL)

    assert len(transport.contracts) == 1, "exactly one market-data request"
    contract = transport.contracts[0]
    assert contract.conId == 265598, (
        "reqMktData was built for the RESOLVED security, not a ticker"
    )
    # And the quote itself is usable, so this is not passing by failing.
    assert state.bid == Decimal("100.00")
    assert state.ask == Decimal("100.05")
    assert state.market_data_state is MarketDataState.REALTIME


def test_the_production_quote_path_never_requests_by_ticker() -> None:
    """The ticker seam raises if touched; this asserts it was not."""
    source, transport = _production_chain()

    source.current_quote(AAPL)

    assert not any(
        call.startswith("reqMktData_by_symbol") for call in transport.calls
    ), transport.calls
    assert [c for c in transport.calls if c.startswith("reqMktData")] == [
        "reqMktData_by_con_id:265598"
    ]


def test_the_quote_states_which_security_it_was_requested_for() -> None:
    """Attribution from the REQUEST lineage, not from a returned ticker."""
    source, _ = _production_chain()

    state = source.current_quote(AAPL)

    assert "requested_for=conid:265598" in state.detail, state.detail


# == unusable identities make NO request ====================================


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("zero con id", "conid:0"),
        ("negative con id", "conid:-3"),
        ("empty body", "conid:"),
        ("non numeric", "conid:abc"),
        ("foreign scheme", "research:AAPL"),
        ("bare ticker", "AAPL"),
    ],
)
def test_an_unusable_reference_makes_no_market_data_request(
    name: str, reference: str
) -> None:
    """Fail closed BEFORE the gateway is asked anything.

    No request at all -- not a request that happens to fail. A security we
    cannot name is a security we will not price, and the readiness gate then
    refuses for a stated reason.
    """
    source, transport = _production_chain()

    state = source.current_quote(
        BrokerInstrument(
            broker_instrument_ref=reference,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
        )
    )

    assert state.market_data_state is MarketDataState.UNAVAILABLE, name
    assert state.bid is None and state.ask is None, name
    assert transport.contracts == [], f"{name}: a request was made anyway"
    assert not any(call.startswith("reqMktData") for call in transport.calls), name
    assert reference in state.detail, "the refusal names what it refused"


def test_a_blank_reference_is_structurally_impossible() -> None:
    """The type refuses it, so it cannot reach the quote path at all."""
    with pytest.raises(ValueError, match="broker reference"):
        BrokerInstrument(
            broker_instrument_ref="", symbol="AAPL", exchange="SMART", currency="USD"
        )


# == the production wiring is statically bound ==============================


def test_the_production_quote_source_satisfies_the_canonical_contract() -> None:
    """Structural: the real implementation IS a QuoteSource.

    The drift this catches is exactly what happened: the protocol moved to
    ``BrokerInstrument`` and the production class kept taking a ticker, while a
    ``type: ignore`` in the probe wiring hid it.
    """
    source, _ = _production_chain()
    assert isinstance(source, QuoteSource)

    signature = inspect.signature(IbkrQuoteSource.current_quote)
    instrument = signature.parameters["instrument"]
    assert instrument.annotation in (BrokerInstrument, "BrokerInstrument")
    assert "symbol" not in signature.parameters


def test_the_owner_probe_binds_the_quote_source_without_suppression() -> None:
    """The probe's own builder, typed, with the suppression removed."""
    from trading_execution_agent import __main__ as probe

    builder = inspect.signature(probe._quote_source)  # noqa: SLF001
    assert builder.return_annotation in (QuoteSource, "QuoteSource")

    wiring = inspect.getsource(probe._build)  # noqa: SLF001
    quote_line = next(
        line for line in wiring.splitlines() if "quotes=" in line
    )
    assert "type: ignore" not in quote_line, (
        "the quote binding must not be suppressed; that suppression is what"
        f" hid the ticker drift ({quote_line.strip()})"
    )
