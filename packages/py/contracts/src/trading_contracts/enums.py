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


class SignalFamily(StrEnum):
    """Family of a signal assessment.

    Authority: Decision_Contract_v0.8 section 13.2 (SignalAssessment
    signal_family).
    """

    TECHNICAL = "TECHNICAL"
    FUNDAMENTAL = "FUNDAMENTAL"
    NEWS = "NEWS"
    QUANT_ML = "QUANT_ML"
    EXTERNAL = "EXTERNAL"
    SOCIAL = "SOCIAL"


class SignalDirection(StrEnum):
    """Directional bias of an assessment, candidate or plan.

    Authority: Decision_Contract_v0.8 section 13.2 (SignalAssessment
    direction).
    """

    LONG = "LONG"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class AssessmentStatus(StrEnum):
    """Status of a signal assessment.

    Authority: Decision_Contract_v0.8 section 13.2 (SignalAssessment status).
    """

    TRIGGERED = "TRIGGERED"
    ELIGIBLE = "ELIGIBLE"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    EXPIRED = "EXPIRED"


class CalibrationStatus(StrEnum):
    """Calibration status of a model-produced probability.

    Authority: Decision_Contract_v0.8 section 12.2. PAPER_AUTO requires
    CALIBRATED for any AI task used as a gate.
    """

    UNVERIFIED = "UNVERIFIED"
    HEURISTIC = "HEURISTIC"
    CALIBRATED = "CALIBRATED"


class ActionTier(StrEnum):
    """Action tier of a decision.

    Authority: Master_Architecture_Baseline_v0.3 section 5 (Action Model).
    """

    AUTO_FULL = "AUTO_FULL"
    AUTO_REDUCED = "AUTO_REDUCED"
    MANUAL = "MANUAL"
    SHADOW = "SHADOW"
    WATCH = "WATCH"
    REJECT = "REJECT"
    HALT = "HALT"


class RiskDecisionOutcome(StrEnum):
    """Outcome of a risk evaluation; the Risk Engine holds absolute veto.

    Authority: Decision_Contract_v0.8 section 12.3 and
    Data_Model_and_Retention_Specification_v0.6 (risk_evaluations:
    APPROVE/REDUCE/VETO).
    """

    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    VETO = "VETO"


