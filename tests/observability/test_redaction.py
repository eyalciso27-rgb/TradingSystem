"""Central secret redaction — nothing sensitive may reach operational logs."""

from __future__ import annotations

import io
import json
import logging

from trading_observability import REDACTED, configure_logging, redact


def test_sensitive_keys_redacted_recursively() -> None:
    doc = redact(
        {
            "password": "p", "api_token": "t", "client_secret": "s",
            "cookie": "c", "session_blob": "b", "credentials": {"user": "u"},
            "Authorization": "Basic abc", "API_KEY": "k", "dsn": "postgres://u:p@h/db",
            "nested": {"refresh_token": "r", "ok": "visible"},
            "items": [{"password": "p2"}, "plain"],
        }
    )
    assert isinstance(doc, dict)
    for key in (
        "password", "api_token", "client_secret", "cookie",
        "session_blob", "credentials", "Authorization", "API_KEY", "dsn",
    ):
        assert doc[key] == REDACTED, key
    nested = doc["nested"]
    assert isinstance(nested, dict)
    assert nested["refresh_token"] == REDACTED and nested["ok"] == "visible"
    items = doc["items"]
    assert isinstance(items, list)
    assert items[0]["password"] == REDACTED and items[1] == "plain"


def test_value_patterns_scrubbed_under_innocent_keys() -> None:
    doc = redact(
        {
            "note": "header was Bearer eyJhbGciOi.abc.def",
            "url": "postgresql+psycopg://trading:hunter2@db:5432/trading",
        }
    )
    assert isinstance(doc, dict)
    assert "eyJhbGciOi" not in str(doc["note"]) and REDACTED in str(doc["note"])
    assert "hunter2" not in str(doc["url"]) and REDACTED in str(doc["url"])


def test_formatter_applies_redaction_centrally() -> None:
    """Services get redaction from the shared formatter — no per-service code."""
    stream = io.StringIO()
    configure_logging(service="connectors", environment="dev", stream=stream)
    logging.getLogger("test").info(
        "login done", extra={"password": "hunter2", "detail": "Bearer tok123"}
    )
    raw = stream.getvalue()
    assert "hunter2" not in raw and "tok123" not in raw
    doc = json.loads(raw.strip())
    assert doc["password"] == REDACTED and REDACTED in doc["detail"]
