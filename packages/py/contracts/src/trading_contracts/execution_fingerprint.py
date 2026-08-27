"""The durable execution-fingerprint field contract. One source of truth.

The producer (the execution service) and the consumer (the durable audit
reader) both have to agree on which fields a lifecycle record must carry. They
used to state that separately -- a tuple in the service and a tuple in the
persistence package -- which is not a contract, it is two copies that happen to
match today. A field added to the service's identity and forgotten in the
reader would mean the reader validated an identity the writer no longer used,
and nothing would notice.

So the field names live here, in the lowest layer both sides already import,
and neither side may restate them.

The split is deliberate and load-bearing:

* :data:`REQUEST_IDENTITY_FIELDS` say WHICH execution this is and what the
  caller asked for. All are known before an order is planned, so they are
  always comparable -- including on a replay that has not planned an order yet.
* :data:`PREPARED_ORDER_FIELDS` describe the FROZEN broker-bound order. They
  exist only once an order has actually been prepared, and a record that claims
  to describe a prepared submission must carry them or a replay cannot tell a
  repeat from a change.
"""

from __future__ import annotations

#: WHICH execution, and what was asked for. Always available.
REQUEST_IDENTITY_FIELDS: tuple[str, ...] = (
    "decision_id",
    "client_order_id",
    "strategy_id",
    "source_id",
    "symbol",
    "side",
    "environment",
    "account_alias",
    "requested_quantity",
    "configured_order_type",
    "configured_time_in_force",
)

#: The frozen order. Present only after an order was prepared.
PREPARED_ORDER_FIELDS: tuple[str, ...] = (
    "order_type",
    "quantity",
    "limit_price",
    "stop_price",
    "time_in_force",
    # WHICH SECURITY. Known only once the contract resolves, so it belongs
    # here rather than with the request identity -- but a replay that agrees on
    # the ticker and disagrees on the security is not a replay, it is a
    # different order wearing the same name.
    "broker_instrument_ref",
)

#: The subset of the frozen order that a prepared or terminal record MUST
#: carry. ``limit_price``/``stop_price`` are excluded because a MARKET order
#: legitimately has neither, and requiring a value the order does not have
#: would make a valid record unreadable.
MANDATORY_PREPARED_FIELDS: tuple[str, ...] = (
    "order_type",
    "quantity",
    "time_in_force",
    "broker_instrument_ref",
)

__all__ = [
    "MANDATORY_PREPARED_FIELDS",
    "PREPARED_ORDER_FIELDS",
    "REQUEST_IDENTITY_FIELDS",
]
