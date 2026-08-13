"""Cross-cutting acceptance — Track A subset (items 1-6, 14).

One consolidated proof of the binding Track-A invariants. The Track-C items
(7-13) live with the source-activation package.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.backtest_engine import BacktestCostPolicy, SameBarFillPolicy
from trading_application.backtest_readiness import ReadinessError, assert_backtest_ready
from trading_application.dataset_manifest import (
    DatasetError,
    DatasetManifest,
    DatasetRecord,
    DatasetSource,
    DatasetSplit,
    DatasetStatus,
    NoLookaheadAuditor,
    SplitKind,
)
from trading_application.strategy_freeze import (
    MANDATORY_OWNER_PARAMETERS,
    FreezeStatus,
    OwnerParameter,
    StrategyFreezeRecord,
    freeze,
)
from trading_application.strategy_manifest import ManifestEntry, ManifestRole, StrategyManifest
from trading_contracts.enums import AssessmentStatus

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
SID = "baseline-trend-breakout-v1"

POLICY = BacktestCostPolicy(
    policy_version="p1", commission_per_share=Decimal("0.01"), minimum_commission=Decimal("1"),
    spread_half_pct=Decimal("0"), slippage_pct=Decimal("0"), max_participation_pct=Decimal("1"),
    time_in_force_bars=3, same_bar_fill_policy=SameBarFillPolicy.STOP_FIRST,
)


def _frozen(*, drop_cost: bool = False) -> StrategyFreezeRecord:
    keys = [k for k in MANDATORY_OWNER_PARAMETERS if not (drop_cost and k.startswith("costs."))]
    params = tuple(
        OwnerParameter(key=k, value="owner-decided", source="owner:test", supplied_at=T0)
        for k in keys
    )
    manifest = StrategyManifest(
        strategy_id=SID, version="v1",
        entries=(ManifestEntry(assessment_key="t", role=ManifestRole.REQUIRED,
                               required_status=AssessmentStatus.TRIGGERED),),
    )
    draft = StrategyFreezeRecord(strategy_id=SID, version_id="v1", status=FreezeStatus.DRAFT,
                                 parameters=params, fusion_manifest=manifest)
    if drop_cost:
        return draft  # cannot be frozen; returned as DRAFT for the negative test
    return freeze(draft, owner_approval_ref="s", frozen_at=T0, source_commit="c")


def _record(rid: str, *, observed: datetime | None = None) -> DatasetRecord:
    return DatasetRecord(
        record_id=rid, series_id="AAPL:1d", value="100", event_at=T0,
        observed_at=observed or T0, available_at=T0, ingested_at=T0,
        source_id="md", source_version="1",
    )


def _dataset(records: tuple[DatasetRecord, ...], splits: tuple[DatasetSplit, ...] = ()
             ) -> DatasetManifest:
    src = DatasetSource(source_id="md", version="1", uri="u", checksum="x", record_count=1)
    return DatasetManifest(dataset_id="ds", dataset_version="v1", sources=(src,), records=records,
                           splits=splits, status=DatasetStatus.FIXTURE_VALIDATED)


# 1. a frozen strategy cannot mutate
def test_acceptance_1_frozen_strategy_cannot_mutate() -> None:
    frozen = _frozen()
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.parameters = ()  # type: ignore[misc]


# 2. an unfrozen strategy cannot be evaluated
def test_acceptance_2_unfrozen_strategy_cannot_be_evaluated() -> None:
    draft = StrategyFreezeRecord(strategy_id=SID, version_id="v1", status=FreezeStatus.DRAFT)
    with pytest.raises(ReadinessError, match="not FROZEN"):
        assert_backtest_ready(draft, _dataset((_record("r1"),)), POLICY)


# 3. dataset look-ahead is rejected
def test_acceptance_3_dataset_lookahead_rejected() -> None:
    bad = _record("r1", observed=T0 - timedelta(minutes=1))
    with pytest.raises(DatasetError, match="FAILED"):
        NoLookaheadAuditor().assert_no_lookahead(_dataset((bad,)))


# 4. split contamination is rejected
def test_acceptance_4_split_contamination_rejected() -> None:
    train = DatasetSplit("train", SplitKind.TRAIN, T0, T0 + timedelta(days=10))
    test = DatasetSplit("test", SplitKind.TEST, T0 + timedelta(days=5), T0 + timedelta(days=15))
    with pytest.raises(DatasetError, match="contamination"):
        _dataset((_record("r1"),), (train, test))


# 5. deterministic rebuild produces the same fingerprint
def test_acceptance_5_deterministic_rebuild_same_fingerprint() -> None:
    a = _dataset((_record("r1"), _record("r2")))
    b = _dataset((_record("r2"), _record("r1")))
    assert a.fingerprint() == b.fingerprint()
    assert _frozen().fingerprint() == _frozen().fingerprint()


# 6. missing owner cost assumptions block the backtest
def test_acceptance_6_missing_cost_assumptions_block_backtest() -> None:
    # missing frozen cost parameters
    with pytest.raises(ReadinessError, match="cost"):
        assert_backtest_ready(_frozen(drop_cost=True), _dataset((_record("r1"),)), POLICY)
    # missing cost policy object
    with pytest.raises(ReadinessError, match="cost assumptions"):
        assert_backtest_ready(_frozen(), _dataset((_record("r1"),)), None)


# 14. all evidence carries full lineage / fingerprints
def test_acceptance_14_evidence_has_lineage_and_fingerprints() -> None:
    frozen = _frozen()
    dataset = _dataset((_record("r1"),))
    assert frozen.fingerprint().startswith("sha256:")
    assert dataset.fingerprint().startswith("sha256:")
    assert dataset.snapshot_identity().startswith("snapshot:")
    # a frozen strategy records who supplied every parameter (provenance)
    assert all(p.source for p in frozen.parameters)
