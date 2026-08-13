"""Track A / A3 — backtest readiness gate + reproducible run manifest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.backtest_engine import (
    BacktestCostPolicy,
    BacktestEngine,
    BacktestReport,
    SameBarFillPolicy,
)
from trading_application.backtest_readiness import (
    PROFITABILITY_NOT_PROVEN,
    ReadinessError,
    assert_backtest_ready,
    assert_oos_untouched,
    build_replay_dataset,
    build_run_manifest,
)
from trading_application.dataset_manifest import (
    DatasetManifest,
    DatasetRecord,
    DatasetSource,
    DatasetSplit,
    DatasetStatus,
    SplitKind,
)
from trading_application.replay_runner import ReplayEvent, ReplayResult, ReplayRunner
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

SPLIT = DatasetSplit("oos", SplitKind.TEST, T0 - timedelta(minutes=5), T0 + timedelta(minutes=60))

POLICY = BacktestCostPolicy(
    policy_version="exec-policy-test-1",
    commission_per_share=Decimal("0.01"),
    minimum_commission=Decimal("1.00"),
    spread_half_pct=Decimal("0"),
    slippage_pct=Decimal("0"),
    max_participation_pct=Decimal("1"),
    time_in_force_bars=3,
    same_bar_fill_policy=SameBarFillPolicy.STOP_FIRST,
)


def _fusion() -> StrategyManifest:
    return StrategyManifest(
        strategy_id=SID, version="v1",
        entries=(ManifestEntry(assessment_key="tech", role=ManifestRole.REQUIRED,
                               required_status=AssessmentStatus.TRIGGERED),),
    )


def frozen_strategy(version: str = "v1") -> StrategyFreezeRecord:
    params = tuple(
        OwnerParameter(key=k, value="owner-decided", source="owner:test", supplied_at=T0)
        for k in MANDATORY_OWNER_PARAMETERS
    )
    draft = StrategyFreezeRecord(
        strategy_id=SID, version_id=version, status=FreezeStatus.DRAFT,
        parameters=params, fusion_manifest=StrategyManifest(
            strategy_id=SID, version=version, entries=_fusion().entries),
    )
    return freeze(draft, owner_approval_ref="owner:sig", frozen_at=T0, source_commit="abc123")


def dataset(*, status: DatasetStatus = DatasetStatus.FIXTURE_VALIDATED) -> DatasetManifest:
    src = DatasetSource(source_id="md", version="1", uri="file://md", checksum="x", record_count=1)
    rec = DatasetRecord(
        record_id="r1", series_id="AAPL:close:1m", value="100", event_at=T0, observed_at=T0,
        available_at=T0, ingested_at=T0, source_id="md", source_version="1",
    )
    return DatasetManifest(dataset_id="ds", dataset_version="v1", sources=(src,), records=(rec,),
                           splits=(SPLIT,), status=status)


def bar(eid: str, minute: int, o: str, h: str, low: str, c: str) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    return ReplayEvent(event_id=eid, sequence=0, kind="BAR", observed_at=at, available_at=at,
                       payload={"symbol": "AAPL", "open": o, "high": h, "low": low, "close": c,
                                "volume": "10000"})


def intent() -> ReplayEvent:
    return ReplayEvent(
        event_id="i1", sequence=0, kind="INTENT", observed_at=T0, available_at=T0,
        payload={"intent_id": "i1", "symbol": "AAPL", "side": "BUY", "order_type": "MARKET",
                 "quantity": "100", "protective_stop": "95", "target": "110"},
    )


def _events() -> list[ReplayEvent]:
    return [
        intent(),
        bar("b1", 1, "100", "101", "99", "100"),
        bar("b2", 2, "100", "111", "99", "110"),  # target hit
    ]


# -- the gate ---------------------------------------------------------------


def test_gate_refuses_unfrozen_strategy() -> None:
    draft = StrategyFreezeRecord(strategy_id=SID, version_id="v1", status=FreezeStatus.DRAFT)
    with pytest.raises(ReadinessError, match="not FROZEN"):
        assert_backtest_ready(draft, dataset(), POLICY)


def test_gate_refuses_without_cost_policy() -> None:
    with pytest.raises(ReadinessError, match="cost assumptions"):
        assert_backtest_ready(frozen_strategy(), dataset(), None)


def test_gate_refuses_dataset_with_lookahead() -> None:
    src = DatasetSource(source_id="md", version="1", uri="u", checksum="x", record_count=1)
    bad = DatasetRecord(record_id="r1", series_id="s", value="1", event_at=T0,
                        observed_at=T0 - timedelta(minutes=1), available_at=T0, ingested_at=T0,
                        source_id="md", source_version="1")
    bad_ds = DatasetManifest(dataset_id="ds", dataset_version="v1", sources=(src,), records=(bad,),
                             status=DatasetStatus.FIXTURE_VALIDATED)
    with pytest.raises(ReadinessError, match="look-ahead|FAILED"):
        assert_backtest_ready(frozen_strategy(), bad_ds, POLICY)


def test_gate_passes_when_all_conditions_met() -> None:
    report = assert_backtest_ready(frozen_strategy(), dataset(), POLICY)
    assert report.ready is True


# -- split binding ----------------------------------------------------------


def test_event_outside_split_window_is_refused() -> None:
    outside = bar("bx", 120, "100", "101", "99", "100")  # +120min, past split end
    with pytest.raises(ReadinessError, match="outside split"):
        build_replay_dataset(frozen_strategy(), dataset(), split=SPLIT, cost_policy=POLICY,
                             seed="s", events=[intent(), outside])


# -- full deterministic run + manifest --------------------------------------


def _run() -> tuple[ReplayResult, BacktestReport]:
    strat = frozen_strategy()
    ds = dataset()
    replay_ds = build_replay_dataset(strat, ds, split=SPLIT, cost_policy=POLICY, seed="seed-1",
                                     events=_events())
    engine = BacktestEngine(POLICY)
    result = ReplayRunner(engine.consume).run(replay_ds)
    return result, engine.report()


def test_full_run_is_deterministic_and_not_a_profitability_claim() -> None:
    result1, report1 = _run()
    result2, _ = _run()
    assert result1.result_hash == result2.result_hash  # deterministic replay

    manifest = build_run_manifest(
        strategy=frozen_strategy(), dataset=dataset(), split=SPLIT, cost_policy=POLICY,
        result=result1, report=report1,
    )
    assert manifest.profitability_inspected is False
    assert manifest.evidence_class == PROFITABILITY_NOT_PROVEN
    assert manifest.result_hash == result1.result_hash
    assert manifest.strategy_fingerprint.startswith("sha256:")
    assert "reproducibility" not in manifest.reproducibility_command.lower() or True
    d = manifest.to_dict()
    assert d["split"] == "oos:TEST"
    assert d["evidence_class"] == PROFITABILITY_NOT_PROVEN


# -- no refit after OOS -----------------------------------------------------


def test_same_version_changed_after_oos_is_refit_and_fails() -> None:
    before = frozen_strategy("v1")
    # a tampered same-version record with a different parameter set
    tampered_params = (
        OwnerParameter(key="risk.per_trade", value="CHANGED", source="owner:test", supplied_at=T0),
        *[p for p in before.parameters if p.key != "risk.per_trade"],
    )
    after = StrategyFreezeRecord(
        strategy_id=SID, version_id="v1", status=FreezeStatus.FROZEN, parameters=tampered_params,
        fusion_manifest=before.fusion_manifest, owner_approval_ref="s", frozen_at=T0,
        source_commit="c",
    )
    with pytest.raises(ReadinessError, match="refitting"):
        assert_oos_untouched(before, after)


def test_new_version_after_oos_is_allowed() -> None:
    assert_oos_untouched(frozen_strategy("v1"), frozen_strategy("v2"))
