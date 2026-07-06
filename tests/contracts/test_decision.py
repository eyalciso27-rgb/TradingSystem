"""Positive and negative tests for decision/risk/order contracts (IMP-0104).

Done criterion: no broker-specific field appears in the domain schemas.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError
from trading_contracts.decision import (
    DECISION_MODELS,
    InstrumentRef,
    NormalizedBrokerEvent,
    OrderIntent,
    RiskDecision,
    SignalAssessment,
    TradePlan,
)
from trading_contracts.enums import (
    ActionTier,
    AssessmentStatus,
    CalibrationStatus,
    Environment,
    OperatingMode,
    OrderRole,
    OrderState,
    OrderType,
    RiskDecisionOutcome,
    SignalDirection,
    SignalFamily,
    SourceEntryType,
    TradePlanStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISION_SCHEMA_DIR = REPO_ROOT / "packages" / "schemas" / "json" / "decision"
DECISION_TS_FILE = REPO_ROOT / "packages" / "ts" / "contracts-generated" / "src" / "decision.ts"

NOW = "2026-07-05T10:00:00+00:00"
HASH = "sha256:" + "d" * 64


def _instrument(**o: Any) -> dict[str, Any]:
    base = {"instrument_id": str(uuid4()), "symbol": "AAPL", "currency": "USD"}
    base.update(o)
    return base


def _assessment(**o: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instrument_id": str(uuid4()),
        "signal_family": SignalFamily.TECHNICAL.value,
        "model_id": "technical.breakout.v1",
        "model_version": "1.0.0",
        "direction": SignalDirection.LONG.value,
        "status": AssessmentStatus.TRIGGERED.value,
        "score": "0.72",
        "calibration_status": CalibrationStatus.HEURISTIC.value,
        "observed_at": NOW,
        "available_at": NOW,
    }
    base.update(o)
    return base


def _plan(**o: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trade_plan_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "instrument": _instrument(),
        "direction": SignalDirection.LONG.value,
        "strategy_version": "1.0",
        "risk_policy_version": "1.0",
        "status": TradePlanStatus.APPROVED.value,
        "environment": Environment.PAPER.value,
        "thesis": "Breakout with volume",
        "invalidation_summary": "Close below 24.50",
        "entry": {
            "entry_type": SourceEntryType.BUY_STOP_TRIGGER.value,
            "order_type": OrderType.STOP.value,
            "stop_trigger_price": "25.00",
        },
        "protective_stop": {"stop_price": "23.00", "anchor": "ENTRY_FILL"},
        "profit_targets": [
            {"price": "28.00", "quantity_fraction": "0.5"},
            {"price": "30.00", "quantity_fraction": "0.5"},
        ],
        "max_loss_amount": "200.00",
        "currency": "USD",
        "valid_from": NOW,
    }
    base.update(o)
    return base


def _risk(**o: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "risk_decision_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "trade_plan_id": str(uuid4()),
        "outcome": RiskDecisionOutcome.APPROVE.value,
        "approved_quantity": "100",
        "approved_risk_amount": "200.00",
        "currency": "USD",
        "risk_policy_version": "1.0",
        "evaluated_at": NOW,
    }
    base.update(o)
    return base


def _intent(**o: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "intent_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "trade_plan_id": str(uuid4()),
        "risk_decision_id": str(uuid4()),
        "strategy_version": "1.0",
        "instrument": _instrument(),
        "order_role": OrderRole.ENTRY.value,
        "side": "BUY",
        "order_type": OrderType.STOP.value,
        "quantity": "100",
        "stop_price": "25.00",
        "time_in_force": "DAY",
        "environment": Environment.PAPER.value,
        "operating_mode": OperatingMode.PAPER_MANUAL.value,
        "account_ref": "account-ref-1",
        "idempotency_key": "idem-1",
    }
    base.update(o)
    return base


def _broker_event(**o: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "intent_id": str(uuid4()),
        "order_state": OrderState.PARTIALLY_FILLED.value,
        "external_order_ref": "ext-order-1",
        "filled_quantity": "40",
        "remaining_quantity": "60",
        "last_fill_quantity": "40",
        "average_fill_price": "25.10",
        "currency": "USD",
    }
    base.update(o)
    return base


# --- Positive ---

def test_valid_instrument_ref() -> None:
    ref = InstrumentRef.model_validate(_instrument())
    assert ref.symbol == "AAPL"


def test_valid_assessment_uses_canonical_enums() -> None:
    a = SignalAssessment.model_validate(_assessment())
    assert a.signal_family is SignalFamily.TECHNICAL
    assert a.calibration_status is CalibrationStatus.HEURISTIC
    assert isinstance(a.score, Decimal)


def test_valid_trade_plan_has_no_quantity() -> None:
    plan = TradePlan.model_validate(_plan())
    assert "quantity" not in TradePlan.model_fields
    assert plan.max_loss_amount == Decimal("200.00")


def test_valid_risk_approval() -> None:
    risk = RiskDecision.model_validate(_risk())
    assert risk.outcome is RiskDecisionOutcome.APPROVE
    assert risk.approved_quantity == Decimal("100")


def test_valid_risk_veto() -> None:
    risk = RiskDecision.model_validate(
        _risk(
            outcome=RiskDecisionOutcome.VETO.value,
            approved_quantity=None,
            approved_risk_amount=None,
            veto_reason_codes=["RISK_LIMIT_REACHED"],
        )
    )
    assert risk.outcome is RiskDecisionOutcome.VETO
    assert risk.approved_quantity is None


def test_risk_can_veto_even_when_decision_is_approve() -> None:
    # DecisionOutcome APPROVE is independent; the risk engine still vetoes.
    risk = RiskDecision.model_validate(
        _risk(
            outcome=RiskDecisionOutcome.VETO.value,
            approved_quantity=None,
            approved_risk_amount=None,
            veto_reason_codes=["NO_EXIT_PLAN"],
        )
    )
    assert risk.outcome is RiskDecisionOutcome.VETO


def test_valid_order_intent() -> None:
    intent = OrderIntent.model_validate(_intent())
    assert intent.quantity == Decimal("100")
    assert intent.environment is Environment.PAPER


def test_valid_normalized_broker_event() -> None:
    event = NormalizedBrokerEvent.model_validate(_broker_event())
    assert event.order_state is OrderState.PARTIALLY_FILLED
    assert event.filled_quantity == Decimal("40")


def test_broker_event_has_no_envelope_timestamps() -> None:
    # occurred/observed/available/ingested are carried by EventEnvelope, not
    # duplicated in the payload.
    for field in ("occurred_at", "observed_at", "available_at", "ingested_at"):
        assert field not in NormalizedBrokerEvent.model_fields


@pytest.mark.parametrize("model", DECISION_MODELS, ids=lambda m: m.__name__)
def test_round_trip(model: type[BaseModel]) -> None:
    builders = {
        "InstrumentRef": _instrument,
        "SignalAssessment": _assessment,
        "TradePlan": _plan,
        "RiskDecision": _risk,
        "OrderIntent": _intent,
        "NormalizedBrokerEvent": _broker_event,
    }
    if model.__name__ not in builders:
        pytest.skip("covered via its parent model")
    obj = model.model_validate(builders[model.__name__]())
    restored = model.model_validate_json(obj.model_dump_json())
    assert restored == obj


def test_money_serializes_as_decimal_string_not_float() -> None:
    intent = OrderIntent.model_validate(_intent(quantity="100.5", stop_price="25.25"))
    payload = json.loads(intent.model_dump_json())
    assert payload["quantity"] == "100.5"
    assert payload["stop_price"] == "25.25"
    assert isinstance(payload["quantity"], str)


# --- Negative ---

def test_missing_required_field_rejected() -> None:
    payload = _intent()
    del payload["quantity"]
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(payload)


def test_null_in_required_field_rejected() -> None:
    payload = _intent()
    payload["quantity"] = None
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(payload)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(_intent(surprise="x"))


@pytest.mark.parametrize("bad", ["0", "-5", "-0.01"])
def test_non_positive_quantity_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(_intent(quantity=bad))


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_nan_infinity_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(_intent(quantity=bad))


def test_negative_filled_quantity_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedBrokerEvent.model_validate(_broker_event(filled_quantity="-1"))


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        SignalAssessment.model_validate(_assessment(observed_at="2026-07-05T10:00:00"))


def test_invalid_enum_value_rejected() -> None:
    payload = _assessment()
    payload["signal_family"] = "ASTROLOGY"
    with pytest.raises(ValidationError):
        SignalAssessment.model_validate(payload)


def test_invalid_decision_outcome_rejected() -> None:
    from trading_contracts.decision import DecisionResult

    payload = {
        "decision_id": str(uuid4()),
        "candidate_id": str(uuid4()),
        "outcome": "MAYBE",
        "action_tier": ActionTier.MANUAL.value,
        "strategy_version": "1.0",
        "decision_policy_version": "1.0",
        "decided_at": NOW,
        "decision_hash": HASH,
    }
    with pytest.raises(ValidationError):
        DecisionResult.model_validate(payload)


def test_live_auto_operating_mode_does_not_exist() -> None:
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(_intent(operating_mode="LIVE_AUTO"))


def test_inconsistent_environment_operating_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        OrderIntent.model_validate(
            _intent(
                environment=Environment.LIVE.value,
                operating_mode=OperatingMode.PAPER_MANUAL.value,
            )
        )


def test_risk_veto_with_approved_quantity_rejected() -> None:
    with pytest.raises(ValidationError):
        RiskDecision.model_validate(
            _risk(outcome=RiskDecisionOutcome.VETO.value, veto_reason_codes=["RISK_LIMIT_REACHED"])
        )


def test_risk_approve_without_quantity_rejected() -> None:
    with pytest.raises(ValidationError):
        RiskDecision.model_validate(_risk(approved_quantity=None))


def test_aware_python_datetime_accepted() -> None:
    a = SignalAssessment.model_validate(
        _assessment(observed_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC))
    )
    assert a.observed_at.tzinfo is not None


# --- Broker neutrality (the IMP-0104 done criterion) ---

FORBIDDEN_BROKER_TOKENS = [
    "ibkr",
    "tws",
    "conid",
    "permid",
    "clientid",
    "alpaca",
    "binance",
    "native_order",
    "broker_order_id",
    "broker_status_raw",
    "raw_broker_payload",
]


def _schema_identifiers(schema: dict[str, Any]) -> set[str]:
    """All structural identifiers (property names, $def names) in a schema.

    Descriptions and titles are intentionally excluded so that documentation
    that mentions a broker term cannot cause a false positive.
    """
    identifiers: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("properties", "$defs"):
                if isinstance(node.get(key), dict):
                    identifiers.update(node[key])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return identifiers


@pytest.mark.parametrize("model", DECISION_MODELS, ids=lambda m: m.__name__)
def test_domain_schema_has_no_broker_specific_field(model: type[BaseModel]) -> None:
    identifiers = _schema_identifiers(model.model_json_schema())
    for token in FORBIDDEN_BROKER_TOKENS:
        for name in identifiers:
            assert token not in name.lower(), f"{model.__name__}.{name} looks broker-specific"


def test_all_decision_schema_files_are_broker_neutral() -> None:
    for schema_file in DECISION_SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        identifiers = {name.lower() for name in _schema_identifiers(schema)}
        for token in FORBIDDEN_BROKER_TOKENS:
            assert all(token not in name for name in identifiers), f"{schema_file.name}: {token}"


def test_generated_decision_ts_interfaces_are_broker_neutral() -> None:
    # Scan only interface field declarations, not comments.
    text = DECISION_TS_FILE.read_text(encoding="utf-8")
    field_lines = re.findall(r"^  (\w+)\??:", text, re.MULTILINE)
    for token in FORBIDDEN_BROKER_TOKENS:
        assert all(token not in name.lower() for name in field_lines), token


# --- Parity ---

@pytest.mark.parametrize("model", DECISION_MODELS, ids=lambda m: m.__name__)
def test_json_schema_file_matches_model(model: type[BaseModel]) -> None:
    schema_path = DECISION_SCHEMA_DIR / f"{_snake(model.__name__)}.schema.json"
    on_disk = json.loads(schema_path.read_text(encoding="utf-8"))
    from_model = model.model_json_schema()
    assert on_disk.get("required", []) == from_model.get("required", [])
    assert set(on_disk["properties"]) == set(from_model["properties"])


@pytest.mark.parametrize("model", DECISION_MODELS, ids=lambda m: m.__name__)
def test_typescript_interface_lists_all_fields(model: type[BaseModel]) -> None:
    text = DECISION_TS_FILE.read_text(encoding="utf-8")
    block = re.search(rf"export interface {model.__name__} \{{(.*?)\n\}}", text, re.DOTALL)
    assert block is not None
    for field in model.model_fields:
        assert re.search(rf"^  {field}\??:", block.group(1), re.MULTILINE), field


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
