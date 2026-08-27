"""Runtime trading readiness: the eleven gates computed from real state.

PR #90 defined the contract; this proves the runtime path. The property that
matters most: **no gate is satisfied by a caller passing ``True``**. Each is
derived from a runtime observation, and anything unknown, stale or absent is
BLOCKED.

Also pinned: readiness is not authorization, GICS and PIT membership never
block, and every broker/risk gate still blocks.

Offline and deterministic: no broker connection, no network.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.risk_engine import RiskResult
from trading_application.trading_readiness_runtime import (
    BrokerSessionState,
    ContractState,
    MarketDataRules,
    MarketDataState,
    ProtectionState,
    QuoteState,
    RuntimeInputs,
    RuntimeReadinessError,
    TradabilityState,
    evaluate_runtime_readiness,
    persist_readiness_decision,
)
from trading_contracts.enums import RiskDecisionOutcome
from trading_market_data_core.trading_readiness import (
    MANDATORY_TRADING_GATES,
    GicsMode,
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    UniverseListStatus,
    UniverseType,
)

NOW = datetime(2026, 8, 18, 15, 30, tzinfo=UTC)

def _quote(*args: object, **kw: object) -> QuoteState:
    """A quote that states its feed, because an unstated feed now blocks.

    ``market_data_state`` defaults to UNKNOWN in production for exactly that
    reason; these tests are about other properties, so they declare a healthy
    realtime feed rather than silently relying on a default that must not exist.
    """
    kw.setdefault("market_data_state", MarketDataState.REALTIME)
    return QuoteState(*args, **kw)  # type: ignore[arg-type]




def _risk(
    outcome: RiskDecisionOutcome | str = RiskDecisionOutcome.APPROVE,
    quantity: Decimal = Decimal(10),
) -> RiskResult:
    """A REAL ``RiskResult``, not something shaped like one.

    The runtime path reads the verdict through its declared type, so a fake
    carrying a convenient ``outcome`` string would test nothing about the
    component that actually vetoes.
    """
    resolved = RiskDecisionOutcome(outcome)
    return RiskResult(
        outcome=resolved,
        approved_quantity=quantity,
        approved_risk_amount=Decimal("500"),
        max_loss_amount=Decimal("500"),
        risk_budget_equity=Decimal("100000"),
        decision_ref="decision-1",
        request_ref="risk-request:AAPL@sha256:" + "1" * 64,
        policy_ref="policy-1",
        engine_version="test",
        checks=(),
        reason_codes=() if resolved is RiskDecisionOutcome.APPROVE else ("RISK_VETO",),
        input_refs=(),
    )


class _Audit:
    def __init__(self, available: bool = True, raises: bool = False) -> None:
        self._available = available
        self._raises = raises
        self.records: list[Mapping[str, object]] = []

    def can_persist(self) -> bool:
        if self._raises:
            raise RuntimeError("audit backend down")
        return self._available

    def persist(self, record: Mapping[str, object]) -> str:
        self.records.append(record)
        return "audit-1"


def _inputs(**kw: object) -> RuntimeInputs:
    defaults: dict[str, object] = {
        "mode": TradingMode.PAPER,
        "requirements": StrategyDataRequirements(strategy_id="baseline-trend-breakout-v1"),
        "broker": BrokerSessionState(
            connected=True, account_alias="paper-poi",
            session_mode=TradingMode.PAPER, observed_at=NOW - timedelta(seconds=2),
        ),
        "contract": ContractState(
            resolved=True, broker_instrument_ref="ibkr-paper:265598", symbol="AAPL",
            exchange="SMART", currency="USD",
        ),
        "tradability": TradabilityState(proven_tradable=True),
        "quote": _quote(
            bid=Decimal("100.00"), ask=Decimal("100.05"),
            observed_at=NOW - timedelta(seconds=1),
        ),
        "rules": MarketDataRules(
            max_spread_fraction=Decimal("0.01"), min_price=Decimal("1.00")
        ),
        "protection": ProtectionState(
            protective_order_supported=True, plan_has_protective_stop=True
        ),
        "risk_result": _risk(),
        "audit": _Audit(),
        "evaluated_at": NOW,
    }
    defaults.update(kw)
    return RuntimeInputs(**defaults)  # type: ignore[arg-type]


# == the happy path and the no-booleans property ==============================


def test_a_fully_satisfied_runtime_is_paper_ready() -> None:
    report = evaluate_runtime_readiness(_inputs())
    assert report.readiness is TradingReadiness.PAPER_READY
    assert report.blockers == ()
    assert len(report.gates) == len(MANDATORY_TRADING_GATES)


def test_the_evaluator_takes_no_caller_supplied_gate_verdicts() -> None:
    """A boolean gate map must not be an accepted input in the runtime path."""
    signature = inspect.signature(evaluate_runtime_readiness)
    assert list(signature.parameters) == ["inputs"]
    assert not hasattr(RuntimeInputs, "gate_results")


def test_every_mandatory_gate_has_a_runtime_evaluator() -> None:
    report = evaluate_runtime_readiness(_inputs())
    assert {gate.name for gate in report.gates} == set(MANDATORY_TRADING_GATES)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(RuntimeReadinessError, match="timezone-aware"):
        evaluate_runtime_readiness(_inputs(evaluated_at=datetime(2026, 8, 18, 15, 30)))


# == connectivity and account mode ============================================


def test_a_disconnected_broker_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(broker=BrokerSessionState(False, "paper-poi", TradingMode.PAPER, NOW))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("ibkr_connectivity" in b for b in report.blockers)


def test_unknown_session_state_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(broker=BrokerSessionState(True, None, None, None))
    )
    assert report.readiness is TradingReadiness.BLOCKED


def test_paper_mode_requires_the_broker_to_report_paper() -> None:
    """The session's own environment decides the mode, not the caller's wish."""
    report = evaluate_runtime_readiness(
        _inputs(
            mode=TradingMode.PAPER,
            broker=BrokerSessionState(True, "paper-poi", TradingMode.LIVE, NOW),
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("account_mode_safety" in b for b in report.blockers)


def test_an_environment_the_broker_cannot_state_blocks() -> None:
    """RESEARCH, or any non-trading environment, arrives here as None."""
    report = evaluate_runtime_readiness(
        _inputs(broker=BrokerSessionState(True, "paper-poi", None, NOW))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("account_mode_safety" in b for b in report.blockers)


def test_a_session_in_the_wrong_mode_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            mode=TradingMode.LIVE,
            broker=BrokerSessionState(True, "live", TradingMode.PAPER, NOW),
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED


def test_a_mismatched_account_alias_blocks() -> None:
    report = evaluate_runtime_readiness(_inputs(expected_account_alias="other-alias"))
    assert report.readiness is TradingReadiness.BLOCKED


# == contract identity and tradability ========================================


def test_an_unresolved_contract_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(contract=ContractState(False, None, None, None, None))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("contract_identity_valid" in b for b in report.blockers)


def test_a_symbol_mismatch_blocks_even_though_a_contract_resolved() -> None:
    report = evaluate_runtime_readiness(_inputs(expected_symbol="MSFT"))
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("does not match" in gate.detail for gate in report.gates)


def test_a_disallowed_currency_blocks() -> None:
    base = ContractState(True, "ibkr-paper:1", "AAPL", "SMART", "USD")
    report = evaluate_runtime_readiness(_inputs(contract=replace(base, currency="EUR")))
    assert report.readiness is TradingReadiness.BLOCKED


def test_the_contract_state_mirrors_the_canonical_broker_instrument() -> None:
    """No field here may be one the canonical port does not define.

    This is the test that would have caught reading ``con_id``/``security_type``/
    ``tradable`` off a ``BrokerInstrument`` that has none of them.
    """
    from trading_broker_core.port import BrokerInstrument

    canonical = {field.name for field in fields(BrokerInstrument)}
    observed = {field.name for field in fields(ContractState)}
    # ``resolved`` and ``detail`` describe the observation itself, not the
    # instrument; everything else must exist on the canonical contract.
    assert observed - {"resolved", "detail"} <= canonical


def test_an_unproven_security_blocks_because_unknown_is_not_optimism() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            tradability=TradabilityState(
                proven_tradable=False, evidence_available=False,
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("security_currently_tradable" in b for b in report.blockers)
    assert any("UNKNOWN" in gate.detail for gate in report.gates)


def test_a_refused_what_if_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            tradability=TradabilityState(
                proven_tradable=False, detail="broker refused the what-if"
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("security_currently_tradable" in b for b in report.blockers)


def test_tradability_defaults_to_nothing_at_all() -> None:
    """``TradabilityState`` has no default that means "tradable"."""
    with pytest.raises(TypeError):
        TradabilityState()  # type: ignore[call-arg]


# == market data is RUNTIME state, not a past qualification ===================


def test_a_missing_quote_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(quote=_quote(None, None, NOW - timedelta(seconds=1)))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("market_data_usable" in b for b in report.blockers)


def test_a_stale_quote_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            quote=_quote(
                Decimal("100"), Decimal("100.05"), NOW - timedelta(minutes=5)
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("stale" in gate.detail for gate in report.gates)


def test_a_quote_with_no_timestamp_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(quote=_quote(Decimal("100"), Decimal("100.05"), None))
    )
    assert report.readiness is TradingReadiness.BLOCKED


def test_a_crossed_market_blocks_when_prohibited() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            quote=_quote(
                Decimal("100.10"), Decimal("100.00"), NOW - timedelta(seconds=1)
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("crossed" in gate.detail for gate in report.gates)


def test_a_locked_market_blocks_when_prohibited() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            quote=_quote(Decimal("100"), Decimal("100"), NOW - timedelta(seconds=1))
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("locked" in gate.detail for gate in report.gates)


def test_a_non_positive_quote_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            quote=_quote(Decimal("0"), Decimal("0.05"), NOW - timedelta(seconds=1))
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED


# == configured price / spread rules ==========================================


def test_a_wide_spread_blocks_under_the_configured_rule() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            quote=_quote(
                Decimal("100"), Decimal("105"), NOW - timedelta(seconds=1)
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("spread fraction" in gate.detail for gate in report.gates)


def test_a_price_below_the_configured_minimum_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            quote=_quote(
                Decimal("0.40"), Decimal("0.41"), NOW - timedelta(seconds=1)
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED


def test_unset_rules_are_not_invented() -> None:
    """An unconfigured threshold must not become an arbitrary hardcoded one."""
    report = evaluate_runtime_readiness(
        _inputs(
            rules=MarketDataRules(),
            quote=_quote(
                Decimal("0.01"), Decimal("0.02"), NOW - timedelta(seconds=1)
            ),
        )
    )
    gate = next(g for g in report.gates if g.name == "price_spread_liquidity_rules")
    assert gate.satisfied


# == sizing and risk come from the real risk engine ===========================


def test_a_risk_veto_blocks() -> None:
    report = evaluate_runtime_readiness(_inputs(risk_result=_risk(outcome="VETO")))
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("risk_limits" in b for b in report.blockers)


def test_a_missing_risk_evaluation_blocks_both_sizing_and_risk() -> None:
    report = evaluate_runtime_readiness(_inputs(risk_result=None))
    assert report.readiness is TradingReadiness.BLOCKED
    blocked = " ".join(report.blockers)
    assert "risk_limits" in blocked and "position_sizing" in blocked


def test_a_zero_approved_quantity_blocks_sizing() -> None:
    report = evaluate_runtime_readiness(
        _inputs(risk_result=_risk(quantity=Decimal(0)))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("position_sizing" in b for b in report.blockers)


def test_a_reduced_risk_outcome_is_acceptable() -> None:
    report = evaluate_runtime_readiness(
        _inputs(risk_result=_risk(outcome="REDUCE", quantity=Decimal(3)))
    )
    assert report.readiness is TradingReadiness.PAPER_READY


def test_sizing_is_read_from_the_risk_engine_not_recomputed() -> None:
    report = evaluate_runtime_readiness(
        _inputs(risk_result=_risk(quantity=Decimal(7)))
    )
    assert report.runtime_refs["approved_quantity"] == "7"
    gate = next(g for g in report.gates if g.name == "position_sizing")
    assert "from the risk engine" in gate.detail


# == protection ================================================================


def test_missing_broker_protection_capability_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(protection=ProtectionState(False, True))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("broker_side_protection" in b for b in report.blockers)


def test_a_plan_without_a_protective_stop_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(protection=ProtectionState(True, False))
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("protective_order_requirements" in b for b in report.blockers)


def test_protection_may_be_waived_only_by_the_order_class() -> None:
    report = evaluate_runtime_readiness(
        _inputs(protection=ProtectionState(False, False, protection_required=False))
    )
    assert report.readiness is TradingReadiness.PAPER_READY


# == audit =====================================================================


def test_an_unavailable_audit_sink_blocks() -> None:
    report = evaluate_runtime_readiness(_inputs(audit=_Audit(available=False)))
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("audit_trail" in b for b in report.blockers)


def test_a_missing_audit_sink_blocks() -> None:
    report = evaluate_runtime_readiness(_inputs(audit=None))
    assert report.readiness is TradingReadiness.BLOCKED


def test_an_audit_sink_that_raises_blocks() -> None:
    report = evaluate_runtime_readiness(_inputs(audit=_Audit(raises=True)))
    assert report.readiness is TradingReadiness.BLOCKED


def test_the_decision_record_carries_the_required_audit_fields() -> None:
    audit = _Audit()
    report = evaluate_runtime_readiness(_inputs(audit=audit))
    persist_readiness_decision(report, audit)
    record = audit.records[0]
    for key in (
        "mode", "strategy_id", "symbol", "security_id", "trading_readiness",
        "gates", "blockers", "evaluated_at", "runtime_refs",
    ):
        assert key in record, key
    assert len(record["gates"]) == len(MANDATORY_TRADING_GATES)  # type: ignore[arg-type]


# == optional data never blocks; authorization is separate =====================


def test_missing_gics_does_not_block_the_runtime_path() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            requirements=StrategyDataRequirements(
                strategy_id="s", uses_sector=True, gics_mode=GicsMode.FREE_RECONSTRUCTED
            ),
            gics_sector=None,
        )
    )
    assert report.readiness is TradingReadiness.PAPER_READY
    assert report.optional_context["asof_gics"] == "OPTIONAL_UNAVAILABLE"
    assert any("does not block trading" in note for note in report.notes)


def test_missing_pit_membership_does_not_block_the_runtime_path() -> None:
    report = evaluate_runtime_readiness(_inputs(pit_membership_available=False))
    assert report.readiness is TradingReadiness.PAPER_READY


def test_an_index_strategy_needs_a_current_list() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            requirements=StrategyDataRequirements(
                strategy_id="s", universe=UniverseType.SP500_CURRENT
            ),
            universe_status=UniverseListStatus.CURRENT_LIST_UNAVAILABLE,
            pit_membership_available=True,
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("current constituent list" in b for b in report.blockers)


def test_an_all_equities_strategy_ignores_the_current_list() -> None:
    report = evaluate_runtime_readiness(
        _inputs(universe_status=UniverseListStatus.CURRENT_LIST_UNAVAILABLE)
    )
    assert report.readiness is TradingReadiness.PAPER_READY


def test_readiness_is_not_authorization() -> None:
    report = evaluate_runtime_readiness(_inputs())
    assert report.readiness is TradingReadiness.PAPER_READY
    assert "does NOT authorize" in str(report.to_dict()["authorization_note"])


def test_live_readiness_uses_the_same_mandatory_gates() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            mode=TradingMode.LIVE,
            broker=BrokerSessionState(
                True, "live", TradingMode.LIVE, NOW - timedelta(seconds=1)
            ),
        )
    )
    assert report.readiness is TradingReadiness.LIVE_READY
    assert {g.name for g in report.gates} == set(MANDATORY_TRADING_GATES)


# == the defects architectural review found ===================================


def test_a_stale_session_observation_cannot_prove_connectivity() -> None:
    """observed_at merely EXISTING is not proof the session is up now."""
    report = evaluate_runtime_readiness(
        _inputs(
            broker=BrokerSessionState(
                True, "paper-poi", TradingMode.PAPER, NOW - timedelta(hours=2),
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("stale" in gate.detail for gate in report.gates)


def test_an_unhealthy_broker_health_check_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            broker=BrokerSessionState(
                True, "paper-poi", TradingMode.PAPER,
                NOW - timedelta(seconds=1), health_ok=False,
            )
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED


def test_a_configured_min_bid_size_is_actually_evaluated() -> None:
    """It used to be configurable and silently ignored."""
    report = evaluate_runtime_readiness(
        _inputs(
            rules=MarketDataRules(min_bid_size=1000),
            quote=_quote(
                Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1),
                bid_size=10,
            ),
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("bid size" in gate.detail for gate in report.gates)


def test_an_unknown_bid_size_fails_a_configured_rule_closed() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            rules=MarketDataRules(min_bid_size=100),
            quote=_quote(
                Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1)
            ),
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("refusing to ignore a configured rule" in g.detail for g in report.gates)


def test_a_satisfied_bid_size_passes() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            rules=MarketDataRules(min_bid_size=100),
            quote=_quote(
                Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1),
                bid_size=500,
            ),
        )
    )
    assert report.readiness is TradingReadiness.PAPER_READY


def test_routing_outside_the_canonical_policy_blocks() -> None:
    report = evaluate_runtime_readiness(
        _inputs(
            contract=ContractState(True, "ibkr-paper:1", "AAPL", "ARCA", "USD")
        )
    )
    assert report.readiness is TradingReadiness.BLOCKED
    assert any("routing" in gate.detail for gate in report.gates)
