from __future__ import annotations

import uuid

import pytest
from trading_observability.context import (
    bind_log_context,
    current_context,
    new_correlation_id,
)


def test_new_correlation_id_returns_unique_uuid_strings() -> None:
    first = new_correlation_id()
    second = new_correlation_id()

    assert uuid.UUID(first).version == 4
    assert uuid.UUID(second).version == 4
    assert first != second


def test_bind_log_context_layers_nested_values_and_restores_outer_context() -> None:
    assert dict(current_context()) == {}

    with bind_log_context(correlation_id="correlation-1", source="feed"):
        assert dict(current_context()) == {
            "correlation_id": "correlation-1",
            "source": "feed",
        }

        with bind_log_context(source="strategy", symbol="AAPL", event_id=None):
            assert dict(current_context()) == {
                "correlation_id": "correlation-1",
                "source": "strategy",
                "symbol": "AAPL",
            }

        assert dict(current_context()) == {
            "correlation_id": "correlation-1",
            "source": "feed",
        }

    assert dict(current_context()) == {}


def test_bind_log_context_rejects_unknown_fields_without_mutating_context() -> None:
    with bind_log_context(event_id="event-1"):
        with pytest.raises(ValueError, match=r"unknown log context fields: \['unknown'\]"):
            with bind_log_context(unknown="value"):
                pass

        assert dict(current_context()) == {"event_id": "event-1"}


def test_current_context_is_read_only() -> None:
    with bind_log_context(symbol="MSFT"):
        with pytest.raises(TypeError):
            current_context()["symbol"] = "AAPL"  # type: ignore[index]
