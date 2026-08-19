"""IBKR Paper adapter (IMP-1002) — implements the IMP-1001 BrokerPort.

Authority: Integration_Specification_v0.6 section 9.1 (adapter contract),
9.2 (execution-agent isolation; PAPER and LIVE never share config), 9.3/20.4
(order types + capability matrix), 9.4 (append-only, idempotent broker
events), 9.5 (reconciliation); Technology spec section 12 (OrderIntent in,
canonical receipts/events out; IBKR identifiers never leave this package);
Decision_Contract 12.5 (canonical order states; UNKNOWN resolved only by
reconciliation).

PAPER ONLY, fail-closed:

- Endpoint gate: host 127.0.0.1/localhost only; port 4002 only — the live
  API port 4001 (and anything else) is refused. No override exists.
- Account gate: every broker-managed account and every configured alias
  must start with ``DU``; anything else fails closed and disconnects
  immediately. Account ids are exposed masked only.
- Submit gate: an ``OrderIntent`` without a non-empty SubmitAuthorization
  ref is refused (``AUTHORIZATION_MISSING``) — the adapter never bypasses
  the Risk Engine or the Final Pre-submit check; it only consumes their
  output. Duplicate ``client_order_id`` returns the ORIGINAL receipt
  (idempotent); a duplicate with a different authorization fails closed.
- Unknown broker status strings map to ``OrderState.UNKNOWN``, fail closed
  with ``UNKNOWN_BROKER_RESPONSE`` and are resolvable only by
  ``reconcile()``.

The adapter drives a synchronous ``PaperTransport`` seam. Tests inject a
deterministic fake transport (DoD: PoI fixtures + conformance suite pass —
no real connection in CI). The real SDK-backed transport is built lazily in
``_build_ibapi_paper_transport`` so ``ibapi`` stays confined to this
package and is imported only on the gateway host; running it for real is
the separately-approved POI-IBKR-001 experiment, not part of this task.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, TypeVar, cast

from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    BrokerErrorCode,
    BrokerInstrument,
    BrokerOrderEvent,
    BrokerOrderReceipt,
    BrokerPortError,
    BrokerPosition,
    CancelOrderRequest,
    CancelReceipt,
    InstrumentQuery,
    IntegrationHealth,
    OrderIntent,
    OrderPreview,
    OrderSide,
    ReconciliationResult,
    RecoveredOrder,
    TimeInForce,
)
from trading_contracts.enums import Environment, OrderState, OrderType

from trading_broker_ibkr.callback_router import (
    DIAGNOSTICS_STREAM,
    OPEN_ORDERS_STREAM,
    POSITIONS_STREAM,
    CallbackRouter,
    classify_error_stream,
    order_stream,
    request_stream,
)
from trading_broker_ibkr.ibkr_error_callback import (
    ErrorCallbackShapeError,
    normalize_error_callback,
    normalize_message,
)

_T = TypeVar("_T")

ADAPTER_VERSION = "ibkr-paper-adapter-1.0.0"

ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
LIVE_API_PORT = 4001
PAPER_ACCOUNT_PREFIX = "DU"

#: IBKR order fields -> canonical enums, for recovery. Anything absent here is
#: a shape we cannot express canonically, and an order we will not describe
#: approximately.
_RECOVERED_SIDE: Mapping[str, OrderSide] = {
    "BUY": OrderSide.BUY,
    "SELL": OrderSide.SELL,
}
_RECOVERED_ORDER_TYPE: Mapping[str, OrderType] = {
    "LMT": OrderType.LIMIT,
    "STP": OrderType.STOP,
    "STP LMT": OrderType.STOP_LIMIT,
    "MKT": OrderType.MARKET,
    "LIMIT": OrderType.LIMIT,
    "STOP": OrderType.STOP,
    "STOP_LIMIT": OrderType.STOP_LIMIT,
    "MARKET": OrderType.MARKET,
}

#: How long to wait for the ``reqPnL`` subscription's first update before
#: calling the daily P&L unknown. Short: a trading decision cannot wait, and a
#: slow answer is not a better answer than an honest "unknown".
SESSION_PNL_TIMEOUT_SECONDS = 5.0
DEFAULT_CLIENT_ID = 1
DEFAULT_TIMEOUT_SECONDS = 15.0

#: IBKR paper capability matrix (Integration 20.4). MVP hard rules:
#: fractional and outside-RTH DISABLED; trailing not auto-approved.
#: HONESTY RULE: a capability is advertised only at the level this adapter
#: actually implements TODAY. Bracket/OCA are broker-native at IBKR, but
#: this adapter has no bracket/OCO wiring yet (execution tasks), so they
#: are declared UNSUPPORTED — never NATIVE-by-promise and never silently
#: emulated (20.4: emulation requires explicit policy). Flip to NATIVE only
#: together with the actual implementation + conformance coverage.
IBKR_PAPER_CAPABILITIES = BrokerCapabilities(
    order_types={
        OrderType.LIMIT: BrokerCapabilityMode.NATIVE,
        OrderType.STOP: BrokerCapabilityMode.NATIVE,
        OrderType.STOP_LIMIT: BrokerCapabilityMode.NATIVE,
        OrderType.MARKET: BrokerCapabilityMode.NATIVE,
    },
    bracket=BrokerCapabilityMode.UNSUPPORTED,
    oco=BrokerCapabilityMode.UNSUPPORTED,
    trailing=BrokerCapabilityMode.MANUAL_ONLY,
    fractional=BrokerCapabilityMode.DISABLED,
    outside_rth=BrokerCapabilityMode.DISABLED,
    what_if=BrokerCapabilityMode.NATIVE,
    streaming_events=BrokerCapabilityMode.NATIVE,
)

#: IBKR order-status strings -> canonical OrderState (Decision_Contract
#: 12.5). Anything absent here is UNKNOWN and fails closed.
IBKR_STATUS_MAP: Mapping[str, OrderState] = {
    "PendingSubmit": OrderState.SUBMITTING,
    "PreSubmitted": OrderState.ACKNOWLEDGED,
    "Submitted": OrderState.ACKNOWLEDGED,
    "Filled": OrderState.FILLED,
    "PendingCancel": OrderState.CANCEL_PENDING,
    "ApiCancelled": OrderState.CANCELLED,
    "Cancelled": OrderState.CANCELLED,
    "Inactive": OrderState.REJECTED,
}

#: Canonical OrderType -> IBKR order-type string. The only order types this
#: adapter supports; anything else fails closed (never a silent default).
IBKR_ORDER_TYPE_MAP: Mapping[OrderType, str] = {
    OrderType.LIMIT: "LMT",
    OrderType.STOP: "STP",
    OrderType.STOP_LIMIT: "STP LMT",
    OrderType.MARKET: "MKT",
}

#: Canonical TimeInForce -> IBKR TIF string. IBKR rejects an empty TIF with
#: error 10052 ("Invalid time in force:Empty"), so every outgoing order MUST
#: carry an explicit, supported TIF. Only broker-supported values appear here.
IBKR_TIME_IN_FORCE_MAP: Mapping[TimeInForce, str] = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
}


def ibkr_order_type(order_type: OrderType) -> str:
    """Translate a canonical order type to IBKR's string, fail-closed."""
    mapped = IBKR_ORDER_TYPE_MAP.get(order_type)
    if not mapped:
        raise BrokerPortError(
            BrokerErrorCode.CAPABILITY_UNSUPPORTED,
            f"order type {order_type.value!r} is not supported by this adapter",
        )
    return mapped


def ibkr_time_in_force(time_in_force: TimeInForce) -> str:
    """Translate a canonical TIF to IBKR's string, fail-closed.

    Refuses an absent / empty / unknown / unsupported TIF *before* the order
    is ever built — this is the guard that makes IBKR error 10052 impossible.
    """
    if not isinstance(time_in_force, TimeInForce):
        raise BrokerPortError(
            BrokerErrorCode.VALIDATION_FAILED,
            "time_in_force is absent or not a canonical TimeInForce",
        )
    mapped = IBKR_TIME_IN_FORCE_MAP.get(time_in_force)
    if not mapped:  # unknown/unsupported enum member, or an empty mapping value
        raise BrokerPortError(
            BrokerErrorCode.CAPABILITY_UNSUPPORTED,
            f"time-in-force {time_in_force.value!r} is not supported by this adapter",
        )
    return mapped


@dataclass(frozen=True, slots=True)
class OutgoingOrder:
    """The exact broker fields an ``OrderIntent`` translates to.

    A pure, deterministic projection of the intent onto the concrete IBKR
    ``Order`` fields, so the outgoing wire shape can be asserted in a contract
    test without importing ``ibapi``. The host transport copies these onto a
    real ``ibapi.order.Order``; nothing here is empty or implicit.
    """

    account: str
    action: str
    order_type: str
    tif: str
    total_quantity: int
    limit_price: float | None
    aux_price: float | None
    order_ref: str
    transmit: bool
    outside_rth: bool = False
    e_trade_only: bool = False
    firm_quote_only: bool = False


