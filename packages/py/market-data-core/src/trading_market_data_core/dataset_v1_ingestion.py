"""Dataset V1 PIT reference-layer ingestion (membership + as-of GICS).

Provider-neutral, offline ingestion of historical S&P 500 membership and
historical/as-of GICS classification into the bitemporal Dataset V1 model. NO
commercial provider is selected or hard-coded: a source is an injected
``MembershipSource`` / ``GicsSource`` port; this module supplies only an
in-memory FIXTURE source for tests.

Correctness guarantees:

* **Stable identity.** Raw rows arrive keyed by ``symbol``; normalization
  resolves each to a stable ``security_id`` via an injected resolver and FAILS
  CLOSED on an unknown security — historical truth is never keyed by ticker.
* **Bitemporal.** Every emitted record carries effective validity
  (``valid_from``/``valid_to``) AND knowledge time (``known_at``). Corrections
  are explicit interval assertions linked by ``corrects`` and become
  ``supersedes`` edges, so a later restatement is invisible to any earlier
  as-of query.
* **Deterministic + idempotent.** ``record_id`` is a content hash of the
  assertion; ingesting the same raw rows twice yields byte-identical records and
  the same ``dataset_fingerprint``. Exact duplicates collapse.
* **No false promotion.** A FIXTURE source, a current-snapshot-only source (no
  effective dates / no knowledge time), a source with no historical change, or
  any validation failure can NEVER promote ``pit_sp500_membership`` or
  ``asof_gics`` to ``EMPIRICALLY_VERIFIED``. Promotion additionally requires a
  verifiable real-evidence checksum — absent here, the cells stay blocked and
  ``full_dataset_contract_ready`` stays False.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trading_market_data_core.dataset_v1 import (
    CapabilityState,
    DatasetV1,
    EffectiveInterval,
    GicsRecord,
    MembershipRecord,
    Provenance,
)

INGESTION_VERSION = "dataset-v1-reference-ingestion-1.0.0"


class IngestionError(Exception):
    """Fail-closed reference-ingestion failure (unknown security, bad interval)."""


class SourceKind(StrEnum):
    """Whether a reference source is a real approved provider or a FIXTURE.

    A FIXTURE source can never be empirical evidence — the same discipline as the
    activation ``ClientKind.FAKE`` -> ``NOT_EVIDENCE`` rule.
    """

    REAL = "REAL"
    FIXTURE = "FIXTURE"


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """What a reference source structurally provides (declared by the adapter)."""

    provides_effective_dates: bool  # historical effective dates, not a current snapshot
    provides_known_at: bool  # knowledge/transaction time for restatements
    provides_stable_identity: bool  # a stable id mapping, not ticker-only

    @property
    def is_snapshot_only(self) -> bool:
        return not (self.provides_effective_dates and self.provides_known_at)


# -- raw (pre-normalization) rows --------------------------------------------


@dataclass(frozen=True, slots=True)
class RawMembershipInterval:
    """One canonical membership assertion, keyed by symbol, with knowledge time."""

    index_id: str
    symbol: str
    valid_from: date
    valid_to: date | None
    known_at: datetime
    source_record_id: str
    corrects: str | None = None  # source_record_id of the assertion this restates


@dataclass(frozen=True, slots=True)
class RawGicsInterval:
    symbol: str
    sector: str
    industry_group: str | None
    industry: str | None
    sub_industry: str | None
    valid_from: date
    valid_to: date | None
    known_at: datetime
    source_record_id: str
    corrects: str | None = None


@dataclass(frozen=True, slots=True)
class RawMembershipChange:
    """An ADD/REMOVE reconstitution event (convenience input)."""

    index_id: str
    symbol: str
    change: str  # "ADD" | "REMOVE"
    effective_on: date
    source_record_id: str


@dataclass(frozen=True, slots=True)
class RawGicsAssignment:
    """A 'classification becomes X effective D' event (convenience input)."""

    symbol: str
    sector: str
    effective_on: date
    source_record_id: str
    industry_group: str | None = None
    industry: str | None = None
    sub_industry: str | None = None


@runtime_checkable
class MembershipSource(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def kind(self) -> SourceKind: ...

    @property
    def capabilities(self) -> SourceCapabilities: ...

    def fetch_membership_intervals(self) -> tuple[RawMembershipInterval, ...]: ...


@runtime_checkable
class GicsSource(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def kind(self) -> SourceKind: ...

    @property
    def capabilities(self) -> SourceCapabilities: ...

    def fetch_gics_intervals(self) -> tuple[RawGicsInterval, ...]: ...


# -- identity resolution ------------------------------------------------------


class IdentityResolver:
    """Resolves a provider ``symbol`` to a stable ``security_id`` (fail-closed)."""

    def __init__(self, symbol_to_security_id: Mapping[str, str]) -> None:
        self._map = dict(symbol_to_security_id)

    def resolve(self, symbol: str) -> str:
        security_id = self._map.get(symbol)
        if security_id is None:
            raise IngestionError(
                f"unknown security for symbol {symbol!r}; refusing to key historical"
                " truth by ticker (fail closed)"
            )
        return security_id


# -- event -> interval converters (entry/exit boundary semantics) -------------
#
# Boundary convention (documented + tested): a security ADDED effective D1 is a
# member ON D1; a security REMOVED effective D2 is NOT a member on D2 (removed at
# the open of D2), so its last membership day is D2 - 1. All events in one call
# share a single ``known_at`` (one reconstitution snapshot's history);
# corrections/restatements are expressed as ``corrects``-linked intervals, not
# through this converter.


def membership_changes_to_intervals(
    changes: Sequence[RawMembershipChange], *, known_at: datetime
) -> tuple[RawMembershipInterval, ...]:
    intervals: list[RawMembershipInterval] = []
    by_key: dict[tuple[str, str], list[RawMembershipChange]] = {}
    for change in changes:
        by_key.setdefault((change.index_id, change.symbol), []).append(change)
    for (index_id, symbol), key_changes in by_key.items():
        ordered = sorted(key_changes, key=lambda c: (c.effective_on, c.change))
        open_from: date | None = None
        open_source: str | None = None
        for change in ordered:
            if change.change == "ADD":
                if open_from is not None:
                    raise IngestionError(
                        f"membership ADD for {symbol} in {index_id} while already a member"
                    )
                open_from, open_source = change.effective_on, change.source_record_id
            elif change.change == "REMOVE":
                if open_from is None:
                    raise IngestionError(
                        f"membership REMOVE for {symbol} in {index_id} while not a member"
                    )
                if change.effective_on <= open_from:
                    raise IngestionError(
                        f"membership REMOVE for {symbol} not after its ADD"
                    )
                intervals.append(
                    RawMembershipInterval(
                        index_id=index_id, symbol=symbol, valid_from=open_from,
                        valid_to=change.effective_on - timedelta(days=1), known_at=known_at,
                        source_record_id=f"{open_source}->{change.source_record_id}",
                    )
                )
                open_from, open_source = None, None
            else:
                raise IngestionError(f"unknown membership change {change.change!r}")
        if open_from is not None:
            intervals.append(
                RawMembershipInterval(
                    index_id=index_id, symbol=symbol, valid_from=open_from, valid_to=None,
                    known_at=known_at, source_record_id=str(open_source),
                )
            )
    return tuple(intervals)


def gics_assignments_to_intervals(
    assignments: Sequence[RawGicsAssignment], *, known_at: datetime
) -> tuple[RawGicsInterval, ...]:
    intervals: list[RawGicsInterval] = []
    by_symbol: dict[str, list[RawGicsAssignment]] = {}
    for assignment in assignments:
        by_symbol.setdefault(assignment.symbol, []).append(assignment)
    for symbol, symbol_assignments in by_symbol.items():
        ordered = sorted(symbol_assignments, key=lambda a: a.effective_on)
        for current, following in zip(ordered, [*ordered[1:], None], strict=True):
            valid_to = (
                following.effective_on - timedelta(days=1) if following is not None else None
            )
            intervals.append(
                RawGicsInterval(
                    symbol=symbol, sector=current.sector,
                    industry_group=current.industry_group, industry=current.industry,
                    sub_industry=current.sub_industry, valid_from=current.effective_on,
                    valid_to=valid_to, known_at=known_at,
                    source_record_id=current.source_record_id,
                )
            )
    return tuple(intervals)


# -- normalization (raw -> bitemporal Dataset V1 records) ---------------------


def _record_id(prefix: str, material: Sequence[object]) -> str:
    serialized = json.dumps(list(material), sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()[:24]


def ingest_membership(
    source: MembershipSource, resolver: IdentityResolver, *, observed_at: datetime
) -> tuple[MembershipRecord, ...]:
    """Normalize raw membership intervals into deterministic bitemporal records."""
    raw = source.fetch_membership_intervals()
    id_by_source: dict[str, str] = {}
    resolved: list[tuple[RawMembershipInterval, str, str]] = []
    for row in raw:
        security_id = resolver.resolve(row.symbol)
        record_id = _record_id(
            "m",
            (row.index_id, security_id, row.valid_from.isoformat(),
             row.valid_to.isoformat() if row.valid_to else None,
             row.known_at.isoformat(), source.source_id),
        )
        if row.source_record_id in id_by_source and id_by_source[row.source_record_id] != record_id:
            raise IngestionError(
                f"source_record_id {row.source_record_id!r} maps to two different records"
            )
        id_by_source[row.source_record_id] = record_id
        resolved.append((row, security_id, record_id))

    records: dict[str, MembershipRecord] = {}
    for row, security_id, record_id in resolved:
        supersedes = None
        if row.corrects is not None:
            supersedes = id_by_source.get(row.corrects)
            if supersedes is None:
                raise IngestionError(
                    f"membership correction references unknown source record {row.corrects!r}"
                )
        record = MembershipRecord(
            record_id=record_id, index_id=row.index_id, security_id=security_id,
            interval=EffectiveInterval(row.valid_from, row.valid_to), known_at=row.known_at,
            provenance=Provenance(source.source_id, observed_at, row.source_record_id),
            supersedes=supersedes,
        )
        records[record_id] = record  # idempotent: identical assertions collapse
    return tuple(sorted(records.values(), key=lambda r: r.record_id))


def ingest_gics(
    source: GicsSource, resolver: IdentityResolver, *, observed_at: datetime
) -> tuple[GicsRecord, ...]:
    raw = source.fetch_gics_intervals()
    id_by_source: dict[str, str] = {}
    resolved: list[tuple[RawGicsInterval, str, str]] = []
    for row in raw:
        security_id = resolver.resolve(row.symbol)
        record_id = _record_id(
            "g",
            (security_id, row.sector, row.industry_group, row.industry, row.sub_industry,
             row.valid_from.isoformat(), row.valid_to.isoformat() if row.valid_to else None,
             row.known_at.isoformat(), source.source_id),
        )
        id_by_source[row.source_record_id] = record_id
        resolved.append((row, security_id, record_id))

    records: dict[str, GicsRecord] = {}
    for row, security_id, record_id in resolved:
        supersedes = None
        if row.corrects is not None:
            supersedes = id_by_source.get(row.corrects)
            if supersedes is None:
                raise IngestionError(
                    f"gics correction references unknown source record {row.corrects!r}"
                )
        records[record_id] = GicsRecord(
            record_id=record_id, security_id=security_id, sector=row.sector,
            industry_group=row.industry_group, industry=row.industry,
            sub_industry=row.sub_industry,
            interval=EffectiveInterval(row.valid_from, row.valid_to), known_at=row.known_at,
            provenance=Provenance(source.source_id, observed_at, row.source_record_id),
            supersedes=supersedes,
        )
    return tuple(sorted(records.values(), key=lambda r: r.record_id))


# -- deterministic dataset fingerprint ---------------------------------------


def dataset_fingerprint(dataset: DatasetV1) -> str:
    """A deterministic sha256 over every record (order-independent)."""

    def prov(provenance: Provenance) -> dict[str, object]:
        return {
            "source": provenance.source,
            "observed_at": provenance.observed_at.isoformat(),
            "source_record_id": provenance.source_record_id,
        }

    material = {
        "securities": sorted(
            (
                {"security_id": s.security_id, "symbol": s.symbol,
                 "primary_exchange": s.primary_exchange, "currency": s.currency,
                 "ibkr_con_id": s.ibkr_con_id}
                for s in dataset.securities
            ),
            key=lambda entry: str(entry["security_id"]),
        ),
        "memberships": sorted(
            (
                {"record_id": m.record_id, "index_id": m.index_id, "security_id": m.security_id,
                 "valid_from": m.interval.valid_from.isoformat(),
                 "valid_to": m.interval.valid_to.isoformat() if m.interval.valid_to else None,
                 "known_at": m.known_at.isoformat(), "supersedes": m.supersedes,
                 "provenance": prov(m.provenance)}
                for m in dataset.memberships
            ),
            key=lambda entry: str(entry["record_id"]),
        ),
        "gics": sorted(
            (
                {"record_id": g.record_id, "security_id": g.security_id, "sector": g.sector,
                 "industry_group": g.industry_group, "industry": g.industry,
                 "sub_industry": g.sub_industry, "valid_from": g.interval.valid_from.isoformat(),
                 "valid_to": g.interval.valid_to.isoformat() if g.interval.valid_to else None,
                 "known_at": g.known_at.isoformat(), "supersedes": g.supersedes,
                 "provenance": prov(g.provenance)}
                for g in dataset.gics
            ),
            key=lambda entry: str(entry["record_id"]),
        ),
        "delistings": sorted(
            (
                {"security_id": d.security_id, "delisted_on": d.delisted_on.isoformat(),
                 "reason": d.reason, "provenance": prov(d.provenance)}
                for d in dataset.delistings
            ),
            key=lambda entry: (str(entry["security_id"]), str(entry["delisted_on"])),
        ),
        "corporate_actions": sorted(
            (
                {"security_id": c.security_id, "action_type": c.action_type.value,
                 "effective_on": c.effective_on.isoformat(),
                 "ratio": str(c.ratio) if c.ratio is not None else None,
                 "cash_amount": str(c.cash_amount) if c.cash_amount is not None else None,
                 "successor_security_id": c.successor_security_id,
                 "provenance": prov(c.provenance)}
                for c in dataset.corporate_actions
            ),
            key=lambda entry: (
                str(entry["security_id"]), str(entry["effective_on"]),
                str(entry["action_type"]),
            ),
        ),
        "prices": sorted(
            (
                {"security_id": p.security_id, "trading_date": p.trading_date.isoformat(),
                 "close": str(p.close),
                 "open": str(p.open) if p.open is not None else None,
                 "high": str(p.high) if p.high is not None else None,
                 "low": str(p.low) if p.low is not None else None,
                 "volume": str(p.volume) if p.volume is not None else None,
                 "adjusted_close": (
                     str(p.adjusted_close) if p.adjusted_close is not None else None
                 ),
                 "ibkr_con_id": p.ibkr_con_id, "provenance": prov(p.provenance)}
                for p in dataset.prices
            ),
            key=lambda entry: (str(entry["security_id"]), str(entry["trading_date"])),
        ),
    }
    serialized = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()


# -- capability promotion / readiness ----------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityAssessment:
    """Whether a reference capability can be promoted, and exactly why/why not."""

    capability: str
    state: CapabilityState
    reasons: tuple[str, ...]

    @property
    def promotable(self) -> bool:
        return self.state is CapabilityState.EMPIRICALLY_VERIFIED


def assess_reference_capability(
    capability: str,
    *,
    source_kind: SourceKind,
    capabilities: SourceCapabilities,
    validation_errors: Sequence[str],
    historical_change_count: int,
    real_evidence_checksum: str | None,
) -> CapabilityAssessment:
    """Decide the state of one blocked reference capability, fail-closed.

    A capability is promoted to EMPIRICALLY_VERIFIED ONLY when a REAL source with
    historical effective dates + knowledge time supplies at least one historical
    change, validation passes, AND a verifiable real-evidence checksum is present.
    Anything else stays BLOCKED_EXTERNAL (or CONTRACT_READY for a clean fixture
    that merely proves the machinery).
    """
    reasons: list[str] = []
    if source_kind is SourceKind.FIXTURE:
        reasons.append("fixture source is never empirical evidence")
    if capabilities.is_snapshot_only:
        reasons.append(
            "source is snapshot-only (missing historical effective dates and/or"
            " knowledge time); a current-state snapshot cannot prove PIT history"
        )
    if not capabilities.provides_stable_identity:
        reasons.append("source does not provide a stable identity mapping (ticker-only)")
    if historical_change_count < 1:
        reasons.append("no historical change/event present in the ingested data")
    if validation_errors:
        reasons.append(f"{len(validation_errors)} validation error(s)")
    if not (real_evidence_checksum or "").startswith("sha256:"):
        reasons.append("no verifiable real-evidence checksum supplied")

    if reasons:
        # A clean FIXTURE that only lacks real evidence proves the machinery.
        if (
            source_kind is SourceKind.FIXTURE
            and not capabilities.is_snapshot_only
            and capabilities.provides_stable_identity
            and historical_change_count >= 1
            and not validation_errors
        ):
            return CapabilityAssessment(
                capability, CapabilityState.CONTRACT_READY, tuple(reasons)
            )
        return CapabilityAssessment(capability, CapabilityState.BLOCKED_EXTERNAL, tuple(reasons))
    return CapabilityAssessment(capability, CapabilityState.EMPIRICALLY_VERIFIED, ())


@dataclass(frozen=True, slots=True)
class ReferenceReadinessReport:
    assessments: tuple[CapabilityAssessment, ...]

    @property
    def full_dataset_contract_ready(self) -> bool:
        return bool(self.assessments) and all(a.promotable for a in self.assessments)

    def to_dict(self) -> dict[str, object]:
        return {
            "ingestion_version": INGESTION_VERSION,
            "capabilities": {
                a.capability: {"state": a.state.value, "reasons": list(a.reasons)}
                for a in self.assessments
            },
            "full_dataset_contract_ready": self.full_dataset_contract_ready,
        }


__all__ = [
    "INGESTION_VERSION",
    "CapabilityAssessment",
    "GicsSource",
    "IdentityResolver",
    "IngestionError",
    "MembershipSource",
    "RawGicsAssignment",
    "RawGicsInterval",
    "RawMembershipChange",
    "RawMembershipInterval",
    "ReferenceReadinessReport",
    "SourceCapabilities",
    "SourceKind",
    "assess_reference_capability",
    "dataset_fingerprint",
    "gics_assignments_to_intervals",
    "ingest_gics",
    "ingest_membership",
    "membership_changes_to_intervals",
]
