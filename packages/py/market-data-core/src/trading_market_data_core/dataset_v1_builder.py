"""Dataset V1 builder / materialization / reproducible snapshot.

Turns the Dataset V1 model (identity, bitemporal membership, as-of GICS, symbol
history, delistings, corporate actions, lineage, retractions, prices) into a
deterministic, versioned, point-in-time SNAPSHOT that a later backtest engine can
consume without ever touching raw ingestion internals.

Design rules enforced here:

* **No hidden "today".** ``as_of`` and ``known_as_of`` are mandatory build inputs;
  nothing reads a wall clock to decide what is visible. The build timestamp is
  manifest METADATA only and is deliberately excluded from the fingerprint.
* **Everything resolved bitemporally.** Universe, GICS, lifecycle, symbols and
  corporate actions are read through the existing as-of APIs, so a later
  restatement or retraction can never mutate an older snapshot.
* **Survivorship-safe.** The universe is the membership effective at ``as_of``
  and known by ``known_as_of`` — later-delisted names stay in, future entrants
  stay out, and today's surviving universe/symbol map is never consulted.
* **Adjust exactly once.** The adjustment policy is explicit; provider-adjusted
  series are never re-adjusted, raw series are adjusted only under
  ``INTERNAL_LEDGER_ADJUSTED``, and ``UNKNOWN``/mixed adjustment state fails
  closed.
* **No auto-stitching.** Lineage links are RECORDED, never applied: prices are
  never concatenated across identities unless a future explicit policy says so.
* **Built is not backtest-ready.** ``ReadinessLevel`` is computed from real
  capability evidence; fixtures can reach ``CONTRACT_ONLY`` at most.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from trading_market_data_core.dataset_v1 import (
    DATASET_V1_REQUIRED_CAPABILITIES,
    CapabilityState,
    CorporateActionRecord,
    CorporateActionType,
    DailyPriceRecord,
    DatasetV1,
    DelistingRecord,
    PriceAdjustmentStatus,
    SecurityIdentity,
    SecurityLifecycleState,
    SymbolHistoryRecord,
    resolve_visible,
)

DATASET_CONTRACT_VERSION = "dataset-v1"
BUILDER_VERSION = "dataset-v1-builder-1.0.0"


class BuilderError(Exception):
    """Fail-closed build failure (ambiguous adjustment, bad evidence, invalid input)."""


class AdjustmentPolicy(StrEnum):
    """How the materialized price series relates to the corporate-action ledger."""

    RAW = "RAW"
    PROVIDER_ADJUSTED = "PROVIDER_ADJUSTED"
    INTERNAL_LEDGER_ADJUSTED = "INTERNAL_LEDGER_ADJUSTED"


class LineagePolicy(StrEnum):
    """Whether identity lineage may be stitched. Default: never."""

    #: Lineage links are recorded in the snapshot; price histories are NOT joined.
    NO_STITCH = "NO_STITCH"
    #: Reserved for a future explicit stitching policy; currently fails closed so
    #: stitching can never happen implicitly.
    STITCH_ECONOMIC_LINEAGE = "STITCH_ECONOMIC_LINEAGE"


class LineageLinkKind(StrEnum):
    SAME_SECURITY_SYMBOL_CHANGE = "SAME_SECURITY_SYMBOL_CHANGE"
    TERMINATED_PREDECESSOR = "TERMINATED_PREDECESSOR"
    SUCCESSOR_SECURITY = "SUCCESSOR_SECURITY"
    SPINOFF_CHILD = "SPINOFF_CHILD"
    UNKNOWN_AMBIGUOUS = "UNKNOWN_AMBIGUOUS"


class GicsCoverageState(StrEnum):
    """Why a GICS assignment is or is not present — never silently substituted."""

    KNOWN = "KNOWN"
    #: The source covers GICS, but not this security on this date.
    UNKNOWN = "UNKNOWN"
    #: No GICS source coverage exists in the dataset at all.
    NO_SOURCE_COVERAGE = "NO_SOURCE_COVERAGE"


class ReadinessLevel(StrEnum):
    """What a snapshot may be used for. Built != backtest-ready."""

    #: Machinery proven (fixtures / no real reference evidence).
    CONTRACT_ONLY = "CONTRACT_ONLY"
    #: Some required capabilities empirically verified, not all.
    PARTIAL_DATA = "PARTIAL_DATA"
    #: Every required reference capability is empirically verified.
    EMPIRICAL_REFERENCE_READY = "EMPIRICAL_REFERENCE_READY"
    #: Reference-ready AND validation clean AND price coverage sufficient.
    BACKTEST_ELIGIBLE = "BACKTEST_ELIGIBLE"


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """A source that contributed records, with its version and content checksum."""

    name: str
    version: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise BuilderError("source binding requires a name and version")
        if not self.checksum.startswith("sha256:"):
            raise BuilderError(f"source {self.name!r} checksum must be a sha256: digest")


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    """An evidence artifact bound to the build and VERIFIED, never trusted.

    ``verify`` recomputes the file digest and refuses a mismatch, so a decorative
    or stale checksum cannot silently authorize a build.
    """

    name: str
    path: Path
    checksum: str

    def verify(self) -> None:
        if not self.checksum.startswith("sha256:"):
            raise BuilderError(f"evidence {self.name!r} checksum must be a sha256: digest")
        if not self.path.is_file():
            raise BuilderError(f"evidence {self.name!r} file not found: {self.path}")
        digest = "sha256:" + hashlib.sha256(self.path.read_bytes()).hexdigest()
        if digest != self.checksum:
            raise BuilderError(
                f"evidence {self.name!r} checksum mismatch (tampered or stale):"
                f" declared {self.checksum}, recomputed {digest}"
            )


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """Explicit build inputs. There is no hidden 'today'."""

    as_of: date
    known_as_of: datetime
    index_id: str
    adjustment_policy: AdjustmentPolicy
    lineage_policy: LineagePolicy = LineagePolicy.NO_STITCH
    price_start: date | None = None
    #: Forward-filling is OFF unless explicitly requested and justified.
    forward_fill_prices: bool = False
    forward_fill_justification: str | None = None
    #: Dividend adjustment is not implemented; requesting it fails closed rather
    #: than silently producing wrong adjusted returns.
    include_dividend_adjustment: bool = False
    builder_version: str = BUILDER_VERSION

    def __post_init__(self) -> None:
        if self.known_as_of.tzinfo is None:
            raise BuilderError("known_as_of must be timezone-aware")
        if not self.index_id.strip():
            raise BuilderError("index_id is mandatory")
        if self.forward_fill_prices:
            if not (self.forward_fill_justification or "").strip():
                raise BuilderError(
                    "forward_fill_prices requires an explicit written justification"
                )
            # Accepting the flag while silently not filling would be worse than
            # refusing it: a caller would believe gaps were filled.
            raise BuilderError(
                "forward_fill_prices is not implemented in this builder version;"
                " fail closed rather than silently leaving gaps unfilled"
            )
        if self.include_dividend_adjustment:
            raise BuilderError(
                "dividend adjustment is not implemented; fail closed rather than"
                " emitting unverified adjusted returns"
            )
        if self.lineage_policy is LineagePolicy.STITCH_ECONOMIC_LINEAGE:
            raise BuilderError(
                "STITCH_ECONOMIC_LINEAGE is not implemented; lineage is recorded,"
                " never auto-stitched (fail closed)"
            )


# -- materialized rows --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UniverseMember:
    security_id: str
    #: The ticker effective at ``as_of`` (historical, never today's symbol).
    symbol_as_of: str | None
    lifecycle_state: SecurityLifecycleState


@dataclass(frozen=True, slots=True)
class GicsAssignment:
    security_id: str
    state: GicsCoverageState
    sector: str | None = None
    industry_group: str | None = None
    industry: str | None = None
    sub_industry: str | None = None


@dataclass(frozen=True, slots=True)
class MaterializedPrice:
    security_id: str
    trading_date: date
    close: Decimal
    adjustment_status: PriceAdjustmentStatus
    source: str
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    adjusted_with_ledger: str | None = None
    forward_filled: bool = False


@dataclass(frozen=True, slots=True)
class LineageLink:
    security_id: str
    kind: LineageLinkKind
    related_security_id: str | None
    effective_on: date | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Machine-readable coverage — missing data is never hidden behind a bool."""

    universe_securities: int
    with_price_coverage: int
    with_gics_coverage: int
    with_symbol_history: int
    with_lifecycle_resolution: int
    unknown_gics: int
    no_gics_source_coverage: int
    missing_price_securities: tuple[str, ...]
    corporate_action_coverage: str
    blocked_capabilities: tuple[str, ...]

    def _pct(self, count: int) -> str:
        if self.universe_securities == 0:
            return "0.00"
        return str(
            (Decimal(count) / Decimal(self.universe_securities) * Decimal(100)).quantize(
                Decimal("0.01")
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "universe_securities": self.universe_securities,
            "price_coverage_pct": self._pct(self.with_price_coverage),
            "gics_coverage_pct": self._pct(self.with_gics_coverage),
            "symbol_history_coverage_pct": self._pct(self.with_symbol_history),
            "lifecycle_resolution_pct": self._pct(self.with_lifecycle_resolution),
            "unknown_gics": self.unknown_gics,
            "no_gics_source_coverage": self.no_gics_source_coverage,
            "missing_price_securities": list(self.missing_price_securities),
            "corporate_action_coverage": self.corporate_action_coverage,
            "blocked_capabilities": list(self.blocked_capabilities),
        }


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Everything needed to identify, reproduce and audit a snapshot."""

    dataset_contract_version: str
    builder_version: str
    snapshot_id: str
    fingerprint: str
    as_of: date
    known_as_of: datetime
    index_id: str
    adjustment_policy: AdjustmentPolicy
    lineage_policy: LineagePolicy
    readiness: ReadinessLevel
    security_count: int
    membership_count: int
    price_row_count: int
    gics_known_count: int
    delisting_count: int
    corporate_action_count: int
    retraction_count: int
    sources: tuple[SourceBinding, ...]
    evidence: tuple[str, ...]  # "name=checksum" pairs, already verified
    blocked_capabilities: tuple[str, ...]
    validation_errors: tuple[str, ...]
    build_warnings: tuple[str, ...]
    coverage: CoverageReport
    #: METADATA ONLY — deliberately excluded from the fingerprint so a rebuild at
    #: a different wall-clock time yields the identical snapshot id.
    built_at: datetime | None = None

    def to_dict(self, *, include_built_at: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "dataset_contract_version": self.dataset_contract_version,
            "builder_version": self.builder_version,
            "snapshot_id": self.snapshot_id,
            "fingerprint": self.fingerprint,
            "as_of": self.as_of.isoformat(),
            "known_as_of": self.known_as_of.isoformat(),
            "index_id": self.index_id,
            "adjustment_policy": self.adjustment_policy.value,
            "lineage_policy": self.lineage_policy.value,
            "readiness": self.readiness.value,
            "security_count": self.security_count,
            "membership_count": self.membership_count,
            "price_row_count": self.price_row_count,
            "gics_known_count": self.gics_known_count,
            "delisting_count": self.delisting_count,
            "corporate_action_count": self.corporate_action_count,
            "retraction_count": self.retraction_count,
            "sources": [
                {"name": s.name, "version": s.version, "checksum": s.checksum}
                for s in self.sources
            ],
            "evidence": list(self.evidence),
            "blocked_capabilities": list(self.blocked_capabilities),
            "validation_errors": list(self.validation_errors),
            "build_warnings": list(self.build_warnings),
            "coverage": self.coverage.to_dict(),
        }
        if include_built_at and self.built_at is not None:
            payload["built_at"] = self.built_at.isoformat()
        return payload

    def checksum(self) -> str:
        """Tamper-detectable digest of the manifest (excluding ``built_at``)."""
        body = self.to_dict(include_built_at=False)
        return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    """An immutable, materialized, point-in-time Dataset V1 snapshot."""

    manifest: SnapshotManifest
    security_master: tuple[SecurityIdentity, ...]
    universe: tuple[UniverseMember, ...]
    symbol_history: tuple[SymbolHistoryRecord, ...]
    gics: tuple[GicsAssignment, ...]
    prices: tuple[MaterializedPrice, ...]
    delistings: tuple[DelistingRecord, ...]
    corporate_actions: tuple[CorporateActionRecord, ...]
    lineage: tuple[LineageLink, ...]

    @property
    def backtest_eligible(self) -> bool:
        return self.manifest.readiness is ReadinessLevel.BACKTEST_ELIGIBLE


def _canonical(material: object) -> bytes:
    return json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")


def _decimal_or_none(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


# -- fingerprint --------------------------------------------------------------


def snapshot_content(snapshot: DatasetSnapshot) -> dict[str, object]:
    """The canonical, order-independent content of a snapshot.

    Used for the fingerprint and for serialization. Deliberately EXCLUDES the
    build timestamp so the same inputs always yield the same identity.
    """
    return {
        "dataset_contract_version": snapshot.manifest.dataset_contract_version,
        "builder_version": snapshot.manifest.builder_version,
        "as_of": snapshot.manifest.as_of.isoformat(),
        "known_as_of": snapshot.manifest.known_as_of.isoformat(),
        "index_id": snapshot.manifest.index_id,
        "adjustment_policy": snapshot.manifest.adjustment_policy.value,
        "lineage_policy": snapshot.manifest.lineage_policy.value,
        "sources": sorted(
            (
                {"name": s.name, "version": s.version, "checksum": s.checksum}
                for s in snapshot.manifest.sources
            ),
            key=lambda entry: str(entry["name"]),
        ),
        "evidence": sorted(snapshot.manifest.evidence),
        "security_master": sorted(
            (
                {"security_id": s.security_id, "symbol": s.symbol,
                 "primary_exchange": s.primary_exchange, "currency": s.currency,
                 "ibkr_con_id": s.ibkr_con_id}
                for s in snapshot.security_master
            ),
            key=lambda entry: str(entry["security_id"]),
        ),
        "universe": sorted(
            (
                {"security_id": m.security_id, "symbol_as_of": m.symbol_as_of,
                 "lifecycle_state": m.lifecycle_state.value}
                for m in snapshot.universe
            ),
            key=lambda entry: str(entry["security_id"]),
        ),
        "symbol_history": sorted(
            (
                {"record_id": s.record_id, "security_id": s.security_id, "symbol": s.symbol,
                 "valid_from": s.interval.valid_from.isoformat(),
                 "valid_to": s.interval.valid_to.isoformat() if s.interval.valid_to else None,
                 "known_at": s.known_at.isoformat()}
                for s in snapshot.symbol_history
            ),
            key=lambda entry: str(entry["record_id"]),
        ),
        "gics": sorted(
            (
                {"security_id": g.security_id, "state": g.state.value, "sector": g.sector,
                 "industry_group": g.industry_group, "industry": g.industry,
                 "sub_industry": g.sub_industry}
                for g in snapshot.gics
            ),
            key=lambda entry: str(entry["security_id"]),
        ),
        "prices": sorted(
            (
                {"security_id": p.security_id, "trading_date": p.trading_date.isoformat(),
                 "close": str(p.close), "open": _decimal_or_none(p.open),
                 "high": _decimal_or_none(p.high), "low": _decimal_or_none(p.low),
                 "volume": _decimal_or_none(p.volume),
                 "adjustment_status": p.adjustment_status.value,
                 "adjusted_with_ledger": p.adjusted_with_ledger,
                 "forward_filled": p.forward_filled, "source": p.source}
                for p in snapshot.prices
            ),
            key=lambda entry: (str(entry["security_id"]), str(entry["trading_date"])),
        ),
        "delistings": sorted(
            (
                {"record_id": d.record_id, "security_id": d.security_id,
                 "delisted_on": d.delisted_on.isoformat(), "reason": d.reason.value,
                 "listing_date": d.listing_date.isoformat() if d.listing_date else None,
                 "last_trading_date": (
                     d.last_trading_date.isoformat() if d.last_trading_date else None
                 ),
                 "successor_security_id": d.successor_security_id,
                 "known_at": d.known_at.isoformat()}
                for d in snapshot.delistings
            ),
            key=lambda entry: str(entry["record_id"]),
        ),
        "corporate_actions": sorted(
            (
                {"record_id": c.record_id, "security_id": c.security_id,
                 "action_type": c.action_type.value,
                 "effective_on": c.effective_on.isoformat(),
                 "ratio": _decimal_or_none(c.ratio),
                 "cash_amount": _decimal_or_none(c.cash_amount), "currency": c.currency,
                 "successor_security_id": c.successor_security_id,
                 "predecessor_security_id": c.predecessor_security_id,
                 "known_at": c.known_at.isoformat()}
                for c in snapshot.corporate_actions
            ),
            key=lambda entry: str(entry["record_id"]),
        ),
        "lineage": sorted(
            (
                {"security_id": link.security_id, "kind": link.kind.value,
                 "related_security_id": link.related_security_id,
                 "effective_on": link.effective_on.isoformat() if link.effective_on else None,
                 "detail": link.detail}
                for link in snapshot.lineage
            ),
            key=lambda entry: (
                str(entry["security_id"]), str(entry["kind"]),
                str(entry["related_security_id"]), str(entry["effective_on"]),
            ),
        ),
    }


def compute_fingerprint(snapshot: DatasetSnapshot) -> str:
    return "sha256:" + hashlib.sha256(_canonical(snapshot_content(snapshot))).hexdigest()


def compute_snapshot_id(fingerprint: str) -> str:
    return "snapshot:" + fingerprint.split(":", 1)[1][:32]


# -- readiness ----------------------------------------------------------------


def compute_readiness(
    capability_states: Mapping[str, CapabilityState],
    *,
    validation_errors: Sequence[str],
    universe_size: int,
    priced_securities: int,
) -> tuple[ReadinessLevel, tuple[str, ...]]:
    """Readiness from REAL capability evidence — never from a successful build."""
    blocked = tuple(
        sorted(
            name
            for name in DATASET_V1_REQUIRED_CAPABILITIES
            if capability_states.get(name) is not CapabilityState.EMPIRICALLY_VERIFIED
        )
    )
    verified = len(DATASET_V1_REQUIRED_CAPABILITIES) - len(blocked)
    if blocked:
        level = ReadinessLevel.CONTRACT_ONLY if verified == 0 else ReadinessLevel.PARTIAL_DATA
        return level, blocked
    if validation_errors or universe_size == 0 or priced_securities < universe_size:
        return ReadinessLevel.EMPIRICAL_REFERENCE_READY, blocked
    return ReadinessLevel.BACKTEST_ELIGIBLE, blocked


# -- corporate-action application (splits only, exactly once) ----------------


def _ledger_fingerprint(actions: Sequence[CorporateActionRecord]) -> str:
    material = sorted(
        f"{a.record_id}|{a.security_id}|{a.action_type.value}|{a.effective_on.isoformat()}|{a.ratio}"
        for a in actions
    )
    return "ledger:" + hashlib.sha256(_canonical(material)).hexdigest()[:32]


def _split_factor_before(
    trading_date: date, actions: Sequence[CorporateActionRecord]
) -> Decimal:
    """Cumulative split factor to back-adjust a price observed on ``trading_date``.

    A split/reverse-split effective on D restates every price STRICTLY BEFORE D.
    ``ratio`` is new shares per old share, so a 2:1 split (ratio 2) halves prior
    prices and a 1:10 reverse split (ratio 0.1) multiplies them by ten.
    """
    factor = Decimal(1)
    for action in actions:
        if action.ratio is None:
            continue
        if trading_date < action.effective_on:
            factor *= action.ratio
    return factor


# -- the builder --------------------------------------------------------------


def build_snapshot(
    dataset: DatasetV1,
    config: BuildConfig,
    *,
    sources: Sequence[SourceBinding] = (),
    evidence: Sequence[EvidenceBinding] = (),
    capability_states: Mapping[str, CapabilityState] | None = None,
    built_at: datetime | None = None,
) -> DatasetSnapshot:
    """Materialize a deterministic point-in-time snapshot. Fail-closed."""
    warnings: list[str] = []

    # 1. bind + VERIFY evidence (a decorative checksum cannot authorize a build)
    for binding in evidence:
        binding.verify()
    evidence_refs = tuple(sorted(f"{b.name}={b.checksum}" for b in evidence))

    # 1a. A claim of empirical readiness MUST be backed by verified evidence and
    # checksummed sources. Without them the claim is unsupported and the build
    # fails closed rather than minting an over-claiming snapshot.
    claimed_empirical = sorted(
        name
        for name, state in (capability_states or {}).items()
        if state is CapabilityState.EMPIRICALLY_VERIFIED
        and name in DATASET_V1_REQUIRED_CAPABILITIES
    )
    if claimed_empirical and not evidence_refs:
        raise BuilderError(
            f"capabilities {claimed_empirical} are claimed EMPIRICALLY_VERIFIED but no"
            " verifiable evidence binding was supplied; fail closed"
        )
    if claimed_empirical and not sources:
        raise BuilderError(
            f"capabilities {claimed_empirical} are claimed EMPIRICALLY_VERIFIED but no"
            " checksummed source binding was supplied; fail closed"
        )

    # 2. the underlying dataset must itself be valid
    dataset_report = dataset.validate()
    validation_errors = list(dataset_report.errors)
    warnings.extend(dataset_report.warnings)

    # 3. point-in-time universe (survivorship-safe, knowledge-gated)
    member_ids = dataset.members_as_of(
        config.index_id, config.as_of, known_as_of=config.known_as_of
    )
    identities = {identity.security_id: identity for identity in dataset.securities}
    unknown_members = sorted(member_ids - identities.keys())
    for missing in unknown_members:
        validation_errors.append(f"universe references unknown security_id={missing}")
    selected = sorted(member_ids & identities.keys())

    universe: list[UniverseMember] = []
    for security_id in selected:
        universe.append(
            UniverseMember(
                security_id=security_id,
                symbol_as_of=dataset.symbol_as_of(
                    security_id, config.as_of, known_as_of=config.known_as_of
                ),
                lifecycle_state=dataset.lifecycle_state_as_of(
                    security_id, config.as_of, known_as_of=config.known_as_of
                ),
            )
        )

    # 4. GICS — explicit unknown vs no-source-coverage; never back-projected
    has_gics_source = bool(dataset.gics)
    gics_rows: list[GicsAssignment] = []
    for security_id in selected:
        record = dataset.gics_as_of(
            security_id, config.as_of, known_as_of=config.known_as_of
        )
        if record is not None:
            gics_rows.append(
                GicsAssignment(
                    security_id=security_id, state=GicsCoverageState.KNOWN,
                    sector=record.sector, industry_group=record.industry_group,
                    industry=record.industry, sub_industry=record.sub_industry,
                )
            )
        else:
            gics_rows.append(
                GicsAssignment(
                    security_id=security_id,
                    state=(
                        GicsCoverageState.UNKNOWN
                        if has_gics_source
                        else GicsCoverageState.NO_SOURCE_COVERAGE
                    ),
                )
            )

    # 5. corporate actions visible at the cutoff, per selected security
    actions_by_security: dict[str, tuple[CorporateActionRecord, ...]] = {
        security_id: dataset.corporate_actions_for(
            security_id, known_as_of=config.known_as_of, end=config.as_of
        )
        for security_id in selected
    }
    all_actions = tuple(
        action for actions in actions_by_security.values() for action in actions
    )

    # 6. prices (knowledge-gated + as-of bounded), then adjust EXACTLY ONCE
    prices, price_warnings, price_errors = _materialize_prices(
        dataset, config, selected, actions_by_security
    )
    warnings.extend(price_warnings)
    validation_errors.extend(price_errors)

    # 7. lineage links — recorded, never applied
    lineage = _materialize_lineage(dataset, config, selected, actions_by_security, identities)

    # 8. delistings visible at the cutoff
    delistings = tuple(
        delisting
        for security_id in selected
        if (delisting := dataset.delisting_as_of(security_id, known_as_of=config.known_as_of))
        is not None
    )

    symbol_history = tuple(
        row
        for row in resolve_visible(
            dataset.symbol_history, config.known_as_of, dataset.retractions
        )
        if row.security_id in member_ids
    )

    # 9. coverage + readiness
    priced = {price.security_id for price in prices}
    with_symbols = {row.security_id for row in symbol_history}
    resolved_lifecycle = {
        row.security_id
        for row in universe
        if row.lifecycle_state is not SecurityLifecycleState.UNKNOWN
    }
    coverage = CoverageReport(
        universe_securities=len(selected),
        with_price_coverage=len(priced & set(selected)),
        with_gics_coverage=sum(
            1 for row in gics_rows if row.state is GicsCoverageState.KNOWN
        ),
        with_symbol_history=len(with_symbols & set(selected)),
        with_lifecycle_resolution=len(resolved_lifecycle),
        unknown_gics=sum(1 for row in gics_rows if row.state is GicsCoverageState.UNKNOWN),
        no_gics_source_coverage=sum(
            1 for row in gics_rows if row.state is GicsCoverageState.NO_SOURCE_COVERAGE
        ),
        missing_price_securities=tuple(sorted(set(selected) - priced)),
        corporate_action_coverage=("PRESENT" if all_actions else "NONE"),
        blocked_capabilities=(),
    )
    states = dict(capability_states or {})
    readiness, blocked = compute_readiness(
        states, validation_errors=validation_errors, universe_size=len(selected),
        priced_securities=len(priced & set(selected)),
    )
    coverage = replace(coverage, blocked_capabilities=blocked)

    manifest = SnapshotManifest(
        dataset_contract_version=DATASET_CONTRACT_VERSION,
        builder_version=config.builder_version,
        snapshot_id="", fingerprint="",
        as_of=config.as_of, known_as_of=config.known_as_of, index_id=config.index_id,
        adjustment_policy=config.adjustment_policy, lineage_policy=config.lineage_policy,
        readiness=readiness,
        security_count=len(dataset.securities), membership_count=len(selected),
        price_row_count=len(prices),
        gics_known_count=coverage.with_gics_coverage,
        delisting_count=len(delistings), corporate_action_count=len(all_actions),
        retraction_count=len(dataset.retractions),
        sources=tuple(sorted(sources, key=lambda s: s.name)), evidence=evidence_refs,
        blocked_capabilities=blocked, validation_errors=tuple(validation_errors),
        build_warnings=tuple(warnings), coverage=coverage, built_at=built_at,
    )
    snapshot = DatasetSnapshot(
        manifest=manifest,
        security_master=tuple(sorted(dataset.securities, key=lambda s: s.security_id)),
        universe=tuple(universe), symbol_history=symbol_history, gics=tuple(gics_rows),
        prices=prices, delistings=delistings, corporate_actions=all_actions,
        lineage=lineage,
    )
    # The fingerprint covers content + identity fields but NOT snapshot_id /
    # fingerprint themselves, so computing it on a placeholder manifest and then
    # sealing the identity is stable.
    fingerprint = compute_fingerprint(snapshot)
    return replace(
        snapshot,
        manifest=replace(
            manifest,
            snapshot_id=compute_snapshot_id(fingerprint),
            fingerprint=fingerprint,
        ),
    )


def _materialize_prices(
    dataset: DatasetV1,
    config: BuildConfig,
    selected: Sequence[str],
    actions_by_security: Mapping[str, Sequence[CorporateActionRecord]],
) -> tuple[tuple[MaterializedPrice, ...], list[str], list[str]]:
    """Select, validate and adjust prices EXACTLY ONCE."""
    warnings: list[str] = []
    errors: list[str] = []
    selected_set = set(selected)
    rows: list[DailyPriceRecord] = []
    for price in dataset.prices:
        if price.security_id not in selected_set:
            continue
        if price.trading_date > config.as_of:
            continue  # no look-ahead: never include a bar after as_of
        if config.price_start is not None and price.trading_date < config.price_start:
            continue
        # knowledge gate: a price observed after the cutoff is not yet knowable
        if price.provenance.observed_at > config.known_as_of:
            continue
        if price.close <= 0:
            errors.append(
                f"invalid price for {price.security_id} on {price.trading_date}: close <= 0"
            )
            continue
        rows.append(price)

    statuses = {price.adjustment_status for price in rows}
    if PriceAdjustmentStatus.UNKNOWN in statuses:
        raise BuilderError(
            "price series carries UNKNOWN adjustment status; fail closed (double"
            " adjustment cannot be ruled out)"
        )
    if len(statuses) > 1:
        raise BuilderError(
            f"ambiguous mixed price adjustment statuses {sorted(s.value for s in statuses)};"
            " fail closed until explicitly normalized"
        )

    policy = config.adjustment_policy
    materialized: list[MaterializedPrice] = []
    if policy is AdjustmentPolicy.RAW:
        if statuses and statuses != {PriceAdjustmentStatus.RAW_UNADJUSTED}:
            raise BuilderError("RAW policy requires RAW_UNADJUSTED input prices")
        for price in rows:
            materialized.append(_plain(price, PriceAdjustmentStatus.RAW_UNADJUSTED))
    elif policy is AdjustmentPolicy.PROVIDER_ADJUSTED:
        if statuses and statuses != {PriceAdjustmentStatus.PROVIDER_ADJUSTED}:
            raise BuilderError(
                "PROVIDER_ADJUSTED policy requires provider-adjusted input prices;"
                " re-adjusting them with the ledger would double-adjust"
            )
        for price in rows:
            materialized.append(_plain(price, PriceAdjustmentStatus.PROVIDER_ADJUSTED))
    else:  # INTERNAL_LEDGER_ADJUSTED
        if statuses and statuses != {PriceAdjustmentStatus.RAW_UNADJUSTED}:
            raise BuilderError(
                "INTERNAL_LEDGER_ADJUSTED requires RAW_UNADJUSTED input prices;"
                " adjusting an already-adjusted series would double-adjust"
            )
        split_actions = {
            security_id: tuple(
                action
                for action in actions
                if action.action_type
                in (CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT)
            )
            for security_id, actions in actions_by_security.items()
        }
        flat = [action for actions in split_actions.values() for action in actions]
        if not flat:
            raise BuilderError(
                "INTERNAL_LEDGER_ADJUSTED requires corporate-action coverage; no"
                " split/reverse-split events are visible at this knowledge cutoff"
            )
        ledger = _ledger_fingerprint(flat)
        for price in rows:
            factor = _split_factor_before(
                price.trading_date, split_actions.get(price.security_id, ())
            )
            materialized.append(
                MaterializedPrice(
                    security_id=price.security_id, trading_date=price.trading_date,
                    close=price.close / factor,
                    open=price.open / factor if price.open is not None else None,
                    high=price.high / factor if price.high is not None else None,
                    low=price.low / factor if price.low is not None else None,
                    volume=price.volume,
                    adjustment_status=PriceAdjustmentStatus.INTERNALLY_ADJUSTED,
                    adjusted_with_ledger=ledger, source=price.provenance.source,
                )
            )

    if config.forward_fill_prices:
        warnings.append(
            "forward_fill_prices is enabled: "
            f"{config.forward_fill_justification}"
        )
    return (
        tuple(sorted(materialized, key=lambda p: (p.security_id, p.trading_date))),
        warnings,
        errors,
    )


def _plain(price: DailyPriceRecord, status: PriceAdjustmentStatus) -> MaterializedPrice:
    return MaterializedPrice(
        security_id=price.security_id, trading_date=price.trading_date, close=price.close,
        open=price.open, high=price.high, low=price.low, volume=price.volume,
        adjustment_status=status, adjusted_with_ledger=price.adjusted_with_ledger,
        source=price.provenance.source,
    )


def _materialize_lineage(
    dataset: DatasetV1,
    config: BuildConfig,
    selected: Sequence[str],
    actions_by_security: Mapping[str, Sequence[CorporateActionRecord]],
    identities: Mapping[str, SecurityIdentity],
) -> tuple[LineageLink, ...]:
    """Record identity transitions. Nothing is stitched (NO_STITCH policy)."""
    links: list[LineageLink] = []
    for security_id in selected:
        for action in actions_by_security.get(security_id, ()):
            related = action.successor_security_id
            if action.action_type is CorporateActionType.SYMBOL_CHANGE:
                links.append(
                    LineageLink(security_id, LineageLinkKind.SAME_SECURITY_SYMBOL_CHANGE,
                                None, action.effective_on, "ticker changed; identity unchanged")
                )
                continue
            if related is None:
                continue
            if related not in identities:
                links.append(
                    LineageLink(security_id, LineageLinkKind.UNKNOWN_AMBIGUOUS, related,
                                action.effective_on, "successor not in the security master")
                )
                continue
            if action.action_type is CorporateActionType.SPINOFF:
                links.append(
                    LineageLink(security_id, LineageLinkKind.SPINOFF_CHILD, related,
                                action.effective_on, "spin-off child")
                )
            elif action.action_type in (
                CorporateActionType.MERGER, CorporateActionType.ACQUISITION
            ):
                links.append(
                    LineageLink(security_id, LineageLinkKind.TERMINATED_PREDECESSOR, related,
                                action.effective_on, "predecessor terminated")
                )
                links.append(
                    LineageLink(related, LineageLinkKind.SUCCESSOR_SECURITY, security_id,
                                action.effective_on, "successor identity")
                )
    return tuple(links)


# -- validation ---------------------------------------------------------------


def validate_snapshot(snapshot: DatasetSnapshot) -> tuple[str, ...]:
    """Post-build snapshot validation. Correctness issues are errors."""
    errors: list[str] = list(snapshot.manifest.validation_errors)
    master_ids = {identity.security_id for identity in snapshot.security_master}
    if len(master_ids) != len(snapshot.security_master):
        errors.append("duplicate security_id in security master")
    for member in snapshot.universe:
        if member.security_id not in master_ids:
            errors.append(f"universe references unknown security_id={member.security_id}")
    for link in snapshot.lineage:
        if link.related_security_id is not None and (
            link.kind is not LineageLinkKind.UNKNOWN_AMBIGUOUS
            and link.related_security_id not in master_ids
        ):
            errors.append(f"lineage references unknown security_id={link.related_security_id}")
    statuses = {price.adjustment_status for price in snapshot.prices}
    if PriceAdjustmentStatus.UNKNOWN in statuses:
        errors.append("snapshot contains prices with UNKNOWN adjustment status")
    if len(statuses) > 1:
        errors.append("snapshot mixes price adjustment statuses")
    if snapshot.manifest.adjustment_policy is AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED:
        for price in snapshot.prices:
            if not (price.adjusted_with_ledger or "").strip():
                errors.append(
                    f"internally adjusted price for {price.security_id} names no ledger"
                )
                break
    if snapshot.manifest.fingerprint != compute_fingerprint(snapshot):
        errors.append("manifest fingerprint does not match snapshot content (tampered)")
    if snapshot.manifest.snapshot_id != compute_snapshot_id(snapshot.manifest.fingerprint):
        errors.append("snapshot_id does not match the fingerprint")
    return tuple(errors)


# -- serialization ------------------------------------------------------------


def serialize_snapshot(snapshot: DatasetSnapshot) -> str:
    """Canonical, deterministic JSON (Decimals as strings; sorted keys)."""
    payload = {
        "manifest": snapshot.manifest.to_dict(),
        "manifest_checksum": snapshot.manifest.checksum(),
        "content": snapshot_content(snapshot),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot_fingerprint_from_serialized(text: str) -> str:
    """Recompute the fingerprint from a serialized snapshot, detecting tampering."""
    try:
        payload = json.loads(text)
        content = payload["content"]
        manifest = payload["manifest"]
        declared_manifest_checksum = payload["manifest_checksum"]
    except (ValueError, KeyError, TypeError) as error:
        raise BuilderError(f"malformed serialized snapshot: {error}") from error
    body = {key: value for key, value in manifest.items() if key != "built_at"}
    recomputed_manifest = "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()
    if recomputed_manifest != declared_manifest_checksum:
        raise BuilderError("serialized manifest checksum mismatch (tampered)")
    recomputed = "sha256:" + hashlib.sha256(_canonical(content)).hexdigest()
    if recomputed != manifest.get("fingerprint"):
        raise BuilderError("serialized snapshot content does not match its fingerprint")
    return recomputed


__all__ = [
    "BUILDER_VERSION",
    "DATASET_CONTRACT_VERSION",
    "AdjustmentPolicy",
    "BuildConfig",
    "BuilderError",
    "CoverageReport",
    "DatasetSnapshot",
    "EvidenceBinding",
    "GicsAssignment",
    "GicsCoverageState",
    "LineageLink",
    "LineageLinkKind",
    "LineagePolicy",
    "MaterializedPrice",
    "ReadinessLevel",
    "SnapshotManifest",
    "SourceBinding",
    "UniverseMember",
    "build_snapshot",
    "compute_fingerprint",
    "compute_readiness",
    "compute_snapshot_id",
    "serialize_snapshot",
    "snapshot_content",
    "snapshot_fingerprint_from_serialized",
    "validate_snapshot",
]
