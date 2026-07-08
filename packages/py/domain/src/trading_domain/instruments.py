"""Instrument Master — deterministic, point-in-time instrument resolution.

Authority: Data_Model_and_Retention_Specification_v0.6 section 24.1
(instruments, instrument_symbols, provider_instrument_mappings,
broker_instrument_mappings) and section 24.2 (point-in-time fields). This is
pure domain logic: no adapters, no I/O, no SDK imports. It resolves an
internal instrument identity from a symbol (as of a point in time) and
resolves provider/broker external identifiers from an internal instrument.

Non-negotiable behavior:

- **Deterministic**: the same records + ``as_of`` always yield the same
  result, independent of input ordering.
- **Ambiguity requires review, never a guess**: a symbol that maps to more
  than one internal instrument at ``as_of`` raises ``AmbiguousSymbolError``
  carrying every candidate — the caller must route it to human review. A
  ticker is never auto-picked. Distinct instruments are never merged without
  an explicit alias/mapping.
- **Fail closed**: an unknown symbol, or a missing / expired / ambiguous
  provider or broker mapping, raises rather than returning a default.
- **Point-in-time**: a record is active at ``as_of`` when
  ``valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)`` — the
  same half-open semantics used for replay/backtest as-of joins.

The seven distinct concepts are kept separate: an internal instrument, a
symbol, an alias, an exchange, a currency, a provider mapping and a broker
mapping. Future sources (Telegram/Discord/WhatsApp/News/Market Data/IBKR)
resolve *through* this master; none is wired here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime


class InstrumentResolutionError(Exception):
    """Base class for fail-closed instrument-resolution failures."""


class UnknownSymbolError(InstrumentResolutionError):
    """No instrument symbol matches at the requested point in time."""


class AmbiguousSymbolError(InstrumentResolutionError):
    """A symbol maps to more than one instrument: human review required."""

    def __init__(self, symbol: str, candidates: Iterable[str]) -> None:
        self.symbol = symbol
        self.candidates = tuple(sorted(candidates))
        super().__init__(
            f"symbol {symbol!r} is ambiguous across instruments "
            f"{list(self.candidates)}; requires review (never auto-selected)"
        )


class UnmappedError(InstrumentResolutionError):
    """No active provider/broker mapping for the instrument at ``as_of``."""


class AmbiguousMappingError(InstrumentResolutionError):
    """More than one active provider/broker mapping: fail closed."""


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    """A canonical internal instrument (identity, not a market symbol)."""

    instrument_id: str
    asset_class: str
    currency: str
    status: str


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """A time-aware symbol/exchange listing for one internal instrument."""

    instrument_id: str
    symbol: str
    exchange: str | None
    valid_from: datetime
    valid_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderMappingRecord:
    """A time-aware mapping to a data provider's external instrument id."""

    instrument_id: str
    provider_id: str
    external_instrument_id: str
    valid_from: datetime
    valid_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class BrokerMappingRecord:
    """A time-aware mapping to a broker's external instrument id."""

    instrument_id: str
    broker_id: str
    external_instrument_id: str
    valid_from: datetime
    valid_to: datetime | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)


def _active_at(valid_from: datetime, valid_to: datetime | None, as_of: datetime) -> bool:
    """Half-open point-in-time window: [valid_from, valid_to)."""
    if as_of.tzinfo is None or valid_from.tzinfo is None:
        raise ValueError("as_of and valid_from must be timezone-aware")
    if as_of < valid_from:
        return False
    return valid_to is None or as_of < valid_to


