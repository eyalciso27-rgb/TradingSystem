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

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    ReconciliationResult,
)
from trading_contracts.enums import Environment, OrderState, OrderType

_T = TypeVar("_T")

ADAPTER_VERSION = "ibkr-paper-adapter-1.0.0"

ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
LIVE_API_PORT = 4001
PAPER_ACCOUNT_PREFIX = "DU"
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

#: Order states in which an order is genuinely still working at the broker.
_WORKING_ORDER_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_PENDING,
    }
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


@dataclass(frozen=True, slots=True)
class RawOpenOrder:
    """One order from a read-only ``reqAllOpenOrders`` snapshot (never mutated)."""

    broker_order_id: int
    order_ref: str
    status: str
    symbol: str


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

    def positions(self, account_id: str) -> tuple[BrokerPosition, ...]: ...

    def open_order_snapshot(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        """Read-only snapshot of live open orders (``reqAllOpenOrders``).

        Must never send, cancel, replace or modify an order. Blocks until the
        canonical end-of-snapshot callback, then returns every open order with
        its ``orderRef`` so the adapter can recover lifecycle identity after a
        restart with no process-local state.
        """
        ...

    def next_order_id(self) -> int: ...

    def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
        """Send the order; return the broker's status string."""
        ...

    def cancel(self, broker_order_id: int) -> str:
        """Cancel by broker order id; return the broker's status string."""
        ...


def _deterministic_clock(start: datetime) -> Callable[[], datetime]:
    if start.tzinfo is None:
        raise ValueError("clock start must be timezone-aware")
    state = {"ticks": 0}

    def now() -> datetime:
        state["ticks"] += 1
        return start + timedelta(seconds=state["ticks"])

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
        self._now = clock or _deterministic_clock(datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
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
        if self._transport is not None:
            try:
                self._transport.disconnect()
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
                observed_at=self._now(),
            )
        except KeyError as missing:
            raise BrokerPortError(
                BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                f"account summary missing tag {missing}; failing closed",
            ) from missing

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        transport = self._require_connected()
        account_id = self._account_id(account_alias)
        return self._guarded(lambda: transport.positions(account_id))

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        """Recover open orders from the broker, not from process-local memory.

        Queries a read-only ``reqAllOpenOrders`` snapshot and normalizes it into
        ``BrokerOrderReceipt``s, recovering each order's canonical identity from
        its ``orderRef``. This is what makes reconnect restart-safe: a fresh
        adapter with empty memory still sees the resting protective stop.

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
        connected = self._transport is not None and self._transport.is_connected()
        return IntegrationHealth(
            healthy=connected,
            detail="ibkr paper gateway connected" if connected else "not connected",
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


def _build_ibapi_paper_transport(  # pragma: no cover - host-only, gated by POI-IBKR-001
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> PaperTransport:
    """Build the real SDK-backed paper transport (gateway host only).

    ``ibapi`` is imported here, lazily, keeping the SDK confined to this
    package. Actually running this against the paper gateway is the
    separately-approved POI-IBKR-001 experiment — never CI.
    """
    import queue
    import threading

    try:
        from ibapi.client import EClient  # type: ignore[import-not-found]
        from ibapi.contract import Contract  # type: ignore[import-not-found]
        from ibapi.order import Order  # type: ignore[import-not-found]
        from ibapi.wrapper import EWrapper  # type: ignore[import-not-found]
    except ImportError as error:
        raise BrokerPortError(
            BrokerErrorCode.BROKER_UNHEALTHY,
            "ibapi is not installed on this host; install it inside the broker-ibkr"
            " environment before the paper PoI",
        ) from error

    class _PaperApp(EWrapper, EClient):  # type: ignore[misc]
        def __init__(self) -> None:
            EClient.__init__(self, self)
            self.managed: tuple[str, ...] = ()
            self.ready = threading.Event()
            self.next_id: int = 0
            self.responses: queue.Queue[tuple[str, object]] = queue.Queue()

        def managedAccounts(self, accounts: str) -> None:  # noqa: N802
            self.managed = tuple(a for a in accounts.split(",") if a)

        def nextValidId(self, order_id: int) -> None:  # noqa: N802
            self.next_id = order_id
            self.ready.set()

        def orderStatus(  # noqa: N802
            self, order_id: int, status: str, *args: object, **kwargs: object
        ) -> None:
            self.responses.put(("status", (order_id, status)))

        def contractDetails(self, req_id: int, details: object) -> None:  # noqa: N802
            self.responses.put(("contract", details))

        def contractDetailsEnd(self, req_id: int) -> None:  # noqa: N802
            self.responses.put(("contract_end", req_id))

        def accountSummary(  # noqa: N802
            self, req_id: int, account: str, tag: str, value: str, currency: str
        ) -> None:
            self.responses.put(("account", (account, tag, value)))

        def accountSummaryEnd(self, req_id: int) -> None:  # noqa: N802
            self.responses.put(("account_end", req_id))

        def position(  # noqa: N802
            self, account: str, contract: object, position: object, avg_cost: float
        ) -> None:
            self.responses.put(("position", (account, contract, position, avg_cost)))

        def positionEnd(self) -> None:  # noqa: N802
            self.responses.put(("position_end", None))

        def openOrder(  # noqa: N802
            self, order_id: int, contract: object, order: object, order_state: object
        ) -> None:
            self.responses.put(("open_order", (order_id, contract, order, order_state)))

        def openOrderEnd(self) -> None:  # noqa: N802
            self.responses.put(("open_order_end", None))

    class _IbApiPaperTransport:
        """Blocking, single-threaded-request transport over the SDK."""

        def __init__(self) -> None:
            self._app = _PaperApp()
            self._thread: threading.Thread | None = None
            self._req_id = 100

        def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
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
                if self._thread is not None:
                    self._thread.join(timeout=5)

        def is_connected(self) -> bool:
            return bool(self._app.isConnected())

        def _drain(self, end_kind: str, want: str) -> list[object]:
            collected: list[object] = []
            while True:
                try:
                    kind, payload = self._app.responses.get(timeout=timeout_seconds)
                except queue.Empty as error:
                    raise BrokerPortError(
                        BrokerErrorCode.BROKER_REJECTED, f"timed out waiting for {want}"
                    ) from error
                if kind == end_kind:
                    return collected
                if kind == want:
                    collected.append(payload)

        def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument | None:
            contract = Contract()
            contract.symbol = query.symbol
            contract.secType = "STK"
            contract.exchange = query.exchange or "SMART"
            contract.currency = query.currency or "USD"
            self._req_id += 1
            self._app.reqContractDetails(self._req_id, contract)
            details = self._drain("contract_end", "contract")
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
            self._req_id += 1
            self._app.reqAccountSummary(
                self._req_id, "All", "NetLiquidation,TotalCashValue,BuyingPower"
            )
            rows = self._drain("account_end", "account")
            self._app.cancelAccountSummary(self._req_id)
            values: dict[str, Decimal] = {}
            for row in rows:
                account, tag, value = cast("tuple[str, str, str]", row)
                if account == account_id:
                    values[tag] = Decimal(value)
            return values

        def positions(self, account_id: str) -> tuple[BrokerPosition, ...]:
            self._app.reqPositions()
            rows = self._drain("position_end", "position")
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
            return order_id

        def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
            contract = Contract()
            contract.symbol = intent.symbol
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"
            order = Order()
            order.account = account_id
            order.action = intent.side.value
            order.orderType = {
                OrderType.LIMIT: "LMT",
                OrderType.STOP: "STP",
                OrderType.STOP_LIMIT: "STP LMT",
                OrderType.MARKET: "MKT",
            }[intent.order_type]
            order.totalQuantity = int(intent.quantity)
            if intent.limit_price is not None:
                order.lmtPrice = float(intent.limit_price)
            if intent.stop_price is not None:
                order.auxPrice = float(intent.stop_price)
            order.outsideRth = False
            order.eTradeOnly = False
            order.firmQuoteOnly = False
            # Persist the canonical (non-authorizing) client_order_id so a
            # restart can recover this order's identity from the Gateway.
            order.orderRef = encode_order_ref(intent.client_order_id)
            self._app.placeOrder(broker_order_id, contract, order)
            return self._await_status(broker_order_id)

        def open_order_snapshot(self, account_id: str) -> tuple[RawOpenOrder, ...]:
            # Read-only: reqAllOpenOrders never sends/cancels/modifies an order.
            self._app.reqAllOpenOrders()
            rows = self._drain("open_order_end", "open_order")
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
                )
            return tuple(snapshot.values())

        def cancel(self, broker_order_id: int) -> str:
            self._app.cancelOrder(broker_order_id, "")
            return self._await_status(broker_order_id)

        def _await_status(self, broker_order_id: int) -> str:
            while True:
                try:
                    kind, payload = self._app.responses.get(timeout=timeout_seconds)
                except queue.Empty as error:
                    raise BrokerPortError(
                        BrokerErrorCode.UNKNOWN_BROKER_RESPONSE,
                        f"no status for order {broker_order_id}; reconcile required",
                    ) from error
                if kind == "status":
                    order_id, status = cast("tuple[int, str]", payload)
                    if order_id == broker_order_id:
                        return status

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
    "IBKR_PAPER_CAPABILITIES",
    "IBKR_STATUS_MAP",
    "LIFECYCLE_ORDER_REF_PREFIX",
    "LIVE_API_PORT",
    "ORDER_REF_MAX_LEN",
    "PAPER_ACCOUNT_PREFIX",
    "PAPER_API_PORT",
    "IbkrPaperAdapter",
    "OrderRefTooLongError",
    "PaperTransport",
    "RawOpenOrder",
    "RecoveredIdentity",
    "build_paper_adapter",
    "decode_order_ref",
    "encode_order_ref",
    "mask_account_id",
]
