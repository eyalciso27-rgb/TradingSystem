"""Canonical decision/risk/order domain contracts — SOURCE OF TRUTH (IMP-0104).

Broker-neutral by construction: nothing here names a broker or carries a
broker-native identifier, status string, error code or raw payload. The
future broker adapter (packages/py/broker-ibkr) translates OrderIntent into a
broker order and normalizes broker callbacks into NormalizedBrokerEvent; the
domain never imports an adapter.

Authorities:

- Candidate / decision / plan / risk / order dictionary:
  Decision_Contract_v0.8 sections 11-13 and section 12 (canonical dictionary).
- Persistence field shapes (neutral): Data_Model_and_Retention_Specification
  v0.6 sections 7.5-7.7 (decision_runs, system_trade_plans, order_intents).
- Money/precision: Technology_and_Deployment_Decisions_v0.6 section 5
  (Decimal for every price, money, percentage and non-integer quantity; never
  float; currency separated from amount).
- Broker identifiers stay out of the domain: Decision_Contract_v0.8 (broker
  ids live in BrokerInstrumentMapping, not Instrument or OrderIntent).

Reused canonical enums (IMP-0101): DecisionOutcome, OrderType, OrderRole,
OrderState, Environment, OperatingMode, SourceEntryType, SignalFamily,
SignalDirection, AssessmentStatus, CalibrationStatus, ActionTier,
RiskDecisionOutcome, TradePlanStatus. Point-in-time event timestamps are
carried by EventEnvelope (IMP-0102), never duplicated here.

Fields without a canonical closed value list in the baseline (order `side`
and `time_in_force` — order_intents has the columns but no enumerated values;
Master_Architecture MVP is long/whole-shares) are modeled as constrained
uppercase-token strings, not invented enums. See README for the reported gap.
"""

from decimal import Decimal
from typing import Annotated, Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trading_contracts.enums import (
    OPERATING_MODE_TO_ENVIRONMENT,
    ActionTier,
    AssessmentStatus,
    CalibrationStatus,
    DecisionOutcome,
    Environment,
    OperatingMode,
    OrderRole,
    OrderState,
    OrderType,
    RiskDecisionOutcome,
    SignalDirection,
    SignalFamily,
    SourceEntryType,
    TradePlanStatus,
)

CURRENCY_PATTERN: Final[str] = r"^[A-Z]{3}$"
TOKEN_PATTERN: Final[str] = r"^[A-Z][A-Z0-9_]*$"
VERSION_PATTERN: Final[str] = r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$"

# Positive / non-negative monetary and quantity values, never NaN/Infinity.
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Score = Annotated[Decimal, Field(allow_inf_nan=False)]
Probability = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]

# Initial hard-veto reason codes (Decision_Contract_v0.8 section 6). The list
# is explicitly "initial" and extensible, so veto codes are validated as
# namespaced tokens rather than a closed enum.
HARD_VETO_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "DATA_STALE",
        "TICKER_AMBIGUOUS",
        "BROKER_UNSYNCED",
        "MARKET_DATA_UNAVAILABLE",
        "RISK_LIMIT_REACHED",
        "NO_EXIT_PLAN",
        "ORDER_NOT_SUPPORTED",
        "ENVIRONMENT_MISMATCH",
        "STRATEGY_NOT_PROMOTED",
        "DECISION_EXPIRED",
        "SCRAPER_DATA_INVALID",
        "MODEL_OUTPUT_INVALID",
    }
)

_Token = Annotated[str, Field(pattern=TOKEN_PATTERN)]


