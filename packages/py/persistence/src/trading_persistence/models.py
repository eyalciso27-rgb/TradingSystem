"""Core PostgreSQL schema (IMP-0201).

Broker-neutral core tables backing the canonical contracts (IMP-0101..0105):
identities, sources/evidence, candidates/assessments/decisions, trade plans,
risk, order intents, normalized broker orders/events, fills, positions, and
config versions + active pointers.

Authority: Data_Model_and_Retention_Specification_v0.6 sections 6-7. Enum-like
values are stored as ``text`` + CHECK against the canonical enums (no native
PG ENUM). Money/price are NUMERIC (never FLOAT); every timestamp is
TIMESTAMPTZ; UUID primary keys are generated in the application layer.

Deferred to later phases (not part of the IMP-0201 core): the many
analysis/news/fundamental/feature tables (engine phases), point-in-time
superseded/effective columns and as-of history (IMP-0202), the transactional
outbox / consumer inbox (IMP-0203/0204), and tamper-evident audit_events
(IMP-0304).
"""

from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime
from trading_contracts.enums import (
    ActionTier,
    AssessmentStatus,
    CalibrationStatus,
    DecisionOutcome,
    Environment,
    OperatingMode,
    OrderRole,
    OrderState,
    OrderType,
    PositionStatus,
    RiskDecisionOutcome,
    SignalDirection,
    SignalFamily,
    SocialAccessMode,
    SourceType,
    TradePlanStatus,
)

from trading_persistence.base import Base, enum_check, raw_check, values_check

CONFIG_TYPES = ("POLICY", "STRATEGY", "PROVIDER", "SOURCE")


def _uuid_pk() -> Mapped[PyUUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True)


def _ts(**kw: object) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)  # type: ignore[arg-type]


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[PyUUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)
    updated_at: Mapped[datetime] = _ts(nullable=False)


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[PyUUID] = _uuid_pk()
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str | None] = mapped_column(Text)
    asset_class: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)
    updated_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (UniqueConstraint("symbol", "venue"),)


class InstrumentAlias(Base):
    __tablename__ = "instrument_aliases"

    id: Mapped[PyUUID] = _uuid_pk()
    instrument_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_type: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = _ts(nullable=False)
    valid_to: Mapped[datetime | None] = _ts()

    __table_args__ = (Index("ix_instrument_aliases_instrument_id", "instrument_id"),)


class RawObject(Base):
    __tablename__ = "raw_objects"

    id: Mapped[PyUUID] = _uuid_pk()
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    compression: Mapped[str | None] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (UniqueConstraint("content_hash"),)


class SourceEvent(Base):
    __tablename__ = "source_events"

    id: Mapped[PyUUID] = _uuid_pk()
    workspace_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    connector_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True))
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Point-in-time fields (IMP-0202, Data_Model v0.6 section 24.2):
    # observed = world time, ingested = system intake, effective = when the
    # datum applies, available = first moment usable in a decision,
    # superseded = when replaced by a newer revision.
    observed_at: Mapped[datetime] = _ts(nullable=False)
    ingested_at: Mapped[datetime] = _ts(nullable=False)
    effective_at: Mapped[datetime | None] = _ts()
    available_at: Mapped[datetime] = _ts(nullable=False)
    superseded_at: Mapped[datetime | None] = _ts()
    content_language: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    raw_object_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_objects.id")
    )
    current_revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_deleted_at_source: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("source_type", SourceType, "source_events_source_type"),
        Index(
            "uq_source_events_connector_external",
            "connector_id",
            "external_event_id",
            unique=True,
            postgresql_where=text("external_event_id IS NOT NULL"),
        ),
        Index("ix_source_events_observed_at", "observed_at"),
        Index("ix_source_events_available_at", "available_at"),
        Index("ix_source_events_content_hash", "content_hash"),
    )


class SourceAttachment(Base):
    __tablename__ = "source_attachments"

    id: Mapped[PyUUID] = _uuid_pk()
    source_event_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_events.id"), nullable=False
    )
    raw_object_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_objects.id")
    )
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)


