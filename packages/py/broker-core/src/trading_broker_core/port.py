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


class TimeInForce(StrEnum):
    """Canonical time-in-force at the broker boundary.

    The frozen ``trading_contracts`` baseline deliberately leaves
    ``time_in_force`` as an uppercase token string (documented gap: the MVP is
    whole-share/day). This enum is the typed, closed value list at the ADAPTER
    boundary, where a broker adapter MUST translate it to a concrete broker
    value — an empty/absent TIF is what IBKR rejects with error 10052. Only
    values an adapter is expected to support belong here; anything else fails
    closed at the adapter, never a silent default.
    """

    DAY = "DAY"
    GTC = "GTC"


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
    """Broker-resolved instrument; ``broker_instrument_ref`` is opaque.

    Opaque means opaque: only the adapter that minted a reference may interpret
    it. Application code compares references for equality and never parses
    them, because the moment it does, the broker's identity scheme becomes part
    of the domain.
    """

    broker_instrument_ref: str
    symbol: str
    exchange: str
    currency: str

    def __post_init__(self) -> None:
        if not self.broker_instrument_ref.strip():
            raise ValueError(
                "a resolved instrument must carry a broker reference; an"
                " unresolved security is not a security"
            )


@dataclass(frozen=True, slots=True)
class ProtectiveBracket:
    """The protective structure that MUST be established ATOMICALLY with an entry.

    Carried on an ``OrderIntent`` when — and only when — the entry must be born
    protected. Its presence tells the adapter to submit a native bracket: the
    entry parent and the protective child(ren) as one broker action, never the
    entry alone followed by a separately-transmitted stop. "Submit the entry,
    place the stop afterwards" is a window of unbounded exposure (ADR-0018,
    ADR-0019), so the protective level and the child correlation refs are frozen
    here, before submission, and travel with the entry.

    ``stop_price`` is the protective stop for the child order, distinct from the
    entry's own ``OrderIntent.stop_price`` (which is the aux price of a STOP-TYPE
    entry and is ``None`` for the canonical protected LIMIT entry). The child
    refs are OUR canonical correlation on each protective leg's ``orderRef`` —
    never broker deduplication — and are deterministic so a replay names the same
    legs.
    """

    #: The protective stop level for the resting protective child. Positive.
    stop_price: Decimal
    #: OUR canonical id for the protective STOP child (its ``orderRef``).
    stop_client_order_id: str
    #: Optional take-profit level; when present the adapter adds a LIMIT child in
    #: the same one-cancels-all group as the stop.
    take_profit_price: Decimal | None = None
    #: OUR canonical id for the take-profit child. REQUIRED iff a take-profit
    #: price is present — a leg with no id is a leg a replay cannot name.
    take_profit_client_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.stop_price <= 0:
            raise ValueError("protective stop_price must be positive")
        if not self.stop_client_order_id.strip():
            raise ValueError("stop_client_order_id is mandatory for a protective bracket")
        has_tp_price = self.take_profit_price is not None
        has_tp_ref = bool(
            self.take_profit_client_order_id
            and self.take_profit_client_order_id.strip()
        )
        if has_tp_price and self.take_profit_price is not None and self.take_profit_price <= 0:
            raise ValueError("take_profit_price, when present, must be positive")
        if has_tp_price != has_tp_ref:
            raise ValueError(
                "a take-profit leg needs BOTH a price and a client id, or neither"
            )
        if (
            self.take_profit_client_order_id is not None
            and self.take_profit_client_order_id.strip()
            == self.stop_client_order_id.strip()
        ):
            raise ValueError(
                "the stop and take-profit children must carry distinct client ids"
            )


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A NOT-YET-SUBMITTED domain order intent handed to the port.

    ``client_order_id`` is OUR idempotency key, and this port's contract is
    that submitting the same id twice returns the original receipt and creates
    nothing new. That guarantee is the ADAPTER's to keep -- it is not something
    the broker provides. IBKR in particular does not deduplicate on the
    ``orderRef`` the id travels in; two orders carrying one are two orders.
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
    #: THE security, as the broker itself identified it. Opaque to everyone
    #: outside the adapter that produced it.
    #:
    #: A ticker is not a security. "BRK.B" and "BRK B", a US listing and its
    #: foreign line, two share classes under one root -- these differ as
    #: securities while agreeing as strings, and an order routed on the string
    #: is an order routed on a guess. This carries the resolution the broker
    #: actually performed, so the security that was quoted, risked, authorized
    #: and re-checked is provably the security that is sent.
    broker_instrument_ref: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    #: Explicit, typed time-in-force. Defaults to DAY — the canonical MVP
    #: value for the POI BUY LIMIT entry and protective STOP. It is NEVER
    #: implicitly empty here; the adapter still validates it is a broker-
    #: supported TIF before ``placeOrder`` (IBKR rejects an empty TIF: 10052).
    time_in_force: TimeInForce = TimeInForce.DAY
    #: When present, the entry MUST be established atomically with this
    #: protective structure (a native bracket). ``None`` is an ordinary,
    #: unbracketed order and behaves exactly as before this field existed. The
    #: protective child ids must be distinct from the entry's own
    #: ``client_order_id`` so the three legs never collide.
    protective: ProtectiveBracket | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id is mandatory")
        if not self.broker_instrument_ref.strip():
            # Fail closed rather than letting the adapter fall back to a
            # ticker-shaped contract, which is exactly the substitution this
            # field exists to prevent.
            raise ValueError(
                "broker_instrument_ref is mandatory: an order must name the"
                " security the broker resolved, not a ticker"
            )
        if not self.correlation_id.strip():
            raise ValueError("correlation_id is mandatory")
        if not isinstance(self.time_in_force, TimeInForce):
            raise ValueError("time_in_force must be a TimeInForce")
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
        if self.protective is not None:
            # A protected entry must have a reference price for the parent, and
            # the three legs (entry + stop child + optional target child) must
            # carry three distinct client ids so nothing aliases.
            if self.order_type not in (OrderType.LIMIT, OrderType.MARKET):
                raise ValueError(
                    "a protective bracket entry must be a LIMIT or MARKET parent"
                )
            child_ids = {self.protective.stop_client_order_id.strip()}
            if self.protective.take_profit_client_order_id:
                child_ids.add(self.protective.take_profit_client_order_id.strip())
            if self.client_order_id.strip() in child_ids:
                raise ValueError(
                    "the entry client_order_id must differ from every protective"
                    " child id"
                )


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


