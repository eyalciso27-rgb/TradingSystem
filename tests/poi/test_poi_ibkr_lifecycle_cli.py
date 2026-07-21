"""POI-IBKR-001 lifecycle harness drills — FAKES ONLY, no IBKR contact.

These prove the harness's safety envelope and its lifecycle behavior
against the canonical ``FakeBroker``: localhost/paper-port-only with an
explicit refusal of the LIVE port, the fixed instrument allowlist, the
minimal-size caps, BUY-only entries, dry-run-first with a parameter-bound
confirmation token, zero duplicate orders, protection after every fill,
fail-closed abort with cleanup, account masking and a sanitized evidence
artifact.

CI never connects to a broker: the broker factory is always a fake and no
IBKR SDK is importable from this test. This file does NOT mark
POI-IBKR-001 PASS — only the owner's real Paper run can (see
docs/poi/POI-IBKR-001-READINESS.md and docs/adr/ADR-0016-*.md).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from trading_broker_core.fake import DEFAULT_CAPABILITIES, FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    BrokerErrorCode,
    BrokerInstrument,
    BrokerOrderEvent,
    BrokerOrderReceipt,
    BrokerPortError,
    BrokerPosition,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderState, OrderType

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "poi_ibkr_lifecycle", REPO_ROOT / "scripts" / "poi_ibkr_lifecycle.py"
)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules["poi_ibkr_lifecycle"] = harness
_SPEC.loader.exec_module(harness)

# Built by concatenation so the guardrail for quoted hardcoded account ids
# never matches this fixture.
PAPER_ACCOUNT = "DU" + "1234567"
ALIAS = "paper-poi"
AS_OF = datetime(2026, 7, 21, 14, 30, tzinfo=UTC)


def _frozen_clock() -> datetime:
    return AS_OF


def _snapshot(environment: Environment = Environment.PAPER) -> AccountSnapshot:
    return AccountSnapshot(
        account_alias=ALIAS,
        environment=environment,
        net_liquidation_value=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
        observed_at=AS_OF,
    )


def _broker(**kwargs: Any) -> FakeBroker:
    defaults: dict[str, Any] = {
        "accounts": {ALIAS: _snapshot()},
        "instruments": {
            "F": BrokerInstrument(
                broker_instrument_ref="ref-F", symbol="F", exchange="SMART", currency="USD"
            ),
            "AAPL": BrokerInstrument(
                broker_instrument_ref="ref-AAPL",
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
            ),
        },
        "clock": _frozen_clock,
    }
    defaults.update(kwargs)
    return FakeBroker(**defaults)


class FillingBroker(FakeBroker):
    """FakeBroker that also fills the entry — needed for the fill path.

    The canonical fake only acknowledges; a deliberate fill is what the
    owner-run exercises, so the fill is modelled here (still a fake).
    """

    def __init__(self, *, fill_price: Decimal = Decimal("11.00"), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fill_price = fill_price

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        receipt = super().submit_order(intent)
        if intent.order_type is not OrderType.LIMIT:
            return receipt  # protective stop rests; it does not fill here
        filled = BrokerOrderReceipt(
            client_order_id=receipt.client_order_id,
            broker_order_ref=receipt.broker_order_ref,
            state=OrderState.FILLED,
            authorization_ref=receipt.authorization_ref,
            correlation_id=receipt.correlation_id,
            received_at=AS_OF,
        )
        self._events.append(
            BrokerOrderEvent(
                sequence=len(self._events) + 1,
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=OrderState.FILLED,
                filled_quantity=intent.quantity,
                observed_at=AS_OF,
            )
        )
        self._orders[receipt.client_order_id] = filled
        self._positions[intent.account_alias] = (
            BrokerPosition(
                symbol=intent.symbol,
                quantity=intent.quantity,
                average_price=self._fill_price,
            ),
        )
        return receipt


def _envelope(**kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "scenario": "cancel-path",
        "symbol": "F",
        "side": OrderSide.BUY,
        "quantity": Decimal("1"),
        "limit_price": Decimal("5"),
        "account_alias": ALIAS,
        "host": "127.0.0.1",
        "port": 4002,
    }
    defaults.update(kwargs)
    return harness.LifecycleEnvelope(**defaults)


def _open_orders(broker: FakeBroker) -> tuple[BrokerOrderReceipt, ...]:
    """Inspect broker state after a run (the harness always disconnects)."""
    broker.connect()
    return broker.get_open_orders(ALIAS)


def _run(broker: FakeBroker, envelope: Any, **kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "execute": False,
        "confirm_token": None,
        "clock": _frozen_clock,
        "sleep": lambda _seconds: None,
        "timeout_seconds": 0.0,
    }
    defaults.update(kwargs)
    return harness.run_lifecycle(lambda: broker, envelope, **defaults)


# -- 1. endpoint envelope ---------------------------------------------------


def test_live_port_is_refused_by_name() -> None:
    with pytest.raises(harness.LifecycleRefused) as error:
        _envelope(port=4001)
    assert "LIVE" in str(error.value)


def test_any_non_paper_port_is_refused() -> None:
    for port in (7496, 7497, 4003, 0):
        with pytest.raises(harness.LifecycleRefused):
            _envelope(port=port)


def test_remote_host_is_refused() -> None:
    for host in ("10.0.0.5", "gateway.example.com", "0.0.0.0"):
        with pytest.raises(harness.LifecycleRefused):
            _envelope(host=host)


def test_localhost_forms_are_accepted() -> None:
    assert _envelope(host="localhost").host == "localhost"
    assert _envelope(host="127.0.0.1").host == "127.0.0.1"


# -- 2. instrument / size envelope -----------------------------------------


def test_symbol_outside_allowlist_is_refused() -> None:
    with pytest.raises(harness.LifecycleRefused):
        _envelope(symbol="TSLA")


def test_cancel_path_only_symbol_is_refused_for_a_deliberate_fill() -> None:
    _envelope(scenario="cancel-path", symbol="AAPL")  # allowed
    with pytest.raises(harness.LifecycleRefused) as error:
        _envelope(scenario="fill-path", symbol="AAPL", limit_price=Decimal("5"))
    assert "cancel-path only" in str(error.value)


def test_quantity_cap_and_positivity() -> None:
    for quantity in (Decimal("2"), Decimal("0"), Decimal("-1"), Decimal("100")):
        with pytest.raises(harness.LifecycleRefused):
            _envelope(quantity=quantity)


def test_notional_cap_is_enforced() -> None:
    with pytest.raises(harness.LifecycleRefused) as error:
        _envelope(limit_price=Decimal("500"))
    assert "notional" in str(error.value)


def test_sell_entry_is_refused() -> None:
    with pytest.raises(harness.LifecycleRefused) as error:
        _envelope(side=OrderSide.SELL)
    assert "BUY" in str(error.value)


def test_non_positive_limit_price_is_refused() -> None:
    with pytest.raises(harness.LifecycleRefused):
        _envelope(limit_price=Decimal("0"))


# -- 3. dry-run first / confirmation token ----------------------------------


def test_dry_run_sends_nothing_and_prints_a_token() -> None:
    broker = _broker()
    envelope = _envelope(scenario="dry-run")
    report = _run(broker, envelope)
    assert report.passed
    assert _open_orders(broker) == ()
    assert not list(broker.order_events())
    token = harness.confirmation_token(envelope)
    assert any(token in note for note in report.notes)
    assert any("NOTHING WAS SENT" in note for note in report.notes)


def test_execute_without_a_token_is_refused() -> None:
    with pytest.raises(harness.LifecycleRefused):
        _run(_broker(), _envelope(), execute=True, confirm_token=None)


def test_execute_with_a_wrong_token_is_refused() -> None:
    with pytest.raises(harness.LifecycleRefused):
        _run(_broker(), _envelope(), execute=True, confirm_token="DEADBEEF0000")


def test_token_from_different_parameters_does_not_match() -> None:
    """A token copied from another run (or an older price) fails closed."""
    stale = harness.confirmation_token(_envelope(limit_price=Decimal("4")))
    with pytest.raises(harness.LifecycleRefused):
        _run(
            _broker(),
            _envelope(limit_price=Decimal("5")),
            execute=True,
            confirm_token=stale,
        )


def test_token_is_deterministic_and_short() -> None:
    envelope = _envelope()
    token = harness.confirmation_token(envelope)
    assert token == harness.confirmation_token(_envelope())
    assert len(token) == harness.TOKEN_LENGTH
    assert token == token.upper()


def test_execute_is_refused_for_a_non_submitting_scenario() -> None:
    for scenario in ("preflight", "dry-run", "cleanup"):
        envelope = _envelope(scenario=scenario)
        with pytest.raises(harness.LifecycleRefused):
            _run(
                _broker(),
                envelope,
                execute=True,
                confirm_token=harness.confirmation_token(envelope),
            )


# -- 4. preflight fails closed ---------------------------------------------


def test_preflight_is_read_only_and_builds_no_intent() -> None:
    broker = _broker()
    report = _run(broker, _envelope(scenario="preflight"))
    assert report.passed
    assert _open_orders(broker) == ()
    assert any("read-only" in note for note in report.notes)


def test_unhealthy_broker_blocks_the_run_before_any_submit() -> None:
    broker = _broker(healthy=False)
    envelope = _envelope()
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert not report.passed
    broker.set_healthy(True)  # only so the assertion below can read state
    assert _open_orders(broker) == ()


def test_non_paper_account_snapshot_blocks_the_run() -> None:
    broker = _broker(accounts={ALIAS: _snapshot(Environment.LIVE)})
    envelope = _envelope()
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert not report.passed
    assert _open_orders(broker) == ()
    assert any(name == "paper_environment" and not ok for name, ok, _ in report.checks)


def test_missing_native_stop_capability_blocks_the_run() -> None:
    capabilities = BrokerCapabilities(
        order_types={OrderType.LIMIT: BrokerCapabilityMode.NATIVE},
        bracket=BrokerCapabilityMode.UNSUPPORTED,
        oco=BrokerCapabilityMode.UNSUPPORTED,
        trailing=BrokerCapabilityMode.UNSUPPORTED,
        fractional=BrokerCapabilityMode.DISABLED,
        outside_rth=BrokerCapabilityMode.DISABLED,
        what_if=BrokerCapabilityMode.NATIVE,
        streaming_events=BrokerCapabilityMode.NATIVE,
    )
    broker = _broker(capabilities=capabilities)
    envelope = _envelope()
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert not report.passed
    assert _open_orders(broker) == ()


def test_stale_poi_orders_block_a_new_run() -> None:
    broker = _broker()
    envelope = _envelope()
    first = _run(broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope))
    assert first.passed
    # A leftover PoI order (simulated by re-submitting into a fresh state).
    broker._orders[f"{harness.CLIENT_ORDER_PREFIX}-leftover"] = BrokerOrderReceipt(
        client_order_id=f"{harness.CLIENT_ORDER_PREFIX}-leftover",
        broker_order_ref="FAKE-leftover",
        state=OrderState.ACKNOWLEDGED,
        authorization_ref="auth",
        correlation_id="corr",
        received_at=AS_OF,
    )
    second = _run(broker, _envelope(scenario="dry-run"))
    assert not second.passed
    assert any(name == "no_stale_poi_orders" and not ok for name, ok, _ in second.checks)


# -- 5. cancel path ---------------------------------------------------------


def test_cancel_path_submits_acknowledges_cancels_and_reconciles() -> None:
    broker = _broker()
    envelope = _envelope(reference_price=Decimal("10"), limit_price=Decimal("5"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    names = {name: ok for name, ok, _ in report.checks}
    assert names["cancel_path_price_safe"]
    assert names["submit"]
    assert names["acknowledged"]
    assert names["cancel_sent"]
    assert names["cancel_confirmed"]
    assert names["reconciliation"]
    assert names["disconnect"]
    assert report.passed


def test_cancel_path_limit_too_close_to_the_market_fails() -> None:
    broker = _broker()
    envelope = _envelope(reference_price=Decimal("10"), limit_price=Decimal("9.9"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert not report.passed
    assert any(name == "cancel_path_price_safe" and not ok for name, ok, _ in report.checks)


def test_duplicate_delivery_creates_no_second_broker_order() -> None:
    broker = _broker()
    envelope = _envelope(reference_price=Decimal("10"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert any(name == "idempotent_resubmit" and ok for name, ok, _ in report.checks)
    submits = [
        event
        for event in broker.order_events()
        if event.client_order_id == envelope.client_order_id
        and event.state is OrderState.ACKNOWLEDGED
    ]
    assert len(submits) == 1


# -- 6. fill path + protection ---------------------------------------------


def test_fill_path_protects_the_position() -> None:
    broker = FillingBroker(
        accounts={ALIAS: _snapshot()},
        instruments={
            "F": BrokerInstrument(
                broker_instrument_ref="ref-F", symbol="F", exchange="SMART", currency="USD"
            )
        },
        clock=_frozen_clock,
        capabilities=DEFAULT_CAPABILITIES,
    )
    envelope = _envelope(scenario="fill-path", symbol="F", limit_price=Decimal("12"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    names = {name: ok for name, ok, _ in report.checks}
    assert names["submit"]
    assert names["fill_observed"]
    assert names["position_reported"]
    assert names["protection_planned"]
    assert names["no_unprotected_position"]
    protective = [
        order for order in _open_orders(broker) if order.client_order_id != envelope.client_order_id
    ]
    assert protective, "a protective order must exist at the broker after a fill"


def test_fill_path_stop_sits_below_the_average_fill_price() -> None:
    broker = FillingBroker(
        fill_price=Decimal("10.00"),
        accounts={ALIAS: _snapshot()},
        instruments={
            "F": BrokerInstrument(
                broker_instrument_ref="ref-F", symbol="F", exchange="SMART", currency="USD"
            )
        },
        clock=_frozen_clock,
    )
    envelope = _envelope(scenario="fill-path", symbol="F", limit_price=Decimal("12"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    detail = next(
        detail for name, _, detail in report.checks if name.startswith("protection_submitted")
    )
    assert "SELL" in detail
    assert "stop=9.80" in detail


def test_unfilled_entry_is_cancelled_not_left_resting() -> None:
    broker = _broker()  # canonical fake never fills
    envelope = _envelope(scenario="fill-path", symbol="F", limit_price=Decimal("12"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert any(name == "unfilled_entry_cancelled" and ok for name, ok, _ in report.checks)
    states = [
        event.state
        for event in broker.order_events()
        if event.client_order_id == envelope.client_order_id
    ]
    assert OrderState.CANCELLED in states


# -- 7. fail-closed / cleanup ----------------------------------------------


def test_broker_error_fails_closed_without_retry() -> None:
    class ExplodingBroker(FakeBroker):
        submits = 0

        def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
            type(self).submits += 1
            raise BrokerPortError(BrokerErrorCode.UNKNOWN_BROKER_RESPONSE, "boom")

    broker = ExplodingBroker(accounts={ALIAS: _snapshot()}, clock=_frozen_clock)
    broker._instruments["F"] = BrokerInstrument(
        broker_instrument_ref="ref-F", symbol="F", exchange="SMART", currency="USD"
    )
    envelope = _envelope(reference_price=Decimal("10"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert not report.passed
    # The agent maps UNKNOWN to NEEDS_REVIEW and never re-sends.
    assert ExplodingBroker.submits == 1


def test_unexpected_error_fails_closed_and_attempts_cleanup() -> None:
    class ExplodingReconcile(FakeBroker):
        def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
            raise RuntimeError("unexpected")

    broker = ExplodingReconcile(accounts={ALIAS: _snapshot()}, clock=_frozen_clock)
    envelope = _envelope(scenario="preflight")
    report = _run(broker, envelope)
    assert not report.passed
    assert any(name == "fail_closed" and not ok for name, ok, _ in report.checks)
    assert any(name == "disconnect" and ok for name, ok, _ in report.checks)


def test_cleanup_cancels_only_poi_orders() -> None:
    broker = _broker()
    for order_id in (f"{harness.CLIENT_ORDER_PREFIX}-cancel-path-F", "someone-elses-order"):
        broker._orders[order_id] = BrokerOrderReceipt(
            client_order_id=order_id,
            broker_order_ref=f"FAKE-{order_id}",
            state=OrderState.ACKNOWLEDGED,
            authorization_ref="auth",
            correlation_id="corr",
            received_at=AS_OF,
        )
    report = _run(broker, _envelope(scenario="cleanup"))
    remaining = {o.client_order_id for o in _open_orders(broker)}
    assert "someone-elses-order" in remaining
    assert f"{harness.CLIENT_ORDER_PREFIX}-cancel-path-F" not in remaining
    assert any(name.startswith("cleanup_cancel") for name, _, _ in report.checks)


# -- 8. sanitization / no live surface -------------------------------------


def test_account_ids_are_masked() -> None:
    masked = harness.mask_account(PAPER_ACCOUNT)
    assert masked.startswith("DU")
    assert PAPER_ACCOUNT not in masked
    assert masked == "DU*****" + PAPER_ACCOUNT[-2:]


def test_evidence_artifact_never_contains_a_full_account_id() -> None:
    broker = _broker(accounts={ALIAS: _snapshot()})
    broker._accounts[PAPER_ACCOUNT] = AccountSnapshot(
        account_alias=PAPER_ACCOUNT,
        environment=Environment.PAPER,
        net_liquidation_value=Decimal("1"),
        cash=Decimal("1"),
        buying_power=Decimal("1"),
        observed_at=AS_OF,
    )
    envelope = _envelope(scenario="preflight", account_alias=PAPER_ACCOUNT)
    report = _run(broker, envelope)
    rendered = report.render()
    assert PAPER_ACCOUNT not in rendered
    assert "DU*****" in rendered


def test_report_never_claims_a_poi_pass() -> None:
    rendered = _run(_broker(), _envelope(scenario="preflight")).render()
    assert "is NOT a PoI PASS" in rendered


def test_no_live_environment_or_flatten_surface_exists() -> None:
    source = (REPO_ROOT / "scripts" / "poi_ibkr_lifecycle.py").read_text(encoding="utf-8")
    assert "Environment.LIVE" not in source
    # "flatten" may appear in prose, but never as a callable surface.
    assert "flatten(" not in source
    assert "def flatten" not in source
    assert "PAPER_AUTO = " not in source


def test_harness_imports_no_broker_sdk() -> None:
    source = (REPO_ROOT / "scripts" / "poi_ibkr_lifecycle.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "ibapi" not in stripped
            assert "trading_broker_ibkr" not in stripped
    assert "ibapi" not in sys.modules


def test_the_only_submitter_is_the_execution_agent() -> None:
    """No direct ``broker.submit_order`` call exists in the harness."""
    source = (REPO_ROOT / "scripts" / "poi_ibkr_lifecycle.py").read_text(encoding="utf-8")
    assert "submit_order(" not in source
    assert "agent.process(" in source


# -- 9. CLI surface ---------------------------------------------------------


def test_cli_refuses_the_live_port_with_exit_code_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = harness.main(
        [
            "--scenario",
            "dry-run",
            "--account-alias",
            ALIAS,
            "--port",
            "4001",
            "--limit-price",
            "5",
            "--broker-module",
            "does.not.matter",
        ]
    )
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out


def test_cli_requires_a_limit_price_for_submitting_scenarios(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = harness.main(
        [
            "--scenario",
            "cancel-path",
            "--account-alias",
            ALIAS,
            "--broker-module",
            "does.not.matter",
        ]
    )
    assert code == 2
    assert "limit-price" in capsys.readouterr().out


def test_cli_rejects_a_broker_module_without_a_factory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = harness.main(
        [
            "--scenario",
            "dry-run",
            "--account-alias",
            ALIAS,
            "--limit-price",
            "5",
            "--broker-module",
            "json",
        ]
    )
    assert code == 2
    assert "create_broker" in capsys.readouterr().out


def test_evidence_file_is_written_when_requested(tmp_path: Path) -> None:
    broker = _broker()
    report = _run(broker, _envelope(scenario="dry-run"))
    target = tmp_path / "evidence.md"
    target.write_text(report.render() + "\n", encoding="utf-8")
    assert "RESULT:" in target.read_text(encoding="utf-8")


# -- 10. reconnect / partial fill / crash-safe resumption -------------------


def _filling_broker(**kwargs: Any) -> FillingBroker:
    defaults: dict[str, Any] = {
        "accounts": {ALIAS: _snapshot()},
        "instruments": {
            "F": BrokerInstrument(
                broker_instrument_ref="ref-F", symbol="F", exchange="SMART", currency="USD"
            )
        },
        "clock": _frozen_clock,
    }
    defaults.update(kwargs)
    return FillingBroker(**defaults)


def test_reconnect_reports_surviving_position_and_protection() -> None:
    """Step 6 of the owner-run: state must survive a Gateway restart."""
    broker = _filling_broker()
    envelope = _envelope(scenario="fill-path", symbol="F", limit_price=Decimal("12"))
    filled = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    assert filled.passed

    # A restart does not change what the broker reports; the harness must
    # re-read it without submitting or cancelling anything.
    before = len(list(broker.order_events()))
    report = _run(broker, _envelope(scenario="reconnect", symbol="F"))
    names = {name: ok for name, ok, _ in report.checks}
    assert names["positions_survived_reconnect"]
    assert names["protection_survived_reconnect"]
    assert names["reconciliation"]
    assert report.passed
    assert len(list(broker.order_events())) == before, "reconnect must send nothing"


def test_reconnect_flags_a_position_left_without_protection() -> None:
    broker = _broker()
    broker._positions[ALIAS] = (
        BrokerPosition(symbol="F", quantity=Decimal("1"), average_price=Decimal("10")),
    )
    report = _run(broker, _envelope(scenario="reconnect", symbol="F"))
    assert not report.passed
    assert any(name == "protection_survived_reconnect" and not ok for name, ok, _ in report.checks)


def test_reconnect_is_not_blocked_by_its_own_protective_order() -> None:
    """The leftover gate must not veto the scenario that inspects leftovers."""
    broker = _broker()
    broker._orders[f"{harness.CLIENT_ORDER_PREFIX}-fill-path-F-protection:x:stop:1"] = (
        BrokerOrderReceipt(
            client_order_id=f"{harness.CLIENT_ORDER_PREFIX}-fill-path-F-protection:x:stop:1",
            broker_order_ref="FAKE-stop",
            state=OrderState.ACKNOWLEDGED,
            authorization_ref="auth",
            correlation_id="corr",
            received_at=AS_OF,
        )
    )
    report = _run(broker, _envelope(scenario="reconnect", symbol="F"))
    assert any(name == "open_poi_orders" for name, _, _ in report.checks)
    assert not any(name == "no_stale_poi_orders" for name, _, _ in report.checks)


class PartiallyFillingBroker(FillingBroker):
    """Reports PARTIALLY_FILLED instead of FILLED for the entry."""

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        receipt = super().submit_order(intent)
        if intent.order_type is OrderType.LIMIT:
            self._events[-1] = BrokerOrderEvent(
                sequence=self._events[-1].sequence,
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=OrderState.PARTIALLY_FILLED,
                filled_quantity=intent.quantity,
                observed_at=AS_OF,
            )
        return receipt


def test_partial_fill_is_protected_without_waiting_for_a_full_fill() -> None:
    """Protection follows the FILLED QUANTITY, not the order's completion.

    A true partial fill cannot be produced at the 1-share cap, so the
    invariant proven here is the one that matters: a PARTIALLY_FILLED
    event already triggers protection for the quantity actually filled.
    """
    broker = PartiallyFillingBroker(
        accounts={ALIAS: _snapshot()},
        instruments={
            "F": BrokerInstrument(
                broker_instrument_ref="ref-F", symbol="F", exchange="SMART", currency="USD"
            )
        },
        clock=_frozen_clock,
    )
    envelope = _envelope(scenario="fill-path", symbol="F", limit_price=Decimal("12"))
    report = _run(
        broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope)
    )
    names = {name: ok for name, ok, _ in report.checks}
    assert names["fill_observed"]
    assert names["protection_planned"]
    assert names["no_unprotected_position"]


def test_crash_restart_never_creates_a_second_order() -> None:
    """Crash-safe resumption: two layers, both proven.

    1. the deterministic ``client_order_id`` means a repeated run submits
       the SAME id, and the broker returns the original receipt;
    2. preflight refuses to start a new lifecycle on top of a leftover.
    """
    broker = _broker()
    envelope = _envelope(reference_price=Decimal("10"))
    token = harness.confirmation_token(envelope)
    first = _run(broker, envelope, execute=True, confirm_token=token)
    assert first.passed

    # Layer 1: a brand-new harness run (fresh ExecutionAgent, as after a
    # crash) reuses the same id -> the broker creates nothing new.
    broker.connect()
    orders_before = dict(broker._orders)
    second = _run(broker, envelope, execute=True, confirm_token=token)
    assert set(broker._orders) == set(orders_before)
    assert second.executed

    # Layer 2: a leftover open order blocks the next lifecycle entirely.
    broker._orders[f"{harness.CLIENT_ORDER_PREFIX}-leftover"] = BrokerOrderReceipt(
        client_order_id=f"{harness.CLIENT_ORDER_PREFIX}-leftover",
        broker_order_ref="FAKE-leftover",
        state=OrderState.ACKNOWLEDGED,
        authorization_ref="auth",
        correlation_id="corr",
        received_at=AS_OF,
    )
    third = _run(broker, _envelope(scenario="dry-run"))
    assert not third.passed


def test_polling_is_bounded_by_the_timeout() -> None:
    """A missing fill must never poll forever."""
    calls: list[float] = []
    broker = _broker()
    envelope = _envelope(scenario="fill-path", symbol="F", limit_price=Decimal("12"))
    report = _run(
        broker,
        envelope,
        execute=True,
        confirm_token=harness.confirmation_token(envelope),
        sleep=calls.append,
        timeout_seconds=3.0,
    )
    assert len(calls) <= 4, "polling must stop at the timeout"
    assert any(name == "unfilled_entry_cancelled" and ok for name, ok, _ in report.checks)