class SocialMessageObservation(Base):
    __tablename__ = "social_message_observations"

    id: Mapped[PyUUID] = _uuid_pk()
    source_event_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_events.id")
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    access_mode: Mapped[str] = mapped_column(Text, nullable=False)
    connector_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_at: Mapped[datetime] = _ts(nullable=False)
    message_dedup_key: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        enum_check("source_type", SourceType, "social_obs_source_type"),
        enum_check("access_mode", SocialAccessMode, "social_obs_access_mode"),
        raw_check("revision >= 1", "social_obs_revision"),
        UniqueConstraint(
            "access_mode",
            "source_account_id",
            "channel_external_id",
            "external_message_id",
            "revision",
            name="uq_social_obs_identity",
        ),
        Index("ix_social_message_observations_message_dedup_key", "message_dedup_key"),
    )


class TradeCandidate(Base):
    __tablename__ = "trade_candidates"

    id: Mapped[PyUUID] = _uuid_pk()
    workspace_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    instrument_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_version: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    processing_profile: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False)
    valid_until: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("direction", SignalDirection, "trade_candidates_direction"),
        Index("ix_trade_candidates_status", "status"),
        Index("ix_trade_candidates_instrument_id", "instrument_id"),
    )


class SignalAssessment(Base):
    __tablename__ = "signal_assessments"

    id: Mapped[PyUUID] = _uuid_pk()
    candidate_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_candidates.id")
    )
    instrument_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    signal_family: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[object] = mapped_column(Numeric(18, 10), nullable=False)
    calibrated_probability: Mapped[object | None] = mapped_column(Numeric(18, 10))
    calibration_status: Mapped[str] = mapped_column(Text, nullable=False)
    # Point-in-time fields (IMP-0202, Data_Model v0.6 section 24.2).
    observed_at: Mapped[datetime] = _ts(nullable=False)
    ingested_at: Mapped[datetime] = _ts(nullable=False)
    effective_at: Mapped[datetime | None] = _ts()
    available_at: Mapped[datetime] = _ts(nullable=False)
    superseded_at: Mapped[datetime | None] = _ts()
    valid_until: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("signal_family", SignalFamily, "signal_assessments_family"),
        enum_check("direction", SignalDirection, "signal_assessments_direction"),
        enum_check("status", AssessmentStatus, "signal_assessments_status"),
        enum_check("calibration_status", CalibrationStatus, "signal_assessments_calibration"),
        raw_check(
            "calibrated_probability IS NULL OR "
            "(calibrated_probability >= 0 AND calibrated_probability <= 1)",
            "signal_assessments_probability_range",
        ),
        Index("ix_signal_assessments_instrument_id", "instrument_id"),
    )


class DecisionRun(Base):
    __tablename__ = "decision_runs"

    id: Mapped[PyUUID] = _uuid_pk()
    candidate_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trade_candidates.id"), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(Text, nullable=False)
    decision_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_snapshot_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True))
    processing_profile: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    action_tier: Mapped[str | None] = mapped_column(Text)
    score_display: Mapped[object | None] = mapped_column(Numeric(8, 4))
    data_quality: Mapped[object] = mapped_column(Numeric(18, 10), nullable=False)
    started_at: Mapped[datetime] = _ts(nullable=False)
    completed_at: Mapped[datetime] = _ts(nullable=False)
    deadline_at: Mapped[datetime | None] = _ts()
    expires_at: Mapped[datetime | None] = _ts()
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    decision_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("outcome", DecisionOutcome, "decision_runs_outcome"),
        raw_check(
            "action_tier IS NULL OR action_tier IN "
            "(" + ", ".join(f"'{m.value}'" for m in ActionTier) + ")",
            "decision_runs_action_tier",
        ),
        Index("ix_decision_runs_outcome", "outcome", "completed_at"),
    )


class SystemTradePlan(Base):
    __tablename__ = "system_trade_plans"

    id: Mapped[PyUUID] = _uuid_pk()
    decision_run_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_runs.id"), nullable=False
    )
    instrument_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    invalidation_summary: Mapped[str] = mapped_column(Text, nullable=False)
    max_loss_amount: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    valid_from: Mapped[datetime] = _ts(nullable=False)
    valid_until: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("direction", SignalDirection, "system_trade_plans_direction"),
        enum_check("environment", Environment, "system_trade_plans_environment"),
        enum_check("status", TradePlanStatus, "system_trade_plans_status"),
        UniqueConstraint("decision_run_id", "plan_version"),
        Index("ix_system_trade_plans_environment", "environment"),
    )


