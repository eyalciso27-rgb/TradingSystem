"""Point-in-time Dataset V1 contract.

The contract is deliberately provider-neutral. It models the information a
research/backtest dataset must carry to avoid survivorship and look-ahead bias;
it does not claim that any external source has supplied those fields.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path


class CapabilityState(StrEnum):
    """Evidence state for one Dataset V1 capability."""

    EMPIRICALLY_VERIFIED = "EMPIRICALLY_VERIFIED"
    CONTRACT_READY = "CONTRACT_READY"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"


class CorporateActionType(StrEnum):
    SPLIT = "SPLIT"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    observed_at: datetime
    source_record_id: str | None = None

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.source.strip():
            errors.append("provenance.source must be non-empty")
        if self.observed_at.tzinfo is None:
            errors.append("provenance.observed_at must be timezone-aware")
        return tuple(errors)


@dataclass(frozen=True, slots=True)
class EffectiveInterval:
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be >= valid_from")

    def contains(self, as_of: date) -> bool:
        return self.valid_from <= as_of and (
            self.valid_to is None or as_of <= self.valid_to
        )


@dataclass(frozen=True, slots=True)
class SecurityIdentity:
    security_id: str
    symbol: str
    primary_exchange: str
    currency: str = "USD"
    ibkr_con_id: int | None = None


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    index_id: str
    security_id: str
    interval: EffectiveInterval
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class GicsRecord:
    security_id: str
    sector: str
    industry_group: str | None
    industry: str | None
    sub_industry: str | None
    interval: EffectiveInterval
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class DelistingRecord:
    security_id: str
    delisted_on: date
    reason: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    security_id: str
    action_type: CorporateActionType
    effective_on: date
    provenance: Provenance
    ratio: float | None = None
    cash_amount: float | None = None
    successor_security_id: str | None = None


@dataclass(frozen=True, slots=True)
class DailyPriceRecord:
    security_id: str
    trading_date: date
    close: float
    provenance: Provenance
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    adjusted_close: float | None = None
    ibkr_con_id: int | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise ValueError("Dataset V1 validation failed: " + "; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class DatasetV1:
    securities: tuple[SecurityIdentity, ...] = ()
    memberships: tuple[MembershipRecord, ...] = ()
    gics: tuple[GicsRecord, ...] = ()
    delistings: tuple[DelistingRecord, ...] = ()
    corporate_actions: tuple[CorporateActionRecord, ...] = ()
    prices: tuple[DailyPriceRecord, ...] = ()

    def members_as_of(self, index_id: str, as_of: date) -> frozenset[str]:
        return frozenset(
            row.security_id
            for row in self.memberships
            if row.index_id == index_id and row.interval.contains(as_of)
        )

    def gics_as_of(self, security_id: str, as_of: date) -> GicsRecord | None:
        matches = [
            row
            for row in self.gics
            if row.security_id == security_id and row.interval.contains(as_of)
        ]
        if len(matches) > 1:
            raise ValueError(f"overlapping GICS intervals for {security_id} on {as_of}")
        return matches[0] if matches else None

    def is_delisted_as_of(self, security_id: str, as_of: date) -> bool:
        return any(
            row.security_id == security_id and row.delisted_on <= as_of
            for row in self.delistings
        )

    def validate(self) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        identities = {row.security_id: row for row in self.securities}
        if len(identities) != len(self.securities):
            errors.append("duplicate security_id in securities")

        def require_identity(kind: str, security_id: str) -> None:
            if security_id not in identities:
                errors.append(f"{kind} references unknown security_id={security_id}")

        provenance_rows: list[tuple[str, str, Provenance]] = []
        for row in self.memberships:
            require_identity("membership", row.security_id)
            provenance_rows.append(("membership", row.security_id, row.provenance))
        for row in self.gics:
            require_identity("gics", row.security_id)
            if not row.sector.strip():
                errors.append(f"gics sector missing for {row.security_id}")
            provenance_rows.append(("gics", row.security_id, row.provenance))
        for row in self.delistings:
            require_identity("delisting", row.security_id)
            provenance_rows.append(("delisting", row.security_id, row.provenance))
        for row in self.corporate_actions:
            require_identity("corporate_action", row.security_id)
            if row.successor_security_id is not None:
                require_identity("corporate_action successor", row.successor_security_id)
            if row.action_type is CorporateActionType.SPLIT and (
                row.ratio is None or row.ratio <= 0
            ):
                errors.append(f"split ratio must be > 0 for {row.security_id}")
            provenance_rows.append(("corporate_action", row.security_id, row.provenance))
        seen_prices: set[tuple[str, date]] = set()
        for row in self.prices:
            require_identity("price", row.security_id)
            key = (row.security_id, row.trading_date)
            if key in seen_prices:
                errors.append(
                    f"duplicate price for security_id={row.security_id} date={row.trading_date}"
                )
            seen_prices.add(key)
            if row.close <= 0:
                errors.append(f"price close must be > 0 for {row.security_id}")
            provenance_rows.append(("price", row.security_id, row.provenance))

        for kind, security_id, provenance in provenance_rows:
            for error in provenance.validate():
                errors.append(f"{kind} {security_id}: {error}")

        errors.extend(
            _overlap_errors(
                ((row.index_id, row.security_id), row.interval)
                for row in self.memberships
            )
        )
        errors.extend(
            _overlap_errors(((row.security_id,), row.interval) for row in self.gics)
        )

        membership_ids = {row.security_id for row in self.memberships}
        delisted_ids = {row.security_id for row in self.delistings}
        missing_delisted_history = membership_ids.intersection(identities) - delisted_ids
        if membership_ids and missing_delisted_history:
            warnings.append(
                "delisting coverage is not proven for all historical members; "
                "absence of a delisting row is not evidence that a security survived"
            )
        return ValidationReport(tuple(errors), tuple(warnings))


def _overlap_errors(
    rows: Iterable[tuple[tuple[str, ...], EffectiveInterval]],
) -> list[str]:
    grouped: dict[tuple[str, ...], list[EffectiveInterval]] = {}
    for group_key, interval in rows:
        grouped.setdefault(group_key, []).append(interval)
    errors: list[str] = []
    for group_key, intervals in grouped.items():
        ordered = sorted(intervals, key=lambda value: value.valid_from)
        previous: EffectiveInterval | None = None
        for current in ordered:
            if previous is not None and (
                previous.valid_to is None or current.valid_from <= previous.valid_to
            ):
                errors.append(f"overlapping effective intervals for {group_key}")
                break
            previous = current
    return errors


IBKR_HISTORICAL_CAPABILITIES = frozenset(
    {
        "connectivity_paper_du_gate",
        "daily_trades_rth_schema",
        "contract_identity_conid",
        "head_timestamp_coverage",
        "bid_ask_midpoint",
        "symbol_change_discovery",
    }
)

DATASET_V1_REQUIRED_CAPABILITIES = frozenset(
    {
        *IBKR_HISTORICAL_CAPABILITIES,
        "pit_sp500_membership",
        "asof_gics",
        "delisted_security_history",
        "corporate_actions",
    }
)


@dataclass(frozen=True, slots=True)
class DatasetCapabilityMatrix:
    states: Mapping[str, CapabilityState] = field(default_factory=dict)
    evidence_checksum: str | None = None

    @classmethod
    def from_ibkr_historical_evidence(cls, path: str | Path) -> DatasetCapabilityMatrix:
        payload = json.loads(Path(path).read_bytes())
        if payload.get("poi_id") != "POI-IBKR-HIST":
            raise ValueError("unexpected historical evidence poi_id")
        declared = payload.get("evidence_checksum")
        if not isinstance(declared, str) or not declared.startswith("sha256:"):
            raise ValueError("historical evidence is missing evidence_checksum")
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ValueError("historical evidence capabilities must be an object")
        missing = IBKR_HISTORICAL_CAPABILITIES - capabilities.keys()
        if missing:
            raise ValueError(f"historical evidence missing capabilities: {sorted(missing)}")
        not_pass = sorted(
            name for name in IBKR_HISTORICAL_CAPABILITIES if capabilities.get(name) != "PASS"
        )
        if not_pass:
            raise ValueError(f"historical capabilities not PASS: {not_pass}")
        if payload.get("active_name_price_qualification_sufficient") is not True:
            raise ValueError("active-name qualification is not sufficient")
        states = {
            name: CapabilityState.EMPIRICALLY_VERIFIED
            for name in IBKR_HISTORICAL_CAPABILITIES
        }
        for name in DATASET_V1_REQUIRED_CAPABILITIES - IBKR_HISTORICAL_CAPABILITIES:
            states[name] = CapabilityState.BLOCKED_EXTERNAL
        return cls(states=states, evidence_checksum=declared)

    @property
    def full_dataset_contract_ready(self) -> bool:
        return all(
            self.states.get(name) is CapabilityState.EMPIRICALLY_VERIFIED
            for name in DATASET_V1_REQUIRED_CAPABILITIES
        )

    def assert_full_dataset_contract_ready(self) -> None:
        if self.full_dataset_contract_ready:
            return
        unresolved = sorted(
            name
            for name in DATASET_V1_REQUIRED_CAPABILITIES
            if self.states.get(name) is not CapabilityState.EMPIRICALLY_VERIFIED
        )
        raise ValueError(
            "Dataset V1 is not empirically ready; unresolved: " + ", ".join(unresolved)
        )
