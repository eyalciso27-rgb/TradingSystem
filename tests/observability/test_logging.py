"""Structured logging: serialization, correlation propagation, separation."""

from __future__ import annotations

import io
import json
import logging

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import use_span
from trading_observability import (
    CONTEXT_FIELDS,
    bind_log_context,
    configure_logging,
    new_correlation_id,
)


def emit(record_call: str = "hello", **extra: object) -> dict[str, object]:
    """Configure logging into a buffer, emit one record, return parsed JSON."""
    stream = io.StringIO()
    configure_logging(service="core-worker", environment="paper", stream=stream)
    logging.getLogger("test").info(record_call, extra=extra)
    payload: dict[str, object] = json.loads(stream.getvalue().strip())
    return payload


def test_record_is_single_line_json_with_mandatory_fields() -> None:
    doc = emit()
    assert doc["severity"] == "INFO"
    assert doc["service"] == "core-worker"
    assert doc["environment"] == "paper"
    assert doc["message"] == "hello"
    assert "T" in str(doc["timestamp"]) and str(doc["timestamp"]).endswith("+00:00")


def test_all_canonical_context_fields_supported_and_propagated() -> None:
    fields = {name: f"value-{name}" for name in CONTEXT_FIELDS}
    with bind_log_context(**fields):
        doc = emit()
    for name in CONTEXT_FIELDS:
        assert doc[name] == f"value-{name}"


def test_nested_binding_layers_and_restores() -> None:
    cid = new_correlation_id()
    with bind_log_context(correlation_id=cid):
        with bind_log_context(decision_id="dec-1"):
            inner = emit()
        outer = emit()
    after = emit()
    assert inner["correlation_id"] == cid and inner["decision_id"] == "dec-1"
    assert outer["correlation_id"] == cid and "decision_id" not in outer
    assert "correlation_id" not in after


def test_unknown_context_field_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        with bind_log_context(order_id="not-canonical"):
            pass


def test_environment_field_separates_paper_from_dev() -> None:
    stream = io.StringIO()
    configure_logging(service="api", environment="dev", stream=stream)
    logging.getLogger("test").info("x")
    assert json.loads(stream.getvalue().strip())["environment"] == "dev"
    doc = emit()
    assert doc["environment"] == "paper"


def test_trace_context_included_when_span_active() -> None:
    tracer = TracerProvider().get_tracer("test")
    span = tracer.start_span("op")
    with use_span(span, end_on_exit=True):
        doc = emit()
    assert doc["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert doc["span_id"] == format(span.get_span_context().span_id, "016x")


def test_high_cardinality_ids_live_in_log_body() -> None:
    with bind_log_context(order_intent_id="oi-123", symbol="AAPL"):
        doc = emit()
    assert doc["order_intent_id"] == "oi-123"
    assert doc["symbol"] == "AAPL"


def test_operational_logging_is_independent_of_audit_and_adapters() -> None:
    """Logs never replace the tamper-evident audit: no coupling to it."""
    import trading_observability.context
    import trading_observability.logging
    import trading_observability.redaction
    import trading_observability.timeline

    for module in (
        trading_observability.logging,
        trading_observability.context,
        trading_observability.redaction,
        trading_observability.timeline,
    ):
        source_file = module.__file__
        assert source_file is not None
        source = open(source_file, encoding="utf-8").read()
        assert "trading_persistence" not in source.replace(
            "``trading_persistence.audit``", ""
        ), f"{module.__name__} must not couple operational logs to the audit store"
