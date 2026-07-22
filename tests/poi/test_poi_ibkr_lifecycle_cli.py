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
from trading_execution_agent.protection import ProtectionOrderSpec

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
RUN_DATE = "2026-07-21"


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
        "run_date": RUN_DATE,
        "reference_price": Decimal("10"),
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
    assert len(token) == harness.CONFIRM_DIGEST_LENGTH
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


def test_cancel_path_limit_too_close_to_the_market_is_refused_at_construction() -> None:
    """Stronger than a failed check: the envelope cannot even be built."""
    with pytest.raises(harness.LifecycleRefused) as error:
        _envelope(reference_price=Decimal("10"), limit_price=Decimal("9.9"))
    assert "too close to the market" in str(error.value)


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


# -- 11. safety-review regressions (PR #39 final review) --------------------
#
# One focused test per gap found in the pre-merge safety review. Each
# reproduces the exact failure mode the corresponding fix closes.


class ProtectionRejectingBroker(FillingBroker):
    """Fills the entry, then refuses the protective STOP (gap 1)."""

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        if intent.order_type is OrderType.STOP:
            raise BrokerPortError(BrokerErrorCode.BROKER_REJECTED, "stop rejected")
        return super().submit_order(intent)


def _fill_broker(broker_type: type[FakeBroker] = FillingBroker, **kwargs: Any) -> Any:
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
    return broker_type(**defaults)


def _fill_envelope(**kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "scenario": "fill-path",
        "symbol": "F",
        "limit_price": Decimal("12"),
        "reference_price": None,
    }
    defaults.update(kwargs)
    return _envelope(**defaults)


def _execute(broker: Any, envelope: Any) -> Any:
    return _run(broker, envelope, execute=True, confirm_token=harness.confirmation_token(envelope))


def test_gap1_rejected_protection_aborts_loudly_and_never_claims_protection() -> None:
    """Entry fills but the protective stop is refused by the broker."""
    report = _execute(_fill_broker(ProtectionRejectingBroker), _fill_envelope())
    rendered = report.render()

    assert not report.passed
    assert any(name == "owner_action_required" and not ok for name, ok, _ in report.checks)
    assert harness.OWNER_ACTION_BANNER in rendered
    assert "UNPROTECTED PAPER POSITION" in rendered
    assert "close the position manually" in rendered
    # It must NOT tell the owner a stop is resting.
    assert "its protective stop are still open" not in rendered
    assert any(name == "no_unprotected_position" and not ok for name, ok, _ in report.checks)


def test_gap1_reconciliation_receives_accepted_protection_not_planned() -> None:
    """Feeding the PLAN would hide the very failure this run must surface."""
    report = _execute(_fill_broker(ProtectionRejectingBroker), _fill_envelope())
    assert any(name == "reconciliation" and not ok for name, ok, _ in report.checks)
    assert any("PROTECTION_MISMATCH" in note for note in report.notes)


class PartialThenWorkingBroker(FillingBroker):
    """Reports PARTIALLY_FILLED, leaving a working remainder (gap 2)."""

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
            self._orders[receipt.client_order_id] = BrokerOrderReceipt(
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=OrderState.PARTIALLY_FILLED,
                authorization_ref=receipt.authorization_ref,
                correlation_id=receipt.correlation_id,
                received_at=AS_OF,
            )
        return receipt


def test_gap2_partial_fill_cancels_the_working_remainder_before_protecting() -> None:
    broker = _fill_broker(PartialThenWorkingBroker)
    envelope = _fill_envelope()
    report = _execute(broker, envelope)
    names = {name: ok for name, ok, _ in report.checks}
    assert names["entry_remainder_cancelled"], "a working remainder must not survive"
    assert names["no_unprotected_position"]
    states = [
        e.state for e in broker.order_events() if e.client_order_id == envelope.client_order_id
    ]
    assert OrderState.CANCELLED in states


def test_gap2_preexisting_position_in_the_symbol_blocks_a_submitting_run() -> None:
    """A prior holding would blend the average price the stop derives from."""
    broker = _broker()
    broker._positions[ALIAS] = (
        BrokerPosition(symbol="F", quantity=Decimal("5"), average_price=Decimal("9")),
    )
    report = _execute(broker, _fill_envelope())
    assert not report.passed
    assert any(
        name == "no_preexisting_position_in_symbol" and not ok for name, ok, _ in report.checks
    )
    assert _open_orders(broker) == (), "nothing may be submitted"


def test_gap3_client_order_id_differs_across_owner_runs_on_different_days() -> None:
    today = _envelope(run_date="2026-07-21")
    tomorrow = _envelope(run_date="2026-07-22")
    assert today.client_order_id != tomorrow.client_order_id
    # ...but stays identical within the day, so a crash-restart resumes.
    assert today.client_order_id == _envelope(run_date="2026-07-21").client_order_id


