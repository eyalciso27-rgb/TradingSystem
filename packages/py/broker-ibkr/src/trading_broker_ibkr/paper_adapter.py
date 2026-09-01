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

import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
    ProtectiveBracket,
    ReconciliationResult,
    RecoveredOrder,
    SessionPnlObservation,
    SessionPnlStatus,
    TimeInForce,
)
from trading_contracts.enums import Environment, OrderState, OrderType

from trading_broker_ibkr.callback_router import (
    DIAGNOSTICS_STREAM,
    OPEN_ORDERS_STREAM,
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
from trading_broker_ibkr.instrument_ref import (
    decode_instrument_ref,
    encode_instrument_ref,
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
#: reporting a bounded TIMEOUT (not a permission fact).
#:
#: One bounded value serves both the owner probe and the execution decision,
#: and it can do so honestly ONLY because request-scoped errors now
#: short-circuit: a broker rejection returns immediately as REQUEST_ERROR
#: rather than consuming the whole window, so this timeout governs just the
#: "no callback at all" case. IBKR documents that aggregate P&L can take
#: several seconds to compute for some account structures, and the third
#: owner-host run showed the previous 5s was too tight to distinguish a slow
#: subscription from an absent one. 15s matches ``DEFAULT_TIMEOUT_SECONDS``
#: used elsewhere and stays bounded -- there is never an unbounded wait, and
#: the execution gate still fails closed on TIMEOUT exactly as on None.
#: Process-global request-id sequence, so a reconnect (a NEW transport) never
#: reuses a request id from a previous connection -- a stale reqId can then
#: never be mistaken for a current subscription's. ``itertools.count.__next__``
#: is atomic under the GIL. Starts high enough to stay clear of the fixed
#: account-summary/contract-details ids.
_GLOBAL_REQUEST_ID = itertools.count(10_000)

SESSION_PNL_TIMEOUT_SECONDS = 15.0

#: Maximum age of the persistent subscription's latest ``pnl`` callback for its
#: value to count as CURRENT. IBKR delivers reqPnL updates roughly once a
#: second, so a value last seen more than this long ago means the subscription
#: has gone quiet -- the value is reported STALE and fails closed, never used as
#: if it were current. Generous enough to tolerate a gateway hiccup, tight
#: enough to catch a dead subscription; bounded, never an unbounded wait.
SESSION_PNL_FRESHNESS_SECONDS = 30.0

#: How long ONE account-summary observation is shared by the gates of a single
#: decision before a genuinely fresh request is issued.
#:
#: ``reqAccountSummary`` is a SUBSCRIPTION, and cancelling it is asynchronous
#: and unacknowledged. One execution decision observes the account SIX times
#: (evidence, risk, readiness -- then readiness again at the submission
#: boundary), so the previous shape issued six subscribe/cancel cycles for the
#: same group within a second, each one racing the previous cancel. A gateway is
#: entitled to refuse the duplicate, and that refusal surfaced as a bare
#: "unknown environment" readiness block on the owner host.
#:
#: This is NOT a freshness relaxation of any gate: it de-duplicates observations
#: that are milliseconds apart and describe the same account state. An
#: observation older than this window is not reused -- a real request is issued --
#: so a genuinely later read is still a genuinely fresh read.
ACCOUNT_SUMMARY_FRESHNESS_SECONDS = 5.0
DEFAULT_CLIENT_ID = 1
DEFAULT_TIMEOUT_SECONDS = 15.0

#: IBKR paper capability matrix (Integration 20.4). MVP hard rules:
#: fractional and outside-RTH DISABLED; trailing not auto-approved.
#: HONESTY RULE: a capability is advertised only at the level this adapter
#: actually implements TODAY. ``bracket`` and ``oco`` are NATIVE as of ADR-0019:
#: ``submit_order`` establishes a real native bracket (parent entry + protective
#: STOP child under one ``parentId``, an OCA group, and a single transmit) atomically
#: through ``PaperTransport.submit_bracket``. They were UNSUPPORTED until that
#: wiring + its conformance coverage existed; the flip and the implementation
#: land together, never NATIVE-by-promise and never silently emulated (20.4).
IBKR_PAPER_CAPABILITIES = BrokerCapabilities(
    order_types={
        OrderType.LIMIT: BrokerCapabilityMode.NATIVE,
        OrderType.STOP: BrokerCapabilityMode.NATIVE,
        OrderType.STOP_LIMIT: BrokerCapabilityMode.NATIVE,
        OrderType.MARKET: BrokerCapabilityMode.NATIVE,
    },
    bracket=BrokerCapabilityMode.NATIVE,
    oco=BrokerCapabilityMode.NATIVE,
    trailing=BrokerCapabilityMode.MANUAL_ONLY,
    fractional=BrokerCapabilityMode.DISABLED,
    outside_rth=BrokerCapabilityMode.DISABLED,
    what_if=BrokerCapabilityMode.NATIVE,
    streaming_events=BrokerCapabilityMode.NATIVE,
)

#: Order states in which a leg is genuinely at the broker doing its job — the
#: protective stop must be one of these alongside a working parent, or the entry
#: is unprotected and the bracket fails closed.
WORKING_OR_BETTER_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    }
)