class InstrumentMaster:
    """Deterministic resolver over a snapshot of instrument-master records."""

    def __init__(
        self,
        instruments: Iterable[InstrumentRecord],
        symbols: Iterable[SymbolRecord],
        provider_mappings: Iterable[ProviderMappingRecord] = (),
        broker_mappings: Iterable[BrokerMappingRecord] = (),
    ) -> None:
        self._instruments: dict[str, InstrumentRecord] = {}
        for instrument in instruments:
            if instrument.instrument_id in self._instruments:
                raise ValueError(f"duplicate instrument id {instrument.instrument_id!r}")
            self._instruments[instrument.instrument_id] = instrument
        self._symbols = tuple(symbols)
        self._provider_mappings = tuple(provider_mappings)
        self._broker_mappings = tuple(broker_mappings)
        # Every mapping/symbol must reference a known instrument (fail closed
        # on a dangling reference rather than silently resolving to nothing).
        for symbol in self._symbols:
            self._require_instrument(symbol.instrument_id)
        for provider in self._provider_mappings:
            self._require_instrument(provider.instrument_id)
        for broker in self._broker_mappings:
            self._require_instrument(broker.instrument_id)

    def _require_instrument(self, instrument_id: str) -> InstrumentRecord:
        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            raise ValueError(f"reference to unknown instrument {instrument_id!r}")
        return instrument

    def resolve_symbol(
        self, symbol: str, *, as_of: datetime, exchange: str | None = None
    ) -> InstrumentRecord:
        """Resolve a symbol to exactly one internal instrument at ``as_of``.

        ``exchange`` disambiguates when a symbol is listed on several venues;
        without it, multiple venues for the same symbol are ambiguous.
        """
        matches = [
            record
            for record in self._symbols
            if record.symbol == symbol
            and (exchange is None or record.exchange == exchange)
            and _active_at(record.valid_from, record.valid_to, as_of)
        ]
        instrument_ids = {record.instrument_id for record in matches}
        if not instrument_ids:
            raise UnknownSymbolError(
                f"no instrument for symbol {symbol!r}"
                + (f" on exchange {exchange!r}" if exchange else "")
                + f" as of {as_of.isoformat()}"
            )
        if len(instrument_ids) > 1:
            raise AmbiguousSymbolError(symbol, instrument_ids)
        return self._instruments[instrument_ids.pop()]

    def resolve_provider(
        self, instrument_id: str, provider_id: str, *, as_of: datetime
    ) -> str:
        """Return the provider's external instrument id (fail closed)."""
        self._require_instrument(instrument_id)
        externals = {
            record.external_instrument_id
            for record in self._provider_mappings
            if record.instrument_id == instrument_id
            and record.provider_id == provider_id
            and _active_at(record.valid_from, record.valid_to, as_of)
        }
        return self._single(externals, provider_id, instrument_id, "provider")

    def resolve_broker(
        self, instrument_id: str, broker_id: str, *, as_of: datetime
    ) -> BrokerMappingRecord:
        """Return the active broker mapping (with capabilities); fail closed."""
        self._require_instrument(instrument_id)
        matches = [
            record
            for record in self._broker_mappings
            if record.instrument_id == instrument_id
            and record.broker_id == broker_id
            and _active_at(record.valid_from, record.valid_to, as_of)
        ]
        externals = {record.external_instrument_id for record in matches}
        self._single(externals, broker_id, instrument_id, "broker")
        return matches[0]

    @staticmethod
    def _single(externals: set[str], target: str, instrument_id: str, kind: str) -> str:
        if not externals:
            raise UnmappedError(
                f"no active {kind} mapping for instrument {instrument_id!r} "
                f"and {kind} {target!r}"
            )
        if len(externals) > 1:
            raise AmbiguousMappingError(
                f"multiple active {kind} mappings for instrument "
                f"{instrument_id!r} and {kind} {target!r}: {sorted(externals)}"
            )
        return externals.pop()


__all__ = [
    "AmbiguousMappingError",
    "AmbiguousSymbolError",
    "BrokerMappingRecord",
    "InstrumentMaster",
    "InstrumentRecord",
    "InstrumentResolutionError",
    "ProviderMappingRecord",
    "SymbolRecord",
    "UnknownSymbolError",
    "UnmappedError",
]
