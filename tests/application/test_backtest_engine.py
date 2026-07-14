"""Backtest engine drills (IMP-1202). DoD: look-ahead and leakage tests
pass. The engine runs ONLY as a consumer inside the IMP-1201 ReplayRunner;
all determinism (ordering, versions, hashing) flows through the runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.backtest_engine import (
    BacktestCostPolicy,
    BacktestEngine,
    BacktestError,
    SameBarFillPolicy,
    WalkForwardSplit,
    assert_no_leakage,
    validate_splits,
)
from trading_application.replay_runner import (
    Payload,
    ReplayDataset,
    ReplayError,
    ReplayEvent,
    ReplayResult,
    ReplayRunner,
    VersionRequirement,
)

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)

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

VERSIONS = {"execution-policy": POLICY.policy_version, "schema": "v1"}


def bar(
    event_id: str,
    *,
    minute: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "10000",
    sequence: int = 0,
) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    return ReplayEvent(
        event_id=event_id,
        sequence=sequence,
        kind="BAR",
        payload={
            "symbol": "AAPL",
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        observed_at=at,
        available_at=at,
    )


def intent(
    event_id: str,
    *,
    minute: int,
    sequence: int = 9,
    order_type: str = "MARKET",
    side: str = "BUY",
    quantity: str = "10",
    limit_price: str | None = None,
    stop_price: str | None = None,
    protective_stop: str | None = None,
    target: str | None = None,
) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    payload: dict[str, str | int | bool | None] = {
        "intent_id": event_id,
        "symbol": "AAPL",
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "protective_stop": protective_stop,
        "target": target,
    }
    return ReplayEvent(
        event_id=event_id,
        sequence=sequence,
        kind="INTENT",
        payload=payload,
        observed_at=at,
        available_at=at,
    )


def dataset(events: list[ReplayEvent], *, seed: str = "seed:1") -> ReplayDataset:
    return ReplayDataset(dataset_id="bt-1", events=tuple(events), versions=VERSIONS, seed=seed)


def run(
    events: list[ReplayEvent],
    *,
    policy: BacktestCostPolicy = POLICY,
    seed: str = "seed:1",
) -> tuple[ReplayResult, BacktestEngine]:
    engine = BacktestEngine(policy)
    runner = ReplayRunner(
        engine.consume,
        required_versions=(VersionRequirement("execution-policy", policy.policy_version),),
    )
    result = runner.run(dataset(events, seed=seed))
    return result, engine


def notes_of(result: ReplayResult) -> list[str]:
    return [str(step.output.get("notes")) for step in result.steps]


BASE = [
    bar("b1", minute=0, open_="100", high="101", low="99", close="100"),
    intent("i1", minute=0, protective_stop="95", target="105"),
    bar("b2", minute=1, open_="100", high="102", low="99", close="101"),
    bar("b3", minute=2, open_="100", high="106", low="99", close="105"),
]


# -- determinism through the runner ---------------------------------------------------


def test_repeated_run_identical_result_and_hash() -> None:
    first, _ = run(list(BASE))
    second, _ = run(list(BASE))
    assert first == second
    assert first.result_hash == second.result_hash


def test_shuffled_input_same_result() -> None:
    ordered, _ = run(list(BASE))
    shuffled, _ = run(list(reversed(BASE)))
    assert shuffled == ordered


def test_seed_change_changes_run_id() -> None:
    a, _ = run(list(BASE), seed="seed:1")
    b, _ = run(list(BASE), seed="seed:2")
    assert a.manifest.run_id != b.manifest.run_id


# -- no lookahead ----------------------------------------------------------------------


def test_intent_at_bar_close_never_fills_retroactively_in_that_bar() -> None:
    result, _ = run(list(BASE))
    steps = {step.event_id: str(step.output.get("notes")) for step in result.steps}
    assert "fill:" not in steps["b1"]  # same-close bar: no retroactive fill
    assert "fill:i1:10@100" in steps["b2"]  # filled on the NEXT bar's open


def test_future_bar_change_does_not_change_earlier_fills() -> None:
    variant = list(BASE)
    variant[3] = bar("b3", minute=2, open_="50", high="51", low="49", close="50")
    baseline, _ = run(list(BASE))
    changed, _ = run(variant)
    # Every step up to and including the fill bar is identical.
    assert baseline.steps[:3] == changed.steps[:3]
    assert baseline.result_hash != changed.result_hash  # later data differs, honestly


def test_exits_never_use_the_entry_bar_prices() -> None:
    # b2 itself touches the stop (low 99 > 95? no) — craft one that would:
    events = [
        bar("b1", minute=0, open_="100", high="101", low="99", close="100"),
        intent("i1", minute=0, protective_stop="99.5", target="105"),
        bar("b2", minute=1, open_="100", high="101", low="99", close="100"),
        bar("b3", minute=2, open_="100", high="101", low="99.6", close="100"),
    ]
    result, engine = run(events)
    steps = {step.event_id: str(step.output.get("notes")) for step in result.steps}
    # entry fills during b2; b2's own low (99 <= stop 99.5) must NOT exit it
    assert "fill:i1" in steps["b2"]
    assert "exit:" not in steps["b2"]
    assert engine.report().trades == 0  # b3 low 99.6 never touches the stop


# -- same-bar ambiguity ------------------------------------------------------------------


AMBIGUOUS = [
    bar("b1", minute=0, open_="100", high="101", low="99", close="100"),
    intent("i1", minute=0, protective_stop="95", target="105"),
    bar("b2", minute=1, open_="100", high="101", low="99", close="100"),
    bar("b3", minute=2, open_="100", high="106", low="94", close="100"),  # both touch
]


def test_same_bar_stop_and_target_resolved_conservatively() -> None:
    result, engine = run(list(AMBIGUOUS))
    report = engine.report()
    assert report.trades == 1
    assert "exit:i1:STOP:10@95" in " ".join(notes_of(result))  # adverse-first
    assert len(report.ambiguous_bars) == 1  # recorded, never silent


def test_changing_same_bar_policy_changes_outcome_and_hash() -> None:
    optimistic = BacktestCostPolicy(
        policy_version=POLICY.policy_version,
        commission_per_share=POLICY.commission_per_share,
        minimum_commission=POLICY.minimum_commission,
        spread_half_pct=POLICY.spread_half_pct,
        slippage_pct=POLICY.slippage_pct,
        max_participation_pct=POLICY.max_participation_pct,
        time_in_force_bars=POLICY.time_in_force_bars,
        same_bar_fill_policy=SameBarFillPolicy.TARGET_FIRST,
    )
    conservative, _ = run(list(AMBIGUOUS))
    target_first, engine = run(list(AMBIGUOUS), policy=optimistic)
    assert "exit:i1:TARGET:10@105" in " ".join(notes_of(target_first))
    assert target_first.result_hash != conservative.result_hash


# -- costs / spread / slippage --------------------------------------------------------------


def test_buy_and_sell_adverse_adjustments_are_symmetric() -> None:
    priced = BacktestCostPolicy(
        policy_version="p2",
        commission_per_share=Decimal("0"),
        minimum_commission=Decimal("0"),
        spread_half_pct=Decimal("0.01"),
        slippage_pct=Decimal("0.005"),
        max_participation_pct=Decimal("1"),
        time_in_force_bars=1,
        same_bar_fill_policy=SameBarFillPolicy.STOP_FIRST,
    )
    assert priced.adverse(Decimal("100"), "BUY") == Decimal("101.5000")
    assert priced.adverse(Decimal("100"), "SELL") == Decimal("98.5000")


def test_commissions_with_minimum_are_exact() -> None:
    assert POLICY.commission(Decimal("40")) == Decimal("1.00")  # minimum binds
    assert POLICY.commission(Decimal("200")) == Decimal("2.00")  # per-share binds


def test_gross_minus_costs_equals_net() -> None:
    _, engine = run(list(BASE))  # entry 10@100, target exit @105 on b3
    report = engine.report()
    assert report.trades == 1
    assert Decimal(report.gross_pnl) == Decimal("50")
    assert Decimal(report.total_costs) == Decimal("2.00")  # two legs, minimum each
    assert Decimal(report.net_pnl) == Decimal(report.gross_pnl) - Decimal(report.total_costs)
    assert report.hit_rate == "1.0000"


# -- partial fills / unfilled / TIF ----------------------------------------------------------


def test_partial_fill_capped_by_volume_participation_and_tif_expiry() -> None:
    thin = BacktestCostPolicy(
        policy_version=POLICY.policy_version,
        commission_per_share=Decimal("0.01"),
        minimum_commission=Decimal("1.00"),
        spread_half_pct=Decimal("0"),
        slippage_pct=Decimal("0"),
        max_participation_pct=Decimal("0.10"),
        time_in_force_bars=1,
        same_bar_fill_policy=SameBarFillPolicy.STOP_FIRST,
    )
    events = [
        bar("b1", minute=0, open_="100", high="101", low="99", close="100"),
        intent("i1", minute=0, quantity="100"),
        bar("b2", minute=1, open_="100", high="101", low="99", close="100", volume="50"),
    ]
    result, engine = run(events, policy=thin)
    joined = " ".join(notes_of(result))
    assert "partial:i1:5@100.0000:remaining=95" in joined  # floor(50 * 0.10) = 5
    assert "expired:i1:remaining=95" in joined  # TIF=1: remainder expires, recorded
    report = engine.report()
    assert report.partial_fills == 1
    assert report.expired == 1


def test_limit_never_crossed_is_unfilled_and_reported() -> None:
    events = [
        bar("b1", minute=0, open_="100", high="101", low="99", close="100"),
        intent("i1", minute=0, order_type="LIMIT", limit_price="90", quantity="10"),
        bar("b2", minute=1, open_="100", high="101", low="99", close="100"),
        bar("b3", minute=2, open_="100", high="101", low="99", close="100"),
        bar("b4", minute=3, open_="100", high="101", low="99", close="100"),
    ]
    result, engine = run(events)
    assert "expired:i1:remaining=10" in " ".join(notes_of(result))
    report = engine.report()
    assert report.unfilled == 1
    assert report.fills == 0


def test_limit_fills_at_limit_or_better_without_slippage() -> None:
    events = [
        bar("b1", minute=0, open_="100", high="101", low="99", close="100"),
        intent("i1", minute=0, order_type="LIMIT", limit_price="99.5", quantity="10"),
        bar("b2", minute=1, open_="100", high="101", low="99", close="100"),
    ]
    result, _ = run(events)
    assert "fill:i1:10@99.5" in " ".join(notes_of(result))  # limit price, no worse


# -- walk-forward / leakage -------------------------------------------------------------------


def split(minutes: tuple[int, int, int, int], split_id: str = "s1") -> WalkForwardSplit:
    a, b, c, d = minutes
    return WalkForwardSplit(
        split_id=split_id,
        train_start=T0 + timedelta(minutes=a),
        train_end=T0 + timedelta(minutes=b),
        test_start=T0 + timedelta(minutes=c),
        test_end=T0 + timedelta(minutes=d),
    )


def test_valid_splits_pass_and_test_is_strictly_after_train() -> None:
    validate_splits([split((0, 10, 10, 20)), split((5, 15, 20, 30), "s2")])


def test_invalid_split_orders_fail_closed() -> None:
    with pytest.raises(BacktestError, match="invalid"):
        split((0, 10, 9, 20))  # test starts inside train
    with pytest.raises(BacktestError, match="invalid"):
        split((10, 10, 10, 20))  # empty train
    with pytest.raises(BacktestError, match="overlap"):
        validate_splits([split((0, 10, 10, 20)), split((0, 10, 15, 25), "s2")])
    with pytest.raises(BacktestError, match="at least one"):
        validate_splits([])


def test_parameter_learned_from_test_data_is_rejected() -> None:
    window = split((0, 10, 10, 20))
    assert_no_leakage(window, [T0 + timedelta(minutes=3), T0 + timedelta(minutes=9)])
    with pytest.raises(BacktestError, match="LEAKAGE"):
        assert_no_leakage(window, [T0 + timedelta(minutes=15)])  # inside test
    with pytest.raises(BacktestError, match="LEAKAGE"):
        assert_no_leakage(window, [T0 + timedelta(minutes=25)])  # after test


# -- versions / fail-closed ---------------------------------------------------------------------


def test_policy_version_enforced_through_runner() -> None:
    engine = BacktestEngine(POLICY)
    runner = ReplayRunner(
        engine.consume,
        required_versions=(VersionRequirement("execution-policy", "OTHER-9.9"),),
    )
    with pytest.raises(ReplayError, match="version mismatch"):
        runner.run(dataset(list(BASE)))


def test_invalid_policy_fields_fail_closed() -> None:
    with pytest.raises(BacktestError, match="non-negative"):
        BacktestCostPolicy(
            policy_version="p",
            commission_per_share=Decimal("-1"),
            minimum_commission=Decimal("0"),
            spread_half_pct=Decimal("0"),
            slippage_pct=Decimal("0"),
            max_participation_pct=Decimal("1"),
            time_in_force_bars=1,
            same_bar_fill_policy=SameBarFillPolicy.STOP_FIRST,
        )
    with pytest.raises(BacktestError, match="participation"):
        BacktestCostPolicy(
            policy_version="p",
            commission_per_share=Decimal("0"),
            minimum_commission=Decimal("0"),
            spread_half_pct=Decimal("0"),
            slippage_pct=Decimal("0"),
            max_participation_pct=Decimal("0"),
            time_in_force_bars=1,
            same_bar_fill_policy=SameBarFillPolicy.STOP_FIRST,
        )


def test_corporate_action_is_never_silently_adjusted() -> None:
    ca = ReplayEvent(
        event_id="ca-1",
        sequence=0,
        kind="CORPORATE_ACTION",
        payload={"symbol": "AAPL", "kind": "SPLIT", "ratio": "2"},
        observed_at=T0 + timedelta(minutes=4),
        available_at=T0 + timedelta(minutes=4),
    )
    with pytest.raises(BacktestError, match="corporate action"):
        run(list(BASE) + [ca])


# -- purity / hygiene ------------------------------------------------------------------------------


def test_input_events_not_mutated() -> None:
    events = list(BASE)
    before = [event.canonical() for event in events]
    run(events)
    assert [event.canonical() for event in events] == before


def test_no_clock_random_broker_or_persistence_in_source() -> None:
    import trading_application.backtest_engine as module

    assert module.__file__ is not None
    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    for fragment in ("datetime.now", "time.time", "utcnow", "random.", "uuid4"):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "httpx", "requests", "socket",
            "trading_persistence", "trading_event_bus",
        ):
            assert forbidden not in line, line


def output_types_are_flat_payloads() -> Payload:  # helper for type-checking only
    return {"policy": "x", "notes": None}
