"""Counterfactual-shadow drills (IMP-1204). DoD: no counterfactual event
affects live/paper state — everything is virtual, marked, and conserved."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.counterfactual_shadow import (
    CounterfactualShadowTracker,
    ShadowCandidate,
    ShadowError,
    ShadowOrigin,
    ShadowPolicy,
    ShadowReport,
    VirtualFill,
    VirtualOutcome,
    VirtualOutcomeStatus,
)
from trading_application.replay_runner import ReplayDataset, ReplayEvent, ReplayRunner

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)


def policy(**overrides: object) -> ShadowPolicy:
    values: dict[str, object] = {
        "policy_version": "shadow-policy-1",
        "commission_per_share": Decimal("0.01"),
        "minimum_commission": Decimal("1"),
        "spread_half_pct": Decimal("0"),
        "slippage_pct": Decimal("0"),
        "entry_window_bars": 2,
        "horizon_bars": 2,
    }
    values.update(overrides)
    return ShadowPolicy(**values)  # type: ignore[arg-type]


def candidate_event(
    event_id: str,
    minute: int,
    candidate_id: str,
    *,
    origin: str = "REJECTED",
    symbol: str = "AAPL",
    direction: str = "BUY",
    quantity: str = "10",
    reference: str = "100",
    limit: str = "",
    reason: str = "SCORE_BELOW_THRESHOLD",
    engine: str = "fusion-engine",
    version: str = "1.2.0",
    sequence: int = 0,
) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    return ReplayEvent(
        event_id=event_id,
        sequence=sequence,
        kind="SHADOW_CANDIDATE",
        payload={
            "candidate_id": candidate_id,
            "origin": origin,
            "reason_code": reason,
            "engine": engine,
            "version": version,
            "symbol": symbol,
            "direction": direction,
            "virtual_quantity": quantity,
            "reference_price": reference,
            "virtual_limit_price": limit,
            "run_id": "",
            "split_id": "",
        },
        observed_at=at,
        available_at=at,
    )


def bar_event(
    event_id: str,
    minute: int,
    *,
    symbol: str = "AAPL",
    o: str = "100",
    h: str = "101",
    lo: str = "99",
    c: str = "100",
    v: str = "10000",
    sequence: int = 1,
) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    return ReplayEvent(
        event_id=event_id,
        sequence=sequence,
        kind="BAR",
        payload={"symbol": symbol, "open": o, "high": h, "low": lo, "close": c, "volume": v},
        observed_at=at,
        available_at=at,
    )


def run(
    events: list[ReplayEvent],
    *,
    shadow_policy: ShadowPolicy | None = None,
    seed: str = "seed:1",
    versions: dict[str, str] | None = None,
) -> tuple[str, ShadowReport, CounterfactualShadowTracker]:
    tracker = CounterfactualShadowTracker(shadow_policy or policy())
    runner = ReplayRunner(tracker.consume)
    result = runner.run(
        ReplayDataset(
            dataset_id="shadow-ds",
            events=tuple(events),
            versions=versions or {"shadow": "1.0.0"},
            seed=seed,
        )
    )
    return result.result_hash, tracker.report(), tracker


BASIC_EVENTS = [
    candidate_event("ev-c1", 0, "c1"),
    bar_event("ev-b1", 1, o="100", c="102"),
    bar_event("ev-b2", 2, o="102", c="103"),
    bar_event("ev-b3", 3, o="103", c="105"),
]


# -- population inclusion (scope) ------------------------------------------------------


def test_rejected_candidate_is_included() -> None:
    _, report, _ = run(list(BASIC_EVENTS))
    assert report.total_tracked == 1
    outcome = report.outcomes[0]
    assert outcome.origin is ShadowOrigin.REJECTED
    assert outcome.status is VirtualOutcomeStatus.FILLED


def test_watch_candidate_is_included() -> None:
    events = [candidate_event("ev-c1", 0, "w1", origin="WATCH"), *BASIC_EVENTS[1:]]
    _, report, _ = run(events)
    assert report.outcomes[0].origin is ShadowOrigin.WATCH


def test_manual_candidate_is_classified_only_never_a_real_action() -> None:
    events = [candidate_event("ev-c1", 0, "m1", origin="MANUAL"), *BASIC_EVENTS[1:]]
    _, report, tracker = run(events)
    assert report.outcomes[0].origin is ShadowOrigin.MANUAL
    assert report.outcomes[0].counterfactual_only is True
    # No order-like surface exists to trigger anything real.
    for name in ("submit", "cancel", "flatten", "execute", "place_order"):
        assert not hasattr(tracker, name)


def test_no_candidate_disappears() -> None:
    events = [
        candidate_event("ev-c1", 0, "c-filled"),
        candidate_event("ev-c2", 0, "c-unfilled", limit="90", sequence=1),
        candidate_event("ev-c3", 0, "c-nobars", symbol="MSFT", sequence=2),
        bar_event("ev-b1", 1, o="100", c="102", sequence=3),
        bar_event("ev-b2", 2, o="102", c="103", sequence=4),
        bar_event("ev-b3", 3, o="103", c="105", sequence=5),
    ]
    _, report, _ = run(events)
    assert report.total_tracked == 3
    by_id = {outcome.candidate_id: outcome.status for outcome in report.outcomes}
    assert by_id == {
        "c-filled": VirtualOutcomeStatus.FILLED,
        "c-unfilled": VirtualOutcomeStatus.UNFILLED,
        "c-nobars": VirtualOutcomeStatus.EXPIRED,
    }


def test_conservation_holds_and_is_enforced() -> None:
    _, report, _ = run(list(BASIC_EVENTS))
    accounted = (
        report.filled + report.partial + report.unfilled + report.expired + report.incomplete
    )
    assert report.total_tracked == accounted == len(report.outcomes)
    with pytest.raises(ShadowError, match="conservation"):
        ShadowReport(
            policy_ref="p",
            total_tracked=2,
            filled=1,
            partial=0,
            unfilled=0,
            expired=0,
            incomplete=0,
            outcomes=report.outcomes,
            groups=(),
            findings=(),
        )


# -- point-in-time / no lookahead -----------------------------------------------------


def test_entry_only_from_bar_strictly_after_decision() -> None:
    # A bar at the SAME timestamp as the decision must not fill; the entry
    # comes from the NEXT bar at that bar's own open.
    events = [
        candidate_event("ev-c1", 0, "c1", sequence=0),
        bar_event("ev-b0", 0, o="95", c="96", sequence=1),  # same as_of: ignored
        bar_event("ev-b1", 1, o="100", c="102"),
        bar_event("ev-b2", 2, o="102", c="103"),
        bar_event("ev-b3", 3, o="103", c="105"),
    ]
    _, report, _ = run(events)
    outcome = report.outcomes[0]
    assert outcome.virtual_entry == "100.0000"  # bar1 open, never bar0's 95


def test_no_same_bar_retroactive_fill() -> None:
    events = [
        candidate_event("ev-c1", 0, "c1"),
        bar_event("ev-b0", 0, o="90", c="91", sequence=1),
    ]
    _, report, _ = run(events)
    # The only bar is not strictly later -> no fill at all.
    assert report.outcomes[0].status is VirtualOutcomeStatus.EXPIRED
    assert report.outcomes[0].virtual_entry is None


def test_changing_a_future_bar_leaves_earlier_steps_identical() -> None:
    tracker_a = CounterfactualShadowTracker(policy())
    tracker_b = CounterfactualShadowTracker(policy())
    tail_a = bar_event("ev-b3", 3, o="103", c="105")
    tail_b = bar_event("ev-b3", 3, o="103", c="50")  # future bar changed
    shared = tuple(BASIC_EVENTS[:3])
    versions = {"shadow": "1.0.0"}
    result_a = ReplayRunner(tracker_a.consume).run(
        ReplayDataset(dataset_id="a", events=(*shared, tail_a), versions=versions, seed="seed:1")
    )
    result_b = ReplayRunner(tracker_b.consume).run(
        ReplayDataset(dataset_id="a", events=(*shared, tail_b), versions=versions, seed="seed:1")
    )
    for step_a, step_b in zip(result_a.steps[:3], result_b.steps[:3], strict=True):
        assert step_a.output == step_b.output


def test_fixed_horizon_no_hindsight_optimal_exit() -> None:
    # Bar 2 spikes to 200; the fixed 2-bar horizon still exits at bar 3's
    # much worse close. The spike is never "picked".
    events = [
        candidate_event("ev-c1", 0, "c1"),
        bar_event("ev-b1", 1, o="100", h="101", c="100"),
        bar_event("ev-b2", 2, o="150", h="200", c="190"),
        bar_event("ev-b3", 3, o="95", h="96", lo="90", c="92"),
    ]
    _, report, _ = run(events)
    outcome = report.outcomes[0]
    assert outcome.status is VirtualOutcomeStatus.FILLED
    assert outcome.virtual_exit == "92.0000"  # bar3 close, not the 200 spike


# -- virtual execution economics --------------------------------------------------------


def test_costs_spread_slippage_adverse_for_buy_and_sell() -> None:
    costed = policy(spread_half_pct=Decimal("0.001"), slippage_pct=Decimal("0.001"))
    buy_events = [candidate_event("ev-c1", 0, "b1", direction="BUY"), *BASIC_EVENTS[1:]]
    sell_events = [candidate_event("ev-c1", 0, "s1", direction="SELL"), *BASIC_EVENTS[1:]]
    _, buy_report, _ = run(buy_events, shadow_policy=costed)
    _, sell_report, _ = run(sell_events, shadow_policy=costed)
    # Entry base is bar1 open=100; factor 0.002 adverse in each direction.
    assert buy_report.outcomes[0].virtual_entry == "100.2000"
    assert sell_report.outcomes[0].virtual_entry == "99.8000"
    # Exit base is bar3 close=105, adverse against the closing side.
    assert buy_report.outcomes[0].virtual_exit == "104.7900"  # SELL to close
    assert sell_report.outcomes[0].virtual_exit == "105.2100"  # BUY to close
    # Costs: max(1, 10*0.01)=1 per leg, both legs.
    assert buy_report.outcomes[0].costs == "2"
    gross = (Decimal("104.7900") - Decimal("100.2000")) * 10
    assert buy_report.outcomes[0].gross_pnl == str(gross)
    assert buy_report.outcomes[0].net_pnl == str(gross - 2)


def test_partial_fill_only_when_policy_allows() -> None:
    thin_bars = [
        bar_event("ev-b1", 1, o="100", c="102", v="50"),
        bar_event("ev-b2", 2, o="102", c="103", v="50"),
        bar_event("ev-b3", 3, o="103", c="105", v="50"),
    ]
    events = [candidate_event("ev-c1", 0, "c1", quantity="10"), *thin_bars]
    _, default_report, _ = run(events)
    assert default_report.outcomes[0].status is VirtualOutcomeStatus.FILLED
    partial_policy = policy(
        allow_partial_fills=True, max_participation_pct=Decimal("0.1")
    )
    _, partial_report, _ = run(events, shadow_policy=partial_policy)
    outcome = partial_report.outcomes[0]
    assert outcome.status is VirtualOutcomeStatus.PARTIAL
    assert outcome.filled_quantity == "5"  # 50 * 0.1


def test_unfilled_expired_incomplete_are_kept_never_dropped() -> None:
    events = [
        candidate_event("ev-c1", 0, "c-limit", limit="90"),  # never crossed
        candidate_event("ev-c2", 0, "c-nodata", symbol="MSFT", sequence=2),
        candidate_event("ev-c3", 0, "c-shorttail", sequence=3),
        bar_event("ev-b1", 1, o="100", c="102", sequence=4),
        bar_event("ev-b2", 2, o="102", c="103", sequence=5),
    ]
    horizon_policy = policy(horizon_bars=5)
    _, report, _ = run(events, shadow_policy=horizon_policy)
    by_id = {outcome.candidate_id: outcome for outcome in report.outcomes}
    assert by_id["c-limit"].status is VirtualOutcomeStatus.UNFILLED
    assert by_id["c-nodata"].status is VirtualOutcomeStatus.EXPIRED
    assert by_id["c-shorttail"].status is VirtualOutcomeStatus.INCOMPLETE
    assert report.total_tracked == 3
    assert any("c-nodata" in finding for finding in report.findings)
    assert any("c-shorttail" in finding for finding in report.findings)


# -- attribution / grouping --------------------------------------------------------------


def test_grouping_by_origin_reason_engine_version() -> None:
    events = [
        candidate_event("ev-c1", 0, "c1"),
        candidate_event("ev-c2", 0, "c2", sequence=1),
        candidate_event("ev-c3", 0, "w1", origin="WATCH", reason="AWAIT_CONF", sequence=2),
        *BASIC_EVENTS[1:],
    ]
    _, report, _ = run(events)
    assert len(report.groups) == 2
    rejected_group = next(g for g in report.groups if g.origin is ShadowOrigin.REJECTED)
    assert rejected_group.candidates == 2
    assert rejected_group.reason_code == "SCORE_BELOW_THRESHOLD"
    assert rejected_group.engine == "fusion-engine"
    assert rejected_group.version == "1.2.0"


def test_missing_attribution_is_refused() -> None:
    with pytest.raises(ShadowError, match="reason_code"):
        run([candidate_event("e", 0, "c1", reason="")])
    with pytest.raises(ShadowError, match="attribution"):
        run([candidate_event("e", 0, "c1", version="")])


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ShadowError, match="timezone-aware"):
        ShadowCandidate(
            candidate_id="c",
            origin=ShadowOrigin.REJECTED,
            reason_code="R",
            attributed_engine="e",
            attributed_version="1",
            decision_as_of=datetime(2026, 6, 1, 14, 30),  # noqa: DTZ001
            symbol="AAPL",
            direction="BUY",
            virtual_quantity=Decimal("1"),
            reference_price=Decimal("100"),
        )


# -- dedup / conflict / grouping isolation -------------------------------------------------


def test_identical_duplicate_candidate_is_deduped() -> None:
    events = [
        candidate_event("ev-c1", 0, "c1"),
        candidate_event("ev-c1-dup", 0, "c1", sequence=1),  # same content
        *BASIC_EVENTS[1:],
    ]
    _, report, _ = run(events)
    assert report.total_tracked == 1


def test_conflicting_duplicate_candidate_rejected() -> None:
    events = [
        candidate_event("ev-c1", 0, "c1"),
        candidate_event("ev-c2", 0, "c1", quantity="99", sequence=1),
    ]
    with pytest.raises(ShadowError, match="different content"):
        run(events)


def test_run_split_mixing_rejected() -> None:
    tracker = CounterfactualShadowTracker(policy(), run_id="run-A", split_id="s1")
    foreign = ShadowCandidate(
        candidate_id="c1",
        origin=ShadowOrigin.REJECTED,
        reason_code="R",
        attributed_engine="e",
        attributed_version="1",
        decision_as_of=T0,
        symbol="AAPL",
        direction="BUY",
        virtual_quantity=Decimal("1"),
        reference_price=Decimal("100"),
        run_id="run-B",
        split_id="s1",
    )
    with pytest.raises(ShadowError, match="fail closed"):
        tracker.track(foreign)


# -- determinism / versioning --------------------------------------------------------------


def test_repeat_and_shuffle_yield_identical_report_and_hash() -> None:
    hash_a, report_a, _ = run(list(BASIC_EVENTS))
    hash_b, report_b, _ = run(list(BASIC_EVENTS))
    hash_c, report_c, _ = run(list(reversed(BASIC_EVENTS)))
    assert hash_a == hash_b == hash_c
    assert report_a == report_b == report_c


def test_policy_seed_and_versions_change_fingerprint_and_hash() -> None:
    base_hash, base_report, _ = run(list(BASIC_EVENTS))
    other_policy_hash, other_policy_report, _ = run(
        list(BASIC_EVENTS), shadow_policy=policy(policy_version="shadow-policy-2")
    )
    assert other_policy_report.policy_ref != base_report.policy_ref
    assert other_policy_hash != base_hash
    seed_hash, _, _ = run(list(BASIC_EVENTS), seed="seed:2")
    assert seed_hash != base_hash
    versions_hash, _, _ = run(list(BASIC_EVENTS), versions={"shadow": "2.0.0"})
    assert versions_hash != base_hash


def test_input_events_not_mutated() -> None:
    events = list(BASIC_EVENTS)
    before = [event.canonical() for event in events]
    run(events)
    assert [event.canonical() for event in events] == before


# -- absolute live/paper isolation (DoD) ------------------------------------------------------


def test_every_output_is_marked_virtual_shadow() -> None:
    tracker = CounterfactualShadowTracker(policy())
    runner = ReplayRunner(tracker.consume)
    result = runner.run(
        ReplayDataset(
            dataset_id="mark",
            events=tuple(BASIC_EVENTS),
            versions={"shadow": "1.0.0"},
            seed="seed:1",
        )
    )
    for step in result.steps:
        assert step.output["virtual"] is True
        assert str(step.output["note"]).startswith("SHADOW:")
    report = tracker.report()
    assert report.counterfactual_only is True
    assert report.affects_live_state is False
    assert report.affects_paper_state is False
    for outcome in report.outcomes:
        assert outcome.counterfactual_only is True


def test_virtual_marking_cannot_be_disabled() -> None:
    with pytest.raises(ShadowError, match="counterfactual"):
        VirtualFill(
            candidate_id="c",
            quantity=Decimal("1"),
            price=Decimal("100"),
            filled_as_of=T0,
            counterfactual_only=False,
        )
    with pytest.raises(ShadowError, match="counterfactual"):
        VirtualOutcome(
            candidate_id="c",
            origin=ShadowOrigin.REJECTED,
            reason_code="R",
            attributed_engine="e",
            attributed_version="1",
            status=VirtualOutcomeStatus.UNFILLED,
            filled_quantity="0",
            virtual_entry=None,
            virtual_exit=None,
            gross_pnl="0",
            costs="0",
            net_pnl="0",
            counterfactual_only=False,
        )


def test_no_injection_point_for_live_or_paper_state() -> None:
    parameters = set(inspect.signature(CounterfactualShadowTracker.__init__).parameters)
    assert parameters == {"self", "policy", "run_id", "split_id"}
    consume_params = set(inspect.signature(CounterfactualShadowTracker.consume).parameters)
    assert consume_params == {"self", "event", "snapshot", "as_of", "manifest"}


def test_source_has_no_forbidden_imports_or_order_surface() -> None:
    import trading_application.counterfactual_shadow as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for token in (
        "OrderIntent", "SubmitAuthorization", ".submit(", ".cancel(",
        "FLATTEN", "model_registry", "datetime.now", "utcnow", "time.time",
        "random.", "uuid4",
    ):
        assert token not in source, token
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "execution_agent", "httpx", "requests",
            "socket", "trading_persistence", "trading_event_bus",
        ):
            assert forbidden not in line, line
