"""Unit tests for decision contract schema validators."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from trading_contracts.decision import InstrumentRef, OrderIntent, RiskDecision
from trading_contracts.enums import (
    Environment,
    OperatingMode,
    OrderRole,
    OrderType,
    RiskDecisionOutcome,
)

ID_1 = UUID("00000000-0000-4000-8000-000000000001")
ID_2 = UUID("00000000-0000-4000-8000-000000000002")
ID_3 = UUID("00000000-0000-4000-8000-000000000003")
ID_4 = UUID("00000000-0000-4000-8000-000000000004")
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def valid_risk_decision_data() -> dict[str, object]:
    return {
        "risk_decision_id": ID_1,
        "decision_id": ID_2,
        "trade_plan_id": ID_3,
        "outcome": RiskDecisionOutcome.APPROVE,
        "approved_quantity": Decimal("10"),
        "approved_risk_amount": Decimal("125.50"),
        "currency": "USD",
        "risk_policy_version": "1.2",
        "evaluated_at": NOW,
    }


def valid_order_intent_data() -> dict[str, object]:
    return {
        "intent_id": ID_1,
        "decision_id": ID_2,
        "trade_plan_id": ID_3,
        "risk_decision_id": ID_4,
        "strategy_version": "1.0",
        "instrument": InstrumentRef(instrument_id=ID_4, symbol="AAPL", currency="USD"),
        "order_role": OrderRole.ENTRY,
        "side": "BUY",
        "order_type": OrderType.LIMIT,
        "quantity": Decimal("10"),
        "limit_price": Decimal("175.25"),
        "time_in_force": "DAY",
        "environment": Environment.PAPER,
        "operating_mode": OperatingMode.PAPER_AUTO,
        "account_ref": "paper-primary",
        "idempotency_key": "decision-2-entry",
    }


def test_risk_approval_requires_a_positive_quantity() -> None:
    data = valid_risk_decision_data()
    data["approved_quantity"] = None

    with pytest.raises(ValidationError, match="APPROVE requires approved_quantity"):
        RiskDecision.model_validate(data)

    data["approved_quantity"] = Decimal("0")
    with pytest.raises(ValidationError, match="greater than 0"):
        RiskDecision.model_validate(data)


def test_risk_veto_requires_reasons_and_forbids_quantity() -> None:
    data = valid_risk_decision_data()
    data.update(outcome=RiskDecisionOutcome.VETO, approved_quantity=None)

    with pytest.raises(ValidationError, match="requires at least one veto_reason_code"):
        RiskDecision.model_validate(data)

    data.update(approved_quantity=Decimal("1"), veto_reason_codes=("DATA_STALE",))
    with pytest.raises(ValidationError, match="must not carry approved_quantity"):
        RiskDecision.model_validate(data)


def test_valid_risk_veto_is_accepted() -> None:
    data = valid_risk_decision_data()
    data.update(
        outcome=RiskDecisionOutcome.VETO,
        approved_quantity=None,
        veto_reason_codes=("DATA_STALE",),
    )

    decision = RiskDecision.model_validate(data)

    assert decision.outcome is RiskDecisionOutcome.VETO
    assert decision.veto_reason_codes == ("DATA_STALE",)


def test_order_intent_requires_matching_mode_and_environment() -> None:
    data = valid_order_intent_data()
    data["environment"] = Environment.LIVE

    with pytest.raises(ValidationError, match="implies environment PAPER, not LIVE"):
        OrderIntent.model_validate(data)


def test_order_intent_validates_tokens_versions_and_decimals() -> None:
    valid = OrderIntent.model_validate(valid_order_intent_data())
    assert valid.quantity == Decimal("10")

    for field, invalid in (
        ("side", "buy"),
        ("strategy_version", "v1"),
        ("quantity", Decimal("-1")),
    ):
        data = valid_order_intent_data()
        data[field] = invalid
        with pytest.raises(ValidationError):
            OrderIntent.model_validate(data)


def test_contract_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        InstrumentRef.model_validate(
            {"instrument_id": ID_1, "broker_contract_id": 123}
        )
