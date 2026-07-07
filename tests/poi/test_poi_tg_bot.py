"""POI-SOC-TG-BOT fixtures drill — Bot API golden fixtures and dedup keys.

The live parts (dedicated bot token, webhook secret / long polling against
api.telegram.org, sending a private test alert) are BLOCKED on a provisioned
bot token. What runs here for real: the golden Bot API update fixtures parse,
normalize, and yield the canonical dedup key of Data_Model v0.6 section 31.3
(provider + canonical chat + message id), including the edit case reusing the
same canonical message identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telegram_bot"


def _load(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return payload


def _message_of(update: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    for key, edited in (
        ("channel_post", False),
        ("edited_channel_post", True),
        ("message", False),
    ):
        if key in update:
            found: dict[str, Any] = update[key]
            return found, edited
    raise AssertionError("unsupported update type")


def dedup_key(update: dict[str, Any]) -> str:
    """Canonical dedup key: provider + canonical chat + message ID (31.3)."""
    message, _ = _message_of(update)
    return f"telegram:{message['chat']['id']}:{message['message_id']}"


def test_fixtures_parse_and_carry_stable_ids() -> None:
    for name in ("channel_post.json", "edited_channel_post.json", "group_message.json"):
        update = _load(name)
        message, _ = _message_of(update)
        assert isinstance(update["update_id"], int)
        assert isinstance(message["message_id"], int)
        assert isinstance(message["chat"]["id"], int)
        assert message["text"]


def test_edit_shares_canonical_identity_with_original() -> None:
    original = _load("channel_post.json")
    edited = _load("edited_channel_post.json")
    # same canonical message -> same dedup key -> a revision, not a new event
    assert dedup_key(original) == dedup_key(edited)
    message, is_edited = _message_of(edited)
    assert is_edited and message["edit_date"] > message["date"]


def test_distinct_chats_never_collide() -> None:
    keys = {
        dedup_key(_load("channel_post.json")),
        dedup_key(_load("group_message.json")),
    }
    assert len(keys) == 2


def test_reply_preserves_thread_linkage() -> None:
    update = _load("group_message.json")
    message, _ = _message_of(update)
    parent = message["reply_to_message"]
    assert parent["message_id"] != message["message_id"]
    assert parent["chat"]["id"] == message["chat"]["id"]


def test_fixtures_contain_no_secret_material() -> None:
    for path in FIXTURES.glob("*.json"):
        lowered = path.read_text(encoding="utf-8").lower()
        for marker in ("token", "api_hash", "password", "bearer"):
            assert marker not in lowered, f"{path.name} contains {marker!r}"
