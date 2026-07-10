"""Broker core port: the canonical internal broker boundary (IMP-1001).

Authority: Integration_Specification_v0.6 section 9.1 (BrokerAdapter
contract: connect/disconnect/capabilities/resolveInstrument/
getAccountSnapshot/getPositions/getOpenOrders/previewOrder/submitOrder/
modifyOrder/cancelOrder/streamOrderEvents/reconcile/healthCheck), section
9.2 (Execution Agent is the only consumer; disconnect means no new orders
until successful reconciliation), section 9.4 (broker events are
append-only and processed idempotently), section 20.4 (Broker Capability
Matrix modes; emulation needs explicit policy and is never a default);
Decision_Contract section 12.5 (canonical OrderState; UNKNOWN may only be
resolved by reconciliation); System_Functional 40.4 (submission requires a
one-time SubmitAuthorization).

This module defines the PORT ONLY — an internal boundary, not an adapter.
Every future adapter (including IBKR, which lives exclusively in
``trading_broker_ibkr``) must implement ``BrokerPort`` and pass the
conformance suite in ``trading_broker_core.conformance``. Nothing here
connects anywhere: there is no network code, no SDK import, no secret and
no real order. ADR-0014: fixtures/fakes only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from trading_contracts.enums import Environment, OrderState, OrderType

PORT_VERSION = "broker-port-1.0.0"


class BrokerCapabilityMode(StrEnum):
    """Support mode of one broker capability (Integration section 20.4)."""

    NATIVE = "NATIVE"
    EMULATED = "EMULATED"
    MANUAL_ONLY = "MANUAL_ONLY"
    POLLING_EMULATION = "POLLING_EMULATION"
    UNSUPPORTED = "UNSUPPORTED"
    DISABLED = "DISABLED"


class OrderSide(StrEnum):
    """Direction of an order at the broker boundary."""

    BUY = "BUY"
    SELL = "SELL"


class BrokerErrorCode(StrEnum):
    """Canonical broker error taxonomy (all failures are fail-closed)."""

    NOT_CONNECTED = "NOT_CONNECTED"
    BROKER_UNHEALTHY = "BROKER_UNHEALTHY"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"
    ACCOUNT_UNKNOWN = "ACCOUNT_UNKNOWN"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    AUTHORIZATION_MISSING = "AUTHORIZATION_MISSING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    BROKER_REJECTED = "BROKER_REJECTED"
    UNKNOWN_BROKER_RESPONSE = "UNKNOWN_BROKER_RESPONSE"


class BrokerPortError(Exception):
    """Fail-closed broker-port failure carrying the canonical taxonomy."""

    def __init__(
        self, code: BrokerErrorCode, message: str, *, correlation_id: str | None = None
    ) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.correlation_id = correlation_id


def _require_aware(stamp: datetime, name: str) -> None:
    if stamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    """Capability discovery result (Integration section 20.4).

    ``fractional`` and ``outside_rth`` are DISABLED in the MVP; core code
    must consult this matrix instead of assuming support. Emulated modes
    require explicit policy at the call site — the port only reports them.
    """

    order_types: Mapping[OrderType, BrokerCapabilityMode]
    bracket: BrokerCapabilityMode
    oco: BrokerCapabilityMode
    trailing: BrokerCapabilityMode
    fractional: BrokerCapabilityMode
    outside_rth: BrokerCapabilityMode
    what_if: BrokerCapabilityMode
    streaming_events: BrokerCapabilityMode


@dataclass(frozen=True, slots=True)
class InstrumentQuery:
    symbol: str
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerInstrument:
    """Broker-resolved instrument; ``broker_instrument_ref`` is opaque."""

    broker_instrument_ref: str
    symbol: str
    exchange: str
    currency: str


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A NOT-YET-SUBMITTED domain order intent handed to the port.

    ``client_order_id`` is the idempotency key: submitting the same id
    twice must return the original receipt and create nothing new.
    ``authorization_ref`` references the one-time SubmitAuthorization from
    the final pre-submit check (40.4); an empty ref fails closed. The
    intent itself never carries a submitted status — the port returns
    state via receipts/events only.
    """

    client_order_id: str
    authorization_ref: str
    correlation_id: str
    environment: Environment
    account_alias: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id is mandatory")
        if not self.correlation_id.strip():
            raise ValueError("correlation_id is mandatory")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and (
            self.limit_price is None or self.limit_price <= 0
        ):
            raise ValueError(f"{self.order_type.value} requires a positive limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and (
            self.stop_price is None or self.stop_price <= 0
        ):
            raise ValueError(f"{self.order_type.value} requires a positive stop_price")


