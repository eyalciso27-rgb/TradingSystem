"""Deterministic position ownership / attribution (MVP, mission section 6).

The smallest safe classifier that decides, for every position the broker
reports, whether the system can attribute it to a known system-opened position
by its EXACT broker ``con_id`` (contract id). Exact ``con_id`` identity outranks
ticker equality: two securities can share a ticker string and differ as
contracts, so a ticker match is never sufficient to attribute a position.

Fail-closed rules (mission section 6 + section 9):

- A broker position whose ``con_id`` matches no system record is
  UNATTRIBUTABLE — its exposure is unknown, never zero, and a new entry is
  BLOCKED while it exists.
- A ``con_id`` that matches a record but whose symbol disagrees is a
  CONID_SYMBOL_MISMATCH — a corrupt identity, fail closed and block.
- A system record with no matching broker position is MISSING_AT_BROKER —
  the system believes it holds something the broker does not confirm; that is
  an unknown critical state, so reconcile first and block new entries.
- Only when EVERY reported position is cleanly ATTRIBUTED is a new entry
  permitted, and even then only the attributed exposures (built from the
  system record's OWN durable facts, never from the incoming candidate) flow
  into the risk/portfolio caps.

This module never talks to a broker and never sizes or authorizes anything; it
is a pure function over frozen fixtures (ADR-0014). Exposure facts are carried
as a neutral :class:`AttributedExposure` — the caller maps those into the Risk
Engine's ``PositionExposure``, so this module stays free of risk-engine types.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

ATTRIBUTION_VERSION = "position-attribution-1.0.0"


class PositionAttributionError(Exception):
    """Fail-closed attribution failure (duplicate identity, lookahead)."""


class PositionAttribution(StrEnum):
    """How a reported position relates to the system's own records."""

    ATTRIBUTED = "ATTRIBUTED"
    UNATTRIBUTABLE = "UNATTRIBUTABLE"
    CONID_SYMBOL_MISMATCH = "CONID_SYMBOL_MISMATCH"
    MISSING_AT_BROKER = "MISSING_AT_BROKER"


@dataclass(frozen=True, slots=True)
class BrokerHeldPosition:
    """One position the broker reports it holds (frozen reconciliation fact).

    ``con_id`` is the broker's durable contract identity — the authoritative
    key. ``notional`` is the current gross exposure of the position.
    """

    con_id: int
    symbol: str
    quantity: Decimal
    notional: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise PositionAttributionError("observed_at must be timezone-aware")
        if not self.symbol.strip():
            raise PositionAttributionError("a held position must carry a symbol")


@dataclass(frozen=True, slots=True)
class SystemPositionRecord:
    """What the system durably recorded when IT opened a position.

    Carries the exact ``con_id`` the entry was placed against plus the
    attribution facts (strategy/source/sector/cluster) and the planned open
    risk. This is the ONLY authoritative source of a position's classification —
    it is never inferred from an incoming candidate.
    """

    con_id: int
    symbol: str
    strategy_id: str
    source_id: str
    sector: str
    cluster: str
    open_risk_amount: Decimal
    notional: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise PositionAttributionError("a system record must carry a symbol")
        if self.open_risk_amount < 0 or self.notional < 0:
            raise PositionAttributionError("open risk and notional must be non-negative")


@dataclass(frozen=True, slots=True)
class AttributedExposure:
    """Neutral exposure facts of one cleanly attributed position.

    Deliberately not a Risk Engine ``PositionExposure``: the caller maps this
    into that type. Every field comes from the SYSTEM RECORD, so an existing
    position's sector/cluster/source can never be fabricated from the candidate.
    """

    con_id: int
    symbol: str
    sector: str
    cluster: str
    source_id: str
    strategy_id: str
    open_risk_amount: Decimal
    notional: Decimal


@dataclass(frozen=True, slots=True)
class ClassifiedPosition:
    """One position's attribution outcome + reason (audit/debug)."""

    con_id: int
    symbol: str
    attribution: PositionAttribution
    reason: str
    exposure: AttributedExposure | None


@dataclass(frozen=True, slots=True)
class PortfolioClassification:
    """The whole portfolio's attribution result.

    ``new_entry_blocked`` is True whenever ANY position is not cleanly
    ATTRIBUTED — the conservative, fail-closed outcome for new entries. The
    ``attributed_exposures`` are only the cleanly attributed ones; an unknown
    or missing position never becomes a zero-exposure entry in this list.
    """

    as_of: datetime
    classified: tuple[ClassifiedPosition, ...]
    attributed_exposures: tuple[AttributedExposure, ...]
    new_entry_blocked: bool
    block_reason_codes: tuple[str, ...]
    version: str = ATTRIBUTION_VERSION


