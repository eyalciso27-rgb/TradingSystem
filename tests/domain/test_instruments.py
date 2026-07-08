"""Instrument Master resolver: deterministic, point-in-time, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from trading_domain.instruments import (
    AmbiguousMappingError,
    AmbiguousSymbolError,
    BrokerMappingRecord,
    InstrumentMaster,
    InstrumentRecord,
    ProviderMappingRecord,
    SymbolRecord,
    UnknownSymbolError,
    UnmappedError,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
T2 = datetime(2026, 12, 1, tzinfo=UTC)
NAIVE = datetime(2026, 6, 1)  # noqa: DTZ001 (intentional for the tz test)


def instrument(iid: str) -> InstrumentRecord:
    return InstrumentRecord(
        instrument_id=iid, asset_class="EQUITY", currency="USD", status="ACTIVE"
    )


def test_unambiguous_resolve_succeeds() -> None:
    master = InstrumentMaster(
        [instrument("i1")],
        [SymbolRecord("i1", "AAPL", "NASDAQ", T0)],
    )
    resolved = master.resolve_symbol("AAPL", as_of=T1)
    assert resolved.instrument_id == "i1"
    assert resolved.currency == "USD"


def test_ambiguous_symbol_requires_review_never_autopicks() -> None:
    master = InstrumentMaster(
        [instrument("i1"), instrument("i2")],
        [
            SymbolRecord("i1", "ABC", "NYSE", T0),
            SymbolRecord("i2", "ABC", "LSE", T0),
        ],
    )
    with pytest.raises(AmbiguousSymbolError) as exc:
        master.resolve_symbol("ABC", as_of=T1)
    assert exc.value.candidates == ("i1", "i2")  # both surfaced for review


def test_exchange_disambiguates() -> None:
    master = InstrumentMaster(
        [instrument("i1"), instrument("i2")],
        [
            SymbolRecord("i1", "ABC", "NYSE", T0),
            SymbolRecord("i2", "ABC", "LSE", T0),
        ],
    )
    assert master.resolve_symbol("ABC", as_of=T1, exchange="LSE").instrument_id == "i2"


def test_unknown_symbol_fails_closed() -> None:
    master = InstrumentMaster([instrument("i1")], [SymbolRecord("i1", "AAPL", "NASDAQ", T0)])
    with pytest.raises(UnknownSymbolError):
        master.resolve_symbol("MSFT", as_of=T1)


def test_point_in_time_symbol_window() -> None:
    # symbol belonged to i1 until T1, then reassigned to i2 (half-open window)
    master = InstrumentMaster(
        [instrument("i1"), instrument("i2")],
        [
            SymbolRecord("i1", "SYM", "X", T0, valid_to=T1),
            SymbolRecord("i2", "SYM", "X", T1),
        ],
    )
    assert master.resolve_symbol("SYM", as_of=T0).instrument_id == "i1"
    assert master.resolve_symbol("SYM", as_of=T1).instrument_id == "i2"
    with pytest.raises(UnknownSymbolError):
        master.resolve_symbol("SYM", as_of=datetime(2025, 1, 1, tzinfo=UTC))


def test_resolve_is_order_independent() -> None:
    symbols = [
        SymbolRecord("i1", "AAPL", "NASDAQ", T0),
        SymbolRecord("i1", "AAPL", "NASDAQ", T0),  # duplicate listing, same instrument
    ]
    master = InstrumentMaster([instrument("i1")], list(reversed(symbols)))
    assert master.resolve_symbol("AAPL", as_of=T1).instrument_id == "i1"


def test_provider_mapping_versioned_and_fail_closed() -> None:
    master = InstrumentMaster(
        [instrument("i1")],
        [SymbolRecord("i1", "AAPL", "NASDAQ", T0)],
        provider_mappings=[
            ProviderMappingRecord("i1", "prov", "OLD", T0, valid_to=T1),
            ProviderMappingRecord("i1", "prov", "NEW", T1),
        ],
    )
    assert master.resolve_provider("i1", "prov", as_of=T0) == "OLD"
    assert master.resolve_provider("i1", "prov", as_of=T2) == "NEW"
    with pytest.raises(UnmappedError):
        master.resolve_provider("i1", "other", as_of=T2)


def test_ambiguous_provider_mapping_fails_closed() -> None:
    master = InstrumentMaster(
        [instrument("i1")],
        [SymbolRecord("i1", "AAPL", "NASDAQ", T0)],
        provider_mappings=[
            ProviderMappingRecord("i1", "prov", "A", T0),
            ProviderMappingRecord("i1", "prov", "B", T0),
        ],
    )
    with pytest.raises(AmbiguousMappingError):
        master.resolve_provider("i1", "prov", as_of=T1)


def test_broker_mapping_versioned_with_capabilities() -> None:
    master = InstrumentMaster(
        [instrument("i1")],
        [SymbolRecord("i1", "AAPL", "NASDAQ", T0)],
        broker_mappings=[
            BrokerMappingRecord("i1", "brk", "X123", T0, capabilities=("LIMIT", "STOP")),
        ],
    )
    mapping = master.resolve_broker("i1", "brk", as_of=T1)
    assert mapping.external_instrument_id == "X123"
    assert mapping.capabilities == ("LIMIT", "STOP")
    with pytest.raises(UnmappedError):
        master.resolve_broker("i1", "brk", as_of=datetime(2025, 1, 1, tzinfo=UTC))


def test_dangling_reference_rejected() -> None:
    with pytest.raises(ValueError, match="unknown instrument"):
        InstrumentMaster([instrument("i1")], [SymbolRecord("ghost", "AAPL", "NASDAQ", T0)])


def test_duplicate_instrument_id_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate instrument"):
        InstrumentMaster([instrument("i1"), instrument("i1")], [])


def test_naive_timestamp_rejected() -> None:
    master = InstrumentMaster([instrument("i1")], [SymbolRecord("i1", "AAPL", "NASDAQ", T0)])
    with pytest.raises(ValueError, match="timezone-aware"):
        master.resolve_symbol("AAPL", as_of=NAIVE)
