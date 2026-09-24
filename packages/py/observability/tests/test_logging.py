from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock, patch

from trading_observability.context import bind_log_context
from trading_observability.logging import StructuredJsonFormatter, configure_logging


def make_record(
    message: str = "processed %s", *, args: tuple[object, ...] = ("event",)
) -> logging.LogRecord:
    return logging.LogRecord(
        name="worker",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


@contextmanager
def preserve_root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield root
    finally:
        root.handlers = handlers
        root.setLevel(level)


def test_formatter_emits_context_extras_and_redacts_secrets() -> None:
    formatter = StructuredJsonFormatter(service="core-worker", environment="test")
    record = make_record()
    record.api_token = "top-secret"
    record.details = {"authorization": "Bearer abc123", "attempt": 2}

    with bind_log_context(correlation_id="correlation-1", symbol="AAPL"):
        document = json.loads(formatter.format(record))

    assert document["severity"] == "WARNING"
    assert document["service"] == "core-worker"
    assert document["environment"] == "test"
    assert document["logger"] == "worker"
    assert document["message"] == "processed event"
    assert document["correlation_id"] == "correlation-1"
    assert document["symbol"] == "AAPL"
    assert document["api_token"] == "[REDACTED]"
    assert document["details"] == {"authorization": "[REDACTED]", "attempt": 2}
    assert document["timestamp"].endswith("+00:00")


def test_formatter_adds_valid_trace_identifiers() -> None:
    formatter = StructuredJsonFormatter(service="api", environment="paper")
    span_context = Mock(is_valid=True, trace_id=0x123, span_id=0x456)
    span = Mock()
    span.get_span_context.return_value = span_context

    with patch("trading_observability.logging.trace.get_current_span", return_value=span):
        document = json.loads(formatter.format(make_record("ok", args=())))

    assert document["trace_id"] == "00000000000000000000000000000123"
    assert document["span_id"] == "0000000000000456"


def test_formatter_includes_exception_information() -> None:
    formatter = StructuredJsonFormatter(service="api", environment="test")
    try:
        raise RuntimeError("failure")
    except RuntimeError:
        record = make_record("request failed", args=())
        record.exc_info = __import__("sys").exc_info()

    document = json.loads(formatter.format(record))

    assert "RuntimeError: failure" in document["exception"]


def test_configure_logging_writes_json_and_replaces_its_named_handler() -> None:
    first_stream = io.StringIO()
    second_stream = io.StringIO()

    with preserve_root_logger() as root:
        configure_logging(
            service="api", environment="test", level=logging.DEBUG, stream=first_stream
        )
        configured = configure_logging(
            service="scheduler",
            environment="paper",
            level=logging.ERROR,
            stream=second_stream,
        )

        assert configured is root
        assert root.level == logging.ERROR
        assert [
            handler for handler in root.handlers if handler.get_name() == "trading-structured-json"
        ] == [root.handlers[-1]]

        root.error("stopped", extra={"event_id": "event-1"})

    assert first_stream.getvalue() == ""
    document = json.loads(second_stream.getvalue())
    assert document["service"] == "scheduler"
    assert document["environment"] == "paper"
    assert document["message"] == "stopped"
    assert document["event_id"] == "event-1"