def build_outgoing_order(intent: OrderIntent, account_id: str) -> OutgoingOrder:
    """Translate a validated intent into the exact outgoing IBKR order fields.

    Fails closed (via ``ibkr_order_type`` / ``ibkr_time_in_force``) if the order
    type or TIF is absent/unknown/unsupported — before any order is placed.
    """
    return OutgoingOrder(
        account=account_id,
        action=intent.side.value,
        order_type=ibkr_order_type(intent.order_type),
        tif=ibkr_time_in_force(intent.time_in_force),
        total_quantity=int(intent.quantity),
        limit_price=float(intent.limit_price) if intent.limit_price is not None else None,
        aux_price=float(intent.stop_price) if intent.stop_price is not None else None,
        order_ref=encode_order_ref(intent.client_order_id),
        transmit=True,
    )


#: Order states in which an order is genuinely still working at the broker.
_WORKING_ORDER_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_PENDING,
    }
)

# -- IBKR error-callback classification (D-CANCEL-01 / D-TIF-03) -------------
#
# System/connectivity notices IBKR delivers via ``error()`` that must NEVER
# fail an order operation. They arrive with reqId == -1 (already excluded by
# order-id correlation), but this allowlist is a documented belt-and-braces so
# a farm-status code sharing an order id still cannot fail a submit/cancel.
INFORMATIONAL_IBKR_ERROR_CODES: frozenset[int] = frozenset(
    {2104, 2106, 2107, 2108, 2119, 2158}
)

#: IBKR delivers "Order Cancelled - <reason>" as error code 202 — a cancel
#: CONFIRMATION, not a failure.
CANCEL_CONFIRMED_ERROR_CODES: frozenset[int] = frozenset({202})

#: Deterministic, pre-acceptance order rejects: the order was definitely NOT
#: accepted, so they are TERMINAL (no retry) rather than UNKNOWN. Kept small and
#: conservative — anything not listed here stays UNKNOWN (reconcile-required),
#: which never hides a working order. (10052 is prevented upstream by the TIF
#: validation but is included for completeness / defence in depth.)
DETERMINISTIC_ORDER_REJECT_CODES: frozenset[int] = frozenset({103, 201, 321, 10052})


def is_informational_ibkr_error(code: int) -> bool:
    """True for a connectivity/farm-status notice that must not fail an order."""
    return code in INFORMATIONAL_IBKR_ERROR_CODES


#: IBKR reports an unset double field as a huge sentinel (``UNSET_DOUBLE``);
#: anything at or beyond this magnitude means "not reported", not a price.
_UNSET_DOUBLE_THRESHOLD = Decimal("1e307")


def format_ibkr_error(
    code: int, text: str, advanced_reject: str = "", *, context: str
) -> str:
    """Sanitized, bounded description of an order-scoped IBKR error.

    Preserves the ORIGINAL numeric code (the owner correlates on it) and the
    broker's text, both length-bounded and sanitized by ``normalize_message``.
    Never includes an account id, credential, secret path or authorization
    material — the adapter never puts any of those into a broker error.
    """
    detail = normalize_message(text)
    if advanced_reject:
        detail = f"{detail} [advanced-rejection: {normalize_message(advanced_reject)}]"
    return f"IBKR {code} on {context}: {detail}"


def classify_ibkr_order_error(code: int) -> BrokerErrorCode:
    """Map an order-scoped IBKR error code to the canonical broker error.

    Deterministic pre-acceptance rejects are TERMINAL (``BROKER_REJECTED``, no
    retry); everything else — including ambiguous codes — stays
    ``UNKNOWN_BROKER_RESPONSE`` (reconcile-required), never hiding a live order.
    """
    if code in DETERMINISTIC_ORDER_REJECT_CODES:
        return BrokerErrorCode.BROKER_REJECTED
    return BrokerErrorCode.UNKNOWN_BROKER_RESPONSE


class CancelDisposition(StrEnum):
    """What one order-status callback means while awaiting a cancellation."""

    STALE = "STALE"  # a leftover Submitted/PreSubmitted/ACK — ignore, keep waiting
    PENDING = "PENDING"  # PendingCancel — accepted, not yet final — keep waiting
    CONFIRMED = "CONFIRMED"  # Cancelled/ApiCancelled — terminal success
    EXPOSED = "EXPOSED"  # Filled/PartiallyFilled — exposure, NOT a cancellation
    REJECTED = "REJECTED"  # Inactive/rejected — terminal deterministic failure


def classify_cancel_status(status: str) -> CancelDisposition:
    """Classify an IBKR order-status string encountered while cancelling.

    A stale acknowledgement is NEVER treated as cancellation confirmation; a
    fill is an exposure event, never success. An unmapped status is treated as
    stale (keep waiting → eventual UNKNOWN), never as success.
    """
    state = IBKR_STATUS_MAP.get(status)
    if state is OrderState.CANCELLED:
        return CancelDisposition.CONFIRMED
    if state is OrderState.CANCEL_PENDING:
        return CancelDisposition.PENDING
    if state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
        return CancelDisposition.EXPOSED
    if state is OrderState.REJECTED:
        return CancelDisposition.REJECTED
    # SUBMITTING / ACKNOWLEDGED / unmapped -> stale: keep waiting, never confirm.
    return CancelDisposition.STALE


@dataclass(frozen=True, slots=True)
class CancelCallbackEvent:
    """One correlated broker callback observed while awaiting a cancellation."""

    kind: str  # "status" | "error"
    order_id: int
    status: str | None = None
    error_code: int | None = None
    error_text: str = ""
    advanced_reject: str = ""


@dataclass(frozen=True, slots=True)
class CancelResolution:
    """The terminal disposition of a cancellation wait (pure decision)."""

    outcome: str  # "CONFIRMED" | "EXPOSED" | "REJECTED" | "ERROR" | "TIMEOUT"
    status: str | None = None
    error_code: int | None = None
    detail: str = ""


def resolve_cancel(
    events: Iterable[CancelCallbackEvent], broker_order_id: int
) -> CancelResolution:
    """Reduce correlated cancel callbacks to a single terminal resolution.

    Pure and deterministic: the caller supplies the correlated event stream
    (statuses + errors for this order id). Stale acks and PendingCancel keep the
    wait open; a fill is EXPOSED (never success); an Inactive/reject is REJECTED;
    an order error 202 is a cancel CONFIRMATION; other order errors are ERROR;
    an exhausted stream (queue timeout) is TIMEOUT. Never retries.
    """
    for event in events:
        if event.order_id != broker_order_id:
            continue  # correlate strictly by broker order id
        if event.kind == "error":
            code = event.error_code or 0
            if is_informational_ibkr_error(code):
                continue  # farm-status notice — observable, never a failure
            if code in CANCEL_CONFIRMED_ERROR_CODES:
                return CancelResolution("CONFIRMED", status="Cancelled", error_code=code)
            return CancelResolution(
                "ERROR",
                error_code=code,
                detail=format_ibkr_error(
                    code, event.error_text, event.advanced_reject, context="cancellation"
                ),
            )
        disposition = classify_cancel_status(event.status or "")
        if disposition in (CancelDisposition.STALE, CancelDisposition.PENDING):
            continue
        return CancelResolution(disposition.value, status=event.status)
    return CancelResolution("TIMEOUT")


def cancel_resolution_to_status(resolution: CancelResolution) -> str:
    """Turn a cancel resolution into a confirming status, or fail closed.

    CONFIRMED returns the status string. EXPOSED and TIMEOUT raise
    UNKNOWN_BROKER_RESPONSE (reconcile-required, never a blind retry). REJECTED
    and deterministic order errors raise BROKER_REJECTED (terminal). Ambiguous
    order errors stay UNKNOWN.
    """
    if resolution.outcome == "CONFIRMED":
        return resolution.status or "Cancelled"
    if resolution.outcome == "EXPOSED":
        raise BrokerPortError(
            BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
            "order filled during cancellation — position exposure, not cancelled;"
            " reconcile required",
        )
    if resolution.outcome == "REJECTED":
        raise BrokerPortError(
            BrokerErrorCode.BROKER_REJECTED,
            f"cancel rejected — order {resolution.status or 'inactive'} (terminal)",
        )
    if resolution.outcome == "ERROR":
        raise BrokerPortError(
            classify_ibkr_order_error(resolution.error_code or 0), resolution.detail
        )
    raise BrokerPortError(
        BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
        "no cancellation confirmation before timeout; reconcile required",
    )

