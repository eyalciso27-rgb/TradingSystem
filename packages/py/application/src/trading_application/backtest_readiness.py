"""Backtest / replay readiness gate (Track A / A3).

Ties the Track-A foundations to the EXISTING replay/backtest architecture — it
adds no parallel engine. A backtest may run only when:

* the strategy is **FROZEN** (`strategy_freeze.StrategyFreezeRecord`);
* the **dataset manifest is valid** and its **no-look-ahead audit PASSES**
  (`dataset_manifest.NoLookaheadAuditor`) with no split contamination;
* the owner **cost / slippage / spread assumptions** exist — both as frozen
  strategy parameters AND as a concrete `backtest_engine.BacktestCostPolicy`.

Any missing input makes the run **refuse** (fail closed) — nothing is invented.

The run itself is the canonical `ReplayRunner` (IMP-1201) driving the
`BacktestEngine` consumer (IMP-1202); this module only builds the frozen
`ReplayDataset`, binds every fingerprint into a reproducible
``BacktestRunManifest``, and enforces the in-sample / validation / out-of-sample
separation and the no-refit-after-OOS rule.

Profitability is NEVER inspected here with invented parameters: a run over
fixtures carries ``profitability_inspected = False`` and the evidence class
stays ``PROFITABILITY_NOT_PROVEN`` — a fixture can never mint an OOS/HISTORICAL
pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trading_application.backtest_engine import BACKTEST_VERSION, BacktestCostPolicy, BacktestReport
from trading_application.dataset_manifest import (
    DatasetError,
    DatasetManifest,
    DatasetSplit,
    NoLookaheadAuditor,
)
from trading_application.replay_runner import (
    RUNNER_VERSION,
    ReplayDataset,
    ReplayEvent,
    ReplayResult,
)
from trading_application.strategy_freeze import (
    FreezeStatus,
    StrategyFreezeRecord,
)

READINESS_VERSION = "backtest-readiness-1.0.0"

#: The evidence class a fixture-only run may claim — never more.
PROFITABILITY_NOT_PROVEN = "PROFITABILITY_NOT_PROVEN"


class ReadinessError(Exception):
    """Fail-closed backtest-readiness failure (unfrozen, look-ahead, no costs)."""


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    strategy_id: str
    strategy_version: str
    dataset_id: str
    dataset_fingerprint: str
    ready: bool
    reasons: tuple[str, ...]


def assert_backtest_ready(
    strategy: StrategyFreezeRecord,
    dataset: DatasetManifest,
    cost_policy: BacktestCostPolicy | None,
) -> ReadinessReport:
    """Gate a backtest run. Raises ``ReadinessError`` unless every gate passes."""
    reasons: list[str] = []

    if strategy.status is not FreezeStatus.FROZEN:
        reasons.append(f"strategy is not FROZEN (status={strategy.status.value})")

    # Owner cost/slippage/spread assumptions: needed as frozen parameters AND as
    # a concrete cost policy. A missing assumption refuses the run.
    missing_cost_params = strategy.missing_cost_parameters()
    if missing_cost_params:
        reasons.append(f"missing owner cost parameters: {list(missing_cost_params)}")
    if cost_policy is None:
        reasons.append("no BacktestCostPolicy supplied (owner cost assumptions required)")

    # Dataset validity + no-look-ahead + no split contamination.
    auditor = NoLookaheadAuditor()
    try:
        auditor.assert_no_lookahead(dataset)
        auditor.assert_no_split_contamination(dataset)
    except DatasetError as error:
        reasons.append(str(error))

    report = ReadinessReport(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_id,
        dataset_id=dataset.dataset_id,
        dataset_fingerprint=dataset.fingerprint(),
        ready=not reasons,
        reasons=tuple(reasons),
    )
    if not report.ready:
        raise ReadinessError("backtest not ready: " + "; ".join(reasons))
    return report


def build_replay_dataset(
    strategy: StrategyFreezeRecord,
    dataset: DatasetManifest,
    *,
    split: DatasetSplit,
    cost_policy: BacktestCostPolicy,
    seed: str,
    events: Sequence[ReplayEvent],
) -> ReplayDataset:
    """Build a frozen ReplayDataset bound to this strategy/dataset/split/policy.

    The caller supplies the ``events`` (e.g. bars derived from the dataset plus
    strategy-produced intents) — this module never invents market data or
    intents. It pins every identity into ``versions`` so the run's ``result_hash``
    changes if any input changes, and refuses any event whose ``available_at``
    falls outside the split window (a run may not peek outside its split).
    """
    assert_backtest_ready(strategy, dataset, cost_policy)
    for event in events:
        if not split.contains(event.available_at):
            raise ReadinessError(
                f"event {event.event_id!r} available_at {event.available_at.isoformat()}"
                f" is outside split {split.split_id!r} [{split.start.isoformat()},"
                f" {split.end.isoformat()}); fail closed"
            )
    versions = {
        "strategy": strategy.version_id,
        "strategy_fingerprint": strategy.fingerprint(),
        "dataset_fingerprint": dataset.fingerprint(),
        "dataset_snapshot": dataset.snapshot_identity(),
        "execution-policy": cost_policy.policy_version,
        "cost_policy_fingerprint": cost_policy.fingerprint(),
        "split": split.split_id,
        "backtest-engine": BACKTEST_VERSION,
        "readiness": READINESS_VERSION,
    }
    return ReplayDataset(
        dataset_id=f"{dataset.dataset_id}:{split.split_id}",
        events=tuple(events),
        versions=versions,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Metrics + confidence-interval plumbing (values come from the run/owner)."""

    trades: int
    fills: int
    partial_fills: int
    unfilled: int
    net_pnl: str
    hit_rate: str
    max_drawdown: str
    # Confidence-interval plumbing — method/bounds are supplied, never invented.
    ci_method: str | None = None
    ci_lower: str | None = None
    ci_upper: str | None = None