#: Order states in which a leg is DEFINITELY not live at the broker. Everything
#: outside WORKING_OR_BETTER and outside this set (an unmapped status ->
#: ``UNKNOWN``, say) is uncertain — it MAY be live — and is treated as such.
NOT_LIVE_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.CANCELLED_BY_POLICY,
        OrderState.EXPIRED,
    }
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
    #: The broker's own security identity, when the callback reported one.
    broker_instrument_ref: str = ""
    client_id: int = FOREIGN_CLIENT_ID
    perm_id: int = 0
    account: str = ""
    action: str = ""
    quantity: Decimal = Decimal(0)
    order_type: str = ""
    limit_price: Decimal | None = None
    aux_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BracketSubmission:
    """The broker's answer to one atomic protected-bracket submission.

    Both legs are placed under one transmit (only the last leg carries
    ``transmit=True``), so the broker returns a status for each. The adapter uses
    the pair to decide the lifecycle state: the entry is PROTECTED only when the
    protective stop is working-or-better alongside a working entry. A parent that
    is live while the stop is not is the exact unprotected window this milestone
    exists to close, so the adapter fails closed and cancels the parent.
    """

    parent_broker_order_id: int
    parent_status: str
    stop_broker_order_id: int
    stop_status: str


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

    def session_pnl(self, account_id: str) -> SessionPnlObservation:
        """The account's DAILY P&L observation: a status, a value, a safe detail.

        A distinct operation rather than an account-summary tag, because IBKR's
        ``reqAccountSummary`` genuinely does not carry a daily figure: the daily
        number comes from the separate ``reqPnL`` subscription, and inventing a
        ``DailyPnL`` summary key would have described a feed that does not
        exist.

        The status distinguishes a value (OK) from silence (TIMEOUT), a broker
        rejection (REQUEST_ERROR) and a NaN callback (UNSET). Only OK carries a
        value; every other status means UNKNOWN, and the risk engine, which
        reads only the value, still fails both daily halt limits closed -- a
        zero would silently disable them.

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

    def submit_bracket(
        self,
        *,
        parent_order_id: int,
        stop_order_id: int,
        account_id: str,
        intent: OrderIntent,
    ) -> BracketSubmission:
        """Send an ATOMIC native bracket: parent entry + protective stop child.

        The implementation builds the parent (entry) with ``transmit=False`` and
        the protective STOP child with ``parentId = parent_order_id``, an OCA
        group binding the protective legs, and ``transmit=True`` on the last leg
        only — so IBKR transmits the whole structure at once and the entry is
        never live without its stop. ``intent.protective`` names the stop level
        and the child's ``orderRef``; the parent's ``orderRef`` is
        ``intent.client_order_id``. Returns both broker ids and both status
        strings. Never re-resolves the instrument by ticker.
        """
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
        # Parent entry client id -> its protective stop child client id. The
        # in-memory half of the protective relationship; the durable half rides
        # the execution audit chain (ADR-0019) so a restart can rebuild it.
        self._protective_child: dict[str, str] = {}

    # -- BrokerPort ----------------------------------------------------------
    def connect(self) -> None:
        _validate_paper_endpoint(self._host, self._port)
        # Refuse a second connect while one is live: connect() builds a fresh
        # transport, so a second call without an intervening disconnect would
        # orphan the first transport -- and its live reqPnL subscription, which
        # nothing could then cancel. One live connection per adapter.
        if self._transport is not None:
            raise BrokerPortError(
                BrokerErrorCode.BROKER_UNHEALTHY,
                "already connected; disconnect before connecting again",
            )
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
        pnl = self._session_pnl(transport, account_id)
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
                # closed rather than pretending the day is flat. The status and
                # the SAFE (code-only) detail are carried alongside so the owner
                # probe can say WHY it is unknown -- a timeout, a real request
                # error (with the broker CODE), an unset sentinel -- without a
                # further code change and without exposing the account id.
                session_pnl=pnl.value,
                session_pnl_status=pnl.status,
                session_pnl_detail=pnl.detail or None,
                # WHEN the pnl callback actually arrived, not when this snapshot
                # was built -- the freshness fact the risk gate needs.
                session_pnl_observed_at=pnl.observed_at,
                observed_at=self._now(),
            )
        except KeyError as missing:
            raise BrokerPortError(
                BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                f"account summary missing tag {missing}; failing closed",
            ) from missing

    @staticmethod
    def _session_pnl(
        transport: PaperTransport, account_id: str
    ) -> SessionPnlObservation:
        """Ask for the daily P&L observation. Any raise is UNKNOWN, never zero.

        The transport already classifies the outcome (OK / TIMEOUT /
        REQUEST_ERROR / UNSET). A transport that raises outright -- a fake, or
        an unexpected failure -- is folded into REQUEST_ERROR here so the value
        is still absent and the decision still fails closed.
        """
        try:
            observation = transport.session_pnl(account_id)
        except Exception as error:  # noqa: BLE001 - any failure means UNKNOWN
            return SessionPnlObservation(
                SessionPnlStatus.REQUEST_ERROR,
                detail=f"session_pnl raised {type(error).__name__}",
            )
        # Defence in depth, with the IBKR sentinel knowledge kept HERE in the
        # adapter (never in broker-core): any OK value that is non-finite or the
        # DBL_MAX unset sentinel is downgraded to UNSET, so no transport variant
        # can slip a vast sentinel through as genuine P&L.
        if observation.value is not None and (
            not observation.value.is_finite()
            or abs(observation.value) >= _UNSET_DOUBLE_THRESHOLD
        ):
            return SessionPnlObservation(
                SessionPnlStatus.UNSET,
                detail="daily value unset or not a number",
                observed_at=observation.observed_at,
            )
        return observation

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
                    broker_instrument_ref=raw.broker_instrument_ref,
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
        if intent.protective is not None:
            # A protected entry is an ATOMIC native bracket, never a lone entry
            # followed by a separate stop (ADR-0019). It has its own path.
            return self._submit_bracket(transport, intent)
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

    def _submit_bracket(
        self, transport: PaperTransport, intent: OrderIntent
    ) -> BrokerOrderReceipt:
        """Establish the parent entry and its protective stop as ONE broker action.

        Returns the PARENT receipt. The protective child is recorded under its
        own client id so it is reconcilable and cancellable. If the broker
        accepts the entry but NOT the stop, the entry is unprotected — the exact
        window this milestone closes — so the adapter cancels the parent
        best-effort (cleanup ownership: an attempted order may have reached the
        gateway) and fails closed.
        """
        bracket = intent.protective
        assert bracket is not None  # guarded by the caller; narrows for typing
        self._validate_protective(intent, bracket)
        account_id = self._account_id(intent.account_alias)
        # Two monotonic ids, parent first: the child's parentId is the parent id,
        # and IBKR requires the child id to be greater than the parent id.
        parent_order_id = self._guarded(transport.next_order_id)
        stop_order_id = self._guarded(transport.next_order_id)
        try:
            result = self._guarded(
                lambda: transport.submit_bracket(
                    parent_order_id=parent_order_id,
                    stop_order_id=stop_order_id,
                    account_id=account_id,
                    intent=intent,
                )
            )
        except BrokerPortError:
            # Cleanup ownership: a bracket sends the parent and the stop as
            # separate placeOrder calls, so a synchronous failure PART-WAY
            # through can leave the parent live and unprotected while the stop
            # never arrived. Once an outbound placeOrder has been ATTEMPTED it
            # MAY have reached the gateway, so both ids are cancelled best-effort
            # and the order is marked unresolved rather than left an orphan.
            self._cleanup_bracket_best_effort(
                transport, intent.client_order_id, parent_order_id, stop_order_id
            )
            raise
        received_at = self._now()
        # Map WITHOUT raising: an unmapped status is UNKNOWN, and an UNKNOWN leg
        # is uncertain (it may be live), which the coherence matrix must see
        # rather than have _map_status raise past it before either leg is
        # cleaned up.
        parent_state = IBKR_STATUS_MAP.get(result.parent_status, OrderState.UNKNOWN)
        stop_state = IBKR_STATUS_MAP.get(result.stop_status, OrderState.UNKNOWN)
        # The EXPLICIT bracket-outcome matrix. Only two shapes are coherent:
        #   * both legs working-or-better  -> PROTECTED, proceed;
        #   * both legs definitely-not-live -> a clean rejection with nothing
        #     live at the broker, returned as the (rejected) parent receipt.
        # EVERY other pair -- one live while the other is not coherent, or
        # EITHER leg UNKNOWN -- may leave a working, unexplained leg (an
        # unprotected entry, or an orphan stop). Those fail closed: cancel BOTH
        # ids best-effort, mark unresolved, and refuse. "IBKR would never do
        # this" is not a safety argument.
        parent_live = parent_state in WORKING_OR_BETTER_STATES
        stop_live = stop_state in WORKING_OR_BETTER_STATES
        both_not_live = (
            parent_state in NOT_LIVE_STATES and stop_state in NOT_LIVE_STATES
        )
        if not (parent_live and stop_live) and not both_not_live:
            self._cleanup_bracket_best_effort(
                transport, intent.client_order_id, parent_order_id, stop_order_id
            )
            raise BrokerPortError(
                BrokerErrorCode.BROKER_REJECTED,
                "incoherent bracket acceptance"
                f" (parent {parent_state.value}, stop {stop_state.value}): one"
                " leg may be live while the other is not proven, so both were"
                " cancelled best-effort and the bracket refused. Reconcile.",
                correlation_id=intent.correlation_id,
            )
        parent_receipt = BrokerOrderReceipt(
            client_order_id=intent.client_order_id,
            broker_order_ref=f"ibkr-paper:{parent_order_id}",
            state=parent_state,
            authorization_ref=intent.authorization_ref,
            correlation_id=intent.correlation_id,
            received_at=received_at,
        )
        stop_receipt = BrokerOrderReceipt(
            client_order_id=bracket.stop_client_order_id,
            broker_order_ref=f"ibkr-paper:{stop_order_id}",
            state=stop_state,
            authorization_ref=intent.authorization_ref,
            correlation_id=intent.correlation_id,
            received_at=received_at,
        )
        self._orders[intent.client_order_id] = parent_receipt
        self._orders[bracket.stop_client_order_id] = stop_receipt
        self._broker_ids[intent.client_order_id] = parent_order_id
        self._broker_ids[bracket.stop_client_order_id] = stop_order_id
        self._protective_child[intent.client_order_id] = bracket.stop_client_order_id
        self._append_event(parent_receipt)
        self._append_event(stop_receipt)
        return parent_receipt

    def _cleanup_bracket_best_effort(
        self,
        transport: PaperTransport,
        client_order_id: str,
        *broker_order_ids: int,
    ) -> None:
        """Cancel every reserved bracket id best-effort and mark it unresolved.

        The cleanup-ownership rule for a multi-leg order: once ANY leg's
        placeOrder was attempted, EITHER leg may be live at the broker, so both
        are cancelled and the order is surfaced for reconciliation rather than
        left as an unexplained working order. A cancel that itself fails leaves
        the state uncertain, which is exactly why it is also marked unresolved.
        """
        self._unresolved.add(client_order_id)
        for broker_order_id in broker_order_ids:
            try:
                self._guarded(lambda: transport.cancel(broker_order_id))  # noqa: B023
            except BrokerPortError:
                pass

    def _validate_protective(
        self, intent: OrderIntent, bracket: ProtectiveBracket
    ) -> None:
        """The protective structure must be one the adapter can build natively."""
        # MVP partial-fill safety (ADR-0019, Option A). IBKR holds an attached
        # protective child until the PARENT is completely filled, so a parent of
        # more than one share can sit PARTIALLY_FILLED with its stop not yet
        # active -- an unprotected window. Until live partial-fill protection is
        # built, a protected Paper entry is exactly ONE whole share, enforced
        # HERE at the final boundary every order crosses. Fractional is disabled,
        # so a one-share order fills fully or not at all: no partial window. The
        # authority may reduce a larger risk approval to one (never increase it);
        # this refusal is the floor that cannot be bypassed by constructing a
        # different OrderIntent, because every OrderIntent crosses this method.
        if intent.quantity != Decimal(1):
            raise BrokerPortError(
                BrokerErrorCode.VALIDATION_FAILED,
                "a protected Paper entry is limited to 1 whole share in the MVP"
                " (partial-fill protection is not yet built); refusing a"
                f" protected quantity of {intent.quantity}",
                correlation_id=intent.correlation_id,
            )
        if IBKR_PAPER_CAPABILITIES.bracket is not BrokerCapabilityMode.NATIVE:
            raise BrokerPortError(
                BrokerErrorCode.CAPABILITY_UNSUPPORTED,
                "native bracket is not supported by this adapter",
                correlation_id=intent.correlation_id,
            )
        # The protective STOP child must itself be a natively-supported type.
        stop_mode = IBKR_PAPER_CAPABILITIES.order_types.get(
            OrderType.STOP, BrokerCapabilityMode.UNSUPPORTED
        )
        if stop_mode is not BrokerCapabilityMode.NATIVE:
            raise BrokerPortError(
                BrokerErrorCode.CAPABILITY_UNSUPPORTED,
                f"protective STOP is {stop_mode.value} for IBKR paper",
                correlation_id=intent.correlation_id,
            )
        # A child id already bound to a different order is a collision, and a
        # collision is never silently reused.
        for child_id in (
            bracket.stop_client_order_id,
            bracket.take_profit_client_order_id,
        ):
            if child_id and child_id in self._orders:
                raise BrokerPortError(
                    BrokerErrorCode.VALIDATION_FAILED,
                    "a protective child id is already bound to another order",
                    correlation_id=intent.correlation_id,
                )
        # This milestone establishes the protective STOP only; a take-profit leg
        # is carried by the contract but not yet wired, and silently dropping it
        # would under-deliver the requested structure.
        if bracket.take_profit_price is not None:
            raise BrokerPortError(
                BrokerErrorCode.CAPABILITY_UNSUPPORTED,
                "take-profit legs are not yet wired; this milestone establishes"
                " the protective stop only",
                correlation_id=intent.correlation_id,
            )

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


def _resolve_one_contract(
    details: Sequence[object], query: InstrumentQuery, requested: object
) -> BrokerInstrument | None:
    """The ONE security this query names, or nothing.

    Two failures used to pass silently. ``details[0]`` took whichever contract
    IBKR happened to list first, so an ambiguous query -- several listings,
    several currencies, several share classes under one root -- resolved to an
    arbitrary one of them. And a contract with no usable ``conId`` still
    produced ``"conid:0"``, a reference that identifies nothing.

    An ambiguous or unusable answer is now no answer, which the caller turns
    into INSTRUMENT_NOT_FOUND and zero submits. A security we cannot name is a
    security we will not trade.
    """
    candidates: list[tuple[int, object]] = []
    for detail in details:
        resolved = getattr(detail, "contract", None)
        if resolved is None:
            continue
        con_id = _coerce_int(getattr(resolved, "conId", None), 0)
        if con_id <= 0:
            # IBKR answered without a usable identity. Not a security.
            continue
        candidates.append((con_id, resolved))
    if not candidates:
        return None
    distinct = {con_id for con_id, _ in candidates}
    if len(distinct) > 1:
        # Genuinely ambiguous: the query names more than one security. Choosing
        # one would be choosing at random.
        return None
    con_id, resolved = candidates[0]
    reference = encode_instrument_ref(con_id)
    if not reference:
        return None
    return BrokerInstrument(
        broker_instrument_ref=reference,
        symbol=str(getattr(resolved, "symbol", query.symbol)),
        exchange=str(getattr(resolved, "exchange", getattr(requested, "exchange", ""))),
        currency=str(
            getattr(resolved, "currency", getattr(requested, "currency", ""))
        ),
    )


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
    *,
    session_pnl_timeout_seconds: float = SESSION_PNL_TIMEOUT_SECONDS,
    session_pnl_freshness_seconds: float = SESSION_PNL_FRESHNESS_SECONDS,
    account_summary_freshness_seconds: float = ACCOUNT_SUMMARY_FRESHNESS_SECONDS,
    monotonic_clock: Callable[[], float] | None = None,
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

    #: Injectable for deterministic freshness tests; the real host uses the
    #: wall-independent monotonic clock.
    _monotonic: Callable[[], float] = monotonic_clock or time.monotonic

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
            #: Fresh identity per open-order snapshot (reqAllOpenOrders /
            #: reqOpenOrders carry no request id). Assignment is atomic; the
            #: transport serialises snapshots, so no lock is needed for the
            #: reader thread to read the current target.
            self._open_orders_generation: int = 0
            self._active_open_orders_stream: str | None = None
            # -- persistent session-P&L subscription state --------------------
            # reqPnL is a SUBSCRIPTION: one is started per connection and stays
            # active, its ``pnl`` callbacks caching the latest daily value and
            # the monotonic + wall-clock time it arrived. Callbacks for any other
            # reqId (a previous subscription, a stale reconnect) are ignored.
            self._pnl_lock = threading.Lock()
            self._pnl_active_req_id: int | None = None
            self._pnl_daily: float | None = None
            self._pnl_observed_monotonic: float | None = None
            self._pnl_observed_at: datetime | None = None
            self._pnl_error_code: int | None = None
            self._pnl_first_update = threading.Event()

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
            """``reqPnL`` update. ONLY the daily figure is cached.

            IBKR sends all three on the same callback, and the unrealized and
            realized numbers are CUMULATIVE. Caching them here would invite
            exactly the substitution this whole path exists to prevent, so the
            daily value travels alone. A callback whose reqId is not the active
            subscription belongs to a previous (or reconnected) subscription and
            is ignored.
            """
            with self._pnl_lock:
                if req_id != self._pnl_active_req_id:
                    return
                self._pnl_daily = daily_pnl
                self._pnl_observed_monotonic = _monotonic()
                self._pnl_observed_at = datetime.now(UTC)
                self._pnl_error_code = None
                self._pnl_first_update.set()

        # -- persistent session-P&L subscription -----------------------------
        def start_session_pnl(self, req_id: int) -> None:
            """Arm the cache for a fresh subscription id, discarding any prior."""
            with self._pnl_lock:
                self._pnl_active_req_id = req_id
                self._pnl_daily = None
                self._pnl_observed_monotonic = None
                self._pnl_observed_at = None
                self._pnl_error_code = None
                self._pnl_first_update.clear()

        def stop_session_pnl(self) -> None:
            """Detach: subsequent pnl callbacks for the old id are ignored."""
            with self._pnl_lock:
                self._pnl_active_req_id = None

        def record_session_pnl_error(self, req_id: int, code: int) -> None:
            """A request-scoped error for the active subscription. Unblocks the
            initial wait; the code only, never the raw text."""
            with self._pnl_lock:
                if req_id != self._pnl_active_req_id:
                    return
                self._pnl_error_code = code
                self._pnl_observed_monotonic = _monotonic()
                self._pnl_first_update.set()

        def session_pnl_state(
            self,
        ) -> tuple[int | None, float | None, float | None, datetime | None, int | None]:
            """A consistent snapshot of the cached P&L: (active id, daily,
            observed monotonic, observed wall-clock, error code)."""
            with self._pnl_lock:
                return (
                    self._pnl_active_req_id,
                    self._pnl_daily,
                    self._pnl_observed_monotonic,
                    self._pnl_observed_at,
                    self._pnl_error_code,
                )

        # Positions are observed with reqPositionsMulti, which carries a request
        # id: each observation routes to its OWN request_stream(req_id), a unique
        # lifecycle key the router can safely retire. The global reqPositions
        # callbacks below are NOT used by any waiter -- they route to
        # diagnostics -- because their fixed "positions" channel is reusable and
        # cannot be retired without breaking the next read (round-10 regression).
        def positionMulti(  # noqa: N802
            self,
            req_id: int,
            account: str,
            model_code: str,
            contract: object,
            position: object,
            avg_cost: float,
        ) -> None:
            self.router.publish(
                request_stream(req_id),
                "position",
                (account, contract, position, avg_cost),
            )

        def positionMultiEnd(self, req_id: int) -> None:  # noqa: N802
            self.router.publish(request_stream(req_id), "position_end", None)

        def position(  # noqa: N802
            self, account: str, contract: object, position: object, avg_cost: float
        ) -> None:
            # Legacy global reqPositions feed -- not consumed by any waiter.
            self.router.publish(DIAGNOSTICS_STREAM, "position", None)

        def positionEnd(self) -> None:  # noqa: N802
            self.router.publish(DIAGNOSTICS_STREAM, "position_end", None)

        # Open-order snapshots (reqAllOpenOrders / reqOpenOrders) carry NO
        # request id, so each snapshot is given a fresh transport-owned
        # generation: openOrder/openOrderEnd route to the CURRENT active
        # generation's stream. That key is unique per snapshot, so retiring it
        # cannot starve the next snapshot; a callback outside any active
        # snapshot is diagnostic.
        def _open_orders_target(self) -> str:
            active = self._active_open_orders_stream
            return active if active is not None else DIAGNOSTICS_STREAM

        def openOrder(  # noqa: N802
            self, order_id: int, contract: object, order: object, order_state: object
        ) -> None:
            self.router.publish(
                self._open_orders_target(),
                "open_order",
                (order_id, contract, order, order_state),
            )

        def openOrderEnd(self) -> None:  # noqa: N802
            self.router.publish(self._open_orders_target(), "open_order_end", None)

        # -- open-order snapshot generations ----------------------------------
        def begin_open_orders_snapshot(self) -> str:
            """Open a fresh open-order snapshot generation; return its stream.

            Exactly one snapshot is active at a time on this one-request-at-a-
            time transport. Each gets a DIFFERENT stream identity, so a stale
            ``openOrderEnd`` can never terminate a later snapshot.
            """
            self._open_orders_generation += 1
            stream = f"{OPEN_ORDERS_STREAM}:{self._open_orders_generation}"
            self._active_open_orders_stream = stream
            return stream

        def end_open_orders_snapshot(self, stream: str) -> None:
            """Close the generation and retire its stream (deterministic)."""
            if self._active_open_orders_stream == stream:
                self._active_open_orders_stream = None
            self.router.release(stream)

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
            # Capture a P&L rejection ONLY when this error classified to the
            # ACTIVE P&L request stream. Order ids and request ids share one
            # numeric error space and ORDER attribution wins in
            # classify_error_stream -- so an order error whose orderId happens
            # to equal the P&L reqId routes to the order stream here and can
            # never be mistaken for a P&L rejection. A genuine P&L error (a
            # request id that is not an order id) classifies to this stream and
            # is captured, code only. Informational farm notices are not errors.
            if (
                self._pnl_active_req_id is not None
                and stream == request_stream(self._pnl_active_req_id)
                and not is_informational_ibkr_error(normalized.code)
            ):
                self.record_session_pnl_error(
                    self._pnl_active_req_id, normalized.code
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
            self._session_pnl_timeout = session_pnl_timeout_seconds
            self._session_pnl_freshness = session_pnl_freshness_seconds
            # The persistent subscription's id and the account it is bound to,
            # for the life of THIS transport (i.e. this connection). None until
            # the first session_pnl call starts it; a reconnect is a new
            # transport and therefore a new subscription with a new id.
            self._pnl_req_id: int | None = None
            self._pnl_account: str | None = None
            # Serialises subscription creation and teardown: concurrent first
            # callers must not each start a reqPnL, and disconnect must not race
            # an in-progress init and leave an orphan broker subscription.
            self._pnl_init_lock = threading.Lock()
            # ONE account-summary observation, shared by the gates of a single
            # decision. reqAccountSummary is a subscription whose cancel is
            # asynchronous, so issuing one per gate raced the previous cancel;
            # the lock also means two threads can never have overlapping
            # account-summary subscriptions on this connection.
            self._summary_lock = threading.Lock()
            self._summary_freshness = account_summary_freshness_seconds
            self._summary_values: dict[str, Decimal] | None = None
            self._summary_account: str | None = None
            self._summary_observed_monotonic: float | None = None
            # Serialises the ENTIRE open-order snapshot lifecycle so two
            # snapshots can never be ACTIVE at once (their id-less callbacks
            # would otherwise cross generations). One-request-at-a-time.
            self._open_orders_lock = threading.Lock()
            # Open-order snapshot lifecycle latch. reqAllOpenOrders /
            # reqOpenOrders carry NO request id, so once a snapshot does NOT
            # reach a provable normal end (timeout, sync failure), we cannot
            # tell a later late callback from a current one. We therefore refuse
            # further snapshots until a reconnect (a new transport) provides a
            # clean lifecycle.
            self._open_orders_uncertain = False
            # Single-use guard: this transport is one broker connection.
            self._started = False

        def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
            # A transport is SINGLE-USE: one broker connection per object. A
            # reconnect builds a NEW transport (IbkrPaperAdapter.connect calls
            # the factory), which is what guarantees no stale P&L cache, event,
            # error, account, open-order generation, UNCERTAIN latch, issued-id
            # set or router state can carry into a logically new connection.
            # Refusing a second connect enforces that assumption rather than
            # relying on convention.
            if self._started:
                raise BrokerPortError(
                    BrokerErrorCode.BROKER_UNHEALTHY,
                    "this transport has already been connected; a reconnect must"
                    " use a new transport",
                )
            self._started = True
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
            # Cancel the persistent P&L subscription exactly once, best effort,
            # and detach the cache so no late callback updates a dead connection.
            # Under the init lock, so it cannot race an in-progress init and
            # leave an orphan subscription.
            with self._pnl_init_lock:
                if self._pnl_req_id is not None:
                    try:
                        self._app.cancelPnL(self._pnl_req_id)
                    except Exception:  # noqa: BLE001 - best-effort unsubscribe
                        pass
                    self._app.stop_session_pnl()
                    self._pnl_req_id = None
                    self._pnl_account = None
            # Detach the shared account observation too: a value observed on
            # THIS connection must never be served after it is gone.
            with self._summary_lock:
                self._summary_values = None
                self._summary_account = None
                self._summary_observed_monotonic = None
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
            self._req_id = next(_GLOBAL_REQUEST_ID)
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
            return _resolve_one_contract(details, query, contract)

        def account_summary(self, account_id: str) -> Mapping[str, Decimal]:
            """ONE account observation per decision, not one per gate.

            ``reqAccountSummary`` is a SUBSCRIPTION and ``cancelAccountSummary``
            is asynchronous and unacknowledged. A single execution decision
            observes the account six times (evidence, risk, readiness, and
            readiness again at the submission boundary), so issuing a fresh
            subscribe/cancel cycle per gate meant six cycles for the same group
            within a second, each racing the previous cancel. A gateway may
            legitimately refuse such a duplicate, and on the owner host that
            refusal reached readiness as a bare "unknown environment" block.

            So one observation is shared for a bounded window and a genuinely
            later read still issues a genuinely fresh request. The whole
            request/serve path is under one lock, so two threads can never hold
            overlapping account-summary subscriptions on this connection either.
            """
            with self._summary_lock:
                cached = self._summary_values
                observed = self._summary_observed_monotonic
                if (
                    cached is not None
                    and observed is not None
                    and self._summary_account == account_id
                    and (_monotonic() - observed) < self._summary_freshness
                ):
                    return dict(cached)
                values = self._request_account_summary(account_id)
                # Committed only on success: a failed observation must never
                # leave a half-built one behind for the next gate to reuse.
                self._summary_values = values
                self._summary_account = account_id
                self._summary_observed_monotonic = _monotonic()
                return dict(values)

        def _request_account_summary(self, account_id: str) -> dict[str, Decimal]:
            """One real reqAccountSummary round trip. Always unsubscribes.

            Cancelled in a finally, so a ``_collect`` timeout or error cannot
            orphan the subscription at the gateway.
            """
            req_id = self._next_request_id()
            stream = request_stream(req_id)
            try:
                self._app.reqAccountSummary(
                    req_id, "All", "NetLiquidation,TotalCashValue,BuyingPower"
                )
                rows = self._collect(
                    stream, end_kind="account_end", want="account", what="account summary"
                )
            finally:
                try:
                    self._app.cancelAccountSummary(req_id)
                except Exception:  # noqa: BLE001 - best-effort unsubscribe
                    pass
                self._app.router.release(stream)
            values: dict[str, Decimal] = {}
            for row in rows:
                account, tag, value = cast("tuple[str, str, str]", row)
                if account == account_id:
                    values[tag] = Decimal(value)
            return values

        def _ensure_session_pnl_subscription(
            self, account_id: str
        ) -> SessionPnlObservation | None:
            """Start the persistent subscription ONCE. Returns an error
            observation if it cannot be started, else None.

            A P&L observation is never shared across accounts: if a caller asks
            for a different account than the one this connection subscribed to,
            fail closed rather than serve another account's number.
            """
            # Fast path: an established subscription needs no lock. A concurrent
            # miss here simply enters the lock and re-checks -- double-checked
            # locking, so exactly one reqPnL is ever created per connection.
            if self._pnl_req_id is not None and self._pnl_account == account_id:
                return None
            with self._pnl_init_lock:
                if self._pnl_account is not None and self._pnl_account != account_id:
                    return SessionPnlObservation(
                        SessionPnlStatus.REQUEST_ERROR,
                        detail="session P&L subscription is bound to another account",
                    )
                if self._pnl_req_id is not None:
                    return None  # another thread won the race; share it
                req_id = self._next_request_id()
                self._app.start_session_pnl(req_id)
                try:
                    self._app.reqPnL(req_id, account_id, "")
                except Exception as error:  # noqa: BLE001 - sync failure
                    # A synchronous reqPnL failure does NOT prove the request
                    # never reached the gateway, so cleanup must assume it MAY
                    # have: cancel best-effort before detaching. Local rollback
                    # alone (stop_session_pnl) would leave a live broker
                    # subscription that disconnect can never cancel, because
                    # _pnl_req_id was never committed. Same ownership rule as
                    # positions/market-data cleanup.
                    try:
                        self._app.cancelPnL(req_id)
                    except Exception:  # noqa: BLE001 - best-effort
                        pass
                    self._app.stop_session_pnl()
                    return SessionPnlObservation(
                        SessionPnlStatus.REQUEST_ERROR,
                        detail=f"reqPnL error ({type(error).__name__})",
                    )
                # Assign the account BEFORE the id: a lock-free fast-path reader
                # that sees a committed _pnl_req_id then also sees its account,
                # never a torn (id-set, account-still-None) pair.
                self._pnl_account = account_id
                self._pnl_req_id = req_id
                return None

        def session_pnl(self, account_id: str) -> SessionPnlObservation:
            """Daily P&L from the connection's PERSISTENT ``reqPnL`` subscription.

            reqPnL is a subscription that emits ~1/s. One is started per
            connection and stays active; every ``get_account_snapshot`` consumes
            the LATEST cached callback rather than subscribing and cancelling
            again (the previous one-shot shape blocked each decision for the full
            timeout, and the intermittent owner-host result was consistent with
            that). The status distinguishes:

            * OK -- a finite daily value observed within the freshness window;
            * STALE -- a finite value, but its callback is older than the window
              (the subscription has gone quiet); fails closed;
            * TIMEOUT -- no callback has EVER arrived yet (not a permission fact);
            * REQUEST_ERROR -- a request-scoped broker rejection (code only);
            * UNSET -- a NaN or DBL_MAX (unset) callback.

            Only OK carries a value; everything else is UNKNOWN, never zero. The
            subscription is NOT cancelled here -- a later callback can make a
            later snapshot OK -- and there is no busy-poll and no retry loop.
            """
            started = self._ensure_session_pnl_subscription(account_id)
            if started is not None:
                return started
            # Wait, bounded, for the FIRST callback if none has arrived yet. Once
            # one has, this returns immediately -- no per-call subscribe/cancel.
            if not self._app._pnl_first_update.is_set():
                self._app._pnl_first_update.wait(self._session_pnl_timeout)
            return self._classify_session_pnl()

        def _classify_session_pnl(self) -> SessionPnlObservation:
            _active, daily, observed_monotonic, observed_at, error_code = (
                self._app.session_pnl_state()
            )
            if error_code is not None:
                return SessionPnlObservation(
                    SessionPnlStatus.REQUEST_ERROR,
                    detail=f"reqPnL rejected (IBKR {error_code})",
                    observed_at=observed_at,
                )
            if daily is None or observed_monotonic is None:
                return SessionPnlObservation(
                    SessionPnlStatus.TIMEOUT,
                    detail=f"no reqPnL update within {self._session_pnl_timeout:g}s",
                )
            try:
                value = Decimal(str(daily))
            except (ArithmeticError, ValueError):
                return SessionPnlObservation(
                    SessionPnlStatus.UNSET,
                    detail="reqPnL daily value not a number",
                    observed_at=observed_at,
                )
            if not value.is_finite():
                return SessionPnlObservation(
                    SessionPnlStatus.UNSET,
                    detail="reqPnL daily value not a number",
                    observed_at=observed_at,
                )
            if abs(value) >= _UNSET_DOUBLE_THRESHOLD:
                # IBKR renders an UNSET double field as DBL_MAX (~1.7977e308),
                # which corresponds to an empty TWS cell -- NOT a real value.
                # DBL_MAX is FINITE, so the is_finite check above passes it, and
                # calling it P&L would hand the risk engine a vast positive
                # number and make an UNKNOWN account look wildly profitable. It
                # is the same sentinel _coerce_decimal already rejects for
                # prices; here it becomes UNSET, never OK.
                return SessionPnlObservation(
                    SessionPnlStatus.UNSET,
                    detail="reqPnL daily value unset (DBL_MAX)",
                    observed_at=observed_at,
                )
            age = _monotonic() - observed_monotonic
            if age > self._session_pnl_freshness:
                # A finite value WAS observed, but the subscription has gone
                # quiet. A stale value is not a current one, so it fails closed.
                return SessionPnlObservation(
                    SessionPnlStatus.STALE,
                    detail=(
                        f"reqPnL last update {age:.0f}s ago"
                        f" (> {self._session_pnl_freshness:g}s)"
                    ),
                    observed_at=observed_at,
                )
            return SessionPnlObservation(
                SessionPnlStatus.OK, value=value, observed_at=observed_at
            )

        def positions(self, account_id: str) -> tuple[BrokerPosition, ...]:
            # reqPositionsMulti is REQUEST-SCOPED: a fresh id per observation, so
            # each read routes to its own request_stream(req_id) -- a unique
            # lifecycle key the router retires cleanly. The old global
            # reqPositions used the fixed "positions" channel, which round 9
            # retired after the FIRST read, so a SECOND read (independent
            # portfolio evidence, then canonical evaluation) had its callbacks
            # archived and timed out. Cleanup is ONE finally: a synchronous
            # reqPositionsMulti failure does not prove nothing reached the
            # gateway, so cancel and retire regardless.
            req_id = self._next_request_id()
            stream = request_stream(req_id)
            try:
                self._app.reqPositionsMulti(req_id, account_id, "")
                rows = self._collect(
                    stream, end_kind="position_end", want="position", what="positions"
                )
            finally:
                try:
                    self._app.cancelPositionsMulti(req_id)
                except Exception:  # noqa: BLE001 - best-effort unsubscribe
                    pass
                self._app.router.release(stream)
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
            # THE resolved security, by its broker id -- not a fresh
            # STK/SMART/USD contract rebuilt from the ticker. Rebuilding it
            # meant the security that was resolved, quoted, risked and
            # authorized was not provably the security that was sent: IBKR
            # re-resolved the string itself, at submission time, with no one
            # comparing the two answers.
            con_id = decode_instrument_ref(intent.broker_instrument_ref)
            if con_id is None:
                raise BrokerPortError(
                    BrokerErrorCode.VALIDATION_FAILED,
                    "the order carries no usable broker instrument reference;"
                    " refusing to submit a contract rebuilt from a ticker",
                )
            contract = Contract()
            contract.conId = con_id
            contract.symbol = intent.symbol
            contract.secType = "STK"
            # SMART routes the ORDER; conId identifies the SECURITY, and with a
            # conId present IBKR does not have to guess which listing this is.
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

        def submit_bracket(
            self,
            *,
            parent_order_id: int,
            stop_order_id: int,
            account_id: str,
            intent: OrderIntent,
        ) -> BracketSubmission:
            # A real IBKR native bracket: the parent entry with transmit=False,
            # and the protective STOP child attached by parentId in an OCA group
            # with transmit=True. Only the last leg transmits, so IBKR receives
            # and activates the WHOLE structure at once -- the entry is never
            # live at the broker without its stop attached.
            bracket = intent.protective
            if bracket is None:  # pragma: no cover - adapter guards this first
                raise BrokerPortError(
                    BrokerErrorCode.VALIDATION_FAILED,
                    "submit_bracket requires a protective bracket on the intent",
                    correlation_id=intent.correlation_id,
                )
            con_id = decode_instrument_ref(intent.broker_instrument_ref)
            if con_id is None:
                raise BrokerPortError(
                    BrokerErrorCode.VALIDATION_FAILED,
                    "the order carries no usable broker instrument reference;"
                    " refusing to submit a contract rebuilt from a ticker",
                    correlation_id=intent.correlation_id,
                )
            contract = Contract()
            contract.conId = con_id
            contract.symbol = intent.symbol
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"
            oca_group = f"oca:{intent.client_order_id}"
            # Parent entry -- same translation as a plain order, but held
            # (transmit=False) until the protective child is attached.
            parent_fields = build_outgoing_order(intent, account_id)
            parent = Order()
            parent.account = parent_fields.account
            parent.action = parent_fields.action
            parent.orderType = parent_fields.order_type
            parent.tif = parent_fields.tif
            parent.totalQuantity = parent_fields.total_quantity
            if parent_fields.limit_price is not None:
                parent.lmtPrice = parent_fields.limit_price
            if parent_fields.aux_price is not None:
                parent.auxPrice = parent_fields.aux_price
            parent.outsideRth = parent_fields.outside_rth
            parent.eTradeOnly = parent_fields.e_trade_only
            parent.firmQuoteOnly = parent_fields.firm_quote_only
            parent.transmit = False
            parent.orderRef = parent_fields.order_ref
            # Protective STOP child: opposite side, attached to the parent,
            # one-cancels-all with any sibling, transmitted last.
            stop_side = OrderSide.SELL if intent.side is OrderSide.BUY else OrderSide.BUY
            child = Order()
            child.account = account_id
            child.action = stop_side.value
            child.orderType = ibkr_order_type(OrderType.STOP)
            child.tif = ibkr_time_in_force(intent.time_in_force)
            child.totalQuantity = int(intent.quantity)
            child.auxPrice = float(bracket.stop_price)
            child.parentId = parent_order_id
            child.ocaGroup = oca_group
            child.ocaType = 1
            child.outsideRth = False
            child.eTradeOnly = False
            child.firmQuoteOnly = False
            child.transmit = True
            child.orderRef = encode_order_ref(bracket.stop_client_order_id)
            self._app.issue_order_id(parent_order_id)
            self._app.issue_order_id(stop_order_id)
            self._app.placeOrder(parent_order_id, contract, parent)
            self._app.placeOrder(stop_order_id, contract, child)
            parent_status = self._await_status(parent_order_id)
            stop_status = self._await_status(stop_order_id)
            return BracketSubmission(
                parent_broker_order_id=parent_order_id,
                parent_status=parent_status,
                stop_broker_order_id=stop_order_id,
                stop_status=stop_status,
            )

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
                    # The openOrder callback carries the Contract, so when IBKR
                    # reports a usable conId the recovered order can name the
                    # security it actually is. Empty when it does not -- absent
                    # is absent, and a working order whose security cannot be
                    # proven is reconciled rather than claimed.
                    broker_instrument_ref=encode_instrument_ref(
                        getattr(contract, "conId", None)
                    ),
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
            #
            # Each snapshot gets a FRESH generation stream, so a second snapshot
            # is never starved by the first's retirement (the same reusable-
            # channel defect the positions path had). Cleanup is one finally.
            rows = self._open_orders_snapshot(
                self._app.reqAllOpenOrders, what="open orders"
            )
            return self._open_order_rows(account_id, rows)

        def _open_orders_snapshot(
            self, request: Callable[[], None], *, what: str
        ) -> list[object]:
            """Run ONE fail-closed open-order snapshot.

            State machine: CLEAN -> ACTIVE -> (normal end) SUCCESS -> CLEAN, or
            (timeout / synchronous failure / any incomplete lifecycle) ->
            UNCERTAIN. In UNCERTAIN, no further snapshot is issued on this
            transport until a reconnect, because reqAllOpenOrders/reqOpenOrders
            carry no request id and a late callback from the incomplete snapshot
            could otherwise be mistaken for a current one.
            """
            # The WHOLE lifecycle -- UNCERTAIN check, begin generation, request,
            # collect, end/release, transition -- is serialised, so two
            # snapshots can never be ACTIVE at once. Their callbacks carry no
            # request id, so a second concurrent generation would let snapshot
            # A's openOrder/openOrderEnd be routed into snapshot B. A concurrent
            # second caller waits behind the first (one-request-at-a-time), then
            # runs -- or, if the first left the lifecycle UNCERTAIN, fails closed
            # here before issuing any broker request.
            with self._open_orders_lock:
                if self._open_orders_uncertain:
                    raise BrokerPortError(
                        BrokerErrorCode.BROKER_UNHEALTHY,
                        "open-order snapshot lifecycle is UNCERTAIN after an"
                        " incomplete snapshot; reconnect before another",
                    )
                stream = self._app.begin_open_orders_snapshot()
                completed = False
                try:
                    request()
                    rows = self._collect(
                        stream, end_kind="open_order_end", want="open_order", what=what
                    )
                    completed = True
                    return rows
                finally:
                    self._app.end_open_orders_snapshot(stream)
                    if not completed:
                        # Did NOT reach a provable normal end -> fail closed.
                        self._open_orders_uncertain = True

        def same_client_open_orders(self, account_id: str) -> tuple[RawOpenOrder, ...]:
            # Read-only SAME-CLIENT feed: reqOpenOrders returns the orders bound
            # to this API client id, which is the only basis on which a
            # recovered order may become cancellable. Rows are additionally
            # filtered on the reported clientId (belt and braces).
            rows = self._open_orders_snapshot(
                self._app.reqOpenOrders, what="same-client open orders"
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
    "BracketSubmission",
    "CANCEL_CONFIRMED_ERROR_CODES",
    "WORKING_OR_BETTER_STATES",
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
    "SESSION_PNL_FRESHNESS_SECONDS",
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
    "decode_instrument_ref",
    "encode_instrument_ref",
    "encode_order_ref",
    "format_ibkr_error",
    "ibkr_order_type",
    "ibkr_time_in_force",
    "is_informational_ibkr_error",
    "mask_account_id",
    "resolve_cancel",
    "utc_now",
]
