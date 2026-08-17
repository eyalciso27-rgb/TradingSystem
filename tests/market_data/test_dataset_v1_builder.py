"""Dataset V1 builder / materialization / reproducible-snapshot drills.

Proves the snapshot is deterministic, point-in-time correct, survivorship-safe,
adjusted exactly once, never auto-stitched, and never backtest-eligible on
fixtures. Offline only; no provider is selected and no evidence is fabricated.
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from trading_market_data_core.dataset_v1 import (
    DATASET_V1_REQUIRED_CAPABILITIES,
    CapabilityState,
    CorporateActionRecord,
    CorporateActionType,
    DailyPriceRecord,
    DatasetV1,
    DelistingRecord,
    EffectiveInterval,
    GicsRecord,
    MembershipRecord,
    PriceAdjustmentStatus,
    Provenance,
    RetractionRecord,
    SecurityIdentity,
    SecurityLifecycleState,
    SymbolHistoryRecord,
)
from trading_market_data_core.dataset_v1_builder import (
    AdjustmentPolicy,
    BuildConfig,
    BuilderError,
    EvidenceBinding,
    GicsCoverageState,
    LineageLinkKind,
    LineagePolicy,
    ReadinessLevel,
    SourceBinding,
    build_snapshot,
    compute_fingerprint,
    serialize_snapshot,
    snapshot_fingerprint_from_serialized,
    validate_snapshot,
)

AS_OF = date(2021, 6, 30)
KNOWN = datetime(2021, 7, 1, tzinfo=UTC)
ANCIENT = datetime(2005, 1, 1, tzinfo=UTC)
OBSERVED = datetime(2010, 1, 1, tzinfo=UTC)
PROV = Provenance("fixture-source", OBSERVED)
SOURCE = SourceBinding("fixture-source", "v1", "sha256:" + "a" * 64)


def _fields(dataset: DatasetV1) -> dict[str, Any]:
    """Shallow field mapping (DatasetV1 is slots=True, so it has no __dict__)."""
    return {f.name: getattr(dataset, f.name) for f in fields(dataset)}


def _evidence(tmp_path: Path, name: str = "ref") -> EvidenceBinding:
    """A genuinely verifiable evidence artifact (content-hashed on disk)."""
    import hashlib

    path = tmp_path / f"{name}.json"
    path.write_text(f'{{"artifact":"{name}"}}', encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return EvidenceBinding(name, path, digest)


def _sec(security_id: str, symbol: str) -> SecurityIdentity:
    return SecurityIdentity(security_id=security_id, symbol=symbol, primary_exchange="NYSE")


def _config(**kw: object) -> BuildConfig:
    base: dict[str, object] = {
        "as_of": AS_OF, "known_as_of": KNOWN, "index_id": "SP500",
        "adjustment_policy": AdjustmentPolicy.RAW,
    }
    base.update(kw)
    return BuildConfig(**base)  # type: ignore[arg-type]


def _price(
    security_id: str, day: date, close: str,
    status: PriceAdjustmentStatus = PriceAdjustmentStatus.RAW_UNADJUSTED,
) -> DailyPriceRecord:
    return DailyPriceRecord(
        security_id=security_id, trading_date=day, close=Decimal(close), provenance=PROV,
        adjustment_status=status,
    )


def _dataset() -> DatasetV1:
    """A survivorship-relevant fixture: one member that later delists, one entrant."""
    return DatasetV1(
        securities=(_sec("sec-old", "OLDNOW"), _sec("sec-new", "NEW")),
        memberships=(
            MembershipRecord(
                record_id="m-old", index_id="SP500", security_id="sec-old",
                interval=EffectiveInterval(date(2015, 1, 1), date(2021, 12, 31)),
                known_at=ANCIENT, provenance=PROV,
            ),
            MembershipRecord(
                record_id="m-new", index_id="SP500", security_id="sec-new",
                interval=EffectiveInterval(date(2022, 1, 1)), known_at=ANCIENT, provenance=PROV,
            ),
        ),
        gics=(
            GicsRecord(
                record_id="g-old", security_id="sec-old", sector="Industrials",
                industry_group=None, industry=None, sub_industry=None,
                interval=EffectiveInterval(date(2015, 1, 1)), known_at=ANCIENT, provenance=PROV,
            ),
        ),
        symbol_history=(
            SymbolHistoryRecord(
                record_id="s-old-1", security_id="sec-old", symbol="OLD",
                interval=EffectiveInterval(date(2015, 1, 1), date(2021, 12, 31)),
                known_at=ANCIENT, provenance=PROV,
            ),
        ),
        delistings=(
            DelistingRecord(
                record_id="d-old", security_id="sec-old", delisted_on=date(2022, 3, 1),
                known_at=ANCIENT, provenance=PROV, listing_date=date(2010, 1, 1),
            ),
        ),
        prices=(
            _price("sec-old", date(2021, 6, 29), "100"),
            _price("sec-old", date(2021, 6, 30), "101"),
            _price("sec-old", date(2021, 7, 1), "999"),  # after as_of: excluded
            _price("sec-new", date(2021, 6, 30), "50"),  # not a member yet: excluded
        ),
    )


def _build(dataset: DatasetV1 | None = None, config: BuildConfig | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return build_snapshot(
        dataset if dataset is not None else _dataset(),
        config if config is not None else _config(),
        sources=(SOURCE,),
        **kw,  # type: ignore[arg-type]
    )


# -- point-in-time universe ---------------------------------------------------


def test_universe_is_point_in_time_and_survivorship_safe() -> None:
    snapshot = _build()
    ids = {member.security_id for member in snapshot.universe}
    assert ids == {"sec-old"}  # future entrant excluded, later-delisted name retained
    member = snapshot.universe[0]
    # the HISTORICAL ticker is materialized, not today's SecurityIdentity.symbol
    assert member.symbol_as_of == "OLD"
    assert any(s.symbol == "OLDNOW" for s in snapshot.security_master)
    assert member.lifecycle_state is SecurityLifecycleState.ACTIVE


def test_prices_never_include_bars_after_as_of() -> None:
    snapshot = _build()
    assert {p.trading_date for p in snapshot.prices} == {date(2021, 6, 29), date(2021, 6, 30)}
    assert all(p.security_id == "sec-old" for p in snapshot.prices)


def test_price_knowledge_cutoff_excludes_unknowable_observation() -> None:
    dataset = _dataset()
    late = DailyPriceRecord(
        "sec-old", date(2021, 6, 28), Decimal("77"),
        Provenance("fixture-source", datetime(2026, 1, 1, tzinfo=UTC)),
        adjustment_status=PriceAdjustmentStatus.RAW_UNADJUSTED,
    )
    snapshot = _build(DatasetV1(**{**_fields(dataset), "prices": (*dataset.prices, late)}))
    assert date(2021, 6, 28) not in {p.trading_date for p in snapshot.prices}


def test_universe_unknown_security_is_a_validation_error() -> None:
    dataset = _dataset()
    orphan = MembershipRecord(
        record_id="m-ghost", index_id="SP500", security_id="ghost",
        interval=EffectiveInterval(date(2015, 1, 1)), known_at=ANCIENT, provenance=PROV,
    )
    broken = DatasetV1(
        **{**_fields(dataset), "memberships": (*dataset.memberships, orphan)}
    )
    snapshot = _build(broken)
    assert any("unknown security_id=ghost" in e for e in snapshot.manifest.validation_errors)


# -- GICS materialization -----------------------------------------------------


def test_gics_is_as_of_and_distinguishes_unknown_from_no_coverage() -> None:
    snapshot = _build()
    assignment = snapshot.gics[0]
    assert assignment.state is GicsCoverageState.KNOWN
    assert assignment.sector == "Industrials"

    dataset = _dataset()
    no_gics = DatasetV1(**{**_fields(dataset), "gics": ()})
    assert _build(no_gics).gics[0].state is GicsCoverageState.NO_SOURCE_COVERAGE

    # source covers GICS, but not this security on this date -> UNKNOWN
    other_only = DatasetV1(
        **{
            **_fields(dataset),
            "gics": (
                GicsRecord(
                    record_id="g-new", security_id="sec-new", sector="Tech",
                    industry_group=None, industry=None, sub_industry=None,
                    interval=EffectiveInterval(date(2015, 1, 1)), known_at=ANCIENT,
                    provenance=PROV,
                ),
            ),
        }
    )
    assert _build(other_only).gics[0].state is GicsCoverageState.UNKNOWN


# -- adjustment policy: exactly once -----------------------------------------


def test_raw_policy_keeps_raw_prices() -> None:
    snapshot = _build(config=_config(adjustment_policy=AdjustmentPolicy.RAW))
    assert all(
        p.adjustment_status is PriceAdjustmentStatus.RAW_UNADJUSTED for p in snapshot.prices
    )
    assert snapshot.prices[0].close == Decimal("100")


def test_provider_adjusted_is_never_readjusted() -> None:
    dataset = _dataset()
    provider = DatasetV1(
        **{
            **_fields(dataset),
            "prices": tuple(
                _price(p.security_id, p.trading_date, str(p.close),
                       PriceAdjustmentStatus.PROVIDER_ADJUSTED)
                for p in dataset.prices
            ),
        }
    )
    snapshot = _build(provider, _config(adjustment_policy=AdjustmentPolicy.PROVIDER_ADJUSTED))
    assert all(
        p.adjustment_status is PriceAdjustmentStatus.PROVIDER_ADJUSTED for p in snapshot.prices
    )
    assert snapshot.prices[0].close == Decimal("100")  # untouched
    # and the ledger policy refuses an already-adjusted series
    with pytest.raises(BuilderError, match="would double-adjust"):
        _build(provider, _config(adjustment_policy=AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED))


def test_unknown_and_mixed_adjustment_status_fail_closed() -> None:
    dataset = _dataset()
    unknown = DatasetV1(
        **{
            **_fields(dataset),
            "prices": (
                _price("sec-old", date(2021, 6, 29), "100", PriceAdjustmentStatus.UNKNOWN),
            ),
        }
    )
    with pytest.raises(BuilderError, match="UNKNOWN adjustment status"):
        _build(unknown)
    mixed = DatasetV1(
        **{
            **_fields(dataset),
            "prices": (
                _price("sec-old", date(2021, 6, 29), "100"),
                _price("sec-old", date(2021, 6, 30), "101",
                       PriceAdjustmentStatus.PROVIDER_ADJUSTED),
            ),
        }
    )
    with pytest.raises(BuilderError, match="ambiguous mixed"):
        _build(mixed)


def _split_dataset() -> DatasetV1:
    dataset = _dataset()
    split = CorporateActionRecord(
        record_id="ca-split", security_id="sec-old", action_type=CorporateActionType.SPLIT,
        effective_on=date(2021, 6, 30), known_at=ANCIENT, provenance=PROV, ratio=Decimal("2"),
    )
    return DatasetV1(**{**_fields(dataset), "corporate_actions": (split,)})


def test_internal_ledger_adjustment_applies_split_exactly_once() -> None:
    snapshot = _build(
        _split_dataset(), _config(adjustment_policy=AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED)
    )
    by_date = {p.trading_date: p for p in snapshot.prices}
    # a 2:1 split effective 2021-06-30 halves prices strictly BEFORE that date
    assert by_date[date(2021, 6, 29)].close == Decimal("50")
    assert by_date[date(2021, 6, 30)].close == Decimal("101")  # on/after: untouched
    assert all(
        p.adjustment_status is PriceAdjustmentStatus.INTERNALLY_ADJUSTED
        for p in snapshot.prices
    )
    assert all(p.adjusted_with_ledger for p in snapshot.prices)
    # rebuilding does not adjust again (idempotent by construction: same inputs)
    again = _build(
        _split_dataset(), _config(adjustment_policy=AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED)
    )
    assert compute_fingerprint(again) == compute_fingerprint(snapshot)


def test_internal_adjustment_requires_corporate_action_coverage() -> None:
    with pytest.raises(BuilderError, match="requires corporate-action coverage"):
        _build(config=_config(adjustment_policy=AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED))


def test_reverse_split_uses_ratio_direction() -> None:
    dataset = _dataset()
    reverse = CorporateActionRecord(
        record_id="ca-rev", security_id="sec-old",
        action_type=CorporateActionType.REVERSE_SPLIT, effective_on=date(2021, 6, 30),
        known_at=ANCIENT, provenance=PROV, ratio=Decimal("0.1"),
    )
    snapshot = _build(
        DatasetV1(**{**_fields(dataset), "corporate_actions": (reverse,)}),
        _config(adjustment_policy=AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED),
    )
    by_date = {p.trading_date: p for p in snapshot.prices}
    assert by_date[date(2021, 6, 29)].close == Decimal("1000")  # 100 / 0.1


def test_corporate_action_unknown_at_cutoff_is_not_applied() -> None:
    dataset = _dataset()
    late_split = CorporateActionRecord(
        record_id="ca-late", security_id="sec-old", action_type=CorporateActionType.SPLIT,
        effective_on=date(2021, 6, 30), known_at=datetime(2026, 1, 1, tzinfo=UTC),
        provenance=PROV, ratio=Decimal("2"),
    )
    with pytest.raises(BuilderError, match="requires corporate-action coverage"):
        _build(
            DatasetV1(**{**_fields(dataset), "corporate_actions": (late_split,)}),
            _config(adjustment_policy=AdjustmentPolicy.INTERNAL_LEDGER_ADJUSTED),
        )


def test_dividend_adjustment_and_unjustified_forward_fill_fail_closed() -> None:
    with pytest.raises(BuilderError, match="dividend adjustment is not implemented"):
        _config(include_dividend_adjustment=True)
    with pytest.raises(BuilderError, match="requires an explicit written justification"):
        _config(forward_fill_prices=True)
    with pytest.raises(BuilderError, match="forward_fill_prices is not implemented"):
        _config(forward_fill_prices=True, forward_fill_justification="documented gap policy")


def test_no_hidden_forward_fill_by_default() -> None:
    snapshot = _build()
    assert all(not p.forward_filled for p in snapshot.prices)


# -- lineage ------------------------------------------------------------------


def test_lineage_is_recorded_not_stitched() -> None:
    dataset = _dataset()
    merger = CorporateActionRecord(
        record_id="ca-m", security_id="sec-old", action_type=CorporateActionType.MERGER,
        effective_on=date(2021, 6, 1), known_at=ANCIENT, provenance=PROV,
        successor_security_id="sec-new",
    )
    snapshot = _build(DatasetV1(**{**_fields(dataset), "corporate_actions": (merger,)}))
    kinds = {link.kind for link in snapshot.lineage}
    assert LineageLinkKind.TERMINATED_PREDECESSOR in kinds
    assert LineageLinkKind.SUCCESSOR_SECURITY in kinds
    # NO stitching: the successor's prices are not merged into sec-old's series
    assert {p.security_id for p in snapshot.prices} == {"sec-old"}
    assert snapshot.manifest.lineage_policy is LineagePolicy.NO_STITCH


def test_stitching_policy_is_not_implemented_and_fails_closed() -> None:
    with pytest.raises(BuilderError, match="never auto-stitched"):
        _config(lineage_policy=LineagePolicy.STITCH_ECONOMIC_LINEAGE)


# -- reproducibility ----------------------------------------------------------


def test_same_inputs_produce_same_snapshot_id_and_fingerprint() -> None:
    first, second = _build(), _build()
    assert first.manifest.fingerprint == second.manifest.fingerprint
    assert first.manifest.snapshot_id == second.manifest.snapshot_id


def test_input_ordering_does_not_change_the_fingerprint() -> None:
    dataset = _dataset()
    reordered = DatasetV1(
        securities=tuple(reversed(dataset.securities)),
        memberships=tuple(reversed(dataset.memberships)),
        gics=dataset.gics, symbol_history=dataset.symbol_history,
        delistings=dataset.delistings, prices=tuple(reversed(dataset.prices)),
    )
    assert _build(dataset).manifest.fingerprint == _build(reordered).manifest.fingerprint


def test_wall_clock_does_not_affect_the_fingerprint() -> None:
    early = _build(built_at=datetime(2021, 7, 2, tzinfo=UTC))
    later = _build(built_at=datetime(2030, 1, 1, tzinfo=UTC))
    assert early.manifest.fingerprint == later.manifest.fingerprint
    assert early.manifest.snapshot_id == later.manifest.snapshot_id
    assert early.manifest.built_at != later.manifest.built_at  # metadata only


def test_later_revision_does_not_alter_an_earlier_snapshot() -> None:
    dataset = _dataset()
    baseline = _build(dataset).manifest.fingerprint
    correction = MembershipRecord(
        record_id="m-old-fix", index_id="SP500", security_id="sec-old",
        interval=EffectiveInterval(date(2015, 1, 1), date(2020, 12, 31)),
        known_at=datetime(2026, 1, 1, tzinfo=UTC), provenance=PROV, supersedes="m-old",
    )
    revised = DatasetV1(
        **{**_fields(dataset), "memberships": (*dataset.memberships, correction)}
    )
    # rebuilding the OLD snapshot (same known_as_of) is byte-identical
    assert _build(revised).manifest.fingerprint == baseline
    # a later knowledge cutoff legitimately differs
    later = _build(revised, _config(known_as_of=datetime(2026, 6, 1, tzinfo=UTC)))
    assert later.manifest.fingerprint != baseline
    assert {m.security_id for m in later.universe} == set()  # membership was shortened


def test_later_retraction_does_not_alter_an_earlier_snapshot() -> None:
    dataset = _dataset()
    baseline = _build(dataset).manifest.fingerprint
    retracted = DatasetV1(
        **{
            **_fields(dataset),
            "retractions": (
                RetractionRecord("r1", "m-old", datetime(2026, 1, 1, tzinfo=UTC), PROV),
            ),
        }
    )
    assert _build(retracted).manifest.fingerprint == baseline
    later = _build(retracted, _config(known_as_of=datetime(2026, 6, 1, tzinfo=UTC)))
    assert later.manifest.fingerprint != baseline
    assert later.universe == ()


def test_policy_and_source_changes_change_the_fingerprint() -> None:
    baseline = _build().manifest.fingerprint
    provider_dataset = DatasetV1(
        **{
            **_fields(_dataset()),
            "prices": tuple(
                _price(p.security_id, p.trading_date, str(p.close),
                       PriceAdjustmentStatus.PROVIDER_ADJUSTED)
                for p in _dataset().prices
            ),
        }
    )
    assert (
        build_snapshot(
            provider_dataset, _config(adjustment_policy=AdjustmentPolicy.PROVIDER_ADJUSTED),
            sources=(SOURCE,),
        ).manifest.fingerprint
        != baseline
    )
    other_source = SourceBinding("fixture-source", "v1", "sha256:" + "b" * 64)
    assert (
        build_snapshot(_dataset(), _config(), sources=(other_source,)).manifest.fingerprint
        != baseline
    )


def test_as_of_and_known_as_of_change_the_fingerprint() -> None:
    baseline = _build().manifest.fingerprint
    assert _build(config=_config(as_of=date(2021, 6, 29))).manifest.fingerprint != baseline


# -- manifest, validation, serialization -------------------------------------


def test_manifest_records_the_build_contract() -> None:
    snapshot = _build(built_at=datetime(2021, 7, 2, tzinfo=UTC))
    manifest = snapshot.manifest
    assert manifest.dataset_contract_version == "dataset-v1"
    assert manifest.index_id == "SP500"
    assert manifest.membership_count == 1
    assert manifest.price_row_count == 2
    assert manifest.delisting_count == 1
    assert manifest.sources[0].checksum.startswith("sha256:")
    assert manifest.checksum().startswith("sha256:")
    payload = manifest.to_dict()
    assert payload["readiness"] == ReadinessLevel.CONTRACT_ONLY.value
    assert set(payload["blocked_capabilities"]) == set(DATASET_V1_REQUIRED_CAPABILITIES)


def test_validate_snapshot_is_clean_and_detects_tampering() -> None:
    snapshot = _build()
    assert validate_snapshot(snapshot) == ()
    tampered = replace(
        snapshot, manifest=replace(snapshot.manifest, fingerprint="sha256:" + "0" * 64)
    )
    assert any("does not match" in e for e in validate_snapshot(tampered))


def test_serialization_round_trip_preserves_fingerprint_and_decimals() -> None:
    snapshot = _build()
    text = serialize_snapshot(snapshot)
    assert snapshot_fingerprint_from_serialized(text) == snapshot.manifest.fingerprint
    assert '"close":"100"' in text  # Decimal serialized exactly, as a string
    assert serialize_snapshot(_build()) == text  # deterministic bytes


def test_serialized_data_tampering_is_detected() -> None:
    text = serialize_snapshot(_build())
    tampered = text.replace('"close":"100"', '"close":"1"')
    with pytest.raises(BuilderError, match="does not match its fingerprint"):
        snapshot_fingerprint_from_serialized(tampered)


def test_serialized_manifest_tampering_is_detected() -> None:
    text = serialize_snapshot(_build())
    tampered = text.replace('"index_id":"SP500"', '"index_id":"HACKED"')
    with pytest.raises(BuilderError, match="manifest checksum mismatch"):
        snapshot_fingerprint_from_serialized(tampered)


# -- evidence binding ---------------------------------------------------------


def test_evidence_binding_is_verified_not_trusted(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "evidence.json"
    path.write_text('{"ok":true}', encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    good = EvidenceBinding("ref", path, digest)
    snapshot = _build(evidence=(good,))
    assert snapshot.manifest.evidence == (f"ref={digest}",)

    bad = EvidenceBinding("ref", path, "sha256:" + "0" * 64)
    with pytest.raises(BuilderError, match="checksum mismatch"):
        _build(evidence=(bad,))
    missing = EvidenceBinding("ref", tmp_path / "nope.json", digest)
    with pytest.raises(BuilderError, match="file not found"):
        _build(evidence=(missing,))


# -- readiness (no overclaim) -------------------------------------------------


def test_fixture_build_is_contract_only_and_never_backtest_eligible() -> None:
    snapshot = _build()
    assert snapshot.manifest.readiness is ReadinessLevel.CONTRACT_ONLY
    assert snapshot.backtest_eligible is False
    assert set(snapshot.manifest.blocked_capabilities) == set(DATASET_V1_REQUIRED_CAPABILITIES)


def test_partial_capability_evidence_is_partial_data(tmp_path: Path) -> None:
    states = {
        name: CapabilityState.EMPIRICALLY_VERIFIED
        for name in sorted(DATASET_V1_REQUIRED_CAPABILITIES)[:2]
    }
    snapshot = _build(capability_states=states, evidence=(_evidence(tmp_path),))
    assert snapshot.manifest.readiness is ReadinessLevel.PARTIAL_DATA
    assert snapshot.backtest_eligible is False


def test_full_evidence_reaches_backtest_eligible_only_with_clean_coverage(
    tmp_path: Path,
) -> None:
    states = {
        name: CapabilityState.EMPIRICALLY_VERIFIED
        for name in DATASET_V1_REQUIRED_CAPABILITIES
    }
    evidence = (_evidence(tmp_path),)
    snapshot = _build(capability_states=states, evidence=evidence)
    # every universe member is priced and validation is clean -> eligible
    assert snapshot.manifest.readiness is ReadinessLevel.BACKTEST_ELIGIBLE
    assert snapshot.backtest_eligible is True

    # remove price coverage -> reference-ready but NOT backtest eligible
    dataset = _dataset()
    unpriced = DatasetV1(**{**_fields(dataset), "prices": ()})
    partial = _build(unpriced, capability_states=states, evidence=evidence)
    assert partial.manifest.readiness is ReadinessLevel.EMPIRICAL_REFERENCE_READY
    assert partial.backtest_eligible is False


def test_coverage_report_exposes_gaps_not_a_boolean() -> None:
    dataset = _dataset()
    unpriced = DatasetV1(**{**_fields(dataset), "prices": ()})
    coverage = _build(unpriced).manifest.coverage.to_dict()
    assert coverage["universe_securities"] == 1
    assert coverage["price_coverage_pct"] == "0.00"
    assert coverage["missing_price_securities"] == ["sec-old"]
    assert set(coverage["blocked_capabilities"]) == set(DATASET_V1_REQUIRED_CAPABILITIES)


def test_empirical_claim_without_evidence_fails_closed() -> None:
    states = {
        name: CapabilityState.EMPIRICALLY_VERIFIED
        for name in DATASET_V1_REQUIRED_CAPABILITIES
    }
    with pytest.raises(BuilderError, match="no verifiable evidence binding"):
        _build(capability_states=states)


def test_empirical_claim_without_source_binding_fails_closed(tmp_path: Path) -> None:
    states = {
        name: CapabilityState.EMPIRICALLY_VERIFIED
        for name in DATASET_V1_REQUIRED_CAPABILITIES
    }
    with pytest.raises(BuilderError, match="no checksummed source binding"):
        build_snapshot(
            _dataset(), _config(), sources=(), evidence=(_evidence(tmp_path),),
            capability_states=states,
        )