class RiskEvaluation(Base):
    __tablename__ = "risk_evaluations"

    id: Mapped[PyUUID] = _uuid_pk()
    decision_run_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decision_runs.id"), nullable=False
    )
    trade_plan_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_trade_plans.id")
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    approved_quantity: Mapped[object | None] = mapped_column(Numeric(28, 10))
    approved_risk_amount: Mapped[object | None] = mapped_column(Numeric(20, 8))
    stop_distance: Mapped[object | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    risk_policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = _ts(nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("outcome", RiskDecisionOutcome, "risk_evaluations_outcome"),
        raw_check(
            "approved_quantity IS NULL OR approved_quantity > 0",
            "risk_evaluations_quantity_positive",
        ),
    )


class RiskVeto(Base):
    __tablename__ = "risk_vetoes"

    id: Mapped[PyUUID] = _uuid_pk()
    risk_evaluation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_evaluations.id"), nullable=False
    )
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False)


class Broker(Base):
    __tablename__ = "brokers"

    id: Mapped[PyUUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (UniqueConstraint("name"),)


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[PyUUID] = _uuid_pk()
    broker_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brokers.id"), nullable=False
    )
    account_alias: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)
    updated_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("environment", Environment, "broker_accounts_environment"),
        UniqueConstraint("broker_id", "account_alias", "environment"),
    )


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id: Mapped[PyUUID] = _uuid_pk()
    trade_plan_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_trade_plans.id"), nullable=False
    )
    risk_evaluation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_evaluations.id")
    )
    broker_account_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id"), nullable=False
    )
    parent_intent_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_intents.id")
    )
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    operating_mode: Mapped[str] = mapped_column(Text, nullable=False)
    order_role: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[object] = mapped_column(Numeric(28, 10), nullable=False)
    limit_price: Mapped[object | None] = mapped_column(Numeric(20, 8))
    stop_price: Mapped[object | None] = mapped_column(Numeric(20, 8))
    time_in_force: Mapped[str] = mapped_column(Text, nullable=False)
    outside_rth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("environment", Environment, "order_intents_environment"),
        enum_check("operating_mode", OperatingMode, "order_intents_operating_mode"),
        enum_check("order_role", OrderRole, "order_intents_order_role"),
        enum_check("order_type", OrderType, "order_intents_order_type"),
        raw_check("quantity > 0", "order_intents_quantity_positive"),
        UniqueConstraint("environment", "broker_account_id", "idempotency_key"),
    )


class BrokerOrder(Base):
    __tablename__ = "broker_orders"

    id: Mapped[PyUUID] = _uuid_pk()
    order_intent_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_intents.id"), nullable=False
    )
    broker_account_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id"), nullable=False
    )
    external_order_ref: Mapped[str] = mapped_column(Text, nullable=False)
    current_status: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime | None] = _ts()
    last_broker_update_at: Mapped[datetime | None] = _ts()
    raw_payload_ref: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("current_status", OrderState, "broker_orders_current_status"),
        UniqueConstraint("broker_account_id", "external_order_ref"),
        Index("ix_broker_orders_external_order_ref", "external_order_ref"),
    )


class BrokerOrderEvent(Base):
    __tablename__ = "broker_order_events"

    id: Mapped[PyUUID] = _uuid_pk()
    broker_order_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_orders.id"), nullable=False
    )
    order_state: Mapped[str] = mapped_column(Text, nullable=False)
    external_execution_ref: Mapped[str | None] = mapped_column(Text)
    filled_quantity: Mapped[object] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    remaining_quantity: Mapped[object] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    last_fill_quantity: Mapped[object | None] = mapped_column(Numeric(28, 10))
    average_fill_price: Mapped[object | None] = mapped_column(Numeric(20, 8))
    last_fill_price: Mapped[object | None] = mapped_column(Numeric(20, 8))
    commission: Mapped[object | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    raw_payload_ref: Mapped[str | None] = mapped_column(Text)
    event_at: Mapped[datetime] = _ts(nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("order_state", OrderState, "broker_order_events_order_state"),
        raw_check("filled_quantity >= 0", "broker_order_events_filled_nonneg"),
        raw_check("remaining_quantity >= 0", "broker_order_events_remaining_nonneg"),
        Index("ix_broker_order_events_broker_order_id", "broker_order_id", "event_at"),
    )


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[PyUUID] = _uuid_pk()
    broker_order_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_orders.id"), nullable=False
    )
    external_execution_ref: Mapped[str] = mapped_column(Text, nullable=False)
    executed_at: Mapped[datetime] = _ts(nullable=False)
    quantity: Mapped[object] = mapped_column(Numeric(28, 10), nullable=False)
    price: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False)
    commission: Mapped[object | None] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    liquidity_flag: Mapped[str | None] = mapped_column(Text)
    raw_payload_ref: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        raw_check("quantity > 0", "fills_quantity_positive"),
        UniqueConstraint("broker_order_id", "external_execution_ref"),
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[PyUUID] = _uuid_pk()
    broker_account_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id"), nullable=False
    )
    instrument_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    trade_plan_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("system_trade_plans.id")
    )
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_version: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    quantity_open: Mapped[object] = mapped_column(Numeric(28, 10), nullable=False)
    average_entry_price: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False)
    realized_pnl: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    unrealized_pnl: Mapped[object] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reconciliation_status: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime] = _ts(nullable=False)
    closed_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("environment", Environment, "positions_environment"),
        enum_check("status", PositionStatus, "positions_status"),
        raw_check("quantity_open >= 0", "positions_quantity_nonneg"),
        Index("ix_positions_status", "status"),
        Index("ix_positions_instrument_id", "instrument_id"),
    )


