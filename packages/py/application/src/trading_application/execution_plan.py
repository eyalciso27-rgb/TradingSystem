"""The executable plan: the ONE thing an authorization is granted over.

Before this existed, ``FinalPreSubmitCheck`` proved a decision, a risk result,
an allocation, a policy and a quantity -- and then the execution service built
its own order from its own runtime fields, and the execution agent built the
broker's ``OrderIntent`` from a request whose symbol, side, order type, prices
and time in force were separate, independently substitutable values.

So the check could approve one plan while the broker received another. The
merge gate even contained a real instance of it: the authorization was minted
against an entry of 100.02 with a stop of 98.02 while the service computed its
own entry and stop from the live quote, and the order still went out.

An ``ExecutablePlan`` closes that by being the whole order in one frozen,
content-addressed object:

    the thing FinalPreSubmit checked
    == the thing SingleUseAuthorization authorizes
    == the thing ExecutionService prepares
    == the thing ExecutionAgent sends

Every field that could change the order is inside the hash, so there is no
order fact left that one component can vary without the others noticing. There
is nothing to compare "loosely" and no prefix to match: either the plan is bit
for bit the plan that was authorized, or it is a different plan.

The plan is built in exactly ONE place -- :meth:`FinalPreSubmitCheck.check` --
from the frozen request it just finished checking, using the quantity it just
authorized. A caller cannot hand in a plan that differs from what was checked,
because a caller never constructs the sealed plan at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_contracts.enums import Environment, OrderType


@dataclass(frozen=True, slots=True)
class ExecutablePlan:
    """The exact broker-bound order, plus the lineage that authorized it.

    Frozen and hashed as a whole. The lineage refs are inside the plan rather
    than beside it so that "this order" and "the evaluation that permitted this
    order" cannot drift apart: an authorization for a valid order derived from
    someone else's risk evaluation is not an authorization for this execution.
    """

    # -- lineage: which evaluation permitted this ----------------------------
    decision_ref: str
    risk_request_ref: str
    risk_ref: str
    allocation_ref: str

    # -- OUR idempotency key -------------------------------------------------
    #: The canonical id for this execution, carried to IBKR in ``orderRef``.
    #:
    #: Precisely: this is **our** idempotency key, not the broker's. IBKR does
    #: not deduplicate on ``orderRef`` -- two orders sharing one are two orders,
    #: and it will happily hold both. What ``orderRef`` buys us is correlation
    #: and recovery: after a restart we can tell which working order is ours.
    #: The actual duplicate protection is ours to provide, from the durable
    #: execution history plus the in-process claim.
    #:
    #: Which is exactly why it belongs inside the seal. It lived outside until
    #: the fifth review round: the plan fixed the symbol, side, size, type,
    #: prices and account, and a later component was still free to attach a
    #: different id to the same authorized plan -- and because the broker will
    #: not catch that for us, it would simply have become a second live order.
    client_order_id: str

    # -- who is trading, and where -------------------------------------------
    strategy_id: str
    source_id: str
    environment: Environment
    account_alias: str

    # -- the order itself ------------------------------------------------------
    symbol: str
    #: THE security the broker resolved, opaque to this layer. A ticker is not
    #: a security: two share classes, two listings or two currency lines can
    #: agree as strings and differ as instruments, so an order authorized on a
    #: ticker alone is authorized on a guess. Never parsed here.
    broker_instrument_ref: str
    #: What the broker said about that security. Carried so the audit record
    #: says which listing was traded rather than only which id.
    broker_exchange: str
    broker_currency: str
    side: str
    quantity: Decimal
    order_type: OrderType
    time_in_force: str
    limit_price: Decimal | None
    stop_price: Decimal | None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("an executable plan must have a positive quantity")
        if not self.client_order_id.strip():
            raise ValueError("client_order_id is mandatory on an executable plan")
        if not self.broker_instrument_ref.strip():
            raise ValueError(
                "broker_instrument_ref is mandatory on an executable plan: a"
                " plan that names only a ticker has not identified a security"
            )
        for name in ("decision_ref", "risk_request_ref", "risk_ref", "allocation_ref"):
            if not str(getattr(self, name)).strip():
                # A plan whose lineage is blank proves nothing about what
                # authorized it, and an unprovable lineage is not a weaker
                # proof -- it is none.
                raise ValueError(f"{name} is mandatory on an executable plan")


__all__ = ["ExecutablePlan"]