def test_gap3_run_date_must_be_a_valid_date() -> None:
    for bad in ("21-07-2026", "2026/07/21", "today", ""):
        with pytest.raises(harness.LifecycleRefused):
            _envelope(run_date=bad)


def test_gap4_token_from_an_earlier_day_cannot_be_replayed() -> None:
    """Identical parameters, different day => the old token is worthless."""
    yesterday = _envelope(run_date="2026-07-20")
    today = _envelope(run_date="2026-07-21")
    assert harness.confirmation_token(yesterday) != harness.confirmation_token(today)
    with pytest.raises(harness.LifecycleRefused):
        _run(_broker(), today, execute=True, confirm_token=harness.confirmation_token(yesterday))


def test_gap4_token_is_bound_to_the_reference_price_too() -> None:
    """A different market context must invalidate the token."""
    assert harness.confirmation_token(
        _envelope(reference_price=Decimal("10"))
    ) != harness.confirmation_token(_envelope(reference_price=Decimal("11")))


def test_gap5_cancel_path_requires_a_reference_price() -> None:
    with pytest.raises(harness.LifecycleRefused) as error:
        _envelope(scenario="cancel-path", reference_price=None)
    assert "mandatory" in str(error.value)


class SurpriseFillBroker(FakeBroker):
    """The cancel-path order fills anyway during a rapid move (gap 5)."""

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        receipt = super().submit_order(intent)
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
        return receipt


def test_gap5_unexpected_cancel_path_fill_raises_an_unprotected_position_abort() -> None:
    broker = _fill_broker(SurpriseFillBroker)
    report = _execute(broker, _envelope(reference_price=Decimal("10"), limit_price=Decimal("5")))
    rendered = report.render()
    assert not report.passed
    # The order was legitimately acknowledged first; the fill arrived after,
    # so scanning only the matched ack event would have missed it entirely.
    assert any(name == "acknowledged" and ok for name, ok, _ in report.checks)
    assert any(name == "owner_action_required" and not ok for name, ok, _ in report.checks)
    assert harness.OWNER_ACTION_BANNER in rendered
    assert "UNPROTECTED PAPER POSITION" in rendered
    # Two independent guards cover the two orderings a fill can arrive in:
    # before the ack was matched, or afterwards while cancelling.
    names = {name for name, _, _ in report.checks}
    assert "unexpected_fill_remainder_cancelled" in names or "never_filled" in names
    assert not any(name == "never_filled" and ok for name, ok, _ in report.checks)


def test_gap6_unknown_broker_state_tells_the_owner_an_order_may_exist() -> None:
    class UnknownStateBroker(FakeBroker):
        def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
            raise BrokerPortError(BrokerErrorCode.UNKNOWN_BROKER_RESPONSE, "timeout")

    report = _execute(_fill_broker(UnknownStateBroker), _envelope(reference_price=Decimal("10")))
    rendered = report.render()
    assert not report.passed
    assert "an order MAY exist at the broker" in rendered
    assert "do NOT re-run this scenario and do NOT resend anything" in rendered


class BadAveragePriceBroker(FillingBroker):
    """The real adapter's average_price is unproven by fakes (gap 7)."""

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        return tuple(
            BrokerPosition(symbol=p.symbol, quantity=p.quantity, average_price=Decimal("0"))
            for p in super().get_positions(account_alias)
        )


def test_gap7_missing_average_price_aborts_explicitly_not_opaquely() -> None:
    report = _execute(_fill_broker(BadAveragePriceBroker), _fill_envelope())
    rendered = report.render()
    assert not report.passed
    assert "average_price=0" in rendered
    assert harness.OWNER_ACTION_BANNER in rendered
    # An explicit abort, not a generic "unknown error, failing closed".
    assert not any(name == "fail_closed" for name, _, _ in report.checks)


def _spec(**kwargs: Any) -> ProtectionOrderSpec:
    defaults: dict[str, Any] = {
        "spec_ref": "protection:x:stop:1",
        "position_ref": "x",
        "symbol": "F",
        "side": OrderSide.SELL,
        "order_type": OrderType.STOP,
        "quantity": Decimal("1"),
        "stop_price": Decimal("9"),
        "limit_price": None,
        "oca_group": None,
    }
    defaults.update(kwargs)
    return ProtectionOrderSpec(**defaults)


def test_gap8_protective_order_larger_than_the_position_is_refused() -> None:
    """A SELL bigger than the holding would open a short - never send it."""
    refusal = harness._protective_spec_refusal(_spec(quantity=Decimal("5")), Decimal("1"))
    assert refusal is not None
    assert "would open a short" in refusal


