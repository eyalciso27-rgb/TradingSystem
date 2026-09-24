from __future__ import annotations

import pytest
from trading_observability.redaction import REDACTED, is_sensitive_key, redact


@pytest.mark.parametrize(
    "key",
    [
        "client_secret",
        "accessToken",
        "PASSWORD",
        "session_id",
        "Authorization",
        "api_key",
        "private_key_pem",
        "database_dsn",
    ],
)
def test_is_sensitive_key_recognizes_supported_markers(key: str) -> None:
    assert is_sensitive_key(key)


def test_is_sensitive_key_allows_non_sensitive_keys() -> None:
    assert not is_sensitive_key("correlation_id")
    assert not is_sensitive_key("strategy_name")


def test_redact_recurses_without_mutating_the_input() -> None:
    original = {
        "user": "alice",
        "credentials": {"username": "alice", "password": "hidden"},
        "events": [
            {"message": "Authorization: Bearer abc.def"},
            "postgresql://reader:super-secret@db.internal/trading",
        ],
        "coordinates": (1, "safe"),
    }

    result = redact(original)

    assert result == {
        "user": "alice",
        "credentials": REDACTED,
        "events": [
            {"message": f"Authorization: {REDACTED}"},
            f"postgresql://reader:{REDACTED}@db.internal/trading",
        ],
        "coordinates": [1, "safe"],
    }
    assert original["credentials"] == {"username": "alice", "password": "hidden"}


def test_redact_preserves_non_string_scalars() -> None:
    assert redact(None) is None
    assert redact(42) == 42
    assert redact(True) is True
