"""Load the Instrument Master from PostgreSQL (IMP-0701).

Infrastructure half: reads the instrument-master rows and constructs the pure
deterministic ``trading_domain.instruments.InstrumentMaster`` (no resolution
logic lives here). Point-in-time resolution is done by the domain resolver.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Connection
from trading_domain.instruments import (
    BrokerMappingRecord,
    InstrumentMaster,
    InstrumentRecord,
    ProviderMappingRecord,
    SymbolRecord,
)

from trading_persistence.models import (
    BrokerInstrumentMapping,
    Instrument,
    InstrumentSymbol,
    ProviderInstrumentMapping,
)


def build_instrument_master(connection: Connection) -> InstrumentMaster:
    """Construct an in-memory InstrumentMaster snapshot from the database."""
    instruments = [
        InstrumentRecord(
            instrument_id=str(row.id),
            asset_class=row.asset_class,
            currency=row.currency,
            status=row.status,
        )
        for row in connection.execute(
            select(
                Instrument.id,
                Instrument.asset_class,
                Instrument.currency,
                Instrument.status,
            )
        )
    ]
    symbols = [
        SymbolRecord(
            instrument_id=str(row.instrument_id),
            symbol=row.symbol,
            exchange=row.exchange,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
        )
        for row in connection.execute(
            select(
                InstrumentSymbol.instrument_id,
                InstrumentSymbol.symbol,
                InstrumentSymbol.exchange,
                InstrumentSymbol.valid_from,
                InstrumentSymbol.valid_to,
            )
        )
    ]
    provider_mappings = [
        ProviderMappingRecord(
            instrument_id=str(row.instrument_id),
            provider_id=row.provider_id,
            external_instrument_id=row.external_instrument_id,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
        )
        for row in connection.execute(
            select(
                ProviderInstrumentMapping.instrument_id,
                ProviderInstrumentMapping.provider_id,
                ProviderInstrumentMapping.external_instrument_id,
                ProviderInstrumentMapping.valid_from,
                ProviderInstrumentMapping.valid_to,
            )
        )
    ]
    broker_mappings = [
        BrokerMappingRecord(
            instrument_id=str(row.instrument_id),
            broker_id=row.broker_id,
            external_instrument_id=row.external_instrument_id,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            capabilities=tuple(row.capabilities),
        )
        for row in connection.execute(
            select(
                BrokerInstrumentMapping.instrument_id,
                BrokerInstrumentMapping.broker_id,
                BrokerInstrumentMapping.external_instrument_id,
                BrokerInstrumentMapping.capabilities,
                BrokerInstrumentMapping.valid_from,
                BrokerInstrumentMapping.valid_to,
            )
        )
    ]
    return InstrumentMaster(instruments, symbols, provider_mappings, broker_mappings)
