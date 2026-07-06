"""Positive and negative tests for source/social contracts (IMP-0103).

Done criteria: cross-mode examples validate and the dedup key is explicit.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from trading_contracts.enums import IntegrationHealthStatus, SocialAccessMode, SourceType
from trading_contracts.social import (
    SOCIAL_MODELS,
    SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE,
    CanonicalSocialMessage,
    SocialAttachment,
    SocialConnectionProfile,
    SocialMessageObservation,
    compute_social_message_dedup_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOCIAL_SCHEMA_DIR = REPO_ROOT / "packages" / "schemas" / "json" / "social"
SOCIAL_TS_FILE = REPO_ROOT / "packages" / "ts" / "contracts-generated" / "src" / "social.ts"

CONTENT_HASH = "sha256:" + "a" * 64


def _message(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_type": SourceType.WHATSAPP.value,
        "channel_external_id": "group-1",
        "external_message_id": "wamid.ABC",
        "author_external_id": "author-42",
        "author_display_name": "Trader Alias",
        "content_text": "AAPL breakout above 200",
        "content_hash": CONTENT_HASH,
    }
    base.update(overrides)
    base["dedup_key"] = compute_social_message_dedup_key(
        SourceType(base["source_type"]),
        base["channel_external_id"],
        base.get("external_message_id"),
        base["content_hash"],
    )
    if "dedup_key_override" in overrides:
        base["dedup_key"] = overrides.pop("dedup_key_override")
    return base


def _observation(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_type": SourceType.TELEGRAM.value,
        "access_mode": SocialAccessMode.TELEGRAM_USER_TDLIB.value,
        "connector_id": "conn-1",
        "source_account_id": "srcacct-1",
        "channel_external_id": "-100999",
        "external_message_id": "555",
        "received_at": "2026-07-05T10:00:00+00:00",
        "message_dedup_key": CONTENT_HASH,
    }
    base.update(overrides)
    return base


# --- SocialConnectionProfile: access mode / provider combinations ---

@pytest.mark.parametrize(
    ("access_mode", "provider"),
    [
        (SocialAccessMode.WHATSAPP_USER, SourceType.WHATSAPP),
        (SocialAccessMode.DISCORD_USER, SourceType.DISCORD),
        (SocialAccessMode.TELEGRAM_USER_TDLIB, SourceType.TELEGRAM),
        (SocialAccessMode.TELEGRAM_BOT, SourceType.TELEGRAM),
    ],
)
def test_valid_access_mode_provider_combinations(
    access_mode: SocialAccessMode, provider: SourceType
) -> None:
    profile = SocialConnectionProfile(
        provider=provider,
        access_mode=access_mode,
        identity_ref="ref-1",
        required_capability=access_mode is not SocialAccessMode.TELEGRAM_BOT,
        enabled=True,
        health_status=IntegrationHealthStatus.HEALTHY,
    )
    assert profile.provider is provider


@pytest.mark.parametrize(
    ("access_mode", "provider"),
    [
        (SocialAccessMode.WHATSAPP_USER, SourceType.TELEGRAM),
        (SocialAccessMode.TELEGRAM_BOT, SourceType.DISCORD),
        (SocialAccessMode.DISCORD_USER, SourceType.WHATSAPP),
    ],
)
def test_illegal_access_mode_provider_combinations_rejected(
    access_mode: SocialAccessMode, provider: SourceType
) -> None:
    with pytest.raises(ValidationError):
        SocialConnectionProfile(
            provider=provider,
            access_mode=access_mode,
            identity_ref="ref-1",
            required_capability=True,
            enabled=True,
            health_status=IntegrationHealthStatus.HEALTHY,
        )


# --- CanonicalSocialMessage: per-provider capture ---

@pytest.mark.parametrize(
    "source_type",
    [SourceType.WHATSAPP, SourceType.DISCORD, SourceType.TELEGRAM],
)
def test_valid_message_per_social_source(source_type: SourceType) -> None:
    message = CanonicalSocialMessage.model_validate(_message(source_type=source_type.value))
    assert message.source_type is source_type
    assert message.revision == 1
    assert message.is_deleted_at_source is False


def test_message_round_trip_preserves_dedup_key() -> None:
    message = CanonicalSocialMessage.model_validate(_message())
    restored = CanonicalSocialMessage.model_validate_json(message.model_dump_json())
    assert restored == message
    assert restored.dedup_key == message.dedup_key


def test_message_with_attachments_reference_only() -> None:
    attachment = SocialAttachment(
        media_type="image/png",
        size_bytes=1024,
        content_hash=CONTENT_HASH,
        raw_object_ref="raw-object://abc",
    )
    message = CanonicalSocialMessage.model_validate(_message(attachments=(attachment,)))
    assert message.attachments[0].media_type == "image/png"


def test_message_is_immutable() -> None:
    message = CanonicalSocialMessage.model_validate(_message())
    with pytest.raises(ValidationError):
        message.content_text = "changed"


def test_non_social_source_type_rejected() -> None:
    with pytest.raises(ValidationError):
        CanonicalSocialMessage.model_validate(_message(source_type=SourceType.NEWS.value))


# --- Deduplication ---

def test_dedup_key_is_stable_for_same_input() -> None:
    first = compute_social_message_dedup_key(SourceType.TELEGRAM, "-100999", "555", CONTENT_HASH)
    second = compute_social_message_dedup_key(SourceType.TELEGRAM, "-100999", "555", CONTENT_HASH)
    assert first == second
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)


def test_cross_mode_same_post_deduplicates_to_one_key() -> None:
    # Same Telegram post seen via user mode and via bot mode -> identical key,
    # because access_mode and connector identity are NOT part of the key.
    user_key = compute_social_message_dedup_key(SourceType.TELEGRAM, "-100999", "555", CONTENT_HASH)
    bot_key = compute_social_message_dedup_key(SourceType.TELEGRAM, "-100999", "555", CONTENT_HASH)
    assert user_key == bot_key


def test_dedup_key_changes_when_identity_field_changes() -> None:
    base = compute_social_message_dedup_key(SourceType.TELEGRAM, "-100999", "555", CONTENT_HASH)
    other_message = compute_social_message_dedup_key(
        SourceType.TELEGRAM, "-100999", "556", CONTENT_HASH
    )
    other_channel = compute_social_message_dedup_key(
        SourceType.TELEGRAM, "-100000", "555", CONTENT_HASH
    )
    other_source = compute_social_message_dedup_key(
        SourceType.DISCORD, "-100999", "555", CONTENT_HASH
    )
    assert len({base, other_message, other_channel, other_source}) == 4


def test_dedup_falls_back_to_content_hash_without_message_id() -> None:
    with_id = compute_social_message_dedup_key(SourceType.WHATSAPP, "g1", "m1", CONTENT_HASH)
    without_id = compute_social_message_dedup_key(SourceType.WHATSAPP, "g1", None, CONTENT_HASH)
    assert with_id != without_id
    other_content = compute_social_message_dedup_key(
        SourceType.WHATSAPP, "g1", None, "sha256:" + "b" * 64
    )
    assert without_id != other_content


def test_edit_keeps_dedup_key_but_bumps_revision() -> None:
    original = CanonicalSocialMessage.model_validate(_message())
    edited = CanonicalSocialMessage.model_validate(
        _message(
            revision=2,
            content_text="AAPL breakout above 205",
            source_edited_at="2026-07-05T11:00:00+00:00",
        )
    )
    assert edited.dedup_key == original.dedup_key
    assert edited.revision == 2


def test_deletion_keeps_dedup_key_as_tombstone() -> None:
    original = CanonicalSocialMessage.model_validate(_message())
    tombstone = CanonicalSocialMessage.model_validate(_message(is_deleted_at_source=True))
    assert tombstone.dedup_key == original.dedup_key
    assert tombstone.is_deleted_at_source is True


def test_attachment_does_not_change_dedup_key() -> None:
    attachment = SocialAttachment(media_type="image/png")
    plain = CanonicalSocialMessage.model_validate(_message())
    with_attachment = CanonicalSocialMessage.model_validate(_message(attachments=(attachment,)))
    assert plain.dedup_key == with_attachment.dedup_key


def test_wrong_dedup_key_rejected() -> None:
    with pytest.raises(ValidationError):
        CanonicalSocialMessage.model_validate(_message(dedup_key_override="sha256:" + "0" * 64))


# --- SocialMessageObservation ---

def test_valid_observation_user_and_bot_modes() -> None:
    user = SocialMessageObservation.model_validate(_observation())
    bot = SocialMessageObservation.model_validate(
        _observation(access_mode=SocialAccessMode.TELEGRAM_BOT.value, connector_id="conn-bot")
    )
    assert user.access_mode is SocialAccessMode.TELEGRAM_USER_TDLIB
    assert bot.access_mode is SocialAccessMode.TELEGRAM_BOT
    assert user.received_at.tzinfo is not None


def test_observation_illegal_mode_source_combo_rejected() -> None:
    with pytest.raises(ValidationError):
        SocialMessageObservation.model_validate(
            _observation(access_mode=SocialAccessMode.WHATSAPP_USER.value)
        )


# --- Missing / malformed / privacy negative cases ---

@pytest.mark.parametrize(
    "field", ["source_type", "channel_external_id", "content_hash", "dedup_key"]
)
def test_missing_required_message_field_rejected(field: str) -> None:
    payload = _message()
    del payload[field]
    with pytest.raises(ValidationError):
        CanonicalSocialMessage.model_validate(payload)


@pytest.mark.parametrize("field", ["source_type", "access_mode", "connector_id", "received_at"])
def test_missing_required_observation_field_rejected(field: str) -> None:
    payload = _observation()
    del payload[field]
    with pytest.raises(ValidationError):
        SocialMessageObservation.model_validate(payload)


def test_invalid_enum_value_rejected() -> None:
    payload = _message()
    payload["source_type"] = "MYSPACE"
    with pytest.raises(ValidationError):
        CanonicalSocialMessage.model_validate(payload)


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        SocialMessageObservation.model_validate(_observation(received_at="2026-07-05T10:00:00"))


def test_malformed_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        SocialMessageObservation.model_validate(_observation(received_at="not-a-date"))


def test_null_in_required_field_rejected() -> None:
    payload = _message()
    payload["channel_external_id"] = None
    with pytest.raises(ValidationError):
        CanonicalSocialMessage.model_validate(payload)


@pytest.mark.parametrize("model", SOCIAL_MODELS, ids=lambda m: m.__name__)
def test_extra_field_rejected(model: type[BaseModel]) -> None:
    payloads = {
        "SocialAttachment": {"media_type": "image/png", "session_token": "secret"},
        "SocialConnectionProfile": {
            "provider": SourceType.WHATSAPP.value,
            "access_mode": SocialAccessMode.WHATSAPP_USER.value,
            "identity_ref": "r",
            "required_capability": True,
            "enabled": True,
            "health_status": IntegrationHealthStatus.HEALTHY.value,
            "cookie": "x",
        },
        "CanonicalSocialMessage": _message(qr_code="data"),
        "SocialMessageObservation": _observation(auth_key="secret"),
    }
    with pytest.raises(ValidationError):
        model.model_validate(payloads[model.__name__])


@pytest.mark.parametrize(
    "credential_field",
    ["session_token", "access_token", "cookie", "qr_code", "auth_key", "api_hash", "password"],
)
def test_credential_fields_are_not_part_of_any_contract(credential_field: str) -> None:
    for model in SOCIAL_MODELS:
        assert credential_field not in model.model_fields


def test_author_native_id_separate_from_display_name() -> None:
    fields = CanonicalSocialMessage.model_fields
    assert "author_external_id" in fields
    assert "author_display_name" in fields


# --- Parity: Python <-> JSON Schema <-> TypeScript ---

@pytest.mark.parametrize("model", SOCIAL_MODELS, ids=lambda m: m.__name__)
def test_json_schema_file_matches_model(model: type[BaseModel]) -> None:
    schema_path = SOCIAL_SCHEMA_DIR / f"{_snake(model.__name__)}.schema.json"
    on_disk = json.loads(schema_path.read_text(encoding="utf-8"))
    from_model = model.model_json_schema()
    assert on_disk["required"] == from_model["required"]
    assert set(on_disk["properties"]) == set(from_model["properties"])
    assert on_disk["additionalProperties"] is False


@pytest.mark.parametrize("model", SOCIAL_MODELS, ids=lambda m: m.__name__)
def test_typescript_interface_lists_all_fields(model: type[BaseModel]) -> None:
    text = SOCIAL_TS_FILE.read_text(encoding="utf-8")
    block = re.search(rf"export interface {model.__name__} \{{(.*?)\n\}}", text, re.DOTALL)
    assert block is not None
    for field in model.model_fields:
        assert re.search(rf"^  {field}\??:", block.group(1), re.MULTILINE), field


def test_typescript_mapping_matches_python() -> None:
    text = SOCIAL_TS_FILE.read_text(encoding="utf-8")
    for mode, source in SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE.items():
        assert f'{mode.value}: "{source.value}"' in text


def test_generated_social_files_carry_header() -> None:
    assert "GENERATED FILE — DO NOT EDIT" in SOCIAL_TS_FILE.read_text(encoding="utf-8")


def test_aware_datetime_python_accepted() -> None:
    message = SocialMessageObservation.model_validate(
        _observation(received_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC))
    )
    assert message.received_at.tzinfo is not None


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
