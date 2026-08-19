"""Deterministic in-memory fake broker for conformance testing (IMP-1001).

This is NOT an adapter to anything: no network, no SDK, no secrets, no real
order and no real broker order id (refs are prefixed ``FAKE-``). Its only
purpose is to exercise the ``BrokerPort`` contract in the conformance suite
and future execution-agent tests (ADR-0014: fixtures/fakes only).

Determinism: the fake takes an injected monotonic clock and derives all ids
from inputs, so the same fake state + the same calls produce the same
receipts and the same append-only event log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_contracts.enums import OrderState, OrderType

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

FAKE_ORDER_REF_PREFIX = "FAKE-"

#: MVP-shaped default matrix (Integration 20.4): plain order types native,
#: bracket/OCO native, trailing manual-only, fractional/outside-RTH disabled.
DEFAULT_CAPABILITIES = BrokerCapabilities(
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


def _deterministic_clock(start: datetime) -> Callable[[], datetime]:
    """A clock that advances one second per call — reproducible timelines."""
    if start.tzinfo is None:
        raise ValueError("clock start must be timezone-aware")
    state = {"ticks": 0}

    def now() -> datetime:
        state["ticks"] += 1
        return start + timedelta(seconds=state["ticks"])

    return now


class FakeBroker:
    """In-memory ``BrokerPort`` implementation backed by fixtures only."""

    def __init__(
        self,
        *,
        accounts: dict[str, AccountSnapshot] | None = None,
        positions: dict[str, tuple[BrokerPosition, ...]] | None = None,
        instruments: dict[str, BrokerInstrument] | None = None,
        capabilities: BrokerCapabilities = DEFAULT_CAPABILITIES,
        clock: Callable[[], datetime] | None = None,
        healthy: bool = True,
    ) -> None:
        self._accounts = dict(accounts or {})
        self._positions = dict(positions or {})
        self._instruments = dict(instruments or {})
        self._capabilities = capabilities
        self._now = clock or _deterministic_clock(datetime(2026, 7, 9, 12, 0, tzinfo=UTC))
        self._healthy = healthy
        self._connected = False
        self._orders: dict[str, BrokerOrderReceipt] = {}
        self._events: list[BrokerOrderEvent] = []

    # -- test-fixture control (not part of the port) ------------------------
    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy

    # -- BrokerPort ----------------------------------------------------------
    def connect(self) -> None:
        if not self._healthy:
            raise BrokerPortError(BrokerErrorCode.BROKER_UNHEALTHY, "fake broker is unhealthy")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument:
        self._require_ready()
        instrument = self._instruments.get(query.symbol)
        if instrument is None:
            raise BrokerPortError(
                BrokerErrorCode.INSTRUMENT_NOT_FOUND, f"unknown instrument {query.symbol!r}"
            )
        return instrument

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        self._require_ready()
        return self._account(account_alias)

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        self._require_ready()
        self._account(account_alias)
        return self._positions.get(account_alias, ())

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        self._require_ready()
        self._account(account_alias)
        open_states = (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED)
        return tuple(
            receipt for receipt in self._orders.values() if receipt.state in open_states
        )

    def recover_cancellable_orders(
        self, account_alias: str
    ) -> tuple[BrokerOrderReceipt, ...]:
        """Same-client recovery. This fake owns everything it can see."""
        return self.get_open_orders(account_alias)

    def preview_order(self, intent: OrderIntent) -> OrderPreview:
        self._require_ready()
        self._validate(intent)
        reference_price = intent.limit_price or intent.stop_price or Decimal(0)
        return OrderPreview(
            client_order_id=intent.client_order_id,
            estimated_notional=intent.quantity * reference_price,
            correlation_id=intent.correlation_id,
        )

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        self._require_ready()
        # Idempotency: a duplicate client_order_id returns the ORIGINAL
        # receipt and creates nothing (validated against original inputs
        # first so a changed duplicate still fails closed).
        self._validate(intent)
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            return existing
        receipt = BrokerOrderReceipt(
            client_order_id=intent.client_order_id,
            broker_order_ref=f"{FAKE_ORDER_REF_PREFIX}{intent.client_order_id}",
            state=OrderState.ACKNOWLEDGED,
            authorization_ref=intent.authorization_ref,
            correlation_id=intent.correlation_id,
            received_at=self._now(),
        )
        self._orders[intent.client_order_id] = receipt
        self._append_event(receipt, Decimal(0))
        return receipt

    def cancel_order(self, request: CancelOrderRequest) -> CancelReceipt:
        self._require_ready()
        receipt = self._orders.get(request.client_order_id)
        if receipt is None:
            raise BrokerPortError(
                BrokerErrorCode.ORDER_NOT_FOUND,
                f"no order {request.client_order_id!r}",
                correlation_id=request.correlation_id,
            )
        cancelled = BrokerOrderReceipt(
            client_order_id=receipt.client_order_id,
            broker_order_ref=receipt.broker_order_ref,
            state=OrderState.CANCELLED,
            authorization_ref=receipt.authorization_ref,
            correlation_id=request.correlation_id,
            received_at=self._now(),
        )
        self._orders[request.client_order_id] = cancelled
        self._append_event(cancelled, Decimal(0))
        return CancelReceipt(
            client_order_id=cancelled.client_order_id,
            broker_order_ref=cancelled.broker_order_ref,
            state=OrderState.CANCELLED,
            correlation_id=request.correlation_id,
            received_at=cancelled.received_at,
        )

    def order_events(self) -> Iterator[BrokerOrderEvent]:
        return iter(tuple(self._events))

    def reconcile(self, account_alias: str) -> ReconciliationResult:
        self._require_connected()
        snapshot = self._account(account_alias)
        return ReconciliationResult(
            account_alias=account_alias,
            snapshot=snapshot,
            positions=self._positions.get(account_alias, ()),
            open_orders=self.get_open_orders(account_alias) if self._healthy else (),
            reconciled_at=self._now(),
        )

    def health_check(self) -> IntegrationHealth:
        return IntegrationHealth(
            healthy=self._healthy and self._connected,
            detail="fake broker" if self._healthy else "fake broker marked unhealthy",
            checked_at=self._now(),
        )

    # -- internals -----------------------------------------------------------
    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerPortError(BrokerErrorCode.NOT_CONNECTED, "connect() first")

    def _require_ready(self) -> None:
        self._require_connected()
        if not self._healthy:
            raise BrokerPortError(BrokerErrorCode.BROKER_UNHEALTHY, "fake broker is unhealthy")

    def _account(self, account_alias: str) -> AccountSnapshot:
        snapshot = self._accounts.get(account_alias)
        if snapshot is None:
            raise BrokerPortError(
                BrokerErrorCode.ACCOUNT_UNKNOWN, f"unknown account {account_alias!r}"
            )
        return snapshot

    def _validate(self, intent: OrderIntent) -> None:
        if not intent.authorization_ref.strip():
            raise BrokerPortError(
                BrokerErrorCode.AUTHORIZATION_MISSING,
                "submit/preview requires a SubmitAuthorization ref",
                correlation_id=intent.correlation_id,
            )
        self._account(intent.account_alias)
        mode = self._capabilities.order_types.get(
            intent.order_type, BrokerCapabilityMode.UNSUPPORTED
        )
        if mode is not BrokerCapabilityMode.NATIVE:
            raise BrokerPortError(
                BrokerErrorCode.CAPABILITY_UNSUPPORTED,
                f"{intent.order_type.value} is {mode.value}; emulation needs explicit policy",
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

    def _append_event(self, receipt: BrokerOrderReceipt, filled: Decimal) -> None:
        self._events.append(
            BrokerOrderEvent(
                sequence=len(self._events) + 1,
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=receipt.state,
                filled_quantity=filled,
                observed_at=receipt.received_at,
            )
        )


__all__ = ["DEFAULT_CAPABILITIES", "FAKE_ORDER_REF_PREFIX", "FakeBroker"]