# -- Order-ref identity codec (IMP-1304 D-ADAPTER-01) -----------------------
#
# Every lifecycle-owned order carries its canonical, NON-AUTHORIZING
# client_order_id in IBKR ``Order.orderRef`` under a versioned prefix, so that
# a fresh adapter (after a process or Gateway restart) can recover the order's
# identity from ``reqAllOpenOrders`` alone — with no process-local state.
#
# The client_order_id embeds the operation reference, never the confirmation
# token, an account secret or an authorization value (enforced by the lifecycle
# harness and asserted by tests), so persisting it is safe. The encoding is a
# plain, fully reversible prefix + verbatim id; ``ORDER_REF_MAX_LEN`` is a
# conservative, documented budget comfortably within what the installed TWS
# API accepts. If a future id ever exceeds the budget, submission fails closed
# rather than silently truncating into a colliding ref (a "poi2:" compact
# encoding would then be introduced under a new version prefix).
LIFECYCLE_ORDER_REF_PREFIX = "poi1:"
ORDER_REF_MAX_LEN = 200


class OrderRefTooLongError(BrokerPortError):
    """A client_order_id will not fit in orderRef; never truncate — fail closed."""


def encode_order_ref(client_order_id: str) -> str:
    """Encode a canonical client_order_id into a lifecycle orderRef (v1)."""
    ref = f"{LIFECYCLE_ORDER_REF_PREFIX}{client_order_id}"
    if len(ref) > ORDER_REF_MAX_LEN:
        raise OrderRefTooLongError(
            BrokerErrorCode.VALIDATION_FAILED,
            f"client_order_id too long to persist in orderRef "
            f"({len(ref)} > {ORDER_REF_MAX_LEN}); refusing to submit rather than truncate",
        )
    return ref


@dataclass(frozen=True, slots=True)
class RecoveredIdentity:
    """The identity recovered from an order's orderRef.

    ``lifecycle_owned`` is True only for an order this harness placed (a
    ``poi1:`` prefix with a non-empty body). Everything else — a missing ref, a
    foreign/manual order, or a malformed lifecycle-looking ref — is NOT
    lifecycle-owned and carries a clearly-marked, non-canonical id so it can
    never be silently matched as this run's protection.
    """

    client_order_id: str
    lifecycle_owned: bool


def decode_order_ref(order_ref: str | None, broker_order_id: int) -> RecoveredIdentity:
    """Recover a canonical client_order_id from an orderRef (fail-closed)."""
    if not order_ref or not order_ref.startswith(LIFECYCLE_ORDER_REF_PREFIX):
        # missing ref, or a foreign / manual order — never lifecycle-owned.
        return RecoveredIdentity(f"foreign:ibkr-paper:{broker_order_id}", False)
    body = order_ref[len(LIFECYCLE_ORDER_REF_PREFIX) :]
    if not body.strip():
        # a lifecycle-looking but malformed ref — surfaced, never claimed.
        return RecoveredIdentity(f"malformed:ibkr-paper:{broker_order_id}", False)
    return RecoveredIdentity(body, True)


#: Client id recorded for an order whose owning API client is unknown or was
#: not reported. It can never equal a configured adapter client id (those are
#: non-negative), so an unattributed order is NEVER cancellable — fail closed.
FOREIGN_CLIENT_ID = -1


@dataclass(frozen=True, slots=True)
class RawOpenOrder:
    """One order observed in a read-only open-order snapshot (never mutated).

    Two provenances produce these rows and they are NOT interchangeable:

    - ``reqAllOpenOrders`` — GLOBAL VISIBILITY across every API client and the
      TWS/Gateway UI. Seeing an order here proves it exists; it proves nothing
      about whether this API client may cancel it.
    - ``reqOpenOrders`` — orders bound to THIS API client id, which is the only
      provenance that can support cancellation authority.

    ``client_id`` therefore defaults to ``FOREIGN_CLIENT_ID``: a transport that
    does not report the owning client leaves the order visible-but-never-
    cancellable rather than silently cancellable.
    """

    broker_order_id: int
    order_ref: str
    status: str
    symbol: str
    client_id: int = FOREIGN_CLIENT_ID
    perm_id: int = 0
    account: str = ""
    action: str = ""
    quantity: Decimal = Decimal(0)
    order_type: str = ""
    limit_price: Decimal | None = None
    aux_price: Decimal | None = None


def mask_account_id(account_id: str) -> str:
    """``DU1234567`` -> ``DU*****67``; the full id never leaves the adapter."""
    if len(account_id) <= 4:
        return account_id[:2] + "**"
    return account_id[:2] + "*" * (len(account_id) - 4) + account_id[-2:]


def _validate_paper_endpoint(host: str, port: int) -> None:
    if host not in ALLOWED_HOSTS:
        raise BrokerPortError(
            BrokerErrorCode.VALIDATION_FAILED,
            f"host {host!r} refused: only 127.0.0.1/localhost is allowed (paper isolation)",
        )
    if port == LIVE_API_PORT:
        raise BrokerPortError(
            BrokerErrorCode.VALIDATION_FAILED,
            f"port {LIVE_API_PORT} is the LIVE API port and is never allowed",
        )
    if port != PAPER_API_PORT:
        raise BrokerPortError(
            BrokerErrorCode.VALIDATION_FAILED,
            f"port {port} refused: only the paper API port {PAPER_API_PORT} is allowed",
        )


