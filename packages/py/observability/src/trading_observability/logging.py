"""Uniform structured JSON logging for every service.

Operational logs only. Metrics belong to Prometheus, traces to the
OpenTelemetry SDK, and the tamper-evident audit trail to
``trading_persistence.audit`` (hash-chained, append-only) — none of those is
replaced by these logs, and this module deliberately depends on none of the
other trading packages.

Each record is a single JSON line on stdout (collected by Grafana Alloy and
shipped to Loki) carrying, when relevant: timestamp, severity, service,
environment, event_id, correlation_id, causation_id, source, symbol,
strategy_id, decision_id and order_intent_id. High-cardinality identifiers
stay in the log body per the Technology spec (section 15). PAPER/LIVE and
dev/test records are filterable — and kept separate — by the mandatory
``environment`` field. Every record passes through central redaction before
serialisation.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Final

from opentelemetry import trace

from trading_observability.context import CONTEXT_FIELDS, current_context
from trading_observability.redaction import redact

# logging.LogRecord attributes that are not user-supplied extras.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class StructuredJsonFormatter(logging.Formatter):
    """Format records as redacted single-line JSON documents."""

    def __init__(self, *, service: str, environment: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field, value in current_context().items():
            document[field] = value
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_"):
                document[key] = value
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            document["trace_id"] = format(span_context.trace_id, "032x")
            document["span_id"] = format(span_context.span_id, "016x")
        if record.exc_info and record.exc_info[0] is not None:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(document), default=str, ensure_ascii=False)


def configure_logging(
    *,
    service: str,
    environment: str,
    level: int = logging.INFO,
    stream: object | None = None,
) -> logging.Logger:
    """Configure the root logger for uniform structured JSON output.

    Idempotent per process: replaces previously configured structured
    handlers instead of stacking duplicates.
    """
    formatter = StructuredJsonFormatter(service=service, environment=environment)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)  # type: ignore[arg-type]
    handler.setFormatter(formatter)
    handler.set_name("trading-structured-json")
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != "trading-structured-json"]
    root.addHandler(handler)
    root.setLevel(level)
    return root


__all__ = [
    "CONTEXT_FIELDS",
    "StructuredJsonFormatter",
    "configure_logging",
]