def classify_positions(
    held: Sequence[BrokerHeldPosition],
    system_records: Sequence[SystemPositionRecord],
    *,
    as_of: datetime,
) -> PortfolioClassification:
    """Classify every reported position by exact ``con_id`` identity."""
    if as_of.tzinfo is None:
        raise PositionAttributionError("as_of must be timezone-aware")

    held_ids = [position.con_id for position in held]
    if len(held_ids) != len(set(held_ids)):
        raise PositionAttributionError("duplicate con_id among broker positions; fail closed")
    record_ids = [record.con_id for record in system_records]
    if len(record_ids) != len(set(record_ids)):
        raise PositionAttributionError("duplicate con_id among system records; fail closed")

    for position in held:
        if position.observed_at > as_of:
            raise PositionAttributionError("lookahead: a broker position is newer than as_of")

    records_by_conid = {record.con_id: record for record in system_records}
    held_conids = set(held_ids)

    classified: list[ClassifiedPosition] = []
    attributed: list[AttributedExposure] = []
    block_reasons: list[str] = []

    for position in held:
        record = records_by_conid.get(position.con_id)
        if record is None:
            classified.append(
                ClassifiedPosition(
                    con_id=position.con_id,
                    symbol=position.symbol,
                    attribution=PositionAttribution.UNATTRIBUTABLE,
                    reason="no system record for this con_id; exposure is unknown",
                    exposure=None,
                )
            )
            block_reasons.append(f"POSITION_UNATTRIBUTABLE_CONID_{position.con_id}")
            continue
        if record.symbol != position.symbol:
            classified.append(
                ClassifiedPosition(
                    con_id=position.con_id,
                    symbol=position.symbol,
                    attribution=PositionAttribution.CONID_SYMBOL_MISMATCH,
                    reason=(
                        f"con_id {position.con_id} maps to record symbol"
                        f" {record.symbol!r} but broker reports {position.symbol!r}"
                    ),
                    exposure=None,
                )
            )
            block_reasons.append(f"CONID_SYMBOL_MISMATCH_{position.con_id}")
            continue
        exposure = _exposure_from_record(record)
        classified.append(
            ClassifiedPosition(
                con_id=position.con_id,
                symbol=position.symbol,
                attribution=PositionAttribution.ATTRIBUTED,
                reason="con_id matches a system record with a consistent symbol",
                exposure=exposure,
            )
        )
        attributed.append(exposure)

    for record in system_records:
        if record.con_id in held_conids:
            continue
        classified.append(
            ClassifiedPosition(
                con_id=record.con_id,
                symbol=record.symbol,
                attribution=PositionAttribution.MISSING_AT_BROKER,
                reason="system record has no matching broker position; reconcile first",
                exposure=None,
            )
        )
        block_reasons.append(f"POSITION_MISSING_AT_BROKER_CONID_{record.con_id}")

    new_entry_blocked = bool(block_reasons)
    return PortfolioClassification(
        as_of=as_of,
        classified=tuple(classified),
        attributed_exposures=tuple(attributed),
        new_entry_blocked=new_entry_blocked,
        block_reason_codes=tuple(dict.fromkeys(block_reasons)),
    )


def _exposure_from_record(record: SystemPositionRecord) -> AttributedExposure:
    """Build neutral exposure facts from a system record's OWN fields.

    Every value is bound to a local first, so no exposure field is written as a
    direct candidate attribute access — an existing position's classification
    comes only from the record the system durably kept.
    """
    con_id = record.con_id
    symbol = record.symbol
    sector = record.sector
    cluster = record.cluster
    source_id = record.source_id
    strategy_id = record.strategy_id
    open_risk_amount = record.open_risk_amount
    notional = record.notional
    return AttributedExposure(
        con_id=con_id,
        symbol=symbol,
        sector=sector,
        cluster=cluster,
        source_id=source_id,
        strategy_id=strategy_id,
        open_risk_amount=open_risk_amount,
        notional=notional,
    )


__all__ = [
    "ATTRIBUTION_VERSION",
    "AttributedExposure",
    "BrokerHeldPosition",
    "ClassifiedPosition",
    "PortfolioClassification",
    "PositionAttribution",
    "PositionAttributionError",
    "SystemPositionRecord",
    "classify_positions",
]