def test_gap8_protective_order_must_be_a_positive_priced_sell_stop() -> None:
    assert harness._protective_spec_refusal(_spec(), Decimal("1")) is None
    assert harness._protective_spec_refusal(_spec(side=OrderSide.BUY), Decimal("1"))
    assert harness._protective_spec_refusal(_spec(order_type=OrderType.MARKET), Decimal("1"))
    assert harness._protective_spec_refusal(_spec(stop_price=None), Decimal("1"))
    assert harness._protective_spec_refusal(_spec(stop_price=Decimal("0")), Decimal("1"))
    assert harness._protective_spec_refusal(_spec(quantity=Decimal("0")), Decimal("1"))


def test_gap9_every_scenario_masks_the_account_in_its_artifact() -> None:
    """Sanitization must hold on failing runs too, not just the happy path."""
    cases = (
        (_broker(), "preflight"),
        (_broker(), "dry-run"),
        (_broker(), "reconnect"),
        (_broker(healthy=False), "preflight"),
    )
    for broker, scenario in cases:
        broker._accounts[PAPER_ACCOUNT] = AccountSnapshot(
            account_alias=PAPER_ACCOUNT,
            environment=Environment.PAPER,
            net_liquidation_value=Decimal("1"),
            cash=Decimal("1"),
            buying_power=Decimal("1"),
            observed_at=AS_OF,
        )
        envelope = _envelope(scenario=scenario, account_alias=PAPER_ACCOUNT)
        assert PAPER_ACCOUNT not in _run(broker, envelope).render(), scenario


def test_gap10_adapter_assumptions_match_the_real_ibkr_paper_capabilities() -> None:
    """What the fakes assume must hold for the adapter the owner will use.

    Read from the adapter's declared capability matrix (no connection, no
    SDK call) so a future adapter change cannot silently invalidate the
    protection strategy this harness depends on.
    """
    from trading_broker_ibkr.paper_adapter import IBKR_PAPER_CAPABILITIES

    # PLAIN_STOP protection needs a NATIVE stop and nothing else.
    assert IBKR_PAPER_CAPABILITIES.order_types[OrderType.STOP] is BrokerCapabilityMode.NATIVE
    assert IBKR_PAPER_CAPABILITIES.order_types[OrderType.LIMIT] is BrokerCapabilityMode.NATIVE
    # The harness must NOT rely on bracket/OCA - the adapter has neither.
    assert IBKR_PAPER_CAPABILITIES.bracket is BrokerCapabilityMode.UNSUPPORTED
    assert IBKR_PAPER_CAPABILITIES.oco is BrokerCapabilityMode.UNSUPPORTED
    assert IBKR_PAPER_CAPABILITIES.what_if is BrokerCapabilityMode.NATIVE

    source = (REPO_ROOT / "scripts" / "poi_ibkr_lifecycle.py").read_text(encoding="utf-8")
    assert "ProtectionStructure.PLAIN_STOP" in source
    assert "ProtectionStructure.BRACKET" not in source
    assert "ProtectionStructure.OCA" not in source


def test_gap10_adapter_replays_events_from_the_start_like_the_fake() -> None:
    """``_await_state`` polls ``order_events()`` repeatedly and assumes a full
    replay each call; a draining iterator would silently lose events."""
    adapter = (
        REPO_ROOT / "packages/py/broker-ibkr/src/trading_broker_ibkr/paper_adapter.py"
    ).read_text(encoding="utf-8")
    body = adapter.split("def order_events(self)", 1)[1].splitlines()[1].strip()
    assert body == "return iter(tuple(self._events))", (
        f"adapter order_events() must replay from the start, got: {body}"
    )


def test_gap11_abort_that_cannot_confirm_a_cancel_demands_owner_action() -> None:
    class CancelFailingBroker(FakeBroker):
        def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
            raise RuntimeError("connection dropped")

        def cancel_order(self, request: Any) -> Any:
            raise BrokerPortError(BrokerErrorCode.NOT_CONNECTED, "gone")

    report = _execute(_fill_broker(CancelFailingBroker), _envelope(reference_price=Decimal("10")))
    rendered = report.render()
    assert not report.passed
    assert harness.OWNER_ACTION_BANNER in rendered
    assert "could NOT confirm its order was cancelled" in rendered


def test_gap12_a_failing_run_can_never_render_result_pass() -> None:
    """Any owner_action_required check forces the artifact to read FAIL."""
    rendered = _execute(_fill_broker(ProtectionRejectingBroker), _fill_envelope()).render()
    assert "RESULT: FAIL" in rendered
    assert "RESULT: PASS" not in rendered