class SessionPnlStatus(StrEnum):
    """WHY a session-P&L observation has, or does not have, a value.

    The value alone (``Decimal | None``) cannot distinguish a broker that
    rejected the request from one that simply did not answer in time -- both
    collapse to ``None`` -- and reporting either as "no P&L permission" is a
    diagnosis the evidence does not support. These four are surfaced so the
    owner probe can tell them apart WITHOUT exposing the account id.
    """

    #: A finite daily value was returned.
    OK = "PNL_OK"
    #: No P&L callback arrived within the bounded window. Not a permission fact.
    TIMEOUT = "PNL_TIMEOUT"
    #: The broker rejected the request with a request-scoped error (classified,
    #: never the raw text) -- distinct from silence, and no longer hidden as it.
    REQUEST_ERROR = "PNL_REQUEST_ERROR"
    #: A callback arrived but the daily value was NaN / not-a-number. UNKNOWN,
    #: never zero.
    UNSET = "PNL_UNSET"
    #: A finite value WAS observed, but the latest callback is older than the
    #: bounded freshness window -- the persistent subscription has gone quiet.
    #: A stale value is not a current one, so this fails closed like the rest.
    STALE = "PNL_STALE"


@dataclass(frozen=True, slots=True)
class SessionPnlObservation:
    """One session-P&L observation: its status, its value, and a safe detail.

    ``value`` is a finite ``Decimal`` only when ``status is OK``; it is ``None``
    for every other status, so a consumer that reads only the value still fails
    closed. ``detail`` is bounded and safe to show an owner -- a code or class,
    never the account id, a secret, a DSN or a traceback.
    """

    status: SessionPnlStatus
    value: Decimal | None = None
    detail: str = ""
    #: WHEN the underlying ``pnl`` callback actually arrived from the broker --
    #: NOT when this object was built. ``None`` when no callback has ever been
    #: observed. This is what makes freshness decidable: a value observed long
    #: ago is not a current one, and only the arrival time can tell them apart.
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.status is SessionPnlStatus.OK) != (self.value is not None):
            raise ValueError("a session P&L value is present iff the status is OK")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_alias: str
    environment: Environment
    net_liquidation_value: Decimal
    cash: Decimal
    buying_power: Decimal
    observed_at: datetime
    #: The broker's own SESSION (daily) profit and loss, when it reports one.
    #:
    #: Explicitly nullable, and ``None`` means "the broker did not tell us" —
    #: never "zero". The risk engine uses session P&L for the daily soft and
    #: hard halt limits, so an account that has lost money today and is
    #: presented as flat would have its halt silently disabled. A missing value
    #: must therefore fail the decision closed, not pass it.
    #:
    #: It is NOT derived from realized/unrealized totals: those are cumulative,
    #: and calling a cumulative figure a daily one would be a fabrication with
    #: the same consequence.
    session_pnl: Decimal | None = None
    #: WHY ``session_pnl`` is present or absent -- OK / TIMEOUT / REQUEST_ERROR /
    #: UNSET. Diagnostic only; the risk gate reads ``session_pnl`` and fails
    #: closed on anything but a value. Optional so adapters that do not observe
    #: P&L, and the many test snapshots, need not set it. When a value IS
    #: present it must be OK, and an absent value must never be reported as OK.
    session_pnl_status: SessionPnlStatus | None = None
    #: A bounded, owner-SAFE diagnostic detail for the P&L observation (e.g. a
    #: broker error CODE, or "no update within Ns"). Generic on purpose: this is
    #: a free-form safe string, and any broker-specific parsing that produced it
    #: stays in the adapter. It must never carry an account id, raw broker text,
    #: a DSN, a secret or a traceback -- the adapter is responsible for that.
    session_pnl_detail: str | None = None
    #: WHEN the P&L callback behind ``session_pnl`` actually arrived. Distinct
    #: from ``observed_at`` (when this snapshot was built): a persistent
    #: subscription delivers ~1/s, and the risk gate needs the callback age to
    #: reject a stale value. ``None`` when no callback has ever been observed.
    session_pnl_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.session_pnl_observed_at is not None:
            _require_aware(self.session_pnl_observed_at, "session_pnl_observed_at")
        if self.session_pnl_status is SessionPnlStatus.OK and self.session_pnl is None:
            raise ValueError("session_pnl_status OK requires a session_pnl value")
        if self.session_pnl is not None and self.session_pnl_status not in (
            None,
            SessionPnlStatus.OK,
        ):
            raise ValueError("a session_pnl value must carry status OK")


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


