"""Positive and negative tests for the canonical event envelope (IMP-0102).

Done criteria: contract tests reject events with missing point-in-time fields.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from trading_contracts.envelope import EventEnvelope

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = REPO_ROOT / "packages" / "schemas" / "json" / "events" / "event_envelope.schema.json"
TS_FILE = REPO_ROOT / "packages" / "ts" / "contracts-generated" / "src" / "event-envelope.ts"

REQUIRED_FIELDS = (
    "event_id",
    "type",
    "version",
    "occurred_at",
    "observed_at",
    "available_at",
    "ingested_at",
    "correlation_id",
)
POINT_IN_TIME_FIELDS = ("occurred_at", "observed_at", "available_at", "ingested_at")


def _valid_payload(**overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": str(uuid4()),
        "type": "MESSAGE_CREATED",
        "version": "1.0",
        "occurred_at": "2026-07-05T10:00:00+00:00",
        "observed_at": "2026-07-05T10:00:01+00:00",
        "available_at": "2026-07-05T10:00:02+00:00",
        "ingested_at": "2026-07-05T10:00:03+00:00",
        "correlation_id": str(uuid4()),
    }
    payload.update(overrides)
    return payload


def test_valid_envelope_parses_with_typed_fields() -> None:
    envelope = EventEnvelope.model_validate(_valid_payload())
    assert isinstance(envelope.event_id, UUID)
    assert isinstance(envelope.correlation_id, UUID)
    assert envelope.causation_id is None
    for field in POINT_IN_TIME_FIELDS:
        timestamp: datetime = getattr(envelope, field)
        assert timestamp.tzinfo is not None


def test_json_round_trip_is_lossless() -> None:
    envelope = EventEnvelope.model_validate(_valid_payload(causation_id=str(uuid4())))
    restored = EventEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored == envelope


def test_serialization_is_stable() -> None:
    envelope = EventEnvelope.model_validate(_valid_payload())
    assert envelope.model_dump_json() == envelope.model_dump_json()


def test_equal_timestamps_are_allowed() -> None:
    same = "2026-07-05T10:00:00+00:00"
    envelope = EventEnvelope.model_validate(
        _valid_payload(occurred_at=same, observed_at=same, available_at=same, ingested_at=same)
    )
    assert envelope.occurred_at == envelope.ingested_at


def test_correlation_and_causation_chain() -> None:
    root = EventEnvelope.model_validate(_valid_payload())
    child = EventEnvelope.model_validate(
        _valid_payload(
            correlation_id=str(root.correlation_id),
            causation_id=str(root.event_id),
        )
    )
    assert child.correlation_id == root.correlation_id
    assert child.causation_id == root.event_id
    assert root.causation_id is None  # root events have no cause


def test_envelope_is_immutable() -> None:
    envelope = EventEnvelope.model_validate(_valid_payload())
    with pytest.raises(ValidationError):
        envelope.type = "OTHER_TYPE"


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_is_rejected(field: str) -> None:
    payload = _valid_payload()
    del payload[field]
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(payload)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_null_in_required_field_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(**{field: None}))


@pytest.mark.parametrize("field", POINT_IN_TIME_FIELDS)
def test_naive_timestamp_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(**{field: "2026-07-05T10:00:00"}))


def test_naive_python_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            _valid_payload(occurred_at=datetime(2026, 7, 5, 10, 0, 0))  # noqa: DTZ001
        )


def test_aware_python_datetime_is_accepted() -> None:
    envelope = EventEnvelope.model_validate(
        _valid_payload(occurred_at=datetime(2026, 7, 5, 10, 0, 0, tzinfo=UTC))
    )
    assert envelope.occurred_at.tzinfo is not None


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-45T99:99:99Z",
                                 "05/07/2026 10:00"])
def test_malformed_timestamp_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(ingested_at=bad))


@pytest.mark.parametrize("field", ["event_id", "correlation_id", "causation_id"])
def test_invalid_uuid_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(**{field: "not-a-uuid"}))


@pytest.mark.parametrize("bad", ["v1", "1", "", "1.0.0", "one.zero"])
def test_invalid_version_format_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(version=bad))


@pytest.mark.parametrize("bad", ["message_created", "Message.Created", "", "1BAD"])
def test_invalid_type_format_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(type=bad))


def test_unknown_field_is_rejected_strict_contract() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(_valid_payload(payload={"convenient": True}))


def test_json_schema_file_matches_python_model() -> None:
    on_disk = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    from_model = EventEnvelope.model_json_schema()
    assert on_disk["required"] == from_model["required"]
    assert on_disk["required"] == list(REQUIRED_FIELDS)
    assert set(on_disk["properties"]) == set(from_model["properties"])
    assert on_disk["additionalProperties"] is False
    for field in POINT_IN_TIME_FIELDS:
        assert on_disk["properties"][field]["format"] == "date-time"


def test_typescript_matches_python_model() -> None:
    text = TS_FILE.read_text(encoding="utf-8")
    assert "GENERATED FILE — DO NOT EDIT" in text
    for field in REQUIRED_FIELDS:
        assert re.search(rf"^  {field}: string;$", text, re.MULTILINE), field
    assert re.search(r"^  causation_id\?: string \| null;$", text, re.MULTILINE)
    required_list = re.search(
        r"EVENT_ENVELOPE_REQUIRED_FIELDS = \[(.*?)\] as const;", text, re.DOTALL
    )
    assert required_list is not None
    ts_required = re.findall(r'"([a-z_]+)"', required_list.group(1))
    assert ts_required == list(REQUIRED_FIELDS)