class PaperTransport(Protocol):
    """Synchronous seam the adapter drives; the real one wraps the SDK.

    Implementations translate these calls to broker requests and block
    until the response (or timeout) — the adapter stays SDK-free and
    deterministic under a fake transport.
    """

    def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
        """Open the API connection; return the managed account ids."""
        ...

    def disconnect(self) -> None: ...

    def is_connected(self) -> bool: ...

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument | None: ...

    def account_summary(self, account_id: str) -> Mapping[str, Decimal]: ...

    def session_pnl(self, account_id: str) -> Decimal | None:
        """The account's DAILY P&L, or None when the gateway will not say.

        A distinct operation rather than an account-summary tag, because IBKR's
        ``reqAccountSummary`` genuinely does not carry a daily figure: the daily
        number comes from the separate ``reqPnL`` subscription, and inventing a
        ``DailyPnL`` summary key would have described a feed that does not
        exist.

        ``None`` means unknown -- a missing subscription, a timeout, an error.
        It is never zero, because the risk engine computes both daily halt
        limits from this and a zero would silently disable them.

        Cumulative realized/unrealized totals are a different fact and are
        deliberately not returned here; a cumulative number presented as a daily
        one is the same fabrication wearing a different hat.
        """
        ...

    def positions(self, account_id: str) -> tuple[BrokerPosition, ...]: ...

    def open_order_snapshot(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        """GLOBAL read-only snapshot of live open orders (``reqAllOpenOrders``).

        Must never send, cancel, replace or modify an order. Blocks until the
        canonical end-of-snapshot callback, then returns every open order the
        gateway can see — including orders placed by other API clients and by
        the TWS/Gateway UI.

        This is a VISIBILITY/reconciliation feed. It must never, on its own,
        make an order cancellable (see ``same_client_open_orders``).
        """
        ...

    def same_client_open_orders(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        """Read-only snapshot of orders bound to THIS API client (``reqOpenOrders``).

        Must never send, cancel, replace or modify an order. Every returned row
        must carry the owning ``client_id`` so the adapter can verify that the
        order really belongs to the configured client before treating it as
        cancellable after a restart.
        """
        ...

    def next_order_id(self) -> int: ...

    def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
        """Send the order; return the broker's status string."""
        ...

    def cancel(self, broker_order_id: int) -> str:
        """Cancel by broker order id; return the broker's status string."""
        ...


def utc_now() -> datetime:
    """The production clock: real, timezone-aware **UTC wall-clock** time.

    Every timestamp this adapter puts into evidence — account snapshots,
    receipts, broker events, reconciliation and health checks — is UTC
    wall-clock, so an owner can correlate it with the Gateway log and with the
    real world.

    It is deliberately NOT used to measure elapsed time. Wall-clock subtraction
    is unsafe for timeouts because the system clock can jump (NTP step, DST
    database update, manual correction); every waiting loop in this package
    uses a MONOTONIC source instead (``CallbackRouter`` and
    ``threading.Event.wait``). The two are never mixed: monotonic values are
    durations only and never reach evidence.
    """
    return datetime.now(tz=UTC)


def _validated_clock(clock: Callable[[], datetime]) -> Callable[[], datetime]:
    """Wrap a clock so a naive or non-UTC reading can never reach evidence.

    Normalizes any timezone-aware reading to UTC (so the adapter never depends
    on the host's local timezone) and fails closed on a naive one rather than
    silently emitting an ambiguous timestamp.
    """

    def now() -> datetime:
        stamp = clock()
        if stamp.tzinfo is None:
            raise BrokerPortError(
                BrokerErrorCode.VALIDATION_FAILED,
                "adapter clock returned a naive timestamp; evidence timestamps"
                " must be timezone-aware UTC",
            )
        return stamp.astimezone(UTC)

    return now


class IbkrPaperAdapter:
    """``BrokerPort`` implementation for the IBKR PAPER environment only."""

    def __init__(
        self,
        transport_factory: Callable[[], PaperTransport],
        *,
        account_aliases: Mapping[str, str],
        host: str = "127.0.0.1",
        port: int = PAPER_API_PORT,
        client_id: int = DEFAULT_CLIENT_ID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not account_aliases:
            raise BrokerPortError(
                BrokerErrorCode.VALIDATION_FAILED, "at least one account alias is required"
            )
        for alias, account_id in account_aliases.items():
            if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
                raise BrokerPortError(
                    BrokerErrorCode.VALIDATION_FAILED,
                    f"alias {alias!r} maps to non-paper account"
                    f" {mask_account_id(account_id)}; PAPER (DU*) only",
                )
        self._factory = transport_factory
        self._aliases = dict(account_aliases)
        self._host = host
        self._port = port
        self._client_id = client_id
        # PRODUCTION CLOCK (D-CLOCK-01). This default was a deterministic test
        # clock frozen at 2026-07-10T12:00Z, so a REAL owner-run on
        # 2026-07-30T17:05Z stamped every snapshot, receipt, broker event,
        # reconciliation and health check with a ~20-day-stale fabricated
        # timestamp. Tests inject their own clock; the default must be real.
        self._now = _validated_clock(clock or utc_now)
        self._transport: PaperTransport | None = None
        self._orders: dict[str, BrokerOrderReceipt] = {}
        self._broker_ids: dict[str, int] = {}
        self._events: list[BrokerOrderEvent] = []
        self._unresolved: set[str] = set()

    # -- BrokerPort ----------------------------------------------------------
    def connect(self) -> None:
        _validate_paper_endpoint(self._host, self._port)
        try:
            transport = self._factory()
            managed = transport.connect(self._host, self._port, self._client_id)
        except BrokerPortError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise BrokerPortError(
                BrokerErrorCode.BROKER_UNHEALTHY, f"connect failed: {type(error).__name__}"
            ) from error
        for account_id in managed:
            if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
                transport.disconnect()
                raise BrokerPortError(
                    BrokerErrorCode.VALIDATION_FAILED,
                    f"managed account {mask_account_id(account_id)} is not a paper"
                    " account; disconnected immediately (LIVE is forbidden)",
                )
        for alias, account_id in self._aliases.items():
            if managed and account_id not in managed:
                transport.disconnect()
                raise BrokerPortError(
                    BrokerErrorCode.ACCOUNT_UNKNOWN,
                    f"alias {alias!r} account {mask_account_id(account_id)} is not"
                    " managed by this gateway session",
                )
        self._transport = transport

    def disconnect(self) -> None:
        """Drop the transport. Normalizes any SDK failure, never leaks it.

        The transport is cleared FIRST-class in ``finally`` so the adapter can
        never be left holding a half-dead connection, and an arbitrary SDK
        exception is converted to a ``BrokerPortError`` so every public method
        on this port raises only the canonical taxonomy.
        """
        if self._transport is None:
            return
        try:
            self._transport.disconnect()
        except BrokerPortError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise BrokerPortError(
                BrokerErrorCode.BROKER_UNHEALTHY,
                f"disconnect failed: {type(error).__name__}",
            ) from error
        finally:
            self._transport = None

    def capabilities(self) -> BrokerCapabilities:
        return IBKR_PAPER_CAPABILITIES

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument:
        transport = self._require_connected()
        instrument = self._guarded(lambda: transport.resolve_instrument(query))
        if instrument is None:
            raise BrokerPortError(
                BrokerErrorCode.INSTRUMENT_NOT_FOUND, f"unknown instrument {query.symbol!r}"
            )
        return instrument

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        transport = self._require_connected()
        account_id = self._account_id(account_alias)
        values = self._guarded(lambda: transport.account_summary(account_id))
        try:
            return AccountSnapshot(
                account_alias=account_alias,
                environment=Environment.PAPER,
                net_liquidation_value=values["NetLiquidation"],
                cash=values["TotalCashValue"],
                buying_power=values["BuyingPower"],
                # From the transport's own daily-P&L operation (reqPnL), never
                # from an account-summary tag: IBKR's summary carries no daily
                # figure. Unknown stays None, and None fails a trading decision
                # closed rather than pretending the day is flat.
                session_pnl=self._session_pnl(transport, account_id),
                observed_at=self._now(),
            )
        except KeyError as missing:
            raise BrokerPortError(
                BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                f"account summary missing tag {missing}; failing closed",
            ) from missing

    @staticmethod
    def _session_pnl(transport: PaperTransport, account_id: str) -> Decimal | None:
        """Ask for the daily P&L. Any failure is UNKNOWN, never zero.

        A subscription that is missing, slow or erroring is indistinguishable
        from a flat day if the failure is swallowed as a number, so it is
        swallowed as ``None`` instead.
        """
        try:
            value = transport.session_pnl(account_id)
        except Exception:  # noqa: BLE001 - any failure means UNKNOWN
            return None
        if value is None:
            return None
        if not value.is_finite():
            return None
        return value

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        transport = self._require_connected()
        account_id = self._account_id(account_alias)
        return self._guarded(lambda: transport.positions(account_id))

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        """GLOBAL visibility of live open orders — never cancellation authority.

        Queries the read-only ``reqAllOpenOrders`` snapshot and normalizes it
        into ``BrokerOrderReceipt``s, recovering each order's canonical identity
        from its ``orderRef``. This is what makes reconnect restart-safe: a
        fresh adapter with empty memory still SEES the resting protective stop,
        and reconciliation can flag anything unexpected.

        It deliberately hydrates NOTHING. ``reqAllOpenOrders`` also reports
        orders belonging to other API clients and to the TWS/Gateway UI, so an
        order appearing here does not prove this client may cancel it. Making
        an order cancellable is the separate, stricter
        ``recover_cancellable_orders`` path.

        Only genuinely-working orders are returned; a cancelled / filled /
        rejected order is never reported as working protection. Duplicate
        ``openOrder`` callbacks for the same broker order id collapse to one.
        Foreign / manual / malformed / missing-ref orders are returned with a
        clearly non-canonical id so they can never be claimed as lifecycle
        protection (the lifecycle matcher rejects them, fail-closed).
        """
        transport = self._require_connected()
        account_id = self._account_id(account_alias)
        raws = self._guarded(lambda: transport.open_order_snapshot(account_id))
        receipts: dict[int, BrokerOrderReceipt] = {}
        for raw in raws:
            if raw.broker_order_id in receipts:
                continue  # duplicate openOrder callback: collapse to one
            state = IBKR_STATUS_MAP.get(raw.status)
            if state is None or state not in _WORKING_ORDER_STATES:
                # unmapped/UNKNOWN or a non-working (cancelled/filled/rejected)
                # order is never reported as working protection.
                continue
            identity = decode_order_ref(raw.order_ref, raw.broker_order_id)
            receipts[raw.broker_order_id] = BrokerOrderReceipt(
                client_order_id=identity.client_order_id,
                broker_order_ref=f"ibkr-paper:{raw.broker_order_id}",
                state=state,
                authorization_ref="recovered:open-order-snapshot",
                correlation_id="recovered:open-order-snapshot",
                received_at=self._now(),
            )
        return tuple(receipts.values())

    def recover_working_orders(
        self, account_alias: str
    ) -> tuple[RecoveredOrder, ...]:
        """The same eligible set, described as the BROKER reports it.

        Every field comes from the ``reqOpenOrders`` row: symbol, side,
        quantity, order type and prices are what IBKR says it is holding. The
        original correlation id is deliberately absent -- it was ours, the
        broker never stored it, and manufacturing one would fabricate the
        identity a caller is trying to verify.
        """
        transport = self._require_connected()
        account_id = self._account_id(account_alias)
        raws = self._guarded(lambda: transport.same_client_open_orders(account_id))
        eligible = self._eligible_recovered_orders(raws, account_id)
        recovered: list[RecoveredOrder] = []
        for client_order_id, raw in eligible.items():
            side = _RECOVERED_SIDE.get(raw.action.upper())
            order_type = _RECOVERED_ORDER_TYPE.get(raw.order_type.upper())
            if side is None or order_type is None:
                # An order shape we cannot express canonically is not one we may
                # describe approximately.
                continue
            recovered.append(
                RecoveredOrder(
                    client_order_id=client_order_id,
                    broker_order_ref=f"ibkr-paper:{raw.broker_order_id}",
                    state=IBKR_STATUS_MAP[raw.status],
                    account_alias=account_alias,
                    symbol=raw.symbol,
                    side=side,
                    quantity=raw.quantity,
                    order_type=order_type,
                    limit_price=raw.limit_price,
                    stop_price=getattr(raw, "aux_price", None),
                    observed_at=self._now(),
                )
            )
        return tuple(recovered)

    def recover_cancellable_orders(
        self, account_alias: str
    ) -> tuple[BrokerOrderReceipt, ...]:
        """Recover the orders this client may cancel after a restart.

        Distinct from ``get_open_orders`` on purpose. Visibility is global;
        cancellation authority is not. A recovered order becomes cancellable
        ONLY when every one of these holds:

        - it came from the SAME-CLIENT (``reqOpenOrders``) feed;
        - its ``client_id`` equals this adapter's configured API client id;
        - its account equals the configured paper account for the alias;
        - it is genuinely working (not cancelled/filled/rejected/unmapped);
        - its ``orderRef`` is a valid canonical ``poi1:`` lifecycle reference —
          the prefix alone is NOT proof of ownership, it is one of several
          required conditions;
        - its broker order id is a valid, non-zero id;
        - its canonical identity is unambiguous.

        Ambiguity fails closed with ``UNKNOWN_BROKER_RESPONSE`` and NOTHING is
        hydrated: cancelling the wrong order is far worse than cancelling
        nothing. Repeated snapshots are idempotent and can never overwrite an
        existing canonical mapping.
        """
        transport = self._require_connected()
        account_id = self._account_id(account_alias)
        raws = self._guarded(lambda: transport.same_client_open_orders(account_id))
        eligible = self._eligible_recovered_orders(raws, account_id)
        # Validate the WHOLE snapshot before mutating anything, so a conflict
        # never leaves the adapter half-hydrated.
        for client_order_id, raw in eligible.items():
            known = self._broker_ids.get(client_order_id)
            if known is not None and known != raw.broker_order_id:
                raise BrokerPortError(
                    BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                    f"recovered order {client_order_id} maps to broker order"
                    f" {raw.broker_order_id} but this process already knows it as"
                    f" {known}; refusing to act on an ambiguous identity",
                )
        recovered: list[BrokerOrderReceipt] = []
        for client_order_id, raw in eligible.items():
            state = IBKR_STATUS_MAP[raw.status]
            receipt = BrokerOrderReceipt(
                client_order_id=client_order_id,
                broker_order_ref=f"ibkr-paper:{raw.broker_order_id}",
                state=state,
                authorization_ref="recovered:same-client-open-orders",
                correlation_id="recovered:same-client-open-orders",
                received_at=self._now(),
            )
            # Idempotent and non-destructive: an existing in-process mapping
            # (verified identical above) is never rewritten.
            self._orders.setdefault(client_order_id, receipt)
            self._broker_ids.setdefault(client_order_id, raw.broker_order_id)
            recovered.append(receipt)
        return tuple(recovered)

    def _eligible_recovered_orders(
        self, raws: Iterable[RawOpenOrder], account_id: str
    ) -> dict[str, RawOpenOrder]:
        """Filter a same-client snapshot down to unambiguous, owned orders."""
        by_broker_id: dict[int, RawOpenOrder] = {}
        for raw in raws:
            if raw.broker_order_id <= 0:
                # A zero/negative broker order id cannot address a cancellation.
                continue
            if raw.client_id != self._client_id:
                # Visible, but owned by another API client or unattributed.
                continue
            if raw.account and raw.account != account_id:
                continue  # cross-account row: never this alias's order
            state = IBKR_STATUS_MAP.get(raw.status)
            if state is None or state not in _WORKING_ORDER_STATES:
                continue
            identity = decode_order_ref(raw.order_ref, raw.broker_order_id)
            if not identity.lifecycle_owned:
                continue  # foreign / manual / malformed ref: never cancellable
            seen = by_broker_id.get(raw.broker_order_id)
            if seen is None:
                by_broker_id[raw.broker_order_id] = raw
                continue
            # Duplicate callback for the same broker order id: identical rows
            # collapse, contradictory ones fail closed.
            if seen.order_ref != raw.order_ref:
                raise BrokerPortError(
                    BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                    f"broker order {raw.broker_order_id} was reported with two"
                    " different canonical identities; refusing to act on it",
                )
            if seen.perm_id and raw.perm_id and seen.perm_id != raw.perm_id:
                raise BrokerPortError(
                    BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                    f"broker order {raw.broker_order_id} was reported with"
                    " conflicting permIds; refusing to act on it",
                )
        eligible: dict[str, RawOpenOrder] = {}
        for raw in by_broker_id.values():
            client_order_id = decode_order_ref(raw.order_ref, raw.broker_order_id).client_order_id
            duplicate = eligible.get(client_order_id)
            if duplicate is not None:
                raise BrokerPortError(
                    BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                    f"two working broker orders ({duplicate.broker_order_id} and"
                    f" {raw.broker_order_id}) claim the canonical identity"
                    f" {client_order_id}; refusing to act on an ambiguous identity",
                )
            eligible[client_order_id] = raw
        return eligible

    def preview_order(self, intent: OrderIntent) -> OrderPreview:
        self._require_connected()
        self._validate_intent(intent)
        reference_price = intent.limit_price or intent.stop_price or Decimal(0)
        return OrderPreview(
            client_order_id=intent.client_order_id,
            estimated_notional=intent.quantity * reference_price,
            correlation_id=intent.correlation_id,
        )

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        transport = self._require_connected()
        self._validate_intent(intent)
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            # Idempotent: the original receipt, nothing new sent to the broker.
            return existing
        account_id = self._account_id(intent.account_alias)
        broker_order_id = self._guarded(transport.next_order_id)
        status = self._guarded(lambda: transport.submit(broker_order_id, account_id, intent))
        state = self._map_status(status, intent.client_order_id, intent.correlation_id)
        receipt = BrokerOrderReceipt(
            client_order_id=intent.client_order_id,
            broker_order_ref=f"ibkr-paper:{broker_order_id}",
            state=state,
            authorization_ref=intent.authorization_ref,
            correlation_id=intent.correlation_id,
            received_at=self._now(),
        )
        self._orders[intent.client_order_id] = receipt
        self._broker_ids[intent.client_order_id] = broker_order_id
        self._append_event(receipt)
        return receipt

    def cancel_order(self, request: CancelOrderRequest) -> CancelReceipt:
        transport = self._require_connected()
        receipt = self._orders.get(request.client_order_id)
        broker_order_id = self._broker_ids.get(request.client_order_id)
        if receipt is None or broker_order_id is None:
            raise BrokerPortError(
                BrokerErrorCode.ORDER_NOT_FOUND,
                f"no order {request.client_order_id!r}",
                correlation_id=request.correlation_id,
            )
        status = self._guarded(lambda: transport.cancel(broker_order_id))
        state = self._map_status(status, request.client_order_id, request.correlation_id)
        cancelled = BrokerOrderReceipt(
            client_order_id=receipt.client_order_id,
            broker_order_ref=receipt.broker_order_ref,
            state=state,
            authorization_ref=receipt.authorization_ref,
            correlation_id=request.correlation_id,
            received_at=self._now(),
        )
        self._orders[request.client_order_id] = cancelled
        self._append_event(cancelled)
        return CancelReceipt(
            client_order_id=cancelled.client_order_id,
            broker_order_ref=cancelled.broker_order_ref,
            state=state,
            correlation_id=request.correlation_id,
            received_at=cancelled.received_at,
        )

    def order_events(self) -> Iterator[BrokerOrderEvent]:
        return iter(tuple(self._events))

    def reconcile(self, account_alias: str) -> ReconciliationResult:
        snapshot = self.get_account_snapshot(account_alias)
        positions = self.get_positions(account_alias)
        # Reconciliation is the only place UNKNOWN states get resolved
        # (Decision_Contract 12.5): report them, never guess them away.
        unresolved = tuple(sorted(self._unresolved))
        return ReconciliationResult(
            account_alias=account_alias,
            snapshot=snapshot,
            positions=positions,
            open_orders=self.get_open_orders(account_alias),
            unresolved_client_order_ids=unresolved,
            reconciled_at=self._now(),
        )

    def health_check(self) -> IntegrationHealth:
        """Probe the gateway. Never raises an arbitrary transport exception.

        ``is_connected()`` reaches into the SDK (``EClient.isConnected``), so a
        dead socket or an ibapi internal error could previously escape as an
        ARBITRARY exception. The harness only normalizes ``BrokerPortError``, so
        that collapsed a whole read-only run into "unknown error" and hid the
        account state — the D-CLOCK-02 failure mode, on the very first probe.

        A failed probe is answered as UNHEALTHY rather than by raising: that is
        this method's contract, and it keeps the Execution Agent's tested
        ``BROKER_UNHEALTHY``/``NOT_SENT`` pre-submit refusal on its normal path
        instead of rerouting a safety gate through an exception. The failure is
        not swallowed — the exception TYPE is reported in ``detail`` (its text
        is not, as it may come from a third-party library).
        """
        if self._transport is None:
            connected, detail = False, "not connected"
        else:
            try:
                connected = self._transport.is_connected()
                detail = "ibkr paper gateway connected" if connected else "not connected"
            except BrokerPortError as error:
                connected, detail = False, f"health probe failed: {error.code.value}"
            except Exception as error:  # noqa: BLE001 - unknown => unhealthy
                connected = False
                detail = f"health probe raised {type(error).__name__}; treated as unhealthy"
        return IntegrationHealth(
            healthy=connected,
            detail=detail,
            checked_at=self._now(),
        )

    # -- internals -----------------------------------------------------------
    def _require_connected(self) -> PaperTransport:
        if self._transport is None:
            raise BrokerPortError(BrokerErrorCode.NOT_CONNECTED, "connect() first")
        return self._transport

    def _account_id(self, account_alias: str) -> str:
        account_id = self._aliases.get(account_alias)
        if account_id is None:
            raise BrokerPortError(
                BrokerErrorCode.ACCOUNT_UNKNOWN, f"unknown account alias {account_alias!r}"
            )
        return account_id

    def _validate_intent(self, intent: OrderIntent) -> None:
        if not intent.authorization_ref.strip():
            raise BrokerPortError(
                BrokerErrorCode.AUTHORIZATION_MISSING,
                "submit/preview requires a SubmitAuthorization ref (final pre-submit"
                " check output); the adapter never bypasses it",
                correlation_id=intent.correlation_id,
            )
        if intent.environment is not Environment.PAPER:
            raise BrokerPortError(
                BrokerErrorCode.VALIDATION_FAILED,
                f"environment {intent.environment.value} refused: this adapter is PAPER only",
                correlation_id=intent.correlation_id,
            )
        self._account_id(intent.account_alias)
        mode = IBKR_PAPER_CAPABILITIES.order_types.get(
            intent.order_type, BrokerCapabilityMode.UNSUPPORTED
        )
        if mode is not BrokerCapabilityMode.NATIVE:
            raise BrokerPortError(
                BrokerErrorCode.CAPABILITY_UNSUPPORTED,
                f"{intent.order_type.value} is {mode.value} for IBKR paper",
                correlation_id=intent.correlation_id,
            )
        if intent.quantity != intent.quantity.to_integral_value():
            raise BrokerPortError(
                BrokerErrorCode.VALIDATION_FAILED,
                "fractional quantities are DISABLED in the MVP",
                correlation_id=intent.correlation_id,
            )
        # Translate the outbound broker fields NOW, before any placeOrder, so
        # an absent/empty/unknown/unsupported order type or TIF fails closed
        # here — never reaching the wire with an empty value (IBKR 10052).
        try:
            ibkr_order_type(intent.order_type)
            ibkr_time_in_force(intent.time_in_force)
        except BrokerPortError as error:
            raise BrokerPortError(
                error.code, str(error), correlation_id=intent.correlation_id
            ) from error
        existing = self._orders.get(intent.client_order_id)
        if existing is not None and existing.authorization_ref != intent.authorization_ref:
            raise BrokerPortError(
                BrokerErrorCode.VALIDATION_FAILED,
                "duplicate client_order_id with different authorization",
                correlation_id=intent.correlation_id,
            )

    def _map_status(self, status: str, client_order_id: str, correlation_id: str) -> OrderState:
        state = IBKR_STATUS_MAP.get(status)
        if state is None:
            # Unknown broker state: record UNKNOWN (resolvable only by
            # reconciliation) and fail closed.
            self._unresolved.add(client_order_id)
            raise BrokerPortError(
                BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                f"unmapped broker order status {status!r}; order marked UNKNOWN"
                " pending reconciliation",
                correlation_id=correlation_id,
            )
        return state

    def _append_event(self, receipt: BrokerOrderReceipt) -> None:
        self._events.append(
            BrokerOrderEvent(
                sequence=len(self._events) + 1,
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=receipt.state,
                filled_quantity=Decimal(0),
                observed_at=receipt.received_at,
            )
        )

    @staticmethod
    def _guarded(call: Callable[[], _T]) -> _T:
        try:
            return call()
        except BrokerPortError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise BrokerPortError(
                BrokerErrorCode.BROKER_REJECTED,
                f"broker request failed, failing closed: {type(error).__name__}",
            ) from error


def _coerce_int(value: object, default: int) -> int:
    """Read an integer broker field, falling back to a fail-closed default."""
    try:
        return int(cast("int", value))
    except (TypeError, ValueError):
        return default


def _coerce_decimal(value: object) -> Decimal | None:
    """Read an optional numeric broker field.

    IBKR reports an unset double as a huge sentinel (``UNSET_DOUBLE``); treat
    that — and anything unparseable — as "not reported" rather than inventing
    a price.
    """
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    if abs(number) >= _UNSET_DOUBLE_THRESHOLD:
        return None
    return number


def _build_ibapi_paper_transport(  # pragma: no cover - host-only, gated by POI-IBKR-001
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> PaperTransport:
    """Build the real SDK-backed paper transport (gateway host only).

    ``ibapi`` is imported here, lazily, keeping the SDK confined to this
    package. Actually running this against the paper gateway is the
    separately-approved POI-IBKR-001 experiment — never CI.

    Every callback is demultiplexed through a :class:`CallbackRouter` instead
    of one shared queue, so an in-flight request can never consume (and
    discard) the callback another operation is waiting for — the defect that
    turned a real cancellation into a false timeout.
    """
    import threading
    import time

    try:
        from ibapi.client import EClient  # type: ignore[import-not-found]
        from ibapi.contract import Contract  # type: ignore[import-not-found]
        from ibapi.order import Order  # type: ignore[import-not-found]
        from ibapi.order_cancel import OrderCancel  # type: ignore[import-not-found]
        from ibapi.wrapper import EWrapper  # type: ignore[import-not-found]
    except ImportError as error:
        # Fail closed if the installed API lacks the expected surface (e.g. an
        # older API without ibapi.order_cancel.OrderCancel) — never guess a
        # compatibility shim that could send an invalid cancellation request.
        raise BrokerPortError(
            BrokerErrorCode.BROKER_UNHEALTHY,
            "ibapi (with ibapi.order_cancel.OrderCancel) is not available on this"
            " host; install the expected API inside the broker-ibkr environment"
            " before the paper PoI",
        ) from error

    class _PaperApp(EWrapper, EClient):  # type: ignore[misc]
        def __init__(self) -> None:
            EClient.__init__(self, self)
            self.managed: tuple[str, ...] = ()
            self.ready = threading.Event()
            self.next_id: int = 0
            self.router = CallbackRouter()
            #: The configured API client id, recorded so recovered orders can
            #: be checked against it. Set at connect time.
            self.api_client_id: int = FOREIGN_CLIENT_ID
            self._issued = threading.Lock()
            self._order_ids: set[int] = set()
            self._request_ids: set[int] = set()

        # -- id bookkeeping (order ids and request ids share a numeric space) --
        def issue_order_id(self, order_id: int) -> None:
            with self._issued:
                self._order_ids.add(order_id)

        def issue_request_id(self, request_id: int) -> None:
            with self._issued:
                self._request_ids.add(request_id)

        def _issued_ids(self) -> tuple[set[int], set[int]]:
            with self._issued:
                return set(self._order_ids), set(self._request_ids)

        # -- EWrapper callbacks ------------------------------------------------
        def managedAccounts(self, accounts: str) -> None:  # noqa: N802
            self.managed = tuple(a for a in accounts.split(",") if a)

        def nextValidId(self, order_id: int) -> None:  # noqa: N802
            self.next_id = order_id
            self.ready.set()

        def orderStatus(  # noqa: N802
            self, order_id: int, status: str, *args: object, **kwargs: object
        ) -> None:
            self.issue_order_id(order_id)
            self.router.publish(order_stream(order_id), "status", (order_id, status))

        def contractDetails(self, req_id: int, details: object) -> None:  # noqa: N802
            self.router.publish(request_stream(req_id), "contract", details)

        def contractDetailsEnd(self, req_id: int) -> None:  # noqa: N802
            self.router.publish(request_stream(req_id), "contract_end", req_id)

        def accountSummary(  # noqa: N802
            self, req_id: int, account: str, tag: str, value: str, currency: str
        ) -> None:
            self.router.publish(request_stream(req_id), "account", (account, tag, value))

        def accountSummaryEnd(self, req_id: int) -> None:  # noqa: N802
            self.router.publish(request_stream(req_id), "account_end", req_id)

        def pnl(  # noqa: N802
            self,
            req_id: int,
            daily_pnl: float,
            unrealized_pnl: float,
            realized_pnl: float,
        ) -> None:
            """``reqPnL`` update. ONLY the daily figure is forwarded.

            IBKR sends all three on the same callback, and the unrealized and
            realized numbers are CUMULATIVE. Forwarding them here would invite
            exactly the substitution this whole path exists to prevent, so the
            daily value travels alone.
            """
            self.router.publish(request_stream(req_id), "pnl", daily_pnl)

        def position(  # noqa: N802
            self, account: str, contract: object, position: object, avg_cost: float
        ) -> None:
            self.router.publish(
                POSITIONS_STREAM, "position", (account, contract, position, avg_cost)
            )

        def positionEnd(self) -> None:  # noqa: N802
            self.router.publish(POSITIONS_STREAM, "position_end", None)

        def openOrder(  # noqa: N802
            self, order_id: int, contract: object, order: object, order_state: object
        ) -> None:
            self.router.publish(
                OPEN_ORDERS_STREAM, "open_order", (order_id, contract, order, order_state)
            )

        def openOrderEnd(self) -> None:  # noqa: N802
            self.router.publish(OPEN_ORDERS_STREAM, "open_order_end", None)

        def error(self, *args: object, **kwargs: object) -> None:  # noqa: N802
            # The error() signature varies across API versions (10.48 inserts
            # errorTime), so normalize defensively rather than assuming one
            # shape. An unrecognised shape is dropped instead of crashing the
            # reader thread — losing a diagnostic can never fail an order open.
            try:
                normalized = normalize_error_callback(*args, **kwargs)
            except ErrorCallbackShapeError:
                return
            order_ids, request_ids = self._issued_ids()
            stream = classify_error_stream(
                normalized.request_id, order_ids=order_ids, request_ids=request_ids
            )
            payload = (
                normalized.request_id,
                normalized.code,
                normalized.message,
                normalized.advanced_reject,
            )
            self.router.publish(stream, "error", payload)
            if stream != DIAGNOSTICS_STREAM:
                # Also keep an observable copy: the diagnostics stream is never
                # consumed by an operation waiter, so this duplicates nothing
                # that a waiter depends on.
                self.router.publish(DIAGNOSTICS_STREAM, "error", payload)

    class _IbApiPaperTransport:
        """Blocking, one-request-at-a-time transport over the SDK."""

        def __init__(self) -> None:
            self._app = _PaperApp()
            self._thread: threading.Thread | None = None
            self._req_id = 100

        def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
            self._app.api_client_id = client_id
            self._app.connect(host, port, client_id)
            self._thread = threading.Thread(target=self._app.run, daemon=True)
            self._thread.start()
            if not self._app.ready.wait(timeout_seconds):
                self.disconnect()
                raise BrokerPortError(
                    BrokerErrorCode.BROKER_UNHEALTHY, "gateway did not become ready"
                )
            return self._app.managed

        def disconnect(self) -> None:
            try:
                if self._app.isConnected():
                    self._app.disconnect()
            finally:
                self._app.router.close()
                if self._thread is not None:
                    self._thread.join(timeout=5)

        def is_connected(self) -> bool:
            return bool(self._app.isConnected())

        def _next_request_id(self) -> int:
            self._req_id += 1
            self._app.issue_request_id(self._req_id)
            return self._req_id

        def _collect(self, stream: str, *, end_kind: str, want: str, what: str) -> list[object]:
            """Drain ONE stream up to its end marker; other streams untouched.

            An order-scoped error can never appear here (errors are routed by
            issued id), but a request-scoped error is surfaced rather than
            waited out into a misleading timeout.
            """
            collected: list[object] = []
            try:
                while True:
                    envelope = self._app.router.take(stream, timeout=timeout_seconds)
                    if envelope is None:
                        raise BrokerPortError(
                            BrokerErrorCode.BROKER_REJECTED, f"timed out waiting for {what}"
                        )
                    if envelope.kind == "error":
                        _req, code, text, advanced = cast(
                            "tuple[int | None, int, str, str]", envelope.payload
                        )
                        if is_informational_ibkr_error(code):
                            continue  # farm status: observable, never fatal
                        raise BrokerPortError(
                            classify_ibkr_order_error(code),
                            format_ibkr_error(code, text, advanced, context=what),
                        )
                    if envelope.kind == end_kind:
                        return collected
                    if envelope.kind == want:
                        collected.append(envelope.payload)
            finally:
                # Deterministic waiter cleanup. Anything that arrived late is
                # archived by the router, never silently discarded.
                self._app.router.release(stream)

        def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument | None:
            contract = Contract()
            contract.symbol = query.symbol
            contract.secType = "STK"
            contract.exchange = query.exchange or "SMART"
            contract.currency = query.currency or "USD"
            req_id = self._next_request_id()
            self._app.reqContractDetails(req_id, contract)
            details = self._collect(
                request_stream(req_id),
                end_kind="contract_end",
                want="contract",
                what="contract details",
            )
            if not details:
                return None
            first = details[0]
            resolved = getattr(first, "contract", None)
            if resolved is None:
                return None
            return BrokerInstrument(
                broker_instrument_ref=f"conid:{getattr(resolved, 'conId', 0)}",
                symbol=str(getattr(resolved, "symbol", query.symbol)),
                exchange=str(getattr(resolved, "exchange", contract.exchange)),
                currency=str(getattr(resolved, "currency", contract.currency)),
            )

        def account_summary(self, account_id: str) -> Mapping[str, Decimal]:
            req_id = self._next_request_id()
            self._app.reqAccountSummary(
                req_id, "All", "NetLiquidation,TotalCashValue,BuyingPower"
            )
            rows = self._collect(
                request_stream(req_id),
                end_kind="account_end",
                want="account",
                what="account summary",
            )
            self._app.cancelAccountSummary(req_id)
            values: dict[str, Decimal] = {}
            for row in rows:
                account, tag, value = cast("tuple[str, str, str]", row)
                if account == account_id:
                    values[tag] = Decimal(value)
            return values

        def session_pnl(self, account_id: str) -> Decimal | None:
            """Daily P&L via the canonical ``reqPnL`` subscription.

            This is a subscription, not a request/response: there is no "end"
            callback, so the first update is taken and the subscription is
            cancelled immediately. No update inside the timeout means the
            gateway is not serving one -- typically no P&L permission on the
            account -- and the honest answer is None.

            A ``nan`` daily value (IBKR's "not available" for a float field) is
            also None. It is never zero.
            """
            req_id = self._next_request_id()
            try:
                self._app.reqPnL(req_id, account_id, "")
            except Exception:  # noqa: BLE001 - unsupported/unavailable => UNKNOWN
                return None
            payload: object = None
            try:
                deadline = time.monotonic() + SESSION_PNL_TIMEOUT_SECONDS
                while payload is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    envelope = self._app.router.take(
                        request_stream(req_id), timeout=remaining
                    )
                    if envelope is None:
                        break
                    if envelope.kind == "pnl":
                        payload = envelope.payload
                    # Any other envelope on this stream (an error, a farm
                    # notice) simply is not a daily P&L; keep waiting rather
                    # than mistaking it for one.
            except Exception:  # noqa: BLE001 - transport failure => UNKNOWN
                payload = None
            finally:
                try:
                    self._app.cancelPnL(req_id)
                except Exception:  # noqa: BLE001 - best-effort unsubscribe
                    pass
            if payload is None:
                return None
            try:
                value = Decimal(str(payload))
            except (ArithmeticError, ValueError):
                return None
            return value if value.is_finite() else None

        def positions(self, account_id: str) -> tuple[BrokerPosition, ...]:
            self._app.reqPositions()
            rows = self._collect(
                POSITIONS_STREAM, end_kind="position_end", want="position", what="positions"
            )
            self._app.cancelPositions()
            result: list[BrokerPosition] = []
            for row in rows:
                account, contract, quantity, avg_cost = cast(
                    "tuple[str, object, object, float]", row
                )
                if account != account_id:
                    continue
                result.append(
                    BrokerPosition(
                        symbol=str(getattr(contract, "symbol", "")),
                        quantity=Decimal(str(quantity)),
                        average_price=Decimal(str(avg_cost)),
                    )
                )
            return tuple(result)

        def next_order_id(self) -> int:
            order_id = self._app.next_id
            self._app.next_id += 1
            self._app.issue_order_id(order_id)
            return order_id

        def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
            contract = Contract()
            contract.symbol = intent.symbol
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"
            # Translate + validate every outbound field BEFORE placeOrder. A
            # missing/empty order type or TIF raises here, so nothing with an
            # empty value ever reaches the wire (IBKR 10052 becomes impossible).
            fields = build_outgoing_order(intent, account_id)
            order = Order()
            order.account = fields.account
            order.action = fields.action
            order.orderType = fields.order_type
            order.tif = fields.tif
            order.totalQuantity = fields.total_quantity
            if fields.limit_price is not None:
                order.lmtPrice = fields.limit_price
            if fields.aux_price is not None:
                order.auxPrice = fields.aux_price
            order.outsideRth = fields.outside_rth
            order.eTradeOnly = fields.e_trade_only
            order.firmQuoteOnly = fields.firm_quote_only
            # Explicit: submit as a live order (never leave transmit implicit).
            order.transmit = fields.transmit
            # Persist the canonical (non-authorizing) client_order_id so a
            # restart can recover this order's identity from the Gateway.
            order.orderRef = fields.order_ref
            self._app.issue_order_id(broker_order_id)
            self._app.placeOrder(broker_order_id, contract, order)
            return self._await_status(broker_order_id)

        def _open_order_rows(
            self, account_id: str, rows: list[object]
        ) -> tuple[RawOpenOrder, ...]:
            """Normalize openOrder callbacks into the recovered-order model."""
            snapshot: dict[int, RawOpenOrder] = {}
            for row in rows:
                order_id, contract, order, order_state = cast(
                    "tuple[int, object, object, object]", row
                )
                if order_id in snapshot:
                    continue  # duplicate openOrder callback for the same order
                order_account = str(getattr(order, "account", "") or account_id)
                if order_account != account_id:
                    continue
                snapshot[order_id] = RawOpenOrder(
                    broker_order_id=order_id,
                    order_ref=str(getattr(order, "orderRef", "") or ""),
                    status=str(getattr(order_state, "status", "") or ""),
                    symbol=str(getattr(contract, "symbol", "") or ""),
                    # Absent clientId => FOREIGN_CLIENT_ID => never cancellable.
                    client_id=_coerce_int(getattr(order, "clientId", None), FOREIGN_CLIENT_ID),
                    perm_id=_coerce_int(getattr(order, "permId", None), 0),
                    account=order_account,
                    action=str(getattr(order, "action", "") or ""),
                    quantity=_coerce_decimal(getattr(order, "totalQuantity", None)) or Decimal(0),
                    order_type=str(getattr(order, "orderType", "") or ""),
                    limit_price=_coerce_decimal(getattr(order, "lmtPrice", None)),
                    aux_price=_coerce_decimal(getattr(order, "auxPrice", None)),
                )
            return tuple(snapshot.values())

        def open_order_snapshot(self, account_id: str) -> tuple[RawOpenOrder, ...]:
            # Read-only GLOBAL visibility: reqAllOpenOrders never sends,
            # cancels, replaces or modifies an order. It reports orders from
            # every API client and from the TWS/Gateway UI, so the adapter
            # treats it as evidence only, never as cancellation authority.
            self._app.reqAllOpenOrders()
            rows = self._collect(
                OPEN_ORDERS_STREAM,
                end_kind="open_order_end",
                want="open_order",
                what="open orders",
            )
            return self._open_order_rows(account_id, rows)

        def same_client_open_orders(self, account_id: str) -> tuple[RawOpenOrder, ...]:
            # Read-only SAME-CLIENT feed: reqOpenOrders returns the orders bound
            # to this API client id, which is the only basis on which a
            # recovered order may become cancellable. Rows are additionally
            # filtered on the reported clientId (belt and braces).
            self._app.reqOpenOrders()
            rows = self._collect(
                OPEN_ORDERS_STREAM,
                end_kind="open_order_end",
                want="open_order",
                what="same-client open orders",
            )
            return tuple(
                row
                for row in self._open_order_rows(account_id, rows)
                if row.client_id == self._app.api_client_id
            )

        def cancel(self, broker_order_id: int) -> str:
            # Modern IB API: cancelOrder(orderId, OrderCancel()). The previous
            # empty-string argument raised inside ibapi (it reads OrderCancel
            # attributes), so no cancellation request ever reached the wire.
            self._app.issue_order_id(broker_order_id)
            self._app.cancelOrder(broker_order_id, OrderCancel())
            resolution = resolve_cancel(self._cancel_events(broker_order_id), broker_order_id)
            # Fails closed: EXPOSED/TIMEOUT -> UNKNOWN (reconcile required),
            # REJECTED / deterministic errors -> BROKER_REJECTED (terminal).
            # Never retries, and never accepts a stale ack as confirmation.
            return cancel_resolution_to_status(resolution)

        def _cancel_events(self, broker_order_id: int) -> Iterator[CancelCallbackEvent]:
            """Correlated callbacks for one cancellation, in arrival order."""
            stream = order_stream(broker_order_id)
            while True:
                envelope = self._app.router.take(stream, timeout=timeout_seconds)
                if envelope is None:
                    return  # exhausted -> resolve_cancel yields TIMEOUT
                if envelope.kind == "status":
                    order_id, status = cast("tuple[int, str]", envelope.payload)
                    yield CancelCallbackEvent(
                        kind="status", order_id=order_id, status=status
                    )
                elif envelope.kind == "error":
                    request_id, code, text, advanced = cast(
                        "tuple[int | None, int, str, str]", envelope.payload
                    )
                    yield CancelCallbackEvent(
                        kind="error",
                        order_id=request_id if request_id is not None else broker_order_id,
                        error_code=code,
                        error_text=text,
                        advanced_reject=advanced,
                    )

        def _await_status(self, broker_order_id: int) -> str:
            stream = order_stream(broker_order_id)
            while True:
                envelope = self._app.router.take(stream, timeout=timeout_seconds)
                if envelope is None:
                    raise BrokerPortError(
                        BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                        f"no status for order {broker_order_id}; reconcile required",
                    )
                if envelope.kind == "status":
                    order_id, status = cast("tuple[int, str]", envelope.payload)
                    if order_id == broker_order_id:
                        return status
                elif envelope.kind == "error":
                    _req, code, text, advanced = cast(
                        "tuple[int | None, int, str, str]", envelope.payload
                    )
                    if is_informational_ibkr_error(code):
                        continue  # farm status: never fails a submit
                    raise BrokerPortError(
                        classify_ibkr_order_error(code),
                        format_ibkr_error(code, text, advanced, context="submit"),
                    )

    return _IbApiPaperTransport()


def build_paper_adapter(
    *,
    account_aliases: Mapping[str, str],
    host: str = "127.0.0.1",
    port: int = PAPER_API_PORT,
    client_id: int = DEFAULT_CLIENT_ID,
) -> IbkrPaperAdapter:
    """Paper adapter over the lazy SDK transport (gateway host only)."""
    return IbkrPaperAdapter(
        _build_ibapi_paper_transport,
        account_aliases=account_aliases,
        host=host,
        port=port,
        client_id=client_id,
    )


__all__ = [
    "ADAPTER_VERSION",
    "ALLOWED_HOSTS",
    "CANCEL_CONFIRMED_ERROR_CODES",
    "DEFAULT_CLIENT_ID",
    "DETERMINISTIC_ORDER_REJECT_CODES",
    "FOREIGN_CLIENT_ID",
    "IBKR_ORDER_TYPE_MAP",
    "IBKR_PAPER_CAPABILITIES",
    "IBKR_STATUS_MAP",
    "IBKR_TIME_IN_FORCE_MAP",
    "INFORMATIONAL_IBKR_ERROR_CODES",
    "LIFECYCLE_ORDER_REF_PREFIX",
    "LIVE_API_PORT",
    "ORDER_REF_MAX_LEN",
    "PAPER_ACCOUNT_PREFIX",
    "PAPER_API_PORT",
    "CancelCallbackEvent",
    "CancelDisposition",
    "CancelResolution",
    "IbkrPaperAdapter",
    "OrderRefTooLongError",
    "OutgoingOrder",
    "SESSION_PNL_TIMEOUT_SECONDS",
    "PaperTransport",
    "RawOpenOrder",
    "RecoveredIdentity",
    "build_outgoing_order",
    "build_paper_adapter",
    "cancel_resolution_to_status",
    "classify_cancel_status",
    "classify_ibkr_order_error",
    "decode_order_ref",
    "encode_order_ref",
    "format_ibkr_error",
    "ibkr_order_type",
    "ibkr_time_in_force",
    "is_informational_ibkr_error",
    "mask_account_id",
    "resolve_cancel",
    "utc_now",
]