@dataclass(frozen=True, slots=True)
class RecoveredOrder:
    """A working order the broker says THIS client owns, as the broker sees it.

    Distinct from ``BrokerOrderReceipt`` on purpose. A receipt is our record of
    something we sent, and after a restart we no longer have it — so recovery
    must describe what the BROKER still holds, in the broker's own terms.

    Only facts the broker actually reports appear here. There is deliberately no
    correlation id: the original one was ours and the broker never stored it, so
    an implementation that produced one would be inventing the very identity the
    caller is trying to verify.
    """

    client_order_id: str
    broker_order_ref: str
    state: OrderState
    account_alias: str
    symbol: str
    #: The broker's own security identity for this working order, when the
    #: broker reports one. Empty means the broker did not say -- which is a
    #: fact we do not have, never a fact we may assume.
    broker_instrument_ref: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    stop_price: Decimal | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


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

    def recover_working_orders(
        self, account_alias: str
    ) -> tuple[RecoveredOrder, ...]:
        """What the broker still holds for THIS client, in the broker's terms.

        The identity-bearing half of recovery. ``recover_cancellable_orders``
        answers "what may I cancel"; this answers "what does the broker actually
        have, and what shape is it", which is what an ambiguous durable state
        needs in order to decide whether a working order is the execution that
        was prepared.
        """
        ...

    def recover_cancellable_orders(
        self, account_alias: str
    ) -> tuple[BrokerOrderReceipt, ...]:
        """Orders THIS client may act on after a restart (section 9.5).

        Deliberately narrower than ``get_open_orders``: visibility is global,
        authority is not. An implementation returns only orders it can prove
        this client owns — same-client feed, matching client id, matching
        account, genuinely working, canonical order reference.

        Application code needs this to resolve an ambiguous durable state (a
        submission was prepared and no outcome recorded) without reaching for an
        adapter-specific method behind the port's back.
        """
        ...

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
    "ProtectiveBracket",
    "RecoveredOrder",
    "OrderSide",
    "SessionPnlObservation",
    "SessionPnlStatus",
    "TimeInForce",
    "ReconciliationResult",
]
