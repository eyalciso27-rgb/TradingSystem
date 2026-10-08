"""Unit tests for decision contract schema and validation behavior."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from trading_contracts.decision import (
    EntrySpec,
    InstrumentRef,
    OrderIntent,
    ProfitTarget,
    RiskDecision,
)
from trading_contracts.enums import (
    Environment,
    OperatingMode,
    OrderRole,
    OrderType,
    RiskDecisionOutcome,
    SourceEntryType,
)

ID_1 = UUID("00000000-0000-0000-0000-000000000001")
ID_2 = UUID("00000000-0000-0000-0000-000000000002")
ID_3 = UUID("00000000-0000-0000-0000-000000000003")
ID_4 = UUID("00000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _risk_decision(**overrides: object) -> RiskDecision:
    values: dict[str, object] = {
        "risk_decision_id": ID_1,
        "decision_id": ID_2,
        "trade_plan_id": ID_3,
        "outcome": RiskDecisionOutcome.APPROVE,
        "approved_quantity": Decimal("10"),
        "risk_policy_version": "1.0",
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return RiskDecision.model_validate(values)


def _order_intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "intent_id": ID_1,
        "decision_id": ID_2,
        "trade_plan_id": ID_3,
        "risk_decision_id": ID_4,
        "strategy_version": "1.0",
        "instrument": {"instrument_id": ID_1, "symbol": "ACME", "currency": "USD"},
        "order_role": OrderRole.ENTRY,
        "side": "BUY",
        "order_type": OrderType.LIMIT,
        "quantity": Decimal("5"),
        "limit_price": Decimal("12.50"),
        "time_in_force": "DAY",
        "environment": Environment.PAPER,
        "operating_mode": OperatingMode.PAPER_MANUAL,
        "account_ref": "paper-account",
        "idempotency_key": "intent-1",
    }
    values.update(overrides)
    return OrderIntent.model_validate(values)


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_positive_decimal_fields_reject_non_positive_or_non_finite_values(
    quantity: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        ProfitTarget(price=Decimal("10"), quantity_fraction=quantity)


def test_probability_fraction_cannot_exceed_one() -> None:
    with pytest.raises(ValidationError):
        ProfitTarget(price=Decimal("10"), quantity_fraction=Decimal("1.01"))


def test_contract_models_forbid_unknown_fields_and_are_frozen() -> None:
    with pytest.raises(ValidationError, match="extra"):
        InstrumentRef.model_validate(
            {"instrument_id": ID_1, "broker_id": "native-123"}
        )

    instrument = InstrumentRef(instrument_id=ID_1, symbol="ACME")
    with pytest.raises(ValidationError, match="frozen"):
        instrument.symbol = "OTHER"


def test_entry_schema_exposes_enum_and_decimal_constraints() -> None:
    schema = EntrySpec.model_json_schema()

    assert schema["$defs"]["OrderType"]["enum"] == [member.value for member in OrderType]
    assert schema["$defs"]["SourceEntryType"]["enum"] == [
        member.value for member in SourceEntryType
    ]
    assert schema["properties"]["limit_price"]["anyOf"][0]["exclusiveMinimum"] == 0


def test_veto_requires_a_reason_and_forbids_approved_quantity() -> None:
    with pytest.raises(ValidationError, match="requires at least one veto_reason_code"):
        _risk_decision(outcome=RiskDecisionOutcome.VETO, approved_quantity=None)

    with pytest.raises(ValidationError, match="must not carry approved_quantity"):
        _risk_decision(outcome=RiskDecisionOutcome.VETO, veto_reason_codes=("DATA_STALE",))


def test_approved_risk_decision_requires_quantity_and_forbids_veto_reasons() -> None:
    with pytest.raises(ValidationError, match="requires approved_quantity"):
        _risk_decision(approved_quantity=None)

    with pytest.raises(ValidationError, match="must not carry veto_reason_codes"):
        _risk_decision(veto_reason_codes=("DATA_STALE",))


def test_order_intent_environment_must_match_operating_mode() -> None:
    assert _order_intent().environment is Environment.PAPER

    with pytest.raises(ValidationError, match="implies environment PAPER, not LIVE"):
        _order_intent(environment=Environment.LIVE)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("side", "buy"), ("time_in_force", "good till cancelled"), ("quantity", Decimal("0"))],
)
def test_order_intent_rejects_invalid_tokens_and_quantity(
    field: str, invalid_value: object
) -> None:
    with pytest.raises(ValidationError):
        _order_intent(**{field: invalid_value})
