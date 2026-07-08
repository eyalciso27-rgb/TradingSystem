"""Instrument Master persistence + resolution (IMP-0701).

PostgreSQL-only (runs in CI where DATABASE_URL is set).
"""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert
from trading_domain.instruments import AmbiguousSymbolError, UnmappedError
from trading_persistence import models as m
from trading_persistence.instruments import build_instrument_master

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL not set; instrument tests run in CI only.", allow_module_level=True
    )

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ASOF = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture()
def clean(engine: Any) -> Any:
    tables = (
        m.BrokerInstrumentMapping,
        m.ProviderInstrumentMapping,
        m.InstrumentSymbol,
        m.Instrument,
    )
    with engine.begin() as conn:
        for table in tables:
            conn.execute(delete(table))
    yield engine
    with engine.begin() as conn:
        for table in tables:
            conn.execute(delete(table))


def _instrument(conn: Any, symbol: str, venue: str) -> Any:
    iid = uuid4()
    conn.execute(
        insert(m.Instrument).values(
            id=iid, symbol=symbol, venue=venue, asset_class="EQUITY",
            currency="USD", status="ACTIVE", created_at=NOW, updated_at=NOW,
        )
    )
    return iid


def test_build_and_resolve_from_db(clean: Any) -> None:
    with clean.begin() as conn:
        iid = _instrument(conn, "AAPL", "NASDAQ")
        conn.execute(
            insert(m.InstrumentSymbol).values(
                id=uuid4(), instrument_id=iid, symbol="AAPL",
                exchange="NASDAQ", valid_from=NOW, valid_to=None,
            )
        )
        conn.execute(
            insert(m.ProviderInstrumentMapping).values(
                id=uuid4(), instrument_id=iid, provider_id="prov",
                external_instrument_id="P-1", valid_from=NOW, valid_to=None,
            )
        )
        conn.execute(
            insert(m.BrokerInstrumentMapping).values(
                id=uuid4(), instrument_id=iid, broker_id="brk",
                external_instrument_id="B-1", capabilities=["LIMIT", "STOP"],
                valid_from=NOW, valid_to=None,
            )
        )
    with clean.connect() as conn:
        master = build_instrument_master(conn)
    resolved = master.resolve_symbol("AAPL", as_of=ASOF)
    assert master.resolve_provider(resolved.instrument_id, "prov", as_of=ASOF) == "P-1"
    broker = master.resolve_broker(resolved.instrument_id, "brk", as_of=ASOF)
    assert broker.external_instrument_id == "B-1" and "STOP" in broker.capabilities


def test_ambiguous_symbol_from_db_requires_review(clean: Any) -> None:
    with clean.begin() as conn:
        i1 = _instrument(conn, "ABC", "NYSE")
        i2 = _instrument(conn, "ABC", "LSE")
        for iid, ex in ((i1, "NYSE"), (i2, "LSE")):
            conn.execute(
                insert(m.InstrumentSymbol).values(
                    id=uuid4(), instrument_id=iid, symbol="ABC",
                    exchange=ex, valid_from=NOW, valid_to=None,
                )
            )
    with clean.connect() as conn:
        master = build_instrument_master(conn)
    with pytest.raises(AmbiguousSymbolError):
        master.resolve_symbol("ABC", as_of=ASOF)
    # exchange disambiguates
    assert master.resolve_symbol("ABC", as_of=ASOF, exchange="LSE") is not None


def test_missing_mapping_fails_closed_from_db(clean: Any) -> None:
    with clean.begin() as conn:
        iid = _instrument(conn, "AAPL", "NASDAQ")
        conn.execute(
            insert(m.InstrumentSymbol).values(
                id=uuid4(), instrument_id=iid, symbol="AAPL",
                exchange="NASDAQ", valid_from=NOW, valid_to=None,
            )
        )
    with clean.connect() as conn:
        master = build_instrument_master(conn)
    resolved = master.resolve_symbol("AAPL", as_of=ASOF)
    with pytest.raises(UnmappedError):
        master.resolve_provider(resolved.instrument_id, "prov", as_of=ASOF)
