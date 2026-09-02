"""Trading readiness vs backtest quality (ADR-0017).

Historical sector and historical index membership answer "how faithful is this
backtest", not "is this trade safe now". These tests pin that separation and,
just as importantly, pin what did NOT change: every broker/account/risk gate is
still mandatory in both modes.

Offline and deterministic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.sp500_current_universe import (
    Constituent,
    ConstituentSnapshot,
    UniverseStore,
    classify_freshness,
    load_snapshot,
    parse_constituent_table,
    refresh_universe,
    sector_context,
    validate_snapshot,
)
from trading_market_data_core.trading_readiness import (
    DEFAULT_FRESHNESS,
    MANDATORY_TRADING_GATES,
    OPTIONAL_FOR_TRADING,
    BacktestQuality,
    GicsMode,
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    UniverseListStatus,
    UniverseType,
    evaluate_backtest_quality,
    evaluate_trading_readiness,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _all_gates(satisfied: bool = True) -> dict[str, bool]:
    return dict.fromkeys(MANDATORY_TRADING_GATES, satisfied)


def _strategy(**kw: object) -> StrategyDataRequirements:
    defaults: dict[str, object] = {"strategy_id": "baseline-trend-breakout-v1"}
    defaults.update(kw)
    return StrategyDataRequirements(**defaults)  # type: ignore[arg-type]


# == GICS never blocks trading ================================================


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.LIVE])
def test_missing_gics_does_not_block_trading(mode: TradingMode) -> None:
    report = evaluate_trading_readiness(
        mode=mode, requirements=_strategy(), gate_results=_all_gates(),
        gics_available=False,
    )
    assert report.may_trade
    assert report.readiness is (
        TradingReadiness.LIVE_READY if mode is TradingMode.LIVE else TradingReadiness.PAPER_READY
    )
    assert not any("gics" in blocker for blocker in report.blockers)


def test_gics_is_reported_as_optional_context_not_a_blocker() -> None:
    report = evaluate_trading_readiness(
        mode=TradingMode.PAPER, requirements=_strategy(), gate_results=_all_gates()
    )
    assert report.optional_context["asof_gics"] == "OPTIONAL_DISABLED"
    assert "asof_gics" not in " ".join(report.blockers)


def test_gics_default_is_disabled() -> None:
    assert _strategy().gics_mode is GicsMode.DISABLED
    assert _strategy().requires_gics_for_trading is False


def test_free_reconstructed_gics_is_flagged_as_non_authoritative() -> None:
    report = evaluate_trading_readiness(
        mode=TradingMode.PAPER,
        requirements=_strategy(gics_mode=GicsMode.FREE_RECONSTRUCTED, uses_sector=True),
        gate_results=_all_gates(), gics_available=True,
    )
    assert report.may_trade
    assert any("non-authoritative" in note for note in report.notes)


def test_a_strategy_that_uses_sector_still_trades_without_gics() -> None:
    """Sector context is enrichment; its absence cannot make a trade unsafe."""
    report = evaluate_trading_readiness(
        mode=TradingMode.LIVE,
        requirements=_strategy(uses_sector=True, gics_mode=GicsMode.FREE_RECONSTRUCTED),
        gate_results=_all_gates(), gics_available=False,
    )
    assert report.readiness is TradingReadiness.LIVE_READY
    assert report.optional_context["asof_gics"] == "OPTIONAL_UNAVAILABLE"


# == historical PIT membership never blocks trading ===========================


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.LIVE])
def test_missing_pit_membership_does_not_block_trading(mode: TradingMode) -> None:
    report = evaluate_trading_readiness(
        mode=mode, requirements=_strategy(), gate_results=_all_gates(),
        pit_membership_available=False,
    )
    assert report.may_trade
    assert not any("pit_sp500" in blocker for blocker in report.blockers)


def test_optional_capabilities_are_exactly_the_two_reclassified_ones() -> None:
    assert set(OPTIONAL_FOR_TRADING) == {"asof_gics", "pit_sp500_membership"}
    assert not set(OPTIONAL_FOR_TRADING) & set(MANDATORY_TRADING_GATES)


def test_pit_membership_is_never_substituted_for_a_current_list() -> None:
    """An index strategy with PIT history but no CURRENT list is BLOCKED."""
    report = evaluate_trading_readiness(
        mode=TradingMode.PAPER,
        requirements=_strategy(universe=UniverseType.SP500_CURRENT),
        gate_results=_all_gates(),
        universe_status=UniverseListStatus.CURRENT_LIST_UNAVAILABLE,
        pit_membership_available=True,
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("current constituent list" in b for b in report.blockers)


# == broker/risk safety is unchanged ==========================================


@pytest.mark.parametrize("gate", MANDATORY_TRADING_GATES)
def test_every_mandatory_gate_still_blocks_both_modes(gate: str) -> None:
    gates = _all_gates()
    gates[gate] = False
    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        report = evaluate_trading_readiness(
            mode=mode, requirements=_strategy(), gate_results=gates
        )
        assert report.readiness is TradingReadiness.BLOCKED, gate
        assert any(gate in blocker for blocker in report.blockers)


def test_the_mandatory_set_still_covers_broker_and_risk_controls() -> None:
    """A regression here would be a safety regression, not a naming change."""
    required = {
        "ibkr_connectivity_account_safety", "account_mode_safety",
        "contract_identity_valid", "security_currently_tradable",
        "market_data_usable", "position_sizing", "risk_limits",
        "broker_side_protection", "protective_order_requirements",
        "audit_trail_decision_logging",
    }
    assert required <= set(MANDATORY_TRADING_GATES)


def test_an_absent_gate_result_is_treated_as_unsatisfied() -> None:
    """Missing information is never optimism."""
    report = evaluate_trading_readiness(
        mode=TradingMode.LIVE, requirements=_strategy(), gate_results={}
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert len(report.blockers) == len(MANDATORY_TRADING_GATES)


# == strategy-derived universe gating =========================================


def test_a_non_index_strategy_ignores_the_universe_list() -> None:
    report = evaluate_trading_readiness(
        mode=TradingMode.LIVE,
        requirements=_strategy(universe=UniverseType.ALL_ELIGIBLE_US_EQUITIES),
        gate_results=_all_gates(),
        universe_status=UniverseListStatus.CURRENT_LIST_UNAVAILABLE,
    )
    assert report.readiness is TradingReadiness.LIVE_READY


def test_an_index_strategy_requires_a_current_list() -> None:
    report = evaluate_trading_readiness(
        mode=TradingMode.PAPER,
        requirements=_strategy(universe=UniverseType.SP500_CURRENT),
        gate_results=_all_gates(),
        universe_status=UniverseListStatus.CURRENT_LIST_VALID,
    )
    assert report.readiness is TradingReadiness.PAPER_READY


def test_paper_may_use_a_stale_list_but_live_may_not() -> None:
    requirements = _strategy(universe=UniverseType.SP500_CURRENT)
    paper = evaluate_trading_readiness(
        mode=TradingMode.PAPER, requirements=requirements, gate_results=_all_gates(),
        universe_status=UniverseListStatus.CURRENT_LIST_STALE,
    )
    live = evaluate_trading_readiness(
        mode=TradingMode.LIVE, requirements=requirements, gate_results=_all_gates(),
        universe_status=UniverseListStatus.CURRENT_LIST_STALE,
    )
    assert paper.readiness is TradingReadiness.PAPER_READY
    assert any("STALE" in note for note in paper.notes)
    assert live.readiness is TradingReadiness.BLOCKED
    assert any("STALE" in blocker for blocker in live.blockers)


def test_plain_language_never_implies_optional_data_blocks_trading() -> None:
    report = evaluate_trading_readiness(
        mode=TradingMode.PAPER, requirements=_strategy(), gate_results=_all_gates()
    )
    text = "\n".join(report.plain_language())
    assert "does not block trading" in text
    assert "BLOCKED_EXTERNAL" not in text


# == backtest quality ==========================================================


def test_approximate_runs_without_pit_and_reports_its_limits() -> None:
    report = evaluate_backtest_quality(
        requirements=_strategy(universe=UniverseType.SP500_CURRENT),
        requested=BacktestQuality.APPROXIMATE,
        pit_membership_available=False,
    )
    assert report.achieved is BacktestQuality.APPROXIMATE
    assert any("survivorship" in limit for limit in report.limitations)


def test_strict_refuses_when_pit_membership_is_missing() -> None:
    report = evaluate_backtest_quality(
        requirements=_strategy(universe=UniverseType.SP500_CURRENT),
        requested=BacktestQuality.PIT_STRICT,
        pit_membership_available=False,
        delisted_history_complete=True, corporate_actions_complete=True,
    )
    assert report.achieved is BacktestQuality.INCOMPLETE
    assert "pit_sp500_membership" in report.missing_evidence
    assert not report.is_strict


def test_an_approximate_run_is_never_labelled_strict() -> None:
    report = evaluate_backtest_quality(
        requirements=_strategy(), requested=BacktestQuality.APPROXIMATE
    )
    assert report.achieved is not BacktestQuality.PIT_STRICT


def test_strict_needs_historical_gics_only_when_the_strategy_uses_sector() -> None:
    without_sector = evaluate_backtest_quality(
        requirements=_strategy(uses_sector=False),
        requested=BacktestQuality.PIT_STRICT,
        historical_gics_available=False,
        delisted_history_complete=True, corporate_actions_complete=True,
    )
    with_sector = evaluate_backtest_quality(
        requirements=_strategy(uses_sector=True),
        requested=BacktestQuality.PIT_STRICT,
        historical_gics_available=False,
        delisted_history_complete=True, corporate_actions_complete=True,
    )
    assert without_sector.achieved is BacktestQuality.PIT_STRICT
    assert with_sector.achieved is BacktestQuality.INCOMPLETE
    assert "asof_gics" in with_sector.missing_evidence


def test_strict_still_demands_delisted_and_corporate_completeness() -> None:
    report = evaluate_backtest_quality(
        requirements=_strategy(), requested=BacktestQuality.PIT_STRICT,
        delisted_history_complete=False, corporate_actions_complete=False,
    )
    assert report.achieved is BacktestQuality.INCOMPLETE
    assert {"delisted_security_history", "corporate_actions"} <= set(report.missing_evidence)


def test_strict_succeeds_when_all_its_evidence_is_present() -> None:
    report = evaluate_backtest_quality(
        requirements=_strategy(universe=UniverseType.SP500_CURRENT, uses_sector=True),
        requested=BacktestQuality.PIT_STRICT,
        pit_membership_available=True, historical_gics_available=True,
        delisted_history_complete=True, corporate_actions_complete=True,
    )
    assert report.is_strict


def test_free_gics_in_an_approximate_run_is_reported_as_look_ahead() -> None:
    report = evaluate_backtest_quality(
        requirements=_strategy(uses_sector=True, gics_mode=GicsMode.FREE_RECONSTRUCTED),
        requested=BacktestQuality.APPROXIMATE,
    )
    assert any("look-ahead" in limit for limit in report.limitations)


# == current universe store ====================================================


def _snapshot(count: int = 500, as_of: date = date(2026, 8, 1)) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_id="SP500", as_of=as_of, fetched_at=NOW, source_id="free-constituents",
        source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(
                ticker=f"T{index:03d}", name=f"Company {index}",
                security_id=f"sec-cik:{index:010d}", gics_sector="Information Technology",
            )
            for index in range(count)
        ),
    )


def test_a_fresh_list_is_valid() -> None:
    status = classify_freshness(
        _snapshot(), now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )
    assert status is UniverseListStatus.CURRENT_LIST_VALID


def test_an_old_list_is_stale_for_paper_and_unavailable_for_live() -> None:
    old = _snapshot(as_of=date(2026, 6, 1))
    assert (
        classify_freshness(old, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER])
        is UniverseListStatus.CURRENT_LIST_STALE
    )
    assert (
        classify_freshness(old, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.LIVE])
        is UniverseListStatus.CURRENT_LIST_UNAVAILABLE
    )


def test_no_snapshot_is_unavailable() -> None:
    assert (
        classify_freshness(None, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER])
        is UniverseListStatus.CURRENT_LIST_UNAVAILABLE
    )


def test_an_empty_or_implausible_snapshot_is_rejected() -> None:
    assert "snapshot is empty" in validate_snapshot(_snapshot(count=0))[0]
    assert "outside the plausible range" in validate_snapshot(_snapshot(count=5))[0]


def test_a_current_snapshot_may_never_claim_to_be_history() -> None:
    from dataclasses import replace

    lying = replace(_snapshot(), is_point_in_time_history=True)
    assert any("never claim" in f for f in validate_snapshot(lying))


def test_a_failed_refresh_keeps_the_last_known_good(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot())
    current, status, findings = refresh_universe(
        store, None, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )
    assert current is not None and current.count == 500
    assert status is UniverseListStatus.CURRENT_LIST_VALID
    assert any("keeping the last-known-good" in f for f in findings)


def test_a_malformed_refresh_never_replaces_a_good_list(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot())
    current, _, findings = refresh_universe(
        store, _snapshot(count=3, as_of=date(2026, 8, 15)), now=NOW,
        policy=DEFAULT_FRESHNESS[TradingMode.PAPER], allow_normalized_only=True,
    )
    assert current is not None and current.count == 500, "the good list survives"
    assert any("refresh rejected" in f for f in findings)


def test_a_valid_refresh_is_accepted(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot(as_of=date(2026, 7, 1)))
    current, status, findings = refresh_universe(
        store, _snapshot(as_of=date(2026, 8, 15)), now=NOW,
        policy=DEFAULT_FRESHNESS[TradingMode.PAPER], allow_normalized_only=True,
    )
    assert findings == ()
    assert current is not None and current.as_of == date(2026, 8, 15)
    assert status is UniverseListStatus.CURRENT_LIST_VALID


def test_monthly_refresh_is_due_after_the_interval(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    assert store.due_for_refresh(now=NOW), "no snapshot at all is always due"
    store.accept(_snapshot(as_of=date(2026, 8, 15)))
    assert not store.due_for_refresh(now=NOW)
    store.accept(_snapshot(as_of=date(2026, 6, 1)))
    assert not store.due_for_refresh(now=NOW), "the LATEST snapshot decides"


def test_a_tampered_snapshot_fails_on_load(tmp_path: Path) -> None:
    import json

    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, _ = store.accept(_snapshot())
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["constituents"][0]["ticker"] = "HACKED"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="checksum mismatch"):
        load_snapshot(path)


def test_snapshots_round_trip(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, findings = store.accept(_snapshot())
    assert findings == () and path is not None
    loaded = load_snapshot(path)
    assert loaded.count == 500
    assert loaded.is_point_in_time_history is False
    assert loaded.source_authority == "PUBLIC_SECONDARY"


# == free constituent dataset ==================================================


TABLE = b"""<table>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
<th>CIK</th><th>Date added</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td>
<td>Technology Hardware</td><td>0000320193</td><td>1982-11-30</td></tr>
<tr><td>MSFT</td><td>Microsoft</td><td>Information Technology</td>
<td>Systems Software</td><td>0000789019</td><td>1994-06-01</td></tr>
</table>
"""


def test_the_free_table_parses_into_a_current_snapshot() -> None:
    snapshot = parse_constituent_table(
        TABLE, as_of=date(2026, 8, 1), fetched_at=NOW, source_id="free-constituents"
    )
    assert snapshot.count == 2
    assert snapshot.tickers() == {"AAPL", "MSFT"}
    apple = next(c for c in snapshot.constituents if c.ticker == "AAPL")
    assert apple.security_id == "sec-cik:0000320193", "identity is CIK, not the ticker"
    assert apple.gics_sector == "Information Technology"


def test_the_free_snapshot_is_marked_non_authoritative_and_not_history() -> None:
    snapshot = parse_constituent_table(
        TABLE, as_of=date(2026, 8, 1), fetched_at=NOW, source_id="free-constituents"
    )
    assert snapshot.source_authority == "PUBLIC_SECONDARY"
    assert snapshot.is_point_in_time_history is False
    assert any("NOT point-in-time history" in note for note in snapshot.notes)


def test_sector_context_is_unknown_when_gics_is_disabled() -> None:
    snapshot = parse_constituent_table(
        TABLE, as_of=date(2026, 8, 1), fetched_at=NOW, source_id="free-constituents"
    )
    sector, provenance = sector_context(snapshot, "AAPL", gics_mode=GicsMode.DISABLED)
    assert sector is None
    assert "DISABLED" in provenance


def test_sector_context_states_its_provenance_when_enabled() -> None:
    snapshot = parse_constituent_table(
        TABLE, as_of=date(2026, 8, 1), fetched_at=NOW, source_id="free-constituents"
    )
    sector, provenance = sector_context(
        snapshot, "AAPL", gics_mode=GicsMode.FREE_RECONSTRUCTED
    )
    assert sector == "Information Technology"
    assert "non-authoritative" in provenance
    assert "not point-in-time" in provenance


def test_an_unknown_ticker_yields_unknown_not_a_guess() -> None:
    snapshot = parse_constituent_table(
        TABLE, as_of=date(2026, 8, 1), fetched_at=NOW, source_id="free-constituents"
    )
    sector, provenance = sector_context(
        snapshot, "NOPE", gics_mode=GicsMode.FREE_RECONSTRUCTED
    )
    assert sector is None
    assert "UNKNOWN" in provenance