class OutboxMessage(Base):
    """Transactional outbox row (IMP-0203, Data_Model v0.6 section 24.3).

    Written in the same transaction as the domain state; a separate publisher
    later publishes to the event bus using ``id`` as the message id and marks
    the row PUBLISHED. Crash after commit therefore yields exactly one
    logical event.
    """

    __tablename__ = "outbox_messages"

    id: Mapped[PyUUID] = _uuid_pk()
    aggregate_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _ts(nullable=False)
    published_at: Mapped[datetime | None] = _ts()

    __table_args__ = (
        values_check("status", ("PENDING", "PUBLISHED"), "outbox_messages_status"),
        raw_check("attempts >= 0", "outbox_messages_attempts_nonneg"),
        raw_check(
            "(status = 'PUBLISHED') = (published_at IS NOT NULL)",
            "outbox_messages_published_consistency",
        ),
        Index("ix_outbox_messages_status_created_at", "status", "created_at"),
    )


class ConsumerInboxRecord(Base):
    """Consumer inbox row (IMP-0204, Data_Model v0.6 section 24.3).

    One row per (consumer_id, message_id): the idempotency key at every
    service boundary. The inbox row and the business effect are written in
    the same transaction, so duplicate deliveries become no-ops and a crash
    mid-processing rolls back both, allowing a clean retry.
    """

    __tablename__ = "consumer_inbox"

    id: Mapped[PyUUID] = _uuid_pk()
    consumer_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = _ts(nullable=False)
    completed_at: Mapped[datetime | None] = _ts()
    result_hash: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("consumer_id", "message_id"),)


class AuditEvent(Base):
    """Tamper-evident audit record (IMP-0304, Data_Model section 6.8).

    Append-only hash chain: every record carries the previous record's hash
    and its own hash over the canonical field serialization, so mutation,
    deletion, reordering or forged insertion breaks verification. Sensitive
    payloads are never stored inline — only a redacted detail or an opaque
    payload_ref.
    """

    __tablename__ = "audit_events"

    id: Mapped[PyUUID] = _uuid_pk()
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_ref: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_ref: Mapped[str | None] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    payload_ref: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True))
    occurred_at: Mapped[datetime] = _ts(nullable=False)
    previous_hash: Mapped[str] = mapped_column(Text, nullable=False)
    current_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        enum_check("environment", Environment, "audit_events_environment"),
        UniqueConstraint("sequence_no"),
        UniqueConstraint("current_hash"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )


class ConfigVersion(Base):
    __tablename__ = "config_versions"

    id: Mapped[PyUUID] = _uuid_pk()
    config_type: Mapped[str] = mapped_column(String(16), nullable=False)
    config_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    environments: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime] = _ts(nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        values_check("config_type", CONFIG_TYPES, "config_versions_config_type"),
        UniqueConstraint("config_type", "config_id", "version"),
        Index("ix_config_versions_config_id", "config_type", "config_id"),
    )


class ActiveVersionPointer(Base):
    __tablename__ = "active_version_pointers"

    id: Mapped[PyUUID] = _uuid_pk()
    config_type: Mapped[str] = mapped_column(String(16), nullable=False)
    config_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = _ts(nullable=False)
    activated_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts(nullable=False)

    __table_args__ = (
        values_check("config_type", CONFIG_TYPES, "active_pointers_config_type"),
        enum_check("environment", Environment, "active_pointers_environment"),
        UniqueConstraint("config_type", "config_id", "environment"),
    )
