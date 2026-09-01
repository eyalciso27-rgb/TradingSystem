"""The production protected-entry lifecycle authority (ADR-0019).

``ReadinessGuardedBroker`` and ``ExecutionService`` both ask ONE object whether
a new entry can be born protected: a ``ProtectedEntryLifecycle`` whose
``protection_for(intent)`` returns ``ProtectionEvidence``. Until now there was
no production implementation, so a protection-required entry was refused
(ADR-0018 option B). This is that implementation for IBKR Paper.

It PROVES, it does not submit. Proof is: the intent carries a protective bracket
(the additive ``OrderIntent.protective``), the broker reports a NATIVE bracket
capability, the environment is Paper, and the protective stop sits on the
correct side of the entry. When all hold it returns
``established=True, mechanism="native-bracket"`` naming the protective child
ref(s) the adapter is contractually bound to attach. The single
``submit_order(intent)`` that follows establishes the parent entry and the
protective child as ONE atomic broker action inside the adapter — never the
entry alone followed by a separate stop, which is the unbounded-exposure window
ADR-0018/0019 forbid.

Anything unproven is unprotected: no bracket on the intent, a non-native
capability, a Live environment, a stop on the wrong side, or a missing parent
reference price each returns ``established=False`` with a reason, and the caller
fails closed. Nothing here talks to a broker; capability is read through the
declared ``BrokerPort`` surface, never guessed.
"""

from __future__ import annotations

from trading_application.readiness_guarded_broker import ProtectionEvidence
from trading_broker_core.port import (
    BrokerCapabilityMode,
    BrokerPort,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment

PROTECTED_ENTRY_VERSION = "ibkr-protected-entry-1.0.0"

#: The mechanism this lifecycle establishes. A single source of truth so the
#: evidence, the durable ``bracket_mechanism`` fingerprint field and the tests
#: cannot drift apart.
NATIVE_BRACKET_MECHANISM = "native-bracket"


class IbkrProtectedEntryLifecycle:
    """Proves a native IBKR bracket will protect an entry. It never submits.

    Structurally satisfies ``ProtectedEntryLifecycle``. Takes the same
    ``BrokerPort`` the agent holds, so the capability it reads is the capability
    the submission will actually cross — not a second opinion that could drift
    from the adapter.
    """

    def __init__(self, broker: BrokerPort) -> None:
        self._broker = broker

    def protection_for(self, intent: OrderIntent) -> ProtectionEvidence:
        bracket = intent.protective
        if bracket is None:
            return ProtectionEvidence(
                established=False,
                detail=(
                    "the order carries no protective bracket; an unbracketed"
                    " entry has no broker-resident protection"
                ),
            )
        # Live protection is out of scope for this milestone, and defence in
        # depth: proving protection for a Live entry here would be a second path
        # to a prohibited environment. Paper only.
        if intent.environment is not Environment.PAPER:
            return ProtectionEvidence(
                established=False,
                mechanism=NATIVE_BRACKET_MECHANISM,
                detail=(
                    f"protected entry is Paper-only; environment is"
                    f" {intent.environment.value}"
                ),
            )
        try:
            capability = self._broker.capabilities().bracket
        except Exception as error:  # noqa: BLE001 - unknown capability => unprotected
            return ProtectionEvidence(
                established=False,
                mechanism=NATIVE_BRACKET_MECHANISM,
                detail=f"broker capability query raised {type(error).__name__}",
            )
        if capability is not BrokerCapabilityMode.NATIVE:
            return ProtectionEvidence(
                established=False,
                mechanism=NATIVE_BRACKET_MECHANISM,
                detail=(
                    f"broker bracket capability is {capability.value}; a native"
                    " bracket cannot be established atomically, so the entry"
                    " would rest unprotected"
                ),
            )
        # The parent needs a positive reference price to place a protective stop
        # against, and the stop must sit on the loss-limiting side of it: below a
        # long entry, above a short one. A stop on the wrong side is not
        # protection, it is a second entry.
        entry = intent.limit_price
        if entry is None or entry <= 0:
            return ProtectionEvidence(
                established=False,
                mechanism=NATIVE_BRACKET_MECHANISM,
                detail=(
                    "a protected entry needs a positive parent reference price to"
                    " anchor its protective stop"
                ),
            )
        stop = bracket.stop_price
        if intent.side is OrderSide.BUY and not stop < entry:
            return ProtectionEvidence(
                established=False,
                mechanism=NATIVE_BRACKET_MECHANISM,
                detail=(
                    f"a long entry's protective stop {stop} must sit below the"
                    f" entry {entry}; a stop at or above it protects nothing"
                ),
            )
        if intent.side is OrderSide.SELL and not stop > entry:
            return ProtectionEvidence(
                established=False,
                mechanism=NATIVE_BRACKET_MECHANISM,
                detail=(
                    f"a short entry's protective stop {stop} must sit above the"
                    f" entry {entry}; a stop at or below it protects nothing"
                ),
            )
        refs: tuple[str, ...] = (bracket.stop_client_order_id,)
        if bracket.take_profit_client_order_id:
            refs = (*refs, bracket.take_profit_client_order_id)
        return ProtectionEvidence(
            established=True,
            mechanism=NATIVE_BRACKET_MECHANISM,
            protective_order_refs=refs,
            detail=(
                f"native bracket: parent {intent.client_order_id} atomically"
                f" establishes protective stop {bracket.stop_client_order_id} at"
                f" {stop}"
            ),
        )


__all__ = [
    "NATIVE_BRACKET_MECHANISM",
    "PROTECTED_ENTRY_VERSION",
    "IbkrProtectedEntryLifecycle",
]