class TradePlanStatus(StrEnum):
    """Lifecycle status of a system trade plan.

    Authority: Data_Model_and_Retention_Specification_v0.6 section 7.6
    (system_trade_plans.status).
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class NotificationSeverity(StrEnum):
    """Severity of an operational notification / alert.

    Authority: System_Functional_Specification_v0.7 section 20 and
    Integration_Specification_v0.6 section 20 (CRITICAL / HIGH / MEDIUM /
    LOW). Critical notifications additionally require dedup, correlation_id
    and acknowledgement (Technology spec section 32.3).
    """

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class NotificationChannel(StrEnum):
    """Delivery channel for an operational notification (MVP).

    Authority: Claude_Code_Implementation_Plan IMP-0405 scope (UI + Telegram
    private alerts); Operations_Console_and_UX_Specification_v0.4 section 14
    (in-app alert drawer) and Integration_Specification_v0.6 section 33.4
    (Telegram send_notification to a defined private chat). Notification
    channels are outbound only and never carry trading commands.
    """

    UI = "UI"
    TELEGRAM = "TELEGRAM"


class FreshnessClass(StrEnum):
    """Freshness class of a market datum.

    Authority: Integration_Specification_v0.6 section 7.3. STALE is never
    allowed for a new decision; UNKNOWN fails closed. DELAYED serves
    research/replay only — never automatic trading, and there is no fallback
    to DELAYED when a strategy requires REALTIME (section 7.4).
    """

    REALTIME = "REALTIME"
    NEAR_REALTIME = "NEAR_REALTIME"
    DELAYED = "DELAYED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class MarketRegime(StrEnum):
    """Market regime classification.

    Authority: Portfolio_Capital_Allocation_and_Market_Regime_v0.1 section 5.
    UNKNOWN is an explicit state and is never treated as NORMAL (section 10).
    """

    TRENDING_RISK_ON = "TRENDING_RISK_ON"
    RANGE_BOUND = "RANGE_BOUND"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    RISK_OFF = "RISK_OFF"
    EVENT_STRESS = "EVENT_STRESS"
    UNKNOWN = "UNKNOWN"


class CorporateActionType(StrEnum):
    """Type of a corporate action event.

    Authority: System_Functional_Specification_v0.7 section 40.6 (Split,
    Reverse Split, Symbol Change, Delisting and Merger create a
    CorporateActionEvent) and the IMP-0705 scope.
    """

    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"
    MERGER = "MERGER"


class MarketSessionStatus(StrEnum):
    """Session status of a market at a point in time.

    Authority: Integration_Specification_v0.6 section 7.2 (Session: Regular,
    Pre-market, After-hours, Halt/Closed) and System_Functional v0.7 section
    40.5 (the authoritative calendar defines Open, Close, Early Close,
    Holiday and Halt).
    """

    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    HALTED = "HALTED"
    CLOSED = "CLOSED"


class HaltScope(StrEnum):
    """Scope levels of an emergency halt / kill switch.

    Authority: Security_and_Operations_Specification_v0.6 section 12 (Kill
    Switch levels). A halt's default behavior is to stop new entries while
    preserving existing protective orders (section 12 preamble, 24.1
    KEEP_PROTECTION default).
    """

    GLOBAL_ENTRY_HALT = "GLOBAL_ENTRY_HALT"
    ENVIRONMENT_HALT = "ENVIRONMENT_HALT"
    BROKER_HALT = "BROKER_HALT"
    ACCOUNT_HALT = "ACCOUNT_HALT"
    STRATEGY_HALT = "STRATEGY_HALT"
    SYMBOL_HALT = "SYMBOL_HALT"
    SOURCE_HALT = "SOURCE_HALT"
    MODEL_HALT = "MODEL_HALT"


class EmergencyCommandType(StrEnum):
    """Emergency commands recorded in the append-only emergency log.

    Authority: Security_and_Operations_Specification_v0.6 section 12.2
    (activation / cancellation of a Kill Switch — ACTIVATE_HALT / RESUME)
    and section 12.1 (position emergency actions: cancel unfilled entries,
    cancel-and-rebuild protection, reduce risk, flatten account). Flatten is
    a separate, explicitly approved, re-authenticated action (12.2).
    """

    ACTIVATE_HALT = "ACTIVATE_HALT"
    RESUME = "RESUME"
    CANCEL_UNFILLED_ENTRIES = "CANCEL_UNFILLED_ENTRIES"
    CANCEL_AND_REBUILD_PROTECTION = "CANCEL_AND_REBUILD_PROTECTION"
    REDUCE_RISK = "REDUCE_RISK"
    FLATTEN_ACCOUNT = "FLATTEN_ACCOUNT"


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
    SignalFamily,
    SignalDirection,
    AssessmentStatus,
    CalibrationStatus,
    ActionTier,
    RiskDecisionOutcome,
    TradePlanStatus,
    HaltScope,
    EmergencyCommandType,
    NotificationSeverity,
    NotificationChannel,
    FreshnessClass,
    MarketSessionStatus,
    CorporateActionType,
    MarketRegime,
)

# Canonical mapping from operating mode to its environment axis value
# (MVP_Scope_v0.6 section 4). Single source of truth — trading_config imports
# this rather than restating it.
OPERATING_MODE_TO_ENVIRONMENT: Final[dict[OperatingMode, Environment]] = {
    OperatingMode.RESEARCH: Environment.RESEARCH,
    OperatingMode.REPLAY: Environment.RESEARCH,
    OperatingMode.SHADOW: Environment.RESEARCH,
    OperatingMode.PAPER_MANUAL: Environment.PAPER,
    OperatingMode.PAPER_AUTO: Environment.PAPER,
    OperatingMode.LIVE_MANUAL: Environment.LIVE,
    OperatingMode.LIVE_LIMITED_AUTO: Environment.LIVE,
}

SCHEMA_VERSION: Final[str] = "v1"