@dataclass(frozen=True, slots=True)
class RunAttribution:
    """Where outcomes came from — costs, missing data, rejected events."""

    total_costs: str
    ambiguous_bars: tuple[str, ...]
    rejected_events: tuple[str, ...]
    missing_data_notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestRunManifest:
    """Fully-fingerprinted, reproducible record of one backtest run."""

    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    dataset_snapshot: str
    split_id: str
    split_kind: str
    cost_policy_fingerprint: str
    seed: str
    run_id: str
    result_hash: str
    runner_version: str
    backtest_version: str
    metrics: BacktestMetrics
    attribution: RunAttribution
    profitability_inspected: bool
    evidence_class: str
    reproducibility_command: str
    lineage: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": f"{self.strategy_id}@{self.strategy_version}",
            "strategy_fingerprint": self.strategy_fingerprint,
            "dataset": f"{self.dataset_id}@{self.dataset_version}",
            "dataset_fingerprint": self.dataset_fingerprint,
            "dataset_snapshot": self.dataset_snapshot,
            "split": f"{self.split_id}:{self.split_kind}",
            "cost_policy_fingerprint": self.cost_policy_fingerprint,
            "seed": self.seed,
            "run_id": self.run_id,
            "result_hash": self.result_hash,
            "runner_version": self.runner_version,
            "backtest_version": self.backtest_version,
            "metrics": {
                "trades": self.metrics.trades,
                "fills": self.metrics.fills,
                "partial_fills": self.metrics.partial_fills,
                "unfilled": self.metrics.unfilled,
                "net_pnl": self.metrics.net_pnl,
                "hit_rate": self.metrics.hit_rate,
                "max_drawdown": self.metrics.max_drawdown,
                "ci_method": self.metrics.ci_method,
                "ci_lower": self.metrics.ci_lower,
                "ci_upper": self.metrics.ci_upper,
            },
            "attribution": {
                "total_costs": self.attribution.total_costs,
                "ambiguous_bars": list(self.attribution.ambiguous_bars),
                "rejected_events": list(self.attribution.rejected_events),
                "missing_data_notes": list(self.attribution.missing_data_notes),
            },
            "profitability_inspected": self.profitability_inspected,
            "evidence_class": self.evidence_class,
            "reproducibility_command": self.reproducibility_command,
            "lineage": list(self.lineage),
        }


