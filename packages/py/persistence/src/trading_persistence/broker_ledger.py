"""Durable broker-observation ledger (autonomous-paper-runtime Phase A/A5).

The runtime's consumer boundary for raw broker callbacks. Three append verbs
land an order-status update, an execution/fill or a position observation into
``broker_observations``; the reads reconstruct what the broker last told us so a
restart does not lose exposure knowledge. Everything here is broker-neutral: the
IBKR adapter maps its own order id, execution id and contract id onto the
``external_order_ref`` / ``external_execution_ref`` / ``instrument_ref`` this
module stores.

Idempotency is the point and it is structural, not conventional. Every append
computes a ``dedup_key`` from the STABLE identity of the observation and inserts
with ``ON CONFLICT (environment, dedup_key) DO NOTHING``:

* a fill is keyed on its execution ref -- a broker that redelivers an execId, or
  two callbacks carrying the same one, land exactly one fill;
* an order-status update is keyed on ``(order ref, state, filled quantity, price)``
  -- an exact duplicate callback is ignored while a genuine progression (a new
  state or a larger filled quantity) lands a new row;
* a position observation is keyed on ``(instrument ref, quantity, price)`` -- a
  periodic snapshot that has not changed lands nothing, so the ledger records
  every change without growing on every poll.

Each append returns whether a NEW row was written (``True``) or the observation
was a duplicate (``False``), so a caller can tell an economic event from a
redelivery without a second read.

Reads never invent a zero. ``open_positions`` reconstructs exposure only from
position observations that exist; an instrument never observed is simply absent,
so an unknown position stays unknown and is never reported as flat.

The projector that folds these durable observations into the normalized
``broker_orders`` / ``fills`` / ``positions`` graph is deferred to the runtime's
Phase B, when the submission pipeline that creates those parent rows lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from trading_contracts.enums import Environment, OrderState

from trading_persistence.models import BrokerObservation

ORDER_STATUS = "ORDER_STATUS"
FILL = "FILL"
POSITION = "POSITION"

#: Canonical order sides accepted by the ledger. Kept as literals rather than
#: importing ``trading_broker_core.OrderSide`` so persistence stays free of a
#: broker dependency; the runtime maps its OrderSide onto these before landing.
_ALLOWED_SIDES = ("BUY", "SELL")


def _side(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in _ALLOWED_SIDES:
        raise ValueError(f"side must be one of {_ALLOWED_SIDES}, not {value!r}")
    return value


def _q(value: Decimal | None) -> str:
    """A stable textual form of a quantity/price for a dedup key.

    ``str(Decimal)`` is used deliberately: ``Decimal('1.0')`` and
    ``Decimal('1.00')`` are different keys because the broker sent different
    text, and normalising them would merge two callbacks the broker kept
    distinct. ``None`` is a fixed sentinel so a missing field never collides
    with a present one.
    """
    return "-" if value is None else str(value)


@dataclass(frozen=True, slots=True)
class OrderStatusObservation:
    """One order-status update as read back from the durable ledger."""

    external_order_ref: str
    order_state: OrderState
    filled_quantity: Decimal | None
    remaining_quantity: Decimal | None
    last_fill_quantity: Decimal | None
    price: Decimal | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FillObservation:
    """One execution/fill as read back from the durable ledger."""

    external_order_ref: str | None
    external_execution_ref: str
    quantity: Decimal | None
    price: Decimal | None
    commission: Decimal | None
    currency: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PositionObservation:
    """The latest observed position for one instrument (never an invented zero)."""

    instrument_ref: str
    symbol: str | None
    position_quantity: Decimal
    average_price: Decimal | None
    observed_at: datetime


class BrokerObservationLedger:
    """Idempotent append + reconstruct reads over ``broker_observations``.

    Holds no connection: every method takes the caller's ``Connection`` so the
    append lands in the caller's transaction, exactly like the rest of the
    persistence layer (``record_emergency_command``, ``append_audit_record``).
    """

    def __init__(self, environment: Environment) -> None:
        self._environment = environment

    # -- appends (idempotent) ------------------------------------------------

    def record_order_status(
        self,
        connection: Connection,
        *,
        external_order_ref: str,
        order_state: OrderState,
        account_alias: str,
        observed_at: datetime,
        filled_quantity: Decimal | None = None,
        remaining_quantity: Decimal | None = None,
        last_fill_quantity: Decimal | None = None,
        price: Decimal | None = None,
        symbol: str | None = None,
        side: str | None = None,
        instrument_ref: str | None = None,
        correlation_ref: str | None = None,
        raw_payload_ref: str | None = None,
    ) -> bool:
        """Land one order-status update. Returns True iff a new row was written.

        Keyed on ``(order ref, state, filled quantity, price)`` so an exact
        duplicate callback is idempotent while a real progression is not.
        """
        if not external_order_ref.strip():
            raise ValueError("external_order_ref is mandatory for an order status")
        dedup_key = ":".join(
            (
                ORDER_STATUS,
                external_order_ref,
                order_state.value,
                _q(filled_quantity),
                _q(price),
            )
        )
        return self._append(
            connection,
            observation_kind=ORDER_STATUS,
            dedup_key=dedup_key,
            account_alias=account_alias,
            observed_at=observed_at,
            external_order_ref=external_order_ref,
            order_state=order_state.value,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            last_fill_quantity=last_fill_quantity,
            price=price,
            symbol=symbol,
            side=_side(side),
            instrument_ref=instrument_ref,
            correlation_ref=correlation_ref,
            raw_payload_ref=raw_payload_ref,
        )

    def record_fill(
        self,
        connection: Connection,
        *,
        external_execution_ref: str,
        account_alias: str,
        observed_at: datetime,
        external_order_ref: str | None = None,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        commission: Decimal | None = None,
        currency: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        instrument_ref: str | None = None,
        correlation_ref: str | None = None,
        raw_payload_ref: str | None = None,
    ) -> bool:
        """Land one execution/fill. Returns True iff a new row was written.

        Keyed on the execution ref alone -- a broker execId identifies exactly
        one economic fill, so a redelivered execId is always a duplicate.
        """
        if not external_execution_ref.strip():
            raise ValueError("external_execution_ref is mandatory for a fill")
        dedup_key = ":".join((FILL, external_execution_ref))
        return self._append(
            connection,
            observation_kind=FILL,
            dedup_key=dedup_key,
            account_alias=account_alias,
            observed_at=observed_at,
            external_order_ref=external_order_ref,
            external_execution_ref=external_execution_ref,
            last_fill_quantity=quantity,
            price=price,
            commission=commission,
            currency=currency,
            symbol=symbol,
            side=_side(side),
            instrument_ref=instrument_ref,
            correlation_ref=correlation_ref,
            raw_payload_ref=raw_payload_ref,
        )

    def record_position(
        self,
        connection: Connection,
        *,
        instrument_ref: str,
        position_quantity: Decimal,
        account_alias: str,
        observed_at: datetime,
        average_price: Decimal | None = None,
        symbol: str | None = None,
        raw_payload_ref: str | None = None,
    ) -> bool:
        """Land one position observation. Returns True iff it changed.

        Keyed on ``(instrument ref, quantity, price)`` so a periodic snapshot
        that has not moved lands nothing, and every genuine change is recorded.
        A zero quantity is a real, OBSERVED flat -- distinct from an instrument
        that was never observed, which the reads leave absent.
        """
        if not instrument_ref.strip():
            raise ValueError("instrument_ref is mandatory for a position")
        dedup_key = ":".join(
            (POSITION, instrument_ref, _q(position_quantity), _q(average_price))
        )
        return self._append(
            connection,
            observation_kind=POSITION,
            dedup_key=dedup_key,
            account_alias=account_alias,
            observed_at=observed_at,
            instrument_ref=instrument_ref,
            position_quantity=position_quantity,
            average_price=average_price,
            symbol=symbol,
            raw_payload_ref=raw_payload_ref,
        )

    def _append(
        self,
        connection: Connection,
        *,
        observation_kind: str,
        dedup_key: str,
        account_alias: str,
        observed_at: datetime,
        **fields: object,
    ) -> bool:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        statement = (
            pg_insert(BrokerObservation)
            .values(
                id=uuid4(),
                environment=self._environment.value,
                account_alias=account_alias,
                observation_kind=observation_kind,
                dedup_key=dedup_key,
                observed_at=observed_at,
                created_at=observed_at,
                **fields,
            )
            .on_conflict_do_nothing(
                index_elements=["environment", "dedup_key"]
            )
            .returning(BrokerObservation.id)
        )
        result = connection.execute(statement)
        # PostgreSQL returns one id when the row was inserted and no row when
        # the conflict clause swallowed a duplicate. This is the
        # economic-event/redelivery signal; it avoids relying on driver-specific
        # rowcount behaviour for INSERT .. ON CONFLICT DO NOTHING.
        return result.scalar_one_or_none() is not None

    # -- reconstruct reads ---------------------------------------------------

    def order_status_history(
        self, connection: Connection, external_order_ref: str
    ) -> list[OrderStatusObservation]:
        """Every landed status for one order, oldest first."""
        rows = connection.execute(
            select(
                BrokerObservation.order_state,
                BrokerObservation.filled_quantity,
                BrokerObservation.remaining_quantity,
                BrokerObservation.last_fill_quantity,
                BrokerObservation.price,
                BrokerObservation.observed_at,
            )
            .where(BrokerObservation.environment == self._environment.value)
            .where(BrokerObservation.observation_kind == ORDER_STATUS)
            .where(BrokerObservation.external_order_ref == external_order_ref)
            .order_by(BrokerObservation.observed_at, BrokerObservation.created_at)
        ).all()
        return [
            OrderStatusObservation(
                external_order_ref=external_order_ref,
                order_state=OrderState(state),
                filled_quantity=_dec(filled),
                remaining_quantity=_dec(remaining),
                last_fill_quantity=_dec(last),
                price=_dec(price),
                observed_at=observed_at,
            )
            for state, filled, remaining, last, price, observed_at in rows
        ]

    def latest_order_state(
        self, connection: Connection, external_order_ref: str
    ) -> OrderState | None:
        """The most recently observed state for one order, or None if unknown."""
        history = self.order_status_history(connection, external_order_ref)
        if not history:
            return None
        return history[-1].order_state

    def fill_recorded(
        self, connection: Connection, external_execution_ref: str
    ) -> bool:
        """Whether this execution id has already been landed (idempotency probe)."""
        dedup_key = ":".join((FILL, external_execution_ref))
        row = connection.execute(
            select(BrokerObservation.id)
            .where(BrokerObservation.environment == self._environment.value)
            .where(BrokerObservation.dedup_key == dedup_key)
            .limit(1)
        ).first()
        return row is not None

    def fills_for_order(
        self, connection: Connection, external_order_ref: str
    ) -> list[FillObservation]:
        """Every fill landed for one order, oldest first."""
        rows = connection.execute(
            select(
                BrokerObservation.external_order_ref,
                BrokerObservation.external_execution_ref,
                BrokerObservation.last_fill_quantity,
                BrokerObservation.price,
                BrokerObservation.commission,
                BrokerObservation.currency,
                BrokerObservation.observed_at,
            )
            .where(BrokerObservation.environment == self._environment.value)
            .where(BrokerObservation.observation_kind == FILL)
            .where(BrokerObservation.external_order_ref == external_order_ref)
            .order_by(BrokerObservation.observed_at, BrokerObservation.created_at)
        ).all()
        return [
            FillObservation(
                external_order_ref=order_ref,
                external_execution_ref=exec_ref,
                quantity=_dec(quantity),
                price=_dec(price),
                commission=_dec(commission),
                currency=currency,
                observed_at=observed_at,
            )
            for order_ref, exec_ref, quantity, price, commission, currency, observed_at in rows
        ]

    def open_positions(self, connection: Connection) -> list[PositionObservation]:
        """The latest observation per instrument with a non-zero quantity.

        Reconstructed only from observations that exist. An instrument never
        observed is absent -- never returned as a zero -- so an unknown position
        can never be mistaken for a flat one.
        """
        rows = connection.execute(
            select(
                BrokerObservation.instrument_ref,
                BrokerObservation.symbol,
                BrokerObservation.position_quantity,
                BrokerObservation.average_price,
                BrokerObservation.observed_at,
                BrokerObservation.created_at,
            )
            .where(BrokerObservation.environment == self._environment.value)
            .where(BrokerObservation.observation_kind == POSITION)
            .order_by(BrokerObservation.observed_at, BrokerObservation.created_at)
        ).all()
        # Latest-wins per instrument by observation order; the query is ordered
        # oldest-first, so the last write for each instrument is authoritative.
        latest: dict[str, PositionObservation] = {}
        for instrument_ref, symbol, quantity, price, observed_at, _created in rows:
            if instrument_ref is None:
                continue
            latest[instrument_ref] = PositionObservation(
                instrument_ref=instrument_ref,
                symbol=symbol,
                position_quantity=_dec(quantity) or Decimal(0),
                average_price=_dec(price),
                observed_at=observed_at,
            )
        return [
            observation
            for observation in latest.values()
            if observation.position_quantity != 0
        ]


def _dec(value: object) -> Decimal | None:
    """Coerce a NUMERIC column back to Decimal, preserving None as unknown."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


__all__ = [
    "FILL",
    "ORDER_STATUS",
    "POSITION",
    "BrokerObservationLedger",
    "FillObservation",
    "OrderStatusObservation",
    "PositionObservation",
]
