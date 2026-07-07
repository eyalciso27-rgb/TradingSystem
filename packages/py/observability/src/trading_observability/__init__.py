"""Uniform observability helpers: structured logging, correlation, redaction.

Operational logs / metrics / traces only. The tamper-evident audit trail
lives in ``trading_persistence.audit`` and is not replaced by anything here.
"""

from trading_observability.context import (
    CONTEXT_FIELDS,
    bind_log_context,
    current_context,
    new_correlation_id,
)
from trading_observability.logging import StructuredJsonFormatter, configure_logging
from trading_observability.redaction import REDACTED, is_sensitive_key, redact
from trading_observability.timeline import (
    TIMELINE_STAGES,
    BrokenTimelineError,
    TimelineEntry,
    build_timeline,
)

__all__ = [
    "CONTEXT_FIELDS",
    "REDACTED",
    "TIMELINE_STAGES",
    "BrokenTimelineError",
    "StructuredJsonFormatter",
    "TimelineEntry",
    "bind_log_context",
    "build_timeline",
    "configure_logging",
    "current_context",
    "is_sensitive_key",
    "new_correlation_id",
    "redact",
]