@dataclass(frozen=True, slots=True)
class OrderPreview:
    """What-if result; never places anything."""

    client_order_id: str
    estimated_notional: Decimal
    correlation_id: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrokerOrderReceipt:
    """Port acknowledgement of a submit/modify; carries full provenance."""

    client_order_id: str
    broker_order_ref: str
    state: OrderState
    authorization_ref: str
    correlation_id: str
    received_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    client_order_id: str
    broker_order_ref: str
    state: OrderState
    correlation_id: str
    received_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class BrokerOrderEvent:
    """One normalized, append-only order event (section 9.4).

    ``sequence`` is per-broker-connection monotonic so consumers can
    process idempotently; replaying yields identical events.
    """

    sequence: int
    client_order_id: str
    broker_order_ref: str
    state: OrderState
    filled_quantity: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: Decimal
    average_price: Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_alias: str
    environment: Environment
    net_liquidation_value: Decimal
    cash: Decimal
    buying_power: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class IntegrationHealth:
    healthy: bool
    detail: str
    checked_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.checked_at, "checked_at")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Startup/periodic reconciliation snapshot (section 9.5).

    Reconciliation is the ONLY way an UNKNOWN order state is resolved
    (Decision_Contract 12.5).
    """

    account_alias: str
    snapshot: AccountSnapshot
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrderReceipt, ...]
    unresolved_client_order_ids: tuple[str, ...] = ()
    reconciled_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        _require_aware(self.reconciled_at, "reconciled_at")


class ModifyOrderRequest(Protocol):
    """Reserved: modify is in the section 9.1 contract; concrete shape is
    adapter-phase work (IMP-1002+). The port keeps the slot without
    inventing fields the authority does not fix yet."""


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    client_order_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id is mandatory")


class BrokerPort(Protocol):
    """The canonical broker boundary (Integration 9.1), synchronous form.

    Implementations must be deterministic for identical broker state and
    inputs, raise ``BrokerPortError`` with the canonical taxonomy on every
    failure (fail closed — never guess), and never mutate state on error.
    """

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def capabilities(self) -> BrokerCapabilities: ...

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument: ...

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot: ...

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]: ...

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]: ...

    def preview_order(self, intent: OrderIntent) -> OrderPreview: ...

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt: ...

    def cancel_order(self, request: CancelOrderRequest) -> CancelReceipt: ...

    def order_events(self) -> Iterator[BrokerOrderEvent]: ...

    def reconcile(self, account_alias: str) -> ReconciliationResult: ...

    def health_check(self) -> IntegrationHealth: ...


__all__ = [
    "PORT_VERSION",
    "AccountSnapshot",
    "BrokerCapabilities",
    "BrokerCapabilityMode",
    "BrokerErrorCode",
    "BrokerInstrument",
    "BrokerOrderEvent",
    "BrokerOrderReceipt",
    "BrokerPort",
    "BrokerPortError",
    "BrokerPosition",
    "CancelOrderRequest",
    "CancelReceipt",
    "InstrumentQuery",
    "IntegrationHealth",
    "ModifyOrderRequest",
    "OrderIntent",
    "OrderPreview",
    "OrderSide",
    "ReconciliationResult",
]
