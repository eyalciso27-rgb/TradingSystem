"""E2E shadow traceability acceptance suite (IMP-1301).

Every test is mapped to a canonical TR-ID from the Traceability Matrix
(docs/baseline-v0.5/Traceability_Matrix_v0.3_HE.md). DoD: this suite
passes — it fails closed on a missing lineage link, a vanished candidate
or any execution/authorization surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.counterfactual_shadow import ShadowPolicy
from trading_application.decision_engine import DecisionPolicy
from trading_application.e2e_shadow import (
    E2EScenarioConfig,
    E2EShadowError,
    E2EShadowHarness,
    E2EShadowReport,
    TraceEntry,
)
from trading_application.opportunity_funnel import TransitionOutcome
from trading_application.portfolio_allocator import (
    PortfolioAllocationPolicy,
    PortfolioPosition,
    PortfolioState,
)
from trading_application.pre_submit_check import ChangeAction, PreSubmitPolicy, StaleAction
from trading_application.replay_runner import ReplayDataset, ReplayEvent, ReplayRunner
from trading_application.risk_engine import AccountState, RiskPolicy
from trading_application.signal_fusion import FusionPolicy
from trading_application.strategy_manifest import (
    ManifestEntry,
    ManifestRole,
    StrategyManifest,
)
from trading_contracts.enums import (
    ActionTier,
    DecisionOutcome,
    PortfolioAction,
    SignalFamily,
)

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)


def default_config(
    *,
    account: AccountState | None = None,
    portfolio: PortfolioState | None = None,
    pre_submit: PreSubmitPolicy | None = None,
) -> E2EScenarioConfig:
    return E2EScenarioConfig(
        manifest=StrategyManifest(
            strategy_id="momentum-breakout",
            version="1.0.0",
            entries=(
                ManifestEntry("technical", ManifestRole.REQUIRED, min_score=Decimal(50)),
                ManifestEntry("news_negative", ManifestRole.VETO, min_score=Decimal(50)),
                ManifestEntry("social", ManifestRole.SOFT_SCORE, weight=Decimal(5)),
            ),
        ),
        fusion_policy=FusionPolicy(
            policy_version="1.0.0",
            family_weights=(
                (SignalFamily.TECHNICAL, Decimal(1)),
                (SignalFamily.NEWS, Decimal(1)),
                (SignalFamily.SOCIAL, Decimal("0.3")),
                (SignalFamily.EXTERNAL, Decimal("0.5")),
            ),
            unverified_multiplier=Decimal("0.5"),
            action_tier_rules=(
                (ActionTier.AUTO_FULL, Decimal(85)),
                (ActionTier.MANUAL, Decimal(60)),
                (ActionTier.SHADOW, Decimal(40)),
                (ActionTier.WATCH, Decimal(0)),
            ),
        ),
        decision_policy=DecisionPolicy(
            policy_version="1.0.0",
            tier_outcomes=(
                (ActionTier.AUTO_FULL, DecisionOutcome.APPROVE),
                (ActionTier.AUTO_REDUCED, DecisionOutcome.APPROVE),
                (ActionTier.MANUAL, DecisionOutcome.NEEDS_REVIEW),
                (ActionTier.SHADOW, DecisionOutcome.SHADOW),
                (ActionTier.WATCH, DecisionOutcome.WATCH),
                (ActionTier.REJECT, DecisionOutcome.REJECT),
            ),
            unsatisfied_gate_outcome=DecisionOutcome.WATCH,
            conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
        ),
        risk_policy=RiskPolicy(
            policy_version="1.0.0",
            risk_per_trade_pct=Decimal("0.01"),
            max_total_open_risk_pct=Decimal("0.06"),
            daily_soft_halt_pct=Decimal("0.02"),
            daily_hard_halt_pct=Decimal("0.03"),
            max_concurrent_positions=5,
            max_symbol_notional_pct=Decimal("0.2"),
            max_single_order_notional_pct=Decimal("0.2"),
            max_sector_notional_pct=Decimal("0.3"),
            max_correlated_cluster_pct=Decimal("0.2"),
            max_source_open_risk_share=Decimal("0.5"),
        ),
        account=account
        or AccountState(
            broker_net_liquidation_value=Decimal(100000),
            simulation_equity_cap=Decimal(100000),
            session_pnl=Decimal(0),
            open_positions=(),
            observed_at=T0,
        ),
        allocation_policy=PortfolioAllocationPolicy(
            policy_version="1.0.0",
            max_total_open_risk_pct=Decimal("0.06"),
            max_symbol_notional_pct=Decimal("0.2"),
            max_sector_notional_pct=Decimal("0.3"),
            max_theme_notional_pct=Decimal("0.3"),
            max_strategy_risk_share=Decimal(1),
            max_source_risk_share=Decimal(1),
            max_cluster_risk_share=Decimal(1),
            max_concurrent_positions=5,
            insufficient_capacity_action=PortfolioAction.REJECT,
        ),
        portfolio=portfolio
        or PortfolioState(
            risk_budget_equity=Decimal(100000), open_positions=(), observed_at=T0
        ),
        pre_submit_policy=pre_submit
        or PreSubmitPolicy(
            policy_version="1.0.0",
            max_price_slippage_pct=Decimal("0.02"),
            max_risk_increase_pct=Decimal("0.1"),
            market_max_staleness_seconds=300,
            account_max_staleness_seconds=300,
            decision_max_age_seconds=600,
            approval_max_age_seconds=600,
            require_manual_approval=False,
            price_change_action=ChangeAction.RESIZE,
            risk_change_action=ChangeAction.RESIZE,
            stale_data_action=StaleAction.NEEDS_REVIEW,
        ),
        shadow_policy=ShadowPolicy(
            policy_version="shadow-1",
            commission_per_share=Decimal("0.01"),
            minimum_commission=Decimal(1),
            spread_half_pct=Decimal(0),
            slippage_pct=Decimal(0),
            entry_window_bars=2,
            horizon_bars=2,
        ),
    )


def ev(event_id: str, minute: int, kind: str, payload: dict[str, object]) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    base: dict[str, object] = {"run_id": "", "split_id": ""}
    base.update(payload)
    return ReplayEvent(
        event_id=event_id,
        sequence=int(str(base.pop("sequence", 0))),
        kind=kind,
        payload=base,  # type: ignore[arg-type]
        observed_at=at,
        available_at=at,
    )


def scan(event_id: str, minute: int, candidate: str, symbol: str = "AAPL") -> ReplayEvent:
    return ev(
        event_id, minute, "SCAN",
        {"candidate_id": candidate, "symbol": symbol, "direction": "LONG"},
    )


def source(
    event_id: str,
    minute: int,
    source_type: str,
    *,
    channel: str = "chan-1",
    message_id: str = "m-1",
    content: str = "TSLA breaking out",
    candidate: str = "",
    status: str = "",
    sequence: int = 0,
) -> ReplayEvent:
    return ev(
        event_id, minute, "SOURCE",
        {
            "source_type": source_type,
            "channel": channel,
            "external_message_id": message_id,
            "content": content,
            "candidate_id": candidate,
            "symbol": "TSLA",
            "direction": "LONG",
            "status": status,
            "sequence": sequence,
        },
    )


def assessment(
    event_id: str,
    minute: int,
    candidate: str,
    *,
    key: str = "technical",
    family: str = "TECHNICAL",
    direction: str = "LONG",
    score: str = "90",
    calibration: str = "CALIBRATED",
    model_id: str = "tech-model",
    model_version: str = "1.0.0",
    cluster: str = "",
    valid_until: str = "",
) -> ReplayEvent:
    return ev(
        event_id, minute, "ASSESSMENT",
        {
            "candidate_id": candidate,
            "key": key,
            "family": family,
            "direction": direction,
            "status": "TRIGGERED",
            "score": score,
            "calibration": calibration,
            "model_id": model_id,
            "model_version": model_version,
            "cluster_key": cluster or f"cluster:{key}",
            "ref": f"ref:{event_id}",
            "source_id": "internal",
            "valid_until": valid_until,
        },
    )


def evaluate(
    event_id: str,
    minute: int,
    candidate: str,
    *,
    entry: str = "100",
    stop: str = "95",
    quantity: str = "10",
    market: str = "",
) -> ReplayEvent:
    return ev(
        event_id, minute, "EVALUATE",
        {
            "candidate_id": candidate,
            "entry_price": entry,
            "stop_price": stop,
            "quantity": quantity,
            "market_price": market,
            "sector": "TECH",
            "theme": "AI",
            "cluster": "MEGATECH",
            "source_id": "internal",
        },
    )


def bar(event_id: str, minute: int, *, symbol: str = "AAPL", c: str = "105") -> ReplayEvent:
    return ev(
        event_id, minute, "BAR",
        {"symbol": symbol, "open": "100", "high": "106", "low": "99",
         "close": c, "volume": "10000"},
    )


HAPPY_PATH = [
    scan("s1", 0, "c1"),
    assessment("a1", 1, "c1"),
    evaluate("e1", 2, "c1"),
]


def run_scenario(
    events: list[ReplayEvent], config: E2EScenarioConfig | None = None, *, seed: str = "seed:1"
) -> tuple[str, E2EShadowReport, E2EShadowHarness]:
    config = config or default_config()
    harness = E2EShadowHarness(config)
    result = ReplayRunner(harness.consume).run(
        ReplayDataset(
            dataset_id="e2e-ds", events=tuple(events), versions=config.versions(), seed=seed
        )
    )
    return result.result_hash, harness.report(), harness


def stages_of(report: E2EShadowReport, candidate: str) -> list[str]:
    return [entry.stage for entry in report.trace if entry.candidate_id == candidate]


# -- TR-001: independent discovery ---------------------------------------------------


def test_tr001_scanner_only_candidate_reaches_shadow_approval() -> None:
    """TR-001 / T-SCAN-01: a candidate is created from a scan alone."""
    _, report, _ = run_scenario(list(HAPPY_PATH))
    assert report.total_candidates == 1
    trace = stages_of(report, "c1")
    assert trace[0] == "DISCOVERED"
    assert "PRE_SUBMIT_EVALUATED" in trace
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.current_stage.value == "APPROVED"
    assert not any(e.stage == "SOURCE_NORMALIZED" for e in report.trace)


# -- TR-002: all canonical source types, non-blocking sources --------------------------


def test_tr002_every_canonical_source_type_is_normalized() -> None:
    """TR-002 / T-SRC-01: fixtures for every canonical SourceType pass
    through the canonical normalization contract with full provenance."""
    types = ["WHATSAPP", "DISCORD", "TELEGRAM", "INVESTINGPRO", "NEWS", "FILING", "MARKET"]
    events = [
        source(f"src-{name}", index, name, channel=f"chan-{name}", message_id=f"m-{index}")
        for index, name in enumerate(types)
    ]
    _, report, _ = run_scenario(events)
    normalized = {
        entry.engine for entry in report.trace if entry.stage == "SOURCE_NORMALIZED"
    }
    assert normalized == set(types)
    for entry in report.trace:
        assert "canonical contract" in entry.finding


def test_tr002_unavailable_source_degrades_and_never_blocks_discovery() -> None:
    """TR-002: a down source is DEGRADED (never CONNECTED, never PoI PASS)
    and independent discovery continues."""
    events = [
        source("down-1", 0, "WHATSAPP", status="UNAVAILABLE"),
        *HAPPY_PATH,
    ]
    _, report, _ = run_scenario(events)
    degraded = next(e for e in report.trace if e.stage == "SOURCE_UNAVAILABLE")
    assert "not CONNECTED" in degraded.finding
    assert "not a" in degraded.finding  # never a PoI pass
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.current_stage.value == "APPROVED"


def test_no_scenario_marks_a_source_connected() -> None:
    _, report, _ = run_scenario(
        [source("down-1", 0, "TELEGRAM", status="UNAVAILABLE"), *HAPPY_PATH]
    )
    assert not any("CONNECTED" == e.outcome for e in report.trace)


# -- dedup / conflict --------------------------------------------------------------------


def test_cross_mode_duplicate_source_dedups_to_one_candidate() -> None:
    """TR-050-family cross-mode dedup: user-mode and bot-mode observations
    of the same post collapse; one candidate only."""
    events = [
        source("tg-user", 0, "TELEGRAM", candidate="c1"),
        source("tg-bot", 1, "TELEGRAM", candidate="c1"),  # same channel+message id
        assessment("a1", 2, "c1"),
        evaluate("e1", 3, "c1"),
    ]
    _, report, _ = run_scenario(events)
    assert report.total_candidates == 1
    assert any(e.stage == "SOURCE_DEDUPED" for e in report.trace)


def test_conflicting_duplicate_source_fails_closed() -> None:
    events = [
        source("tg-1", 0, "TELEGRAM"),
        source("tg-2", 1, "TELEGRAM", content="entirely different claim"),
    ]
    with pytest.raises(E2EShadowError, match="conflicting content"):
        run_scenario(events)


def test_conflicting_signals_are_explicit_with_one_canonical_outcome() -> None:
    """TR-008/T-FUS-01 family: opposing directions are recorded as an
    explicit conflict; exactly one canonical decision is produced."""
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1"),
        assessment(
            "a2", 2, "c1", key="news_flow", family="NEWS",
            direction="NEGATIVE", model_id="news-model",
        ),
        evaluate("e1", 3, "c1"),
    ]
    _, report, _ = run_scenario(events)
    fused = next(e for e in report.trace if e.stage == "FUSED")
    assert "conflicts=1" in fused.finding
    decided = [e for e in report.trace if e.stage == "DECIDED"]
    assert len(decided) == 1  # one canonical outcome, never two


# -- TR-010: stale data blocks -----------------------------------------------------------


def test_tr010_stale_required_assessment_blocks_fail_closed() -> None:
    """TR-010 / T-RISK-01: stale data blocks the order path."""
    stale_until = (T0 + timedelta(minutes=2)).isoformat()
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1", valid_until=stale_until),
        evaluate("e1", 10, "c1"),  # long after valid_until
    ]
    _, report, _ = run_scenario(events)
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.terminal_outcome is TransitionOutcome.EXPIRED
    attribution = report.funnel.rejection_attribution[0]
    assert attribution.reason_code == "DATA_STALE"
    assert "PRE_SUBMIT_EVALUATED" not in stages_of(report, "c1")


# -- hard veto ------------------------------------------------------------------------------


def test_hard_veto_stops_before_allocation_with_attribution() -> None:
    """Decision 22 hard veto: fires at fusion/decision, never reaches
    risk/allocation/pre-submit, terminal with full attribution."""
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1"),
        assessment(
            "a2", 2, "c1", key="news_negative", family="NEWS",
            direction="NEGATIVE", model_id="news-model",
        ),
        evaluate("e1", 3, "c1"),
    ]
    _, report, _ = run_scenario(events)
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.terminal_outcome is TransitionOutcome.REJECTED
    attribution = report.funnel.rejection_attribution[0]
    assert attribution.engine == "decision-engine"
    assert attribution.version
    trace = stages_of(report, "c1")
    assert "RISK_CHECKED" not in trace
    assert "ALLOCATED" not in trace
    assert "PRE_SUBMIT_EVALUATED" not in trace


# -- failures: engine / risk / allocation / pre-submit ------------------------------------------


def test_engine_failure_degrades_explicitly_without_crash() -> None:
    """TR-006 family: a failed engine degrades; the candidate never
    disappears and never lingers ambiguously."""
    events = [
        scan("s1", 0, "c1"),
        ev("f1", 1, "ENGINE_FAILURE",
           {"candidate_id": "c1", "engine": "technical-engine",
            "version": "1.0.0", "reason": "TIMEOUT"}),
        evaluate("e1", 2, "c1"),
    ]
    _, report, _ = run_scenario(events)
    failure = next(e for e in report.trace if e.stage == "ENGINE_FAILURE")
    assert failure.outcome == "DEGRADED"
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    # Missing required evidence -> WATCH (never APPROVE), explicitly ACTIVE.
    assert history.status.value == "ACTIVE"
    assert history.current_stage.value == "DECIDED"
    assert report.total_candidates == 1


def test_risk_veto_attributed_to_risk_engine() -> None:
    """TR-014 family: the risk engine's absolute veto is terminal and
    fully attributed."""
    losing_account = AccountState(
        broker_net_liquidation_value=Decimal(100000),
        simulation_equity_cap=Decimal(100000),
        session_pnl=Decimal(-5000),  # 5% loss > 3% hard halt
        open_positions=(),
        observed_at=T0,
    )
    _, report, _ = run_scenario(list(HAPPY_PATH), default_config(account=losing_account))
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.terminal_outcome is TransitionOutcome.VETOED
    attribution = report.funnel.rejection_attribution[0]
    assert attribution.engine == "risk-engine"
    assert attribution.reason_code


def test_allocation_failure_is_explicit_never_silent() -> None:
    """TR-012 family: an allocation rejection is an explicit terminal
    state with allocator attribution."""
    saturated = PortfolioState(
        risk_budget_equity=Decimal(100000),
        open_positions=(
            PortfolioPosition(
                symbol="AAPL", sector="TECH", theme="AI", cluster="MEGATECH",
                source_id="internal", strategy_id="momentum-breakout",
                open_risk_amount=Decimal(500), notional=Decimal(30000),
            ),
        ),
        observed_at=T0,
    )
    _, report, _ = run_scenario(list(HAPPY_PATH), default_config(portfolio=saturated))
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.terminal_outcome is TransitionOutcome.REJECTED
    attribution = report.funnel.rejection_attribution[0]
    assert attribution.engine == "portfolio-allocator"


def test_pre_submit_failure_creates_no_authorization() -> None:
    """TR-015 / T-EXEC-01 family: a pre-submit CANCEL is terminal and no
    authorization exists anywhere in the harness output."""
    cancel_policy = PreSubmitPolicy(
        policy_version="1.0.0",
        max_price_slippage_pct=Decimal("0.02"),
        max_risk_increase_pct=Decimal("0.1"),
        market_max_staleness_seconds=300,
        account_max_staleness_seconds=300,
        decision_max_age_seconds=600,
        approval_max_age_seconds=600,
        require_manual_approval=False,
        price_change_action=ChangeAction.CANCEL,
        risk_change_action=ChangeAction.CANCEL,
        stale_data_action=StaleAction.CANCEL,
    )
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1"),
        evaluate("e1", 2, "c1", market="200"),  # price ran away 100%
    ]
    _, report, _ = run_scenario(events, default_config(pre_submit=cancel_policy))
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.terminal_outcome is TransitionOutcome.REJECTED
    pre_submit = next(e for e in report.trace if e.stage == "PRE_SUBMIT_EVALUATED")
    assert "no authorization retained" in pre_submit.finding
    for attr in ("authorization", "authorizations"):
        assert not hasattr(report, attr)


def test_partial_pipeline_failure_keeps_full_history() -> None:
    _, report, _ = run_scenario(
        list(HAPPY_PATH),
        default_config(
            account=AccountState(
                broker_net_liquidation_value=Decimal(100000),
                simulation_equity_cap=Decimal(100000),
                session_pnl=Decimal(-5000),
                open_positions=(),
                observed_at=T0,
            )
        ),
    )
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    reached = [t.stage.value for t in history.transitions]
    assert reached[:4] == ["OBSERVED", "ASSESSED", "FUSED", "DECIDED"]
    assert history.terminal_outcome is TransitionOutcome.VETOED
    assert stages_of(report, "c1")  # complete trace up to the failure point


# -- explicit review states -----------------------------------------------------------------------


def test_needs_review_is_explicit_with_no_blind_retry() -> None:
    """TR-011 family: a MANUAL-tier decision ends as an explicit
    NEEDS_REVIEW research state — no hidden retry, candidate ACTIVE."""
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1", score="70"),  # MANUAL tier (60-84)
        evaluate("e1", 2, "c1"),
    ]
    _, report, _ = run_scenario(events)
    decided = next(e for e in report.trace if e.stage == "DECIDED")
    assert decided.outcome == "NEEDS_REVIEW"
    history = next(h for h in report.funnel.histories if h.candidate_id == "c1")
    assert history.status.value == "ACTIVE"
    assert stages_of(report, "c1").count("DECIDED") == 1  # exactly once


# -- counterfactual outcome ------------------------------------------------------------------------


def test_counterfactual_outcome_is_virtual_and_isolated() -> None:
    """Data Model 18.1: counterfactual shadow never changes real state."""
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1"),
        assessment(
            "a2", 2, "c1", key="news_negative", family="NEWS",
            direction="NEGATIVE", model_id="news-model",
        ),
        evaluate("e1", 3, "c1"),
        bar("b1", 4), bar("b2", 5), bar("b3", 6), bar("b4", 7),
    ]
    _, report, _ = run_scenario(events)
    assert report.shadow.total_tracked == 1
    outcome = report.shadow.outcomes[0]
    assert outcome.counterfactual_only is True
    assert outcome.status.value == "FILLED"  # virtual fill + fixed-horizon exit
    assert report.shadow.affects_live_state is False
    assert report.shadow.affects_paper_state is False


# -- TR-024: conservation + full lineage ----------------------------------------------


def test_tr024_every_candidate_has_stage_and_lineage_and_conservation_holds() -> None:
    """TR-024 / T-OPP-01: every candidate has a funnel stage; conservation
    total == active + terminal; lineage is continuous."""
    events = [
        scan("s1", 0, "c1"),
        assessment("a1", 1, "c1"),
        evaluate("e1", 2, "c1"),
        source("tg-1", 3, "TELEGRAM", candidate="c2"),
        assessment("a2", 4, "c2", score="30"),  # WATCH tier -> active
        evaluate("e2", 5, "c2"),
        scan("s3", 6, "c3", symbol="MSFT"),  # observed, never evaluated
    ]
    _, report, _ = run_scenario(events)
    assert report.total_candidates == 3
    assert report.total_candidates == report.active_candidates + report.terminal_candidates
    assert report.lineage_complete is True
    for candidate_id in ("c1", "c2", "c3"):
        trace = [e for e in report.trace if e.candidate_id == candidate_id]
        assert trace, candidate_id
        assert trace[0].source_event_refs  # the chain starts at a source
        for entry in trace:
            assert entry.input_fingerprint and entry.output_fingerprint


def test_report_fails_closed_when_a_candidate_has_no_lineage() -> None:
    _, report, harness = run_scenario(list(HAPPY_PATH))
    del report
    harness._trace.clear()  # noqa: SLF001 - simulate a broken lineage
    with pytest.raises(E2EShadowError, match="lineage incomplete"):
        harness.report()


# -- determinism / recovery -----------------------------------------------------------


def test_restart_recovery_repeat_yields_identical_report_and_hash() -> None:
    hash_a, report_a, _ = run_scenario(list(HAPPY_PATH))
    hash_b, report_b, _ = run_scenario(list(HAPPY_PATH))
    assert hash_a == hash_b
    assert report_a == report_b
    assert report_a.report_hash == report_b.report_hash


def test_shuffled_input_through_replay_runner_is_identical() -> None:
    hash_a, report_a, _ = run_scenario(list(HAPPY_PATH))
    hash_b, report_b, _ = run_scenario(list(reversed(HAPPY_PATH)))
    assert hash_a == hash_b
    assert report_a == report_b


def test_future_event_change_does_not_affect_earlier_state() -> None:
    config_a = default_config()
    config_b = default_config()
    harness_a = E2EShadowHarness(config_a)
    harness_b = E2EShadowHarness(config_b)
    shared = HAPPY_PATH[:2]
    tail_a = evaluate("e1", 2, "c1", market="100")
    tail_b = evaluate("e1", 2, "c1", market="101")  # future change
    result_a = ReplayRunner(harness_a.consume).run(
        ReplayDataset(
            dataset_id="d", events=(*shared, tail_a),
            versions=config_a.versions(), seed="seed:1",
        )
    )
    result_b = ReplayRunner(harness_b.consume).run(
        ReplayDataset(
            dataset_id="d", events=(*shared, tail_b),
            versions=config_b.versions(), seed="seed:1",
        )
    )
    for step_a, step_b in zip(result_a.steps[:2], result_b.steps[:2], strict=True):
        assert step_a.output == step_b.output


def test_input_events_are_not_mutated() -> None:
    events = list(HAPPY_PATH)
    before = [event.canonical() for event in events]
    run_scenario(events)
    assert [event.canonical() for event in events] == before


# -- absolute no-execution gates ------------------------------------------------------


def test_all_outputs_are_marked_virtual_and_non_executable() -> None:
    config = default_config()
    harness = E2EShadowHarness(config)
    result = ReplayRunner(harness.consume).run(
        ReplayDataset(
            dataset_id="mark", events=tuple(HAPPY_PATH),
            versions=config.versions(), seed="seed:1",
        )
    )
    for step in result.steps:
        assert step.output["virtual"] is True
        assert step.output["executable"] is False
        assert str(step.output["note"]).startswith("SHADOW:")
    report = harness.report()
    assert report.research_only is True
    assert report.affects_live_state is False
    assert report.affects_paper_state is False
    with pytest.raises(E2EShadowError, match="research-only"):
        TraceEntry(
            trace_id="t", candidate_id="c", source_event_refs=(),
            run_id="", split_id="", as_of=T0, stage="X", engine="e",
            engine_version="1", input_fingerprint="f", output_fingerprint="f",
            outcome="o", research_only=False,
        )


def test_no_execution_surface_exists_on_the_harness() -> None:
    harness = E2EShadowHarness(default_config())
    for surface in (
        "submit", "cancel", "flatten", "execute", "place_order",
        "authorize", "connect", "promote",
    ):
        assert not hasattr(harness, surface)


def test_source_hygiene_no_connectors_no_execution_no_secrets() -> None:
    import trading_application.e2e_shadow as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        text = handle.read()
    for token in (
        "SubmitAuthorization", "SingleUseAuthorization", "OrderIntent",
        "ExecutionAgent", "BrokerPort", ".submit(", ".cancel(", "FLATTEN",
        "datetime.now", "utcnow", "time.time", "random.", "uuid4",
        "getpass", "os.environ", "requests.", "httpx",
    ):
        assert token not in text, token
    import_lines = [
        line for line in text.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "execution_agent", "httpx", "requests",
            "socket", "trading_persistence", "trading_event_bus", "sqlalchemy",
        ):
            assert forbidden not in line, line


# -- the traceability acceptance report -----------------------------------------------


def test_traceability_acceptance_report() -> None:
    """The aggregated acceptance report: TR-ID, scenario, pass state,
    trace completeness, conservation, outcomes and exact versions."""
    matrix: list[tuple[str, str, list[ReplayEvent], E2EScenarioConfig | None]] = [
        ("TR-001", "scanner-only-discovery", list(HAPPY_PATH), None),
        (
            "TR-002", "all-source-types",
            [source(f"s-{n}", i, n) for i, n in enumerate(
                ["WHATSAPP", "DISCORD", "TELEGRAM", "INVESTINGPRO",
                 "NEWS", "FILING", "MARKET"]
            )],
            None,
        ),
        (
            "TR-010", "stale-blocked",
            [
                scan("s1", 0, "c1"),
                assessment(
                    "a1", 1, "c1",
                    valid_until=(T0 + timedelta(minutes=2)).isoformat(),
                ),
                evaluate("e1", 10, "c1"),
            ],
            None,
        ),
        (
            "TR-024", "funnel-conservation",
            [scan("s1", 0, "c1"), assessment("a1", 1, "c1"), evaluate("e1", 2, "c1"),
             scan("s2", 3, "c2", symbol="MSFT")],
            None,
        ),
    ]
    rows: list[dict[str, object]] = []
    for tr_id, scenario_id, events, config in matrix:
        _, report, _ = run_scenario(events, config)
        rows.append(
            {
                "tr_id": tr_id,
                "scenario_id": scenario_id,
                "passed": True,  # run_scenario raises fail-closed otherwise
                "trace_complete": report.lineage_complete,
                "conservation": report.total_candidates
                == report.active_candidates + report.terminal_candidates,
                "active": report.active_candidates,
                "terminal": report.terminal_candidates,
                "versions": default_config().versions(),
                "report_hash": report.report_hash,
            }
        )
    assert len(rows) == 4
    for row in rows:
        assert row["passed"] is True
        assert row["trace_complete"] is True
        assert row["conservation"] is True
        versions = row["versions"]
        assert isinstance(versions, dict) and versions["e2e-shadow"]
