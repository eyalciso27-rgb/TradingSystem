"""Canonical enums — SINGLE SOURCE OF TRUTH (IMP-0101).

Every value below is taken verbatim from the authoritative baseline documents
(see docs/baseline-v0.5/CONTRACT_AUTHORITY_MAP.json). JSON Schemas and
TypeScript types are GENERATED from this module by
scripts/generate_contracts.py — never edit the generated files and never
define these values anywhere else.

Per the Technology and Deployment specification, Python is the source of
truth for JSON Schemas, and TypeScript types are generated from them.

Adding a new enum: add the StrEnum class here with an authority reference,
append it to CANONICAL_ENUMS, run `uv run python scripts/generate_contracts.py`,
and add its expected values to tests/contracts/test_enums.py.
Changing an existing enum requires a schema version bump and a migration plan
per the Data Model specification — never mutate values in place.
"""

from enum import StrEnum
from typing import Final


class SourceEntryType(StrEnum):
    """Entry semantics extracted from a source instruction.

    Authority: Decision_Contract_v0.8 section 12.1 (SourceEntryType).
    ENTER_IMMEDIATELY is source intent only and never authorizes an
    automatic market order.
    """

    BUY_LIMIT_MAX = "BUY_LIMIT_MAX"
    BUY_STOP_TRIGGER = "BUY_STOP_TRIGGER"
    BUY_ZONE = "BUY_ZONE"
    ENTER_IMMEDIATELY = "ENTER_IMMEDIATELY"
    BUY_PULLBACK = "BUY_PULLBACK"
    CONDITIONAL_ENTRY = "CONDITIONAL_ENTRY"
    UNKNOWN = "UNKNOWN"


class DecisionOutcome(StrEnum):
    """Terminal outcome of a decision run.

    Authority: Decision_Contract_v0.8 section 12.1 (DecisionOutcome).
    """

    REJECT = "REJECT"
    WATCH = "WATCH"
    SHADOW = "SHADOW"
    APPROVE = "APPROVE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    EXPIRE = "EXPIRE"
    INVALID = "INVALID"


class OrderState(StrEnum):
    """Canonical order state machine states.

    Authority: Decision_Contract_v0.8 section 12.5
    (Canonical Order State Machine). UNKNOWN may only be resolved by
    reconciliation.
    """

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    CANCELLED_BY_POLICY = "CANCELLED_BY_POLICY"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class OrderType(StrEnum):
    """Canonical order types.

    Authority: Decision_Contract_v0.8 section 12.1 (OrderType).
    """

    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    MARKET = "MARKET"


class OrderRole(StrEnum):
    """Role of an order within a trade plan.

    Authority: Decision_Contract_v0.8 section 12.1 (OrderRole).
    """

    ENTRY = "ENTRY"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class PositionStatus(StrEnum):
    """Canonical position status.

    Authority: Decision_Contract_v0.8 section 12.1 (PositionStatus).
    Section 12 explicitly replaces older names (section 11.6 lifecycle
    labels are superseded).
    """

    OPENING = "OPENING"
    OPEN = "OPEN"
    REDUCING = "REDUCING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class IntegrationHealthStatus(StrEnum):
    """Health states of an integration/connector.

    Authority: Integration_Specification_v0.6 section 2.3
    (full integration-state table; supersedes the abbreviated example in
    section 2.2).
    """

    INITIALIZING = "INITIALIZING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    PAUSED = "PAUSED"
    DOWN = "DOWN"


class SourceType(StrEnum):
    """Canonical source/evidence origin classification.

    Authority: Integration_Specification_v0.6 section 2.4
    (Canonical Source Event, source_type).
    """

    WHATSAPP = "WHATSAPP"
    DISCORD = "DISCORD"
    TELEGRAM = "TELEGRAM"
    INVESTINGPRO = "INVESTINGPRO"
    NEWS = "NEWS"
    FILING = "FILING"
    MARKET = "MARKET"


class EvidenceRole(StrEnum):
    """Role of evidence in a fusion run.

    Authority: Data_Model_and_Retention_Specification_v0.6
    (fusion_evidence.role: REQUIRED, OPTIONAL, VETO or CONTEXT).
    """

    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    VETO = "VETO"
    CONTEXT = "CONTEXT"


class TelegramAccessMode(StrEnum):
    """Telegram-specific access mode recorded on profiles and events.

    Authority: Data_Model_and_Retention_Specification_v0.6 sections 30.1/30.2.
    USER_ACCOUNT_TDLIB is required-primary; BOT_API is supplemental and can
    never satisfy the user-account requirement.
    """

    BOT_API = "BOT_API"
    USER_ACCOUNT_TDLIB = "USER_ACCOUNT_TDLIB"


class SocialAccessMode(StrEnum):
    """Cross-provider access mode of a social connection profile.

    Authority: Data_Model_and_Retention_Specification_v0.6 section 31
    (social_connection_profile.access_mode).
    """

    WHATSAPP_USER = "WHATSAPP_USER"
    DISCORD_USER = "DISCORD_USER"
    TELEGRAM_USER_TDLIB = "TELEGRAM_USER_TDLIB"
    TELEGRAM_BOT = "TELEGRAM_BOT"


class OperatingMode(StrEnum):
    """Canonical operating modes.

    Authority: MVP_Scope_v0.6 section 4. LIVE modes are prohibited by
    baseline policy (BASELINE_MANIFEST.json live_enabled=false); PAPER_AUTO
    requires explicit enablement. There is no LIVE_AUTO mode.
    """

    RESEARCH = "RESEARCH"
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    PAPER_MANUAL = "PAPER_MANUAL"
    PAPER_AUTO = "PAPER_AUTO"
    LIVE_MANUAL = "LIVE_MANUAL"
    LIVE_LIMITED_AUTO = "LIVE_LIMITED_AUTO"


class Environment(StrEnum):
    """Environment axis, separate from operating mode.

    Authority: Data_Model_and_Retention_Specification_v0.6 section 4
    (environment text: RESEARCH/PAPER/LIVE per Contract). There is no
    default environment and no fallback.
    """

    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE = "LIVE"


CANONICAL_ENUMS: Final[tuple[type[StrEnum], ...]] = (
    SourceEntryType,
    DecisionOutcome,
    OrderState,
    OrderType,
    OrderRole,
    PositionStatus,
    IntegrationHealthStatus,
    SourceType,
    EvidenceRole,
    TelegramAccessMode,
    SocialAccessMode,
    OperatingMode,
    Environment,
)

SCHEMA_VERSION: Final[str] = "v1"
