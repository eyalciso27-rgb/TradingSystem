"""Unit tests for the canonical event envelope schema."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError
from trading_contracts.envelope import EventEnvelope

EVENT_ID = UUID("00000000-0000-4000-8000-000000000001")
CORRELATION_ID = UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def valid_envelope_data() -> dict[str, object]:
    return {
        "event_id": EVENT_ID,
        "type": "ORDER_ACKNOWLEDGED",
        "version": "1.0",
        "occurred_at": NOW,
        "observed_at": NOW,
        "available_at": NOW,
        "ingested_at": NOW,
        "correlation_id": CORRELATION_ID,
    }


def test_event_envelope_accepts_valid_schema_and_defaults_causation() -> None:
    envelope = EventEnvelope.model_validate(valid_envelope_data())

    assert envelope.event_id == EVENT_ID
    assert envelope.causation_id is None
    assert envelope.model_dump(mode="json")["type"] == "ORDER_ACKNOWLEDGED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "order.acknowledged"),
        ("type", "1_ORDER"),
        ("version", "v1"),
        ("version", "1.0.0"),
    ],
)
def test_event_envelope_rejects_invalid_open_string_formats(field: str, value: str) -> None:
    data = valid_envelope_data()
    data[field] = value

    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(data)


def test_event_envelope_requires_timezone_aware_timestamps() -> None:
    data = valid_envelope_data()
    data["occurred_at"] = datetime(2026, 1, 2, 3, 4)

    with pytest.raises(ValidationError, match="timezone"):
        EventEnvelope.model_validate(data)


def test_event_envelope_forbids_unknown_fields() -> None:
    data = valid_envelope_data()
    data["payload"] = {"unexpected": True}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EventEnvelope.model_validate(data)


def test_event_envelope_is_immutable() -> None:
    envelope = EventEnvelope.model_validate(valid_envelope_data())

    with pytest.raises(ValidationError, match="frozen"):
        envelope.type = "ORDER_FILLED"