def build_run_manifest(
    *,
    strategy: StrategyFreezeRecord,
    dataset: DatasetManifest,
    split: DatasetSplit,
    cost_policy: BacktestCostPolicy,
    result: ReplayResult,
    report: BacktestReport,
    rejected_events: Sequence[str] = (),
    missing_data_notes: Sequence[str] = (),
    ci_method: str | None = None,
    ci_lower: str | None = None,
    ci_upper: str | None = None,
) -> BacktestRunManifest:
    """Assemble the reproducible run manifest from a completed replay + report.

    ``profitability_inspected`` is hard-set False and the evidence class stays
    PROFITABILITY_NOT_PROVEN: this manifest records that a run happened and how
    to reproduce it — it is NOT a profitability claim.
    """
    metrics = BacktestMetrics(
        trades=report.trades,
        fills=report.fills,
        partial_fills=report.partial_fills,
        unfilled=report.unfilled,
        net_pnl=report.net_pnl,
        hit_rate=report.hit_rate,
        max_drawdown=report.max_drawdown,
        ci_method=ci_method,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
    )
    attribution = RunAttribution(
        total_costs=report.total_costs,
        ambiguous_bars=report.ambiguous_bars,
        rejected_events=tuple(rejected_events),
        missing_data_notes=tuple(missing_data_notes),
    )
    reproduce = (
        "uv run python -m trading_application.replay_runner  # "
        f"strategy={strategy.strategy_id}@{strategy.version_id} "
        f"dataset={dataset.dataset_id}@{dataset.dataset_version} "
        f"split={split.split_id} seed={result.manifest.seed}"
    )
    return BacktestRunManifest(
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_id,
        strategy_fingerprint=strategy.fingerprint(),
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_fingerprint=dataset.fingerprint(),
        dataset_snapshot=dataset.snapshot_identity(),
        split_id=split.split_id,
        split_kind=split.kind.value,
        cost_policy_fingerprint=cost_policy.fingerprint(),
        seed=result.manifest.seed,
        run_id=result.manifest.run_id,
        result_hash=result.result_hash,
        runner_version=RUNNER_VERSION,
        backtest_version=BACKTEST_VERSION,
        metrics=metrics,
        attribution=attribution,
        profitability_inspected=False,
        evidence_class=PROFITABILITY_NOT_PROVEN,
        reproducibility_command=reproduce,
        lineage=(
            f"strategy:{strategy.fingerprint()}",
            f"dataset:{dataset.fingerprint()}",
            f"cost_policy:{cost_policy.fingerprint()}",
            f"run:{result.manifest.run_id}",
        ),
    )


def assert_oos_untouched(
    before: StrategyFreezeRecord, after: StrategyFreezeRecord
) -> None:
    """The no-refit rule: a strategy may not change after its OOS was inspected.

    Any material change (fingerprint) after an out-of-sample run requires a NEW
    version identity, not an in-place edit of the same version.
    """
    if before.version_id != after.version_id:
        return  # a new version identity is the legitimate path
    if before.fingerprint() != after.fingerprint():
        raise ReadinessError(
            f"strategy {before.strategy_id}@{before.version_id} changed after OOS"
            " inspection without a new version identity; that is refitting — fail closed"
        )


def assert_split_separation(splits: Sequence[DatasetSplit]) -> None:
    """Fail closed if any two evaluation splits overlap in time."""
    ordered = sorted(splits, key=lambda split: split.start)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start < earlier.end:
            raise ReadinessError(
                f"splits {earlier.split_id!r} and {later.split_id!r} overlap;"
                " in-sample / validation / OOS separation is violated"
            )


__all__ = [
    "PROFITABILITY_NOT_PROVEN",
    "READINESS_VERSION",
    "BacktestMetrics",
    "BacktestRunManifest",
    "ReadinessError",
    "ReadinessReport",
    "RunAttribution",
    "assert_backtest_ready",
    "assert_oos_untouched",
    "assert_split_separation",
    "build_replay_dataset",
    "build_run_manifest",
]