class _Strict(BaseModel):
    """Frozen, strict base: unknown fields rejected, instances immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class InstrumentRef(_Strict):
    """Broker-neutral instrument identity.

    instrument_id is the resolved canonical id; provider and broker-native
    contract identifiers and exchange routing live in the adapter layer, never
    on the domain instrument.
    """

    instrument_id: UUID
    symbol: str | None = None
    asset_class: str | None = None
    currency: Annotated[str, Field(pattern=CURRENCY_PATTERN)] | None = None


class SignalAssessment(_Strict):
    """Structured output of one analysis engine (Decision_Contract 13.2)."""

    instrument_id: UUID
    signal_family: SignalFamily
    model_id: str = Field(min_length=1)
    model_version: str = Field(pattern=VERSION_PATTERN)
    direction: SignalDirection
    status: AssessmentStatus
    score: Score
    calibrated_probability: Probability | None = None
    calibration_status: CalibrationStatus
    observed_at: AwareDatetime
    available_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    evidence_refs: tuple[str, ...] = ()
    risk_flags: tuple[_Token, ...] = ()
    invalidation_ref: str | None = None


class TradeCandidate(_Strict):
    """A trade idea routed to the decision engine (Decision_Contract 12)."""

    candidate_id: UUID
    instrument: InstrumentRef
    strategy_id: str = Field(min_length=1)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    direction: SignalDirection
    source_event_refs: tuple[str, ...] = ()
    created_at: AwareDatetime
    valid_until: AwareDatetime | None = None


class DecisionResult(_Strict):
    """Deterministic system decision for a candidate (decision_runs 7.5).

    LLMs never determine outcome, quantity or submission; this is the output
    of the deterministic decision engine.
    """

    decision_id: UUID
    candidate_id: UUID
    outcome: DecisionOutcome
    action_tier: ActionTier
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    decision_policy_version: str = Field(pattern=VERSION_PATTERN)
    supporting_assessment_refs: tuple[str, ...] = ()
    veto_reason_codes: tuple[_Token, ...] = ()
    decided_at: AwareDatetime
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class EntrySpec(_Strict):
    """Entry idea — not an executable order and with no final quantity."""

    entry_type: SourceEntryType
    order_type: OrderType
    limit_price: PositiveDecimal | None = None
    stop_trigger_price: PositiveDecimal | None = None
    max_chase_price: PositiveDecimal | None = None


class StopSpec(_Strict):
    """Protective stop specification (price- or percent-anchored)."""

    stop_price: PositiveDecimal | None = None
    stop_percent: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None = None
    anchor: str | None = None


class ProfitTarget(_Strict):
    """A profit target and the fraction of the position it closes."""

    price: PositiveDecimal
    quantity_fraction: Fraction


class TradePlan(_Strict):
    """Broker-neutral trade plan (Decision_Contract 11.2, Data_Model 7.6).

    Carries the entry idea, protection and targets — never the final position
    size, which only the Risk Engine determines (see RiskDecision).
    """

    trade_plan_id: UUID
    decision_id: UUID
    instrument: InstrumentRef
    direction: SignalDirection
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    risk_policy_version: str = Field(pattern=VERSION_PATTERN)
    status: TradePlanStatus
    environment: Environment
    thesis: str = Field(min_length=1)
    invalidation_summary: str = Field(min_length=1)
    entry: EntrySpec
    protective_stop: StopSpec
    profit_targets: tuple[ProfitTarget, ...] = ()
    max_loss_amount: PositiveDecimal
    currency: Annotated[str, Field(pattern=CURRENCY_PATTERN)]
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None


class RiskDecision(_Strict):
    """Deterministic risk evaluation with absolute veto (risk_evaluations).

    The Risk Engine can veto a trade even when the DecisionOutcome is APPROVE.
    An approved quantity exists only on APPROVE/REDUCE; a VETO carries reason
    codes and no approved quantity.
    """

    risk_decision_id: UUID
    decision_id: UUID
    trade_plan_id: UUID
    outcome: RiskDecisionOutcome
    approved_quantity: PositiveDecimal | None = None
    approved_risk_amount: NonNegativeDecimal | None = None
    stop_distance: PositiveDecimal | None = None
    currency: Annotated[str, Field(pattern=CURRENCY_PATTERN)] | None = None
    veto_reason_codes: tuple[_Token, ...] = ()
    risk_policy_version: str = Field(pattern=VERSION_PATTERN)
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_outcome(self) -> "RiskDecision":
        if self.outcome is RiskDecisionOutcome.VETO:
            if self.approved_quantity is not None:
                raise ValueError("a VETO risk decision must not carry approved_quantity")
            if not self.veto_reason_codes:
                raise ValueError("a VETO risk decision requires at least one veto_reason_code")
        else:
            if self.approved_quantity is None:
                raise ValueError(f"{self.outcome.value} requires approved_quantity")
            if self.veto_reason_codes:
                raise ValueError(f"{self.outcome.value} must not carry veto_reason_codes")
        return self


class OrderIntent(_Strict):
    """Broker-neutral intent to place/modify/cancel an order (order_intents).

    Not a broker order: no broker order id, native status, contract id or
    credentials. account_ref is an opaque account reference, never a
    credential. environment and operating_mode must be consistent.
    """

    intent_id: UUID
    decision_id: UUID
    trade_plan_id: UUID
    risk_decision_id: UUID
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    instrument: InstrumentRef
    order_role: OrderRole
    side: _Token
    order_type: OrderType
    quantity: PositiveDecimal
    limit_price: PositiveDecimal | None = None
    stop_price: PositiveDecimal | None = None
    time_in_force: _Token
    outside_rth: bool = False
    environment: Environment
    operating_mode: OperatingMode
    account_ref: str = Field(min_length=1)
    parent_intent_id: UUID | None = None
    idempotency_key: str = Field(min_length=1)
    valid_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def _validate_environment(self) -> "OrderIntent":
        expected = OPERATING_MODE_TO_ENVIRONMENT[self.operating_mode]
        if expected is not self.environment:
            raise ValueError(
                f"operating_mode {self.operating_mode.value} implies environment "
                f"{expected.value}, not {self.environment.value}"
            )
        return self


class NormalizedBrokerEvent(_Strict):
    """Broker-neutral, normalized view of a broker callback.

    Published inside an EventEnvelope (IMP-0102), which carries occurred/
    observed/available/ingested — so this payload holds no timestamps. Native
    broker status is converted to OrderState inside the adapter, not here; any
    raw payload is referenced opaquely, never inlined.
    """

    intent_id: UUID
    order_state: OrderState
    external_order_ref: str | None = None
    external_execution_ref: str | None = None
    filled_quantity: NonNegativeDecimal = Decimal(0)
    remaining_quantity: NonNegativeDecimal = Decimal(0)
    last_fill_quantity: PositiveDecimal | None = None
    average_fill_price: PositiveDecimal | None = None
    last_fill_price: PositiveDecimal | None = None
    commission: NonNegativeDecimal | None = None
    currency: Annotated[str, Field(pattern=CURRENCY_PATTERN)] | None = None
    rejection_reason: str | None = None
    raw_payload_ref: str | None = None


DECISION_MODELS: Final[tuple[type[BaseModel], ...]] = (
    InstrumentRef,
    SignalAssessment,
    TradeCandidate,
    DecisionResult,
    EntrySpec,
    StopSpec,
    ProfitTarget,
    TradePlan,
    RiskDecision,
    OrderIntent,
    NormalizedBrokerEvent,
)