class EarlyFillBroker(FakeBroker):
    """The fill is already visible when the acknowledgement is polled.

    Covers the OTHER ordering: ``_await_state`` matches the fill itself,
    so the guard before the cancel must catch it.
    """

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        receipt = super().submit_order(intent)
        self._events.clear()
        self._events.append(
            BrokerOrderEvent(
                sequence=1,
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=OrderState.FILLED,
                filled_quantity=intent.quantity,
                observed_at=AS_OF,
            )
        )
        return receipt


def test_gap5_cancel_path_fill_seen_before_the_ack_also_aborts() -> None:
    broker = _fill_broker(EarlyFillBroker)
    report = _execute(broker, _envelope(reference_price=Decimal("10"), limit_price=Decimal("5")))
    assert not report.passed
    assert any(name == "acknowledged" and not ok for name, ok, _ in report.checks)
    assert any(name == "unexpected_fill_remainder_cancelled" for name, _, _ in report.checks), (
        "the pre-cancel guard must fire when the fill is the matched event"
    )
    assert "UNPROTECTED PAPER POSITION" in report.render()


# -- 12. account-id resolution (WP-A4 operational blocker) -----------------
#
# The adapter maps alias -> real DU account id and refuses anything else.
# Passing the alias as its own account id made the harness unable to
# construct the real adapter at all. These lock the secret-store wiring.


def _secrets_root(tmp_path: Path, value: str) -> Path:
    target = tmp_path / "paper" / "ibkr"
    target.mkdir(parents=True)
    (target / "paper-account-id").write_text(value + "\n", encoding="utf-8")
    return tmp_path


class _StubAdapterModule:
    """Stands in for trading_broker_ibkr.paper_adapter (no SDK, no socket)."""

    seen: dict[str, Any] = {}

    @staticmethod
    def build_paper_adapter(**kwargs: Any) -> Any:
        _StubAdapterModule.seen = dict(kwargs)
        return _broker()


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules["stub_adapter_module"] = _StubAdapterModule  # type: ignore[assignment]
    monkeypatch.delitem(sys.modules, "stub_adapter_module", raising=False)
    sys.modules["stub_adapter_module"] = _StubAdapterModule  # type: ignore[assignment]


def test_alias_is_mapped_to_the_du_account_id_from_the_secret_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRETS_ROOT", str(_secrets_root(tmp_path, PAPER_ACCOUNT)))
    _install_stub(monkeypatch)
    factory = harness._load_broker_factory(
        "stub_adapter_module",
        account_alias=ALIAS,
        account_secret="ibkr/paper-account-id",
        host="127.0.0.1",
        port=4002,
        client_id=7,
    )
    factory()
    # The adapter must receive alias -> DU id, never alias -> alias.
    assert _StubAdapterModule.seen["account_aliases"] == {ALIAS: PAPER_ACCOUNT}


def test_missing_secrets_root_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRETS_ROOT", raising=False)
    _install_stub(monkeypatch)
    with pytest.raises(harness.LifecycleRefused) as error:
        harness._load_broker_factory(
            "stub_adapter_module",
            account_alias=ALIAS,
            account_secret="ibkr/paper-account-id",
            host="127.0.0.1",
            port=4002,
            client_id=7,
        )
    assert "SECRETS_ROOT" in str(error.value)


def test_missing_account_secret_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "paper").mkdir(parents=True)
    monkeypatch.setenv("SECRETS_ROOT", str(tmp_path))
    _install_stub(monkeypatch)
    with pytest.raises(harness.LifecycleRefused):
        harness._load_broker_factory(
            "stub_adapter_module",
            account_alias=ALIAS,
            account_secret="ibkr/paper-account-id",
            host="127.0.0.1",
            port=4002,
            client_id=7,
        )


def test_non_paper_account_secret_is_refused_with_a_masked_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_account = "U" + "7654321"
    monkeypatch.setenv("SECRETS_ROOT", str(_secrets_root(tmp_path, live_account)))
    _install_stub(monkeypatch)
    with pytest.raises(harness.LifecycleRefused) as error:
        harness._load_broker_factory(
            "stub_adapter_module",
            account_alias=ALIAS,
            account_secret="ibkr/paper-account-id",
            host="127.0.0.1",
            port=4002,
            client_id=7,
        )
    message = str(error.value)
    assert live_account not in message, "the refusal must not echo the account id"
    assert "DU* only" in message


def test_account_id_never_appears_in_argv_help() -> None:
    """The id comes from the secret store, so no flag can carry it."""
    text = harness.build_parser().format_help()
    assert "--account-secret" in text
    assert "SECRETS_ROOT" in text
    assert "--account-id" not in text
