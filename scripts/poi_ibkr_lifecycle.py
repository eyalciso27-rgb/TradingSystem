"""POI-IBKR-001 owner-run Paper order-lifecycle harness (WP-A3).

The FIRST code path in this repository that can send a real broker order.
It exists only to produce POI-IBKR-001 evidence — submit / acknowledge /
fill / protection / cancel / reconciliation — against the owner's IBKR
**Paper** gateway, one order at a time, under an owner confirmation.

It **reuses** the existing engines and adds no parallel path:

- ``trading_execution_agent.agent.ExecutionAgent`` — the only component
  that may submit; every gate (idempotency, pre-submit action, halt,
  PAPER-only, health, single-use authorization, no blind retry) runs here
  exactly as in production;
- ``trading_execution_agent.protection.ProtectionManager`` — decides the
  protective structure after a fill;
- ``trading_execution_agent.reconciliation.ReconciliationEngine`` —
  compares internal vs broker state; recommends, never acts;
- ``trading_broker_core.port.BrokerPort`` — the only broker boundary; the
  concrete IBKR adapter is loaded lazily at run time (tests inject a fake).

Hard safety envelope (constants, not options — there is no override flag):

- LOCALHOST ONLY: host must be 127.0.0.1/localhost.
- PAPER PORT ONLY: port must be exactly 4002. Port 4001 (live) is rejected
  by name with a dedicated refusal; every other port is rejected too.
- PAPER ENVIRONMENT ONLY: ``Environment.PAPER``; the account snapshot must
  report PAPER and the adapter separately enforces the ``DU`` account gate.
- FIXED INSTRUMENT ALLOWLIST: ``ALLOWED_SYMBOLS`` only; AAPL is allowed
  for the cancel path only (never for a deliberate fill).
- MINIMAL SIZE: quantity <= 1 share and estimated notional <= $50.
- LIMIT ORDERS ONLY for entries; the protective leg is the STOP produced
  by the ProtectionManager. No market orders, no bracket, no OCA.
- NO UNRESTRICTED SUBMIT SURFACE: the harness cannot be asked to send an
  arbitrary order. Symbol/side/quantity/price/type are validated against
  the envelope before anything is built, and a scenario decides the shape.
- DRY-RUN FIRST: ``--execute`` is required to send anything, and it is
  refused unless ``--confirm`` carries the token printed by the dry run of
  the *identical* parameters. A stale or copied token fails closed.
- FAIL-CLOSED + CLEANUP: any unexpected error aborts the run, attempts to
  cancel whatever was submitted, and reports FAIL.
- SANITIZED EVIDENCE: account ids are masked (``DU*****xx``); no
  credential is read, prompted for or printed (login happens only in the
  Gateway UI, by the owner).

Nothing here enables PAPER_AUTO or LIVE. Flatten is not implemented and
must stay disabled. A PASS is recorded only by the owner, from a real run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from trading_application.owner_run_journal import (
    OwnerRunJournal,
    OwnerRunJournalError,
    OwnerRunReplayRefused,
)
from trading_application.pre_submit_check import (
    PreSubmitAction,
    PreSubmitCheck,
    PreSubmitCheckResult,
    SingleUseAuthorization,
    SubmitAuthorization,
)
from trading_broker_core.port import (
    BrokerCapabilityMode,
    BrokerOrderEvent,
    BrokerOrderReceipt,
    BrokerPort,
    BrokerPortError,
    BrokerPosition,
    CancelOrderRequest,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
    TimeInForce,
)
from trading_contracts.enums import Environment, OrderState, OrderType
from trading_execution_agent.agent import ExecutionAgent, ExecutionRequest, ExecutionStatus
from trading_execution_agent.protection import (
    FillEvent,
    MissingCapabilityAction,
    ProtectionManager,
    ProtectionOrderSpec,
    ProtectionPolicy,
    ProtectionStructure,
)
from trading_execution_agent.reconciliation import (
    FindingKind,
    InternalOrderExpectation,
    InternalState,
    ReconciliationEngine,
    ReconciliationPolicy,
    ReconciliationReport,
    ReconciliationTrigger,
)

HARNESS_VERSION = "poi-ibkr-lifecycle-1.0.0"

# -- safety envelope (constants; no CLI flag may widen any of these) --------
ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
LIVE_API_PORT = 4001
PAPER_ACCOUNT_PREFIX = "DU"

#: The only instruments this harness may ever touch (liquid, low-priced).
ALLOWED_SYMBOLS: frozenset[str] = frozenset({"F", "T", "AAPL"})
#: Allowed for the cancel path only — never for a deliberate fill.
CANCEL_PATH_ONLY_SYMBOLS: frozenset[str] = frozenset({"AAPL"})

MAX_QUANTITY = Decimal("1")
MAX_NOTIONAL = Decimal("50")
#: Entry limit must sit at least this far below the observed bid on the cancel
#: path. This makes the resting order NON-MARKETABLE at the observed quote — it
#: materially reduces fill risk, it does NOT make a fill impossible (a gap, a
#: halt-and-reopen or a fast move can still trade through it).
CANCEL_PATH_MIN_DISCOUNT = Decimal("0.20")

#: A market quote older than this may not authorize a submission. An old,
#: manually copied price is not evidence about the CURRENT market.
MAX_QUOTE_AGE_SECONDS = 120
#: Widest bid/ask spread (as a fraction of the mid) that still counts as a
#: sane, two-sided, tradeable market.
MAX_QUOTE_SPREAD_FRACTION = Decimal("0.02")
#: A mid that moved more than this between owner approval and submission
#: invalidates the approval: the owner must re-observe and re-approve.
MAX_QUOTE_DRIFT_FRACTION = Decimal("0.005")

#: Market-session classifications recorded with a quote.
QUOTE_SESSIONS: tuple[str, ...] = ("regular", "pre-market", "after-hours", "closed")
#: Sessions in which a submission may be authorized. A closed market has no
#: live two-sided quote, so nothing may be justified against one.
SUBMITTABLE_QUOTE_SESSIONS: frozenset[str] = frozenset(
    {"regular", "pre-market", "after-hours"}
)
#: Protective stop distance below (BUY) the observed average fill price.
PROTECTION_STOP_FRACTION = Decimal("0.02")

#: Secret name (under $SECRETS_ROOT/<ENVIRONMENT>, i.e. .../PAPER) holding
#: the DU paper account id. The directory is Environment.PAPER.value.
DEFAULT_ACCOUNT_SECRET = "ibkr/paper-account-id"

CLIENT_ORDER_PREFIX = "poi-ibkr-001"
#: Domain-separation label mixed into the confirmation-token hash so a
#: digest from this harness can never collide with one computed elsewhere.
#: **Not a credential**: it is a fixed, public namespace string, derived
#: here from the order prefix rather than written as a literal so that it
#: is self-evidently a label. It grants nothing and unlocks nothing — the
#: confirmation token is a per-run checksum of owner-supplied parameters,
#: not an authentication factor.
OWNER_CONFIRM_NAMESPACE = f"{CLIENT_ORDER_PREFIX}-owner-run"
CONFIRM_DIGEST_LENGTH = 12

#: Domain-separation prefix for the NON-AUTHORIZING operation reference. It is
#: deliberately different from OWNER_CONFIRM_NAMESPACE so the operation
#: reference can never equal a confirmation token and can never be accepted by
#: --confirm. Versioned so the derivation can evolve without ambiguity.
OPERATION_REF_NAMESPACE = "poi-ibkr-run-ref-v1"
#: Length (hex chars) of the operation reference. 16 hex chars = 64 bits, a
#: documented, collision-resistant length for the handful of owner-run
#: operations that share an account.
OPERATION_REF_LENGTH = 16

SCENARIOS: tuple[str, ...] = (
    "preflight",
    "dry-run",
    "cancel-path",
    "fill-path",
    "reconnect",
    "cleanup",
    "verify-clean",
)
#: Scenarios that may send anything at all.
SUBMITTING_SCENARIOS: frozenset[str] = frozenset({"cancel-path", "fill-path"})
#: Scenarios that legitimately run while a PoI order is still open, so the
#: "no leftover order" gate must not block them.
LEFTOVER_TOLERANT_SCENARIOS: frozenset[str] = frozenset(
    {"reconnect", "cleanup", "verify-clean"}
)
#: Scenarios that never construct an order intent (no symbol/price envelope).
NON_ORDER_SCENARIOS: frozenset[str] = frozenset(
    {"preflight", "cleanup", "reconnect", "verify-clean"}
)
#: Scenarios that make no broker call capable of changing state. ``cleanup`` is
#: NOT here: it cancels (an exposure-REDUCING call), which is why its evidence
#: header says CANCEL-ONLY rather than DRY-RUN.
READ_ONLY_SCENARIOS: frozenset[str] = frozenset(
    {"preflight", "reconnect", "verify-clean"}
)

POLICY_REF = "poi-ibkr-001-lifecycle-policy-1"

#: Prefix on every owner-visible abort instruction, so it cannot be missed
#: in a long transcript.
OWNER_ACTION_BANNER = ">>> OWNER ACTION REQUIRED <<<"


#: Evidence header for cleanup. Cleanup submits nothing, but it DOES send
#: exposure-reducing cancellation requests, so calling it a dry run would be a
#: dangerous lie about what the run did.
CLEANUP_MODE_LABEL = "CANCEL-ONLY (no order submission; existing POI orders may be cancelled)"
#: Evidence header for the scenarios that make no state-changing broker call.
READ_ONLY_MODE_LABEL = "READ-ONLY (nothing submitted, cancelled or flattened)"

#: Domain-separation prefix for the durable journal's canonical parameters
#: hash. Distinct from both the confirmation-token and operation-reference
#: namespaces, so a journal digest can never be mistaken for either.
PARAMETERS_HASH_NAMESPACE = "poi-ibkr-params-v1"


class LifecycleRefused(Exception):
    """The safety envelope refused the run (always before any broker call)."""


@runtime_checkable
class SupportsCancellableRecovery(Protocol):
    """Optional adapter capability: which orders may THIS client cancel.

    Deliberately not part of ``BrokerPort``. Global open-order visibility is a
    port-level concern; "may this API client cancel that order after a
    restart" is broker-specific and strictly narrower. An adapter without this
    capability simply has no restart recovery and cleanup falls back to
    process-local state — it never gains authority it cannot prove.
    """

    def recover_cancellable_orders(
        self, account_alias: str
    ) -> tuple[BrokerOrderReceipt, ...]: ...


def mask_account(value: str) -> str:
    """Mask an account id for evidence output (``DU*****32``)."""
    text = value.strip()
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}*****{text[-2:]}"


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    """One timestamped, two-sided market observation.

    The cancel path's safety argument is that the entry limit is
    NON-MARKETABLE. That is a claim about a LIVE market, so it must be bound to
    a fresh, valid, two-sided quote rather than to an old number copied by
    hand. It is evidence about fill RISK, never a guarantee about fills.
    """

    bid: Decimal
    ask: Decimal
    observed_at: datetime
    session: str

    def __post_init__(self) -> None:
        if self.session not in QUOTE_SESSIONS:
            raise LifecycleRefused(
                f"quote session {self.session!r} is not one of {list(QUOTE_SESSIONS)}"
            )
        if self.observed_at.tzinfo is None:
            raise LifecycleRefused("quote observed_at must be timezone-aware (UTC)")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def describe(self) -> str:
        """Sanitized one-line description for the evidence artifact."""
        return (
            f"bid={self.bid} ask={self.ask} mid={self.mid}"
            f" session={self.session} observed_at={self.observed_at.isoformat()}"
        )


def quote_refusal(
    quote: QuoteObservation,
    *,
    now: datetime,
    max_age_seconds: int = MAX_QUOTE_AGE_SECONDS,
) -> str | None:
    """Why this quote may not authorize a submission; ``None`` if it may."""
    if quote.bid <= 0 or quote.ask <= 0:
        return f"quote needs a positive bid and ask (bid={quote.bid} ask={quote.ask})"
    if quote.ask <= quote.bid:
        return f"crossed or locked quote (bid={quote.bid} >= ask={quote.ask})"
    if quote.session not in SUBMITTABLE_QUOTE_SESSIONS:
        return (
            f"market session {quote.session!r} has no live two-sided quote;"
            " nothing may be authorized against it"
        )
    age = (now - quote.observed_at).total_seconds()
    if age < 0:
        return f"quote is timestamped {abs(age):.0f}s in the future"
    if age > max_age_seconds:
        return (
            f"quote is {age:.0f}s old (maximum {max_age_seconds}s);"
            " re-observe the market and re-run"
        )
    spread_fraction = quote.spread / quote.mid
    if spread_fraction > MAX_QUOTE_SPREAD_FRACTION:
        return (
            f"spread {spread_fraction:.2%} exceeds the"
            f" {MAX_QUOTE_SPREAD_FRACTION:.2%} sanity cap; the market is not"
            " orderly enough to justify a submission"
        )
    return None


def quote_drift_refusal(
    approved: QuoteObservation, current: QuoteObservation
) -> str | None:
    """Whether the market moved materially since the approval observation."""
    if approved.mid <= 0:
        return "the approved quote has no usable mid"
    drift = abs(current.mid - approved.mid) / approved.mid
    if drift > MAX_QUOTE_DRIFT_FRACTION:
        return (
            f"the mid moved {drift:.2%} (from {approved.mid} to {current.mid}),"
            f" beyond the {MAX_QUOTE_DRIFT_FRACTION:.2%} limit; the owner's"
            " approval no longer describes this market"
        )
    return None


@dataclass(frozen=True, slots=True)
class LifecycleEnvelope:
    """The complete, validated description of one owner-run.

    Constructing this is the ONLY way to describe an order here; there is
    no path that accepts a free-form intent.
    """

    scenario: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    account_alias: str
    host: str
    port: int
    #: UTC date of the owner-run (``YYYY-MM-DD``). It scopes BOTH the
    #: confirmation token and the operation reference (and thus the
    #: ``client_order_id``): identical parameters on a LATER day produce a
    #: different token (an old token cannot be replayed) and a different order
    #: id, while a repeat on the SAME day with identical parameters stays
    #: identical so a crash-restart resumes instead of duplicating (the
    #: crash-safe resumption identity).
    run_date: str
    reference_price: Decimal | None = None
    #: The owner's fresh two-sided market observation. Mandatory for the cancel
    #: path. Deliberately NOT part of the confirmation-token material: the
    #: token binds the owner-chosen parameters, while the quote must be re-
    #: observed and re-validated at execution time (a token bound to a live
    #: quote could never be carried from a dry run to an execute).
    quote: QuoteObservation | None = None

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise LifecycleRefused(f"unknown scenario {self.scenario!r}")
        try:
            datetime.strptime(self.run_date, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as error:
            raise LifecycleRefused(f"run_date must be YYYY-MM-DD: {self.run_date!r}") from error
        if self.host not in ALLOWED_HOSTS:
            raise LifecycleRefused(
                f"host {self.host!r} refused: localhost only ({sorted(ALLOWED_HOSTS)})"
            )
        if self.port == LIVE_API_PORT:
            raise LifecycleRefused(
                f"port {LIVE_API_PORT} is the LIVE API port and is refused by"
                " construction; this harness is Paper-only"
            )
        if self.port != PAPER_API_PORT:
            raise LifecycleRefused(
                f"port {self.port} refused: only the paper API port {PAPER_API_PORT} is allowed"
            )
        if not self.account_alias.strip():
            raise LifecycleRefused("account_alias is mandatory")
        if self.scenario in NON_ORDER_SCENARIOS:
            return
        if self.symbol not in ALLOWED_SYMBOLS:
            raise LifecycleRefused(
                f"symbol {self.symbol!r} is not in the allowlist {sorted(ALLOWED_SYMBOLS)}"
            )
        if self.scenario == "fill-path" and self.symbol in CANCEL_PATH_ONLY_SYMBOLS:
            raise LifecycleRefused(
                f"symbol {self.symbol!r} is cancel-path only and must never be"
                " used for a deliberate fill"
            )
        if self.side is not OrderSide.BUY:
            raise LifecycleRefused("only BUY entries are allowed (a SELL entry could open a short)")
        if self.quantity <= 0 or self.quantity > MAX_QUANTITY:
            raise LifecycleRefused(
                f"quantity {self.quantity} refused: must be > 0 and <= {MAX_QUANTITY}"
            )
        if self.limit_price <= 0:
            raise LifecycleRefused("limit_price must be positive (LIMIT orders only)")
        notional = self.quantity * self.limit_price
        if notional > MAX_NOTIONAL:
            raise LifecycleRefused(f"estimated notional {notional} exceeds the {MAX_NOTIONAL} cap")
        if self.scenario == "cancel-path":
            # The cancel path needs an order that is NON-MARKETABLE at the
            # observed market — fill risk reduced, never eliminated. Both
            # guards are mandatory: the owner's stated reference (which the
            # confirmation token binds) and a fresh two-sided quote.
            if self.reference_price is None or self.reference_price <= 0:
                raise LifecycleRefused(
                    "--reference-price (current market price) is mandatory for the"
                    " cancel path: it bounds how far the limit sits from the market"
                )
            floor = self.reference_price * (Decimal("1") - CANCEL_PATH_MIN_DISCOUNT)
            if self.limit_price > floor:
                raise LifecycleRefused(
                    f"limit {self.limit_price} is too close to the market"
                    f" ({self.reference_price}); it must be <= {floor}"
                    f" ({CANCEL_PATH_MIN_DISCOUNT:%} below) to be non-marketable"
                    " at the observed quote"
                )
            if self.quote is None:
                raise LifecycleRefused(
                    "the cancel path requires a fresh two-sided quote"
                    " (--quote-bid, --quote-ask, --quote-at, --quote-session):"
                    " an old manually copied reference price must never"
                    " authorize a submission"
                )
            # Structural check only. Whether the quote is USABLE (positive,
            # two-sided, fresh, orderly, open session) is decided at run time
            # by ``quote_refusal`` so the owner sees the real reason in the
            # evidence rather than a derived price comparison.
            bid_floor = self.quote.bid * (Decimal("1") - CANCEL_PATH_MIN_DISCOUNT)
            if self.quote.bid > 0 and self.limit_price > bid_floor:
                raise LifecycleRefused(
                    f"limit {self.limit_price} is not far enough below the observed"
                    f" bid {self.quote.bid}; it must be <= {bid_floor}"
                    f" ({CANCEL_PATH_MIN_DISCOUNT:%} below the bid) to be"
                    " non-marketable at the observed quote"
                )

    @property
    def run_ref(self) -> str:
        """The deterministic, scenario-bound operation reference for this run.

        It is the NON-AUTHORIZING operation reference (``operation_reference``)
        — **not** the confirmation token. The token authorizes ``--execute``
        and must never appear in an order id, broker-visible metadata, log, or
        evidence artifact; the operation reference is a separate digest under a
        distinct domain prefix, safe to embed. It is deterministic: two runs
        differing in ANY canonical parameter get different references, while an
        identical-parameter re-run shares one (the crash-safe resumption
        identity). It does not distinguish two identical-parameter runs.
        """
        return operation_reference(self)

    @property
    def client_order_id(self) -> str:
        # The operation reference is part of the id, so the entry order id and
        # the protective stop id derived from it both carry this run's
        # deterministic operation identity (see _current_run_stop_prefix). The
        # confirmation token is never embedded here.
        return f"{CLIENT_ORDER_PREFIX}-{self.run_date}-{self.scenario}-{self.symbol}-{self.run_ref}"


def confirmation_token(envelope: LifecycleEnvelope) -> str:
    """Token binding an execution to the exact dry-run parameters.

    The owner obtains it from a dry run and must pass it back verbatim; a
    token from different parameters (or from an older run with a different
    price) does not match, so ``--execute`` fails closed.

    ``run_date`` is part of the material, so a token from a previous day
    cannot be replayed even when every other parameter is identical — the
    owner must always dry-run again on the day they execute.
    """
    material = "|".join(
        (
            OWNER_CONFIRM_NAMESPACE,
            HARNESS_VERSION,
            envelope.run_date,
            envelope.scenario,
            envelope.symbol,
            envelope.side.value,
            str(envelope.quantity),
            str(envelope.limit_price),
            str(envelope.reference_price),
            envelope.account_alias,
            str(envelope.port),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:CONFIRM_DIGEST_LENGTH].upper()


def parameters_hash(envelope: LifecycleEnvelope) -> str:
    """Full digest of the canonical owner-run parameters (authorizes nothing).

    Binds a durable journal entry to the exact parameters it was created for,
    so a journalled operation reference can never be reused for a different
    order. It is NOT the confirmation token: a different domain prefix and the
    full digest length, and ``--confirm`` will never accept it.
    """
    material = "|".join(
        (
            PARAMETERS_HASH_NAMESPACE,
            HARNESS_VERSION,
            envelope.run_date,
            envelope.scenario,
            envelope.symbol,
            envelope.side.value,
            str(envelope.quantity),
            str(envelope.limit_price),
            str(envelope.reference_price),
            envelope.account_alias,
            str(envelope.port),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def operation_reference(envelope: LifecycleEnvelope) -> str:
    """A deterministic, NON-AUTHORIZING operation reference for a run.

    This is **not** the confirmation token and is never accepted by
    ``--confirm``. It is a separate SHA-256 digest over the same canonical
    parameters but under a **distinct domain-separation prefix**
    (``OPERATION_REF_NAMESPACE``), so it can be safely embedded in order ids
    and evidence without ever exposing the execution-authorization value. Two
    runs differing in any canonical parameter get different references; an
    identical-parameter run yields the same reference (the crash-safe
    resumption identity). It deliberately does not distinguish two
    identical-parameter runs — that reliance is on the preflight no-stale-order
    and pre-existing-position gates, exactly-one matching working stop, and
    fail-closed ambiguity handling.
    """
    material = "|".join(
        (
            OPERATION_REF_NAMESPACE,
            HARNESS_VERSION,
            envelope.run_date,
            envelope.scenario,
            envelope.symbol,
            envelope.side.value,
            str(envelope.quantity),
            str(envelope.limit_price),
            str(envelope.reference_price),
            envelope.account_alias,
            str(envelope.port),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:OPERATION_REF_LENGTH].upper()


@dataclass
class LifecycleReport:
    """Ordered PASS/FAIL checklist rendered into the evidence artifact."""

    scenario: str
    executed: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Overrides the default EXECUTED/DRY-RUN header. Cleanup is neither: it
    #: submits nothing yet may cancel existing POI orders, so "DRY-RUN (nothing
    #: sent)" would be a dangerous lie about what the run did.
    mode_label: str | None = None

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def render(self) -> str:
        if self.mode_label is not None:
            mode = self.mode_label
        elif self.executed:
            mode = "EXECUTED (real broker order path)"
        else:
            mode = "DRY-RUN (nothing sent)"
        lines = [
            f"# POI-IBKR-001 lifecycle harness — {self.scenario}",
            "",
            f"harness: {HARNESS_VERSION}",
            f"mode: {mode}",
            "",
            "| check | result | detail |",
            "|---|---|---|",
        ]
        lines.extend(
            f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |"
            for name, ok, detail in self.checks
        )
        if self.notes:
            lines.extend(["", "## Notes", ""])
            lines.extend(f"- {note}" for note in self.notes)
        lines.extend(
            [
                "",
                f"RESULT: {'PASS' if self.passed else 'FAIL'}",
                "",
                "This artifact alone is NOT a PoI PASS: only the owner may mark",
                "POI-IBKR-001, from a real run, in docs/poi/STATUS.yaml.",
            ]
        )
        return "\n".join(lines)


def _authorized_request(
    envelope: LifecycleEnvelope,
    *,
    client_order_id: str,
    order_type: OrderType,
    side: OrderSide,
    quantity: Decimal,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
    correlation_id: str,
    now: datetime,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> ExecutionRequest:
    """Build the queue item the ExecutionAgent consumes.

    The authorization represents the **owner's explicit confirmation** for
    this single PoI order — it is minted only after the confirmation token
    matched, it is single-use, and it never bypasses any agent gate. It is
    deliberately NOT a claim that the automated decision → risk →
    allocation pipeline ran: this is a manual integration experiment, and
    the reason codes below say so.
    """
    candidate_id = f"{CLIENT_ORDER_PREFIX}:{client_order_id}"
    authorization = SubmitAuthorization(
        candidate_id=candidate_id,
        authorized_quantity=quantity,
        authorized_at=now,
    )
    pre_submit = PreSubmitCheckResult(
        candidate_id=candidate_id,
        action=PreSubmitAction.ALLOW,
        authorized_quantity=quantity,
        authorization=authorization,
        allocation_ref="owner-run:manual-minimal-size",
        risk_ref="owner-run:poi-envelope",
        decision_ref="owner-run:owner-confirmed",
        policy_ref=POLICY_REF,
        checks=(
            PreSubmitCheck(
                name="poi_safety_envelope",
                passed=True,
                limit=(
                    f"paper/localhost/{PAPER_API_PORT}; allowlist"
                    f" {sorted(ALLOWED_SYMBOLS)}; qty<={MAX_QUANTITY};"
                    f" notional<={MAX_NOTIONAL}"
                ),
                observed=f"{side.value} {quantity} {envelope.symbol} {order_type.value}",
                effect="PASS",
            ),
            PreSubmitCheck(
                name="owner_confirmation_token",
                passed=True,
                limit="token from a dry run of identical parameters",
                observed="matched",
                effect="PASS",
            ),
        ),
        reason_codes=("OWNER_CONFIRMED_POI_RUN", "NOT_AN_AUTOMATED_DECISION"),
        input_refs=(f"envelope:{envelope.scenario}",),
    )
    return ExecutionRequest(
        client_order_id=client_order_id,
        correlation_id=correlation_id,
        pre_submit=pre_submit,
        authorization=SingleUseAuthorization(authorization),
        environment=Environment.PAPER,
        account_alias=envelope.account_alias,
        symbol=envelope.symbol,
        side=side,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        strategy_id="poi-ibkr-001-manual",
        source_id="owner-manual",
        time_in_force=time_in_force,
    )


def _owner_action_required(report: LifecycleReport, headline: str, *steps: str) -> None:
    """Record a loud, unmissable, owner-visible abort instruction.

    Used whenever the run may have left real exposure at the broker. It
    records a FAILED check (so the artifact can never read PASS) *and*
    spells out what the owner must do by hand — the harness itself never
    flattens and never places a corrective order.
    """
    report.record("owner_action_required", False, headline)
    report.note(f"**{OWNER_ACTION_BANNER}** {headline}")
    for index, step in enumerate(steps, start=1):
        report.note(f"{OWNER_ACTION_BANNER} step {index}: {step}")
    report.note(
        f"{OWNER_ACTION_BANNER} do NOT run another scenario until the account is"
        " flat and clean; re-run --scenario reconnect to re-inspect."
    )


def _submit_failure_abort(report: LifecycleReport, status: ExecutionStatus) -> None:
    """A submit that did not confirm may still have reached the broker."""
    if status is ExecutionStatus.NEEDS_REVIEW:
        # UNKNOWN broker state: an order MAY exist. Never resend.
        _owner_action_required(
            report,
            "broker state is UNKNOWN after submit — an order MAY exist at the broker",
            "do NOT re-run this scenario and do NOT resend anything",
            "inspect open orders and positions in the IB Gateway/TWS UI (VNC)",
            "cancel anything this run may have left, by hand",
            "only then re-run --scenario reconnect to confirm the account is clean",
        )
    else:
        report.note(
            f"submit terminated as {status.value} before any order was accepted;"
            " nothing was sent and no retry was attempted"
        )


def _cancel_unconfirmed_abort(
    report: LifecycleReport,
    *,
    operation_reference: str,
    broker_order_ref: str,
    detail: str,
) -> None:
    """The cancel was sent but the broker never confirmed it as cancelled.

    The order may still be working. This is not a fill (that is handled by
    ``_unprotected_position_abort``) — it is an UNKNOWN cancellation outcome,
    so the only safe response is owner reconciliation, never a blind re-run of
    the scenario or a reuse of the confirmation token. Both references are
    non-authorizing broker/operation correlators — never account ids, tokens
    or secrets.
    """
    _owner_action_required(
        report,
        "CANCELLATION NOT CONFIRMED — the order MAY STILL BE WORKING at the broker"
        f" ({detail})",
        f"correlate operation_reference={operation_reference}"
        f" broker_order_ref={broker_order_ref} in the IB Gateway/TWS UI (VNC)",
        "do NOT blindly re-run this scenario and do NOT reuse the confirmation"
        " token — a fresh submit could double the exposure",
        "cancel the order by hand if it is still working",
        "re-run --scenario reconnect and confirm reconciliation reports the"
        " account clean (open_poi_orders=0) before anything else",
    )


def _protective_spec_refusal(spec: ProtectionOrderSpec, position_quantity: Decimal) -> str | None:
    """Envelope check for the protective leg; ``None`` means acceptable.

    The protective order is generated, not owner-supplied, so it needs its
    own guard: it must be a STOP that REDUCES the open long by at most the
    quantity actually held, priced positively, within the harness cap.
    """
    if spec.order_type is not OrderType.STOP:
        return f"protective order type {spec.order_type.value} is not a plain STOP"
    if spec.side is not OrderSide.SELL:
        return f"protective side {spec.side.value} would not reduce a long position"
    if spec.quantity <= 0:
        return f"protective quantity {spec.quantity} is not positive"
    if spec.quantity > position_quantity:
        return (
            f"protective quantity {spec.quantity} exceeds the {position_quantity}"
            " held — sending it would open a short"
        )
    if spec.quantity > MAX_QUANTITY:
        return f"protective quantity {spec.quantity} exceeds the {MAX_QUANTITY} cap"
    if spec.stop_price is None or spec.stop_price <= 0:
        return f"protective stop price {spec.stop_price} is not positive"
    return None


def _unprotected_position_abort(report: LifecycleReport, reason: str) -> None:
    """The most dangerous state this harness can reach: a naked position."""
    _owner_action_required(
        report,
        f"UNPROTECTED PAPER POSITION MAY BE OPEN — {reason}",
        "open the IB Gateway/TWS UI (VNC) NOW and inspect the account",
        "close the position manually, or place a protective stop by hand",
        "cancel any leftover working order for this symbol",
        "record what happened in the POI-IBKR-001 evidence file; this run is a FAIL",
    )


def _record_intended_parameters(report: LifecycleReport, envelope: LifecycleEnvelope) -> None:
    """Record the exact intended parameters into the evidence artifact.

    Proves *what was intended* independently of what the broker returned:
    symbol, side, quantity, limit price, run date, the (non-authorizing)
    operation reference and the masked account alias. The confirmation token
    is deliberately NOT recorded — it is an execution-authorization value.
    """
    report.record(
        "intended_parameters",
        True,
        f"symbol={envelope.symbol} side={envelope.side.value}"
        f" quantity={envelope.quantity} limit_price={envelope.limit_price}"
        f" run_date={envelope.run_date} operation_reference={envelope.run_ref}"
        f" account={mask_account(envelope.account_alias)}",
    )


def _record_broker_order_identity(
    report: LifecycleReport, name: str, receipt: BrokerOrderReceipt | None
) -> None:
    """Record a broker order's identity so the owner can correlate the
    artifact with the actual order at the broker. ``broker_order_ref`` is the
    broker's own opaque reference; it is not an account id or a secret."""
    if receipt is None:
        report.record(name, False, "no broker receipt returned")
        return
    report.record(
        name,
        True,
        f"client_order_id={receipt.client_order_id}"
        f" broker_order_ref={receipt.broker_order_ref} state={receipt.state.value}",
    )


#: Hard bound on any broker-supplied text copied into evidence.
BROKER_DETAIL_MAX_LENGTH = 200


def sanitize_broker_detail(text: str) -> str:
    """Bound and scrub broker-supplied text before it reaches the evidence.

    The adapter already masks account ids, but this is the last gate before an
    owner-visible artifact, so it re-masks anything shaped like a broker
    account, strips control characters and anything path-like, and truncates.
    """
    collapsed = " ".join(str(text).split())
    scrubbed = re.sub(r"\b([DU]U?)\d{4,}\b", r"\1*****", collapsed)
    # Never echo a filesystem path: it could name a secret-store location. The
    # pattern deliberately requires a path-like TOKEN (leading separator or
    # drive letter, plus two or more segments) so ordinary broker text such as
    # "SMART/USD" survives intact.
    scrubbed = re.sub(
        r"(?<![\w/\\])(?:[A-Za-z]:)?[\\/][\w.\-]+(?:[\\/][\w.\-]+)+", "<path>", scrubbed
    )
    if len(scrubbed) > BROKER_DETAIL_MAX_LENGTH:
        return scrubbed[: BROKER_DETAIL_MAX_LENGTH - 1] + "…"
    return scrubbed


def describe_broker_error(error: BrokerPortError) -> str:
    """Sanitized, actionable description of a fail-closed broker error.

    A real verify-clean owner-run reported only ``broker error
    BROKER_UNHEALTHY; no retry`` while the account was in fact reachable and
    merely held an unrelated position — the owner had no way to tell a dead
    Gateway from a preflight refusal (D-CLOCK-02). The code alone is not
    enough: the bounded message and the chained root-cause TYPE (never its
    text, which may come from a third-party library) are what make the
    artifact actionable. Fail-closed behaviour is unchanged; only the
    reporting improves.
    """
    parts = [f"broker error {error.code.value}"]
    detail = sanitize_broker_detail(str(error))
    prefix = f"{error.code.value}: "
    if detail.startswith(prefix):
        detail = detail[len(prefix) :]
    if detail:
        parts.append(detail)
    cause = error.__cause__ or error.__context__
    if cause is not None:
        # The TYPE only: a third-party exception's text is not ours to trust.
        parts.append(f"cause={type(cause).__name__}")
    if error.correlation_id:
        parts.append(f"correlation_id={error.correlation_id}")
    parts.append("no retry")
    return "; ".join(parts)


def _record_position_breakdown(
    report: LifecycleReport,
    positions: Sequence[BrokerPosition],
    *,
    symbol: str,
    label: str,
) -> tuple[BrokerPosition, ...]:
    """Report raw rows, NON-ZERO positions and run-symbol positions separately.

    IBKR reports a closed holding as a row with quantity 0. Counting raw rows
    as "positions" makes a flat account read as ``1 position(s)``, which an
    owner can easily misread as live exposure. Returns the non-zero positions.
    """
    nonzero = tuple(p for p in positions if p.quantity != 0)
    in_symbol = tuple(p for p in nonzero if p.symbol == symbol)
    zero_rows = len(positions) - len(nonzero)
    report.record(
        label,
        True,
        f"raw_rows={len(positions)} (of which zero-quantity={zero_rows});"
        f" non_zero_positions={len(nonzero)}"
        f" [{', '.join(f'{p.symbol}:{p.quantity}' for p in nonzero) or 'none'}];"
        f" {symbol}_positions={len(in_symbol)}",
    )
    return nonzero


def _event_id(event: BrokerOrderEvent) -> str:
    """The engine's canonical id for one normalized broker event."""
    return f"{event.sequence}:{event.client_order_id}:{event.state.value}"


def _drain_events(broker: BrokerPort, client_order_id: str) -> list[BrokerOrderEvent]:
    return [e for e in broker.order_events() if e.client_order_id == client_order_id]


def _await_state(
    broker: BrokerPort,
    client_order_id: str,
    wanted: frozenset[OrderState],
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    poll_seconds: float = 1.0,
) -> tuple[BrokerOrderEvent | None, list[BrokerOrderEvent]]:
    """Poll normalized order events until one reaches a wanted state.

    Returns the matching event (or ``None`` on timeout) plus everything
    observed. There is no retry of the ORDER here — only of the read.

    The deadline is measured on a MONOTONIC clock (D-CLOCK-01): a wall-clock
    subtraction would be corrupted by an NTP step or a manual correction, which
    could either abandon a live order early or wait far past the timeout.
    Monotonic values measure duration only and never reach the evidence, which
    is UTC wall-clock throughout.
    """
    seen: list[BrokerOrderEvent] = []
    deadline = monotonic() + timeout_seconds
    while True:
        for event in _drain_events(broker, client_order_id):
            seen.append(event)
            if event.state in wanted:
                return event, seen
        if monotonic() >= deadline:
            return None, seen
        sleep(poll_seconds)


def _first_fill(events: Sequence[BrokerOrderEvent]) -> BrokerOrderEvent | None:
    """The earliest fill among the observed events, if any.

    ``_await_state`` returns as soon as ANY wanted state matches, so a fill
    that arrives after an acknowledgement would be invisible if callers
    only inspected the matched event.
    """
    for event in events:
        if event.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            return event
    return None


def _all_events(broker: BrokerPort, client_order_ids: Sequence[str]) -> list[BrokerOrderEvent]:
    wanted = set(client_order_ids)
    return [e for e in broker.order_events() if e.client_order_id in wanted]


def _reconcile(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    open_orders: tuple[InternalOrderExpectation, ...],
    positions: dict[str, Decimal],
    protected: dict[str, Decimal],
    processed_event_ids: frozenset[str],
    now: datetime,
    trigger: ReconciliationTrigger,
) -> ReconciliationReport:
    engine = ReconciliationEngine(
        ReconciliationPolicy(
            policy_version=POLICY_REF,
            blocking_kinds=frozenset(
                {
                    FindingKind.PROTECTION_MISMATCH,
                    FindingKind.UNEXPECTED_BROKER_ORDER,
                    FindingKind.POSITION_MISMATCH,
                    FindingKind.UNRESOLVED_UNKNOWN_ORDER,
                }
            ),
        )
    )
    internal = InternalState(
        account_alias=envelope.account_alias,
        environment=Environment.PAPER,
        open_orders=open_orders,
        positions=positions,
        protected_quantities=protected,
        # Exactly the events this run actually consumed — never a blanket
        # "everything", so a lost event still surfaces as MISSING_FILL_EVENT.
        processed_fill_event_ids=processed_event_ids,
        as_of=now,
    )
    result = engine.run(broker, internal, trigger=trigger, as_of=now)
    detail = (
        f"reconciled={result.reconciled} findings={len(result.findings)}"
        f" entry_block_required={result.entry_block_required}"
    )
    report.record("reconciliation", result.reconciled, detail)
    for finding in result.findings:
        report.note(
            f"finding {finding.kind.value} on {finding.subject_ref}:"
            f" expected={finding.expected} observed={finding.observed}"
            f" -> {finding.recommendation.value}"
        )
    return result


def _probe[T](report: LifecycleReport, name: str, read: Callable[[], T]) -> T | None:
    """Run one READ-ONLY broker probe, recording its own pass/fail.

    Returns ``None`` when the probe failed. The caller records the affected
    check as UNPROVEN, so the run still fails closed while every other probe's
    answer survives into the evidence.
    """
    try:
        value = read()
    except BrokerPortError as error:
        report.record(name, False, describe_broker_error(error))
        return None
    report.record(name, True, "read")
    return value


def _preflight(broker: BrokerPort, envelope: LifecycleEnvelope, report: LifecycleReport) -> None:
    """Read-only checks that must pass before anything may be submitted.

    Every read is an INDEPENDENT probe (D-CLOCK-02). Previously the first
    failing read raised out of the whole run, so a real verify-clean owner-run
    reported nothing but ``broker error BROKER_UNHEALTHY; no retry`` and the
    owner could not see that the account in fact held an unrelated position.

    Fail-closed is unchanged: a failed probe records a FAILED check, so
    ``report.passed`` is False and ``run_lifecycle`` still refuses to submit.
    The difference is only that the other probes' answers survive into the
    evidence instead of being thrown away.
    """
    health = _probe(report, "broker_health_read", broker.health_check)
    if health is None:
        report.record("broker_health", False, "UNPROVEN: health could not be read")
    else:
        report.record("broker_health", health.healthy, health.detail)

    snapshot = _probe(
        report,
        "account_snapshot_read",
        lambda: broker.get_account_snapshot(envelope.account_alias),
    )
    if snapshot is None:
        report.record("paper_environment", False, "UNPROVEN: account snapshot unreadable")
        report.record("paper_account_gate", False, "UNPROVEN: account snapshot unreadable")
    else:
        report.record(
            "paper_environment",
            snapshot.environment is Environment.PAPER,
            f"environment={snapshot.environment.value}",
        )
        # Always masked: if an operator ever passes a raw DU id as the alias it
        # must still never reach stdout or the evidence artifact in full.
        report.record(
            "paper_account_gate",
            snapshot.account_alias.strip() != "",
            f"alias={mask_account(snapshot.account_alias)} (the adapter enforces the"
            f" {PAPER_ACCOUNT_PREFIX} prefix gate on the real account id)",
        )

    capabilities = _probe(report, "capabilities_read", broker.capabilities)
    if capabilities is None:
        report.record("protective_stop_supported", False, "UNPROVEN: capabilities unreadable")
    else:
        stop_mode = capabilities.order_types.get(OrderType.STOP, BrokerCapabilityMode.UNSUPPORTED)
        report.record(
            "protective_stop_supported",
            stop_mode is BrokerCapabilityMode.NATIVE,
            f"STOP={stop_mode.value}; bracket={capabilities.bracket.value}"
            f"; oca={capabilities.oco.value}",
        )

    if envelope.scenario not in NON_ORDER_SCENARIOS:
        instrument = _probe(
            report,
            "instrument_read",
            lambda: broker.resolve_instrument(InstrumentQuery(symbol=envelope.symbol)),
        )
        report.record(
            "instrument_resolved",
            instrument is not None and instrument.symbol == envelope.symbol,
            "UNPROVEN: instrument could not be resolved"
            if instrument is None
            else f"{instrument.symbol} @ {instrument.exchange} {instrument.currency}",
        )

    open_orders = _probe(
        report, "open_orders_read", lambda: broker.get_open_orders(envelope.account_alias)
    )
    stale = (
        ()
        if open_orders is None
        else tuple(o for o in open_orders if o.client_order_id.startswith(CLIENT_ORDER_PREFIX))
    )
    if envelope.scenario not in LEFTOVER_TOLERANT_SCENARIOS:
        # Cleanup exists precisely to clear these and reconnect exists to
        # observe them, so neither may be blocked by them; every other
        # scenario refuses to start on top of a leftover.
        report.record(
            "no_stale_poi_orders",
            open_orders is not None and not stale,
            "UNPROVEN: the open-order snapshot could not be read"
            if open_orders is None
            else "none"
            if not stale
            else f"{len(stale)} stale PoI order(s) still open: run --scenario cleanup first",
        )
    elif open_orders is not None:
        report.record("open_poi_orders", True, f"{len(stale)} still open at the broker")

    positions = _probe(
        report, "positions_read", lambda: broker.get_positions(envelope.account_alias)
    )
    if positions is None:
        report.record(
            "position_snapshot", False, "UNPROVEN: the position snapshot could not be read"
        )
        return
    existing = tuple(p for p in positions if p.symbol == envelope.symbol and p.quantity != 0)
    if envelope.scenario in SUBMITTING_SCENARIOS:
        # A pre-existing holding in the SAME symbol would blend the average
        # price the protective stop is derived from, and would make
        # "protected quantity vs. position" ambiguous. Refuse to start.
        report.record(
            "no_preexisting_position_in_symbol",
            not existing,
            "none"
            if not existing
            else f"{envelope.symbol} already held ({existing[0].quantity}); close it"
            " first — this run must own the whole position it protects",
        )
    _record_position_breakdown(
        report, positions, symbol=envelope.symbol, label="position_snapshot"
    )


def _revalidate_quote(
    report: LifecycleReport,
    envelope: LifecycleEnvelope,
    *,
    now: datetime,
    quote_provider: Callable[[], QuoteObservation] | None,
) -> QuoteObservation:
    """Re-check the approving quote immediately before submission.

    The owner's observation authorized a NON-MARKETABLE limit. That claim
    decays: it must still be fresh, still two-sided, still an open session,
    and the market must not have moved materially since the approval. Any
    failure refuses the run BEFORE anything is sent — an old, manually copied
    reference price never authorizes a submission on its own.
    """
    approved = envelope.quote
    if approved is None:  # unreachable: the envelope enforces this
        raise LifecycleRefused("the cancel path requires an approving quote")
    refusal = quote_refusal(approved, now=now)
    report.record(
        "approval_quote_valid",
        refusal is None,
        approved.describe() if refusal is None else refusal,
    )
    if refusal is not None:
        raise LifecycleRefused(f"the approving quote is not usable: {refusal}")

    current = approved if quote_provider is None else quote_provider()
    source = "owner-observed (no live quote provider wired)" if quote_provider is None else "live"
    fresh_refusal = quote_refusal(current, now=now)
    if fresh_refusal is not None:
        raise LifecycleRefused(f"pre-submit quote check failed: {fresh_refusal}")
    drift = quote_drift_refusal(approved, current)
    if drift is not None:
        raise LifecycleRefused(f"pre-submit quote check failed: {drift}")
    bid_floor = current.bid * (Decimal("1") - CANCEL_PATH_MIN_DISCOUNT)
    if envelope.limit_price > bid_floor:
        raise LifecycleRefused(
            f"pre-submit quote check failed: limit {envelope.limit_price} is no"
            f" longer {CANCEL_PATH_MIN_DISCOUNT:%} below the bid {current.bid}"
        )
    report.record(
        "pre_submit_quote_revalidated",
        True,
        f"{current.describe()} source={source}; limit {envelope.limit_price} <="
        f" {bid_floor} at submission time",
    )
    return current


def _run_cancel_path(
    broker: BrokerPort,
    agent: ExecutionAgent,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_seconds: float,
    quote_provider: Callable[[], QuoteObservation] | None = None,
) -> None:
    """Submit a non-marketable resting LIMIT, acknowledge, then cancel."""
    _record_intended_parameters(report, envelope)
    # LifecycleEnvelope already refused to construct without a reference
    # price and a limit far enough below it; this records the proof. Note the
    # claim: NON-MARKETABLE at the observed quote. A limit below the market
    # materially reduces fill risk; it does not make a fill impossible (a gap,
    # a halt-and-reopen or a fast move can still trade through it).
    reference = envelope.reference_price or Decimal(0)
    floor = reference * (Decimal("1") - CANCEL_PATH_MIN_DISCOUNT)
    report.record(
        "cancel_path_non_marketable",
        reference > 0 and envelope.limit_price <= floor,
        f"limit {envelope.limit_price} <= {floor}"
        f" ({CANCEL_PATH_MIN_DISCOUNT:%} below reference {reference}):"
        " non-marketable at the observed quote; fill risk reduced but NOT"
        " eliminated",
    )
    # Bind the approval to a fresh, valid, two-sided observation and
    # revalidate it IMMEDIATELY before submission.
    quote = _revalidate_quote(
        report, envelope, now=now, quote_provider=quote_provider
    )
    report.record(
        "quote_session_recorded",
        True,
        f"session={quote.session} (regular / pre-market / after-hours"
        " classification is recorded with the approving quote)",
    )

    correlation_id = f"{CLIENT_ORDER_PREFIX}-cancel-{int(now.timestamp())}"
    request = _authorized_request(
        envelope,
        client_order_id=envelope.client_order_id,
        order_type=OrderType.LIMIT,
        side=envelope.side,
        quantity=envelope.quantity,
        limit_price=envelope.limit_price,
        stop_price=None,
        correlation_id=correlation_id,
        now=now,
    )
    outcome = agent.process(request)
    report.record(
        "submit",
        outcome.status is ExecutionStatus.SUBMITTED,
        f"status={outcome.status.value} codes={','.join(outcome.reason_codes)}",
    )
    if outcome.status is not ExecutionStatus.SUBMITTED:
        _submit_failure_abort(report, outcome.status)
        return
    _record_broker_order_identity(report, "entry_broker_order", outcome.receipt)

    # Idempotency proof: the identical queue item must NOT create a second
    # order (the DoD's "zero duplicate orders").
    repeat = agent.process(request)
    report.record(
        "idempotent_resubmit",
        repeat is outcome or repeat.receipt == outcome.receipt,
        "the same client_order_id returned the original outcome; nothing re-sent",
    )

    ack, seen = _await_state(
        broker,
        envelope.client_order_id,
        frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}),
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )
    report.record(
        "acknowledged",
        ack is not None and ack.state is OrderState.ACKNOWLEDGED,
        f"observed states: {[e.state.value for e in seen] or 'none'}",
    )
    # A rapid market move can fill an order that was priced never to fill,
    # and the fill can arrive AFTER the acknowledgement — so scan every
    # event seen for this order, not just the first one that matched.
    fill = _first_fill(seen)
    if fill is not None:
        _unprotected_position_abort(
            report,
            f"the cancel-path order unexpectedly reached {fill.state.value}"
            f" ({fill.filled_quantity} filled) despite being priced"
            f" {CANCEL_PATH_MIN_DISCOUNT:%} below the market",
        )
        # Still try to cancel any working remainder, then stop: the cancel
        # path has no protection machinery, so the owner takes over.
        remainder = agent.cancel(envelope.client_order_id, correlation_id=correlation_id)
        report.record(
            "unexpected_fill_remainder_cancelled",
            remainder.status is ExecutionStatus.CANCEL_SENT,
            f"status={remainder.status.value}",
        )
        return

    cancel_outcome = agent.cancel(envelope.client_order_id, correlation_id=correlation_id)
    report.record(
        "cancel_sent",
        cancel_outcome.status is ExecutionStatus.CANCEL_SENT,
        f"status={cancel_outcome.status.value}",
    )
    cancelled, seen_after = _await_state(
        broker,
        envelope.client_order_id,
        frozenset({OrderState.CANCELLED}),
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )
    report.record(
        "cancel_confirmed",
        cancelled is not None,
        f"observed states: {[e.state.value for e in seen_after] or 'none'}",
    )
    # Last look: a fill that landed while the cancel was in flight.
    late_fill = _first_fill(seen_after)
    if cancelled is None and late_fill is None:
        # Cancel was sent but neither a CANCELLED nor a fill was observed: the
        # order may still be working. Fail closed with an owner reconciliation
        # instruction — never reconcile-as-clean and never a blind re-run.
        _cancel_unconfirmed_abort(
            report,
            operation_reference=envelope.run_ref or operation_reference(envelope),
            broker_order_ref=outcome.receipt.broker_order_ref
            if outcome.receipt is not None
            else "unknown",
            detail=f"observed states: {[e.state.value for e in seen_after] or 'none'}",
        )
        return
    report.record(
        "never_filled",
        late_fill is None,
        # An OBSERVED post-run fact, never a pre-submit guarantee: the order
        # was non-marketable at the observed quote, and in this window it did
        # not fill.
        "observed: no fill event on the cancel path during this run"
        if late_fill is None
        else f"a fill arrived during cancellation: {late_fill.state.value}",
    )
    if late_fill is not None:
        _unprotected_position_abort(
            report, "the cancel-path order filled while the cancel was in flight"
        )
        return
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=(),
        positions={},
        protected={},
        processed_event_ids=frozenset(_event_id(e) for e in (*seen, *seen_after)),
        now=now,
        trigger=ReconciliationTrigger.PERIODIC,
    )


def _run_fill_path(
    broker: BrokerPort,
    agent: ExecutionAgent,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_seconds: float,
) -> None:
    """Submit a marketable LIMIT, then protect the resulting position."""
    _record_intended_parameters(report, envelope)
    correlation_id = f"{CLIENT_ORDER_PREFIX}-fill-{int(now.timestamp())}"
    request = _authorized_request(
        envelope,
        client_order_id=envelope.client_order_id,
        order_type=OrderType.LIMIT,
        side=envelope.side,
        quantity=envelope.quantity,
        limit_price=envelope.limit_price,
        stop_price=None,
        correlation_id=correlation_id,
        now=now,
    )
    outcome = agent.process(request)
    report.record(
        "submit",
        outcome.status is ExecutionStatus.SUBMITTED,
        f"status={outcome.status.value} codes={','.join(outcome.reason_codes)}",
    )
    if outcome.status is not ExecutionStatus.SUBMITTED:
        _submit_failure_abort(report, outcome.status)
        return
    # Broker order identity of the entry: proves which order was created.
    _record_broker_order_identity(report, "entry_broker_order", outcome.receipt)

    fill, seen = _await_state(
        broker,
        envelope.client_order_id,
        frozenset({OrderState.FILLED, OrderState.PARTIALLY_FILLED}),
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )
    report.record(
        "fill_observed",
        fill is not None,
        f"observed states: {[e.state.value for e in seen] or 'none'}",
    )
    if fill is not None:
        # Fill identity: broker order ref + sequence + filled quantity, so the
        # artifact ties the fill to the entry order.
        report.record(
            "fill_identity",
            True,
            f"client_order_id={fill.client_order_id}"
            f" broker_order_ref={fill.broker_order_ref} sequence={fill.sequence}"
            f" state={fill.state.value} filled_quantity={fill.filled_quantity}",
        )
    if fill is None:
        # Fail closed: an unfilled entry must not be left resting.
        cancel_outcome = agent.cancel(envelope.client_order_id, correlation_id=correlation_id)
        report.record(
            "unfilled_entry_cancelled",
            cancel_outcome.status is ExecutionStatus.CANCEL_SENT,
            f"no fill within {timeout_seconds}s; cancelled instead of waiting",
        )
        return

    # A PARTIALLY_FILLED entry still has a working remainder that could
    # fill later and add quantity nothing is protecting. Close that door
    # before protecting what already filled.
    if fill.state is OrderState.PARTIALLY_FILLED:
        remainder = agent.cancel(envelope.client_order_id, correlation_id=correlation_id)
        report.record(
            "entry_remainder_cancelled",
            remainder.status is ExecutionStatus.CANCEL_SENT,
            f"partial fill {fill.filled_quantity}/{envelope.quantity}: cancelled the"
            f" working remainder (status={remainder.status.value}) so no unprotected"
            " quantity can arrive later",
        )
        if remainder.status is not ExecutionStatus.CANCEL_SENT:
            _unprotected_position_abort(
                report,
                "partially filled entry whose remainder could NOT be cancelled",
            )
            return

    positions = {p.symbol: p for p in broker.get_positions(envelope.account_alias)}
    position = positions.get(envelope.symbol)
    report.record(
        "position_reported",
        position is not None and position.quantity > 0,
        f"quantity={position.quantity if position else 0}",
    )
    if position is None:
        _unprotected_position_abort(
            report, "a fill was observed but the broker reports no position"
        )
        return

    # The real adapter's average_price is not proven by fakes. A missing or
    # non-positive price would otherwise raise deep inside ProtectionManager
    # AFTER the position exists — fail loudly and explicitly instead.
    if position.average_price <= 0:
        _unprotected_position_abort(
            report,
            f"broker reported average_price={position.average_price} so no valid"
            " protective stop can be computed",
        )
        return

    stop_price = (position.average_price * (Decimal("1") - PROTECTION_STOP_FRACTION)).quantize(
        Decimal("0.01")
    )
    if stop_price <= 0 or stop_price >= position.average_price:
        _unprotected_position_abort(
            report,
            f"computed stop {stop_price} is not strictly below the entry price"
            f" {position.average_price}",
        )
        return
    manager = ProtectionManager(
        ProtectionPolicy(
            policy_version=POLICY_REF,
            structure=ProtectionStructure.PLAIN_STOP,
            missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
        )
    )
    plan = manager.on_fill(
        FillEvent(
            fill_id=f"{envelope.client_order_id}-fill",
            position_ref=envelope.client_order_id,
            symbol=envelope.symbol,
            environment=Environment.PAPER,
            entry_side=envelope.side,
            filled_quantity=fill.filled_quantity,
            stop_price=stop_price,
            target_price=None,
            observed_at=fill.observed_at,
            correlation_id=correlation_id,
        ),
        broker.capabilities(),
        as_of=now,
    )
    report.record(
        "protection_planned",
        plan.protected_quantity >= fill.filled_quantity,
        f"status={plan.status.value} protected={plan.protected_quantity}"
        f"/{plan.filled_quantity} actions={len(plan.actions)}",
    )

    protective_ids: list[str] = []
    protected_quantity = Decimal(0)
    for action in plan.actions:
        spec = action.spec
        # The protective leg bypasses LifecycleEnvelope validation (it is
        # built from the plan, not from CLI input), so it gets its own
        # envelope check: it must REDUCE the position, never exceed it —
        # a SELL larger than the holding would open a short.
        refusal = _protective_spec_refusal(spec, position.quantity)
        report.record(
            f"protection_within_envelope[{spec.spec_ref}]",
            refusal is None,
            refusal
            or f"{spec.side.value} {spec.quantity} {spec.order_type.value}"
            f" stop={spec.stop_price} reduces a {position.quantity} long",
        )
        if refusal is not None:
            _unprotected_position_abort(
                report, f"refused to send an out-of-envelope protective order: {refusal}"
            )
            continue

        protective_id = f"{envelope.client_order_id}-{spec.spec_ref}"
        protective = _authorized_request(
            envelope,
            client_order_id=protective_id,
            order_type=spec.order_type,
            side=spec.side,
            quantity=spec.quantity,
            limit_price=spec.limit_price,
            stop_price=spec.stop_price,
            correlation_id=correlation_id,
            now=now,
            # A protective stop MUST outlive the trading session: a DAY stop
            # would expire at the close and leave the open position naked
            # overnight. Submit protection GTC so it rests until the position
            # is closed (the harness never flattens; the owner closes by hand).
            time_in_force=TimeInForce.GTC,
        )
        protective_outcome = agent.process(protective)
        accepted = protective_outcome.status is ExecutionStatus.SUBMITTED
        report.record(
            f"protection_submitted[{spec.spec_ref}]",
            accepted,
            f"{spec.side.value} {spec.quantity} {spec.order_type.value}"
            f" stop={spec.stop_price} status={protective_outcome.status.value}",
        )
        if accepted:
            protective_ids.append(protective_id)
            protected_quantity += spec.quantity
        else:
            _unprotected_position_abort(
                report,
                "the protective stop was NOT accepted by the broker"
                f" (status={protective_outcome.status.value})",
            )

    fully_protected = (
        protected_quantity >= position.quantity and not manager.unprotected_positions()
    )
    report.record(
        "no_unprotected_position",
        fully_protected,
        f"protected {protected_quantity} of a {position.quantity} position via"
        f" {len(protective_ids)} accepted order(s);"
        f" manager unprotected={list(manager.unprotected_positions())}",
    )
    if not fully_protected:
        _unprotected_position_abort(
            report,
            f"only {protected_quantity} of {position.quantity} is protected at the broker",
        )
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=tuple(
            InternalOrderExpectation(
                client_order_id=order_id,
                expected_state=OrderState.ACKNOWLEDGED,
                is_protective=True,
            )
            for order_id in protective_ids
        ),
        positions={envelope.symbol: position.quantity},
        # ACCEPTED protection, never PLANNED protection: feeding the plan
        # here would hide the exact failure this run must surface.
        protected={envelope.symbol: protected_quantity},
        processed_event_ids=frozenset(
            _event_id(e) for e in (*seen, *_all_events(broker, protective_ids))
        ),
        now=now,
        trigger=ReconciliationTrigger.PERIODIC,
    )
    if fully_protected:
        report.note(
            "The position and its protective stop are still open on the paper"
            " account: close them manually in the Gateway/TWS UI (this harness"
            " never flattens — flatten stays hard-disabled)."
        )
    # Record this run's deterministic operation reference so the owner can
    # bind the reconnect scenario to this fill-path operation: reconnect must
    # be given it via --run-ref (see _run_reconnect). This is the
    # NON-AUTHORIZING operation reference, not the confirmation token.
    report.note(f"operation reference (pass to reconnect as --run-ref): {envelope.run_ref}")


#: Order states in which a resting protective stop is actually protecting.
#: A CANCEL_PENDING / CANCELLED / FILLED / REJECTED stop protects nothing, so
#: it must never count as coverage (fail closed).
_WORKING_ORDER_STATES: frozenset[OrderState] = frozenset(
    {OrderState.SUBMITTING, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}
)


def _fill_path_entry_id(*, run_date: str, symbol: str, run_ref: str) -> str:
    """The fill-path entry ``client_order_id`` for an operation reference.

    Mirrors ``LifecycleEnvelope.client_order_id`` for ``scenario='fill-path'``:
    ``poi-ibkr-001-<run_date>-fill-path-<symbol>-<run_ref>`` where ``run_ref``
    is the non-authorizing operation reference.
    """
    return f"{CLIENT_ORDER_PREFIX}-{run_date}-fill-path-{symbol}-{run_ref}"


def _current_run_stop_prefix(*, run_date: str, symbol: str, run_ref: str) -> str:
    """The exact ``client_order_id`` prefix of a fill-path operation's stop.

    A protective stop is created only by the ``fill-path`` scenario. The
    ProtectionManager builds ``spec_ref`` as ``protection:<entry_id>:stop:<n>``
    and the harness submits it as ``<entry_id>-<spec_ref>``. So an order
    belongs to the operation identified by ``run_ref`` for ``symbol`` iff its
    id is exactly::

        <entry_id>-protection:<entry_id>:stop:<positive-int>

    where ``entry_id = poi-ibkr-001-<run_date>-fill-path-<symbol>-<run_ref>``.
    Binding run_date + the fill-path scenario + the symbol + **the operation
    reference** + the exact spec_ref shape rejects a stale prior-day stop
    (different date), a stale same-day stop from a run with different
    parameters (different operation reference), a cross-symbol stop, a
    cross-scenario order, and a malformed protective-looking id — none may
    ever mark this symbol protected.
    """
    entry_id = _fill_path_entry_id(run_date=run_date, symbol=symbol, run_ref=run_ref)
    return f"{entry_id}-protection:{entry_id}:stop:"


def _is_current_run_stop(client_order_id: str, prefix: str) -> bool:
    """True iff the id is exactly this run's canonical protective-stop id.

    The suffix after ``:stop:`` must be a positive ASCII integer sequence;
    anything else (empty, non-numeric, another run/symbol/scenario) is
    rejected, so a malformed or foreign protective-looking order never counts.
    """
    if not client_order_id.startswith(prefix):
        return False
    suffix = client_order_id[len(prefix) :]
    return bool(suffix) and suffix.isascii() and suffix.isdigit() and int(suffix) >= 1


def _run_reconnect(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
    run_ref: str | None,
) -> None:
    """Prove state survives a Gateway restart: position + protection intact.

    Read-only: it re-reads what the broker reports after the reconnection
    and reconciles. It submits nothing and cancels nothing — if protection
    were missing, the correct response is an owner decision, not an
    automatic order.

    Protection is matched to the fill-path operation identified by ``run_ref``
    — the deterministic, non-authorizing **operation reference** the owner
    recorded from the fill-path evidence (never the confirmation token) — for
    this symbol (see ``_current_run_stop_prefix``). Mere presence of any
    harness-looking stop never counts: a stale, foreign, cross-symbol,
    cross-operation, ambiguous, or malformed stop leaves the position naked and
    the run fails closed. Without ``run_ref`` the resting stop cannot be bound
    to this fill-path operation from the ``BrokerOrderReceipt`` surface, so the
    scenario fails closed rather than claiming protection. The resting stop's
    QUANTITY cannot be read from a receipt either, so full coverage is never
    asserted from the receipt — it must be owner-confirmed in the Gateway UI.
    """
    positions = tuple(broker.get_positions(envelope.account_alias))
    # Every NON-ZERO position (long OR short). A short is itself an anomaly
    # this scenario must surface, so we never restrict to quantity > 0. Zero-
    # quantity rows are reported separately so a flat account cannot read as
    # though it still held something.
    nonzero = _record_position_breakdown(
        report, positions, symbol=envelope.symbol, label="position_breakdown"
    )
    open_orders = broker.get_open_orders(envelope.account_alias)

    # Binding a resting stop to this fill-path operation requires the
    # operation reference. Without it no stop may count as coverage.
    normalized_ref = run_ref.strip() if run_ref else ""
    report.record(
        "run_reference_provided",
        bool(normalized_ref),
        "given"
        if normalized_ref
        else "missing --run-ref (the fill-path operation reference): a stop"
        " cannot be bound to this operation",
    )
    if normalized_ref:
        stop_prefix = _current_run_stop_prefix(
            run_date=envelope.run_date, symbol=envelope.symbol, run_ref=normalized_ref
        )
        current_run_stops = tuple(
            o
            for o in open_orders
            if _is_current_run_stop(o.client_order_id, stop_prefix)
            and o.state in _WORKING_ORDER_STATES
        )
    else:
        current_run_stops = ()

    # The harness only ever opens a single LONG in its own allowlisted symbol.
    # Anything else — a different symbol, or a SHORT in any symbol including
    # this one — is an anomaly that must fail closed.
    expected_long = tuple(p for p in nonzero if p.symbol == envelope.symbol and p.quantity > 0)
    anomalous = tuple(p for p in nonzero if p not in expected_long)

    report.record(
        "positions_survived_reconnect",
        True,
        f"{len(nonzero)} non-zero position(s) reported after reconnect"
        f" (from {len(positions)} raw row(s))",
    )
    report.record(
        "no_unexpected_position",
        not anomalous,
        "none"
        if not anomalous
        else "unexpected (cross-symbol or short): "
        + ", ".join(f"{p.symbol}:{p.quantity}" for p in anomalous),
    )
    # Ambiguity fails closed: more than one canonical stop for the position is
    # not a clean, verifiable protection state.
    ambiguous = bool(expected_long) and len(current_run_stops) > 1
    report.record(
        "protection_stop_unambiguous",
        not ambiguous,
        "one canonical stop"
        if len(current_run_stops) == 1
        else f"{len(current_run_stops)} canonical stop(s) matched",
    )

    # The run symbol is protected ONLY when exactly one matching working stop
    # for this operation is resting. Zero => naked; >1 => ambiguous. Both fail closed.
    symbol_protected = bool(expected_long) and len(current_run_stops) == 1
    naked = tuple(p for p in expected_long if not symbol_protected)

    report.record(
        "protection_survived_reconnect",
        not naked,
        f"{len(expected_long) - len(naked)} of {len(expected_long)} run-symbol"
        f" position(s) protected by a matching working stop for this operation"
        f" ({len(current_run_stops)} matched, {len(open_orders)} open order(s))",
    )
    if symbol_protected:
        report.note(
            "A resting protective stop cannot expose its quantity through"
            " BrokerOrderReceipt: confirm in the Gateway/TWS UI that the stop"
            " quantity covers the FULL position before relying on it."
        )
    if naked:
        _unprotected_position_abort(
            report,
            f"reconnect found {len(naked)} run-symbol position(s) with no unique"
            f" matching working protective stop for this operation: "
            + ", ".join(f"{p.symbol}:{p.quantity}" for p in naked),
        )
    if ambiguous:
        _unprotected_position_abort(
            report,
            f"{len(current_run_stops)} canonical stops matched the position;"
            " protection is ambiguous and cannot be verified",
        )
    if anomalous:
        _unprotected_position_abort(
            report,
            "unexpected position(s) on the account (cross-symbol or short): "
            + ", ".join(f"{p.symbol}:{p.quantity}" for p in anomalous),
        )
    if not normalized_ref:
        _owner_action_required(
            report,
            "reconnect cannot verify protection without the fill-path operation reference",
            "re-run with --run-ref <the operation reference printed in the fill-path evidence>",
            "until then, treat every open position as UNVERIFIED and confirm its"
            " protective stop by hand in the Gateway/TWS UI",
        )
    _reconcile(
        broker,
        envelope,
        report,
        # Only the matched working stops for this operation are internally expected;
        # any other resting order surfaces as an UNEXPECTED_BROKER_ORDER rather
        # than being silently accepted.
        open_orders=tuple(
            InternalOrderExpectation(
                client_order_id=o.client_order_id,
                expected_state=o.state,
                is_protective=True,
            )
            for o in current_run_stops
        ),
        positions={p.symbol: p.quantity for p in nonzero},
        # Report protected ONLY for a run-symbol position backed by a unique
        # canonical stop; everything else surfaces as a PROTECTION_MISMATCH /
        # POSITION_MISMATCH rather than being masked.
        protected=({p.symbol: p.quantity for p in expected_long} if symbol_protected else {}),
        processed_event_ids=frozenset(_event_id(e) for e in broker.order_events()),
        now=now,
        trigger=ReconciliationTrigger.STARTUP,
    )


def _cancellable_lifecycle_orders(
    broker: BrokerPort, envelope: LifecycleEnvelope, report: LifecycleReport
) -> tuple[tuple[BrokerOrderReceipt, ...], str | None]:
    """The lifecycle orders this API client may actually cancel.

    Global open-order VISIBILITY is not cancellation AUTHORITY: a
    ``reqAllOpenOrders`` row may belong to another API client or to the
    Gateway UI. Only the adapter's same-client recovery may make an order
    cancellable, and it fails closed on any ambiguity. The canonical PoI
    prefix is then applied on top — it is a narrowing filter, never proof of
    ownership on its own.

    Returns ``(orders, refusal)``; a non-``None`` refusal means recovery
    itself was ambiguous and NOTHING may be cancelled.
    """
    if not isinstance(broker, SupportsCancellableRecovery):
        # No restart recovery: fall back to the visible PoI orders. This grants
        # no authority — the port itself refuses to cancel an order it has no
        # mapping for, so in practice only orders THIS process submitted can be
        # cancelled, and a foreign/manual order is refused by the adapter.
        visible = tuple(
            o
            for o in broker.get_open_orders(envelope.account_alias)
            if o.client_order_id.startswith(CLIENT_ORDER_PREFIX)
        )
        report.record(
            "cancellable_recovery",
            True,
            "adapter exposes no same-client recovery; only orders this process"
            f" submitted can be cancelled ({len(visible)} PoI order(s) visible)",
        )
        return visible, None
    try:
        recovered = broker.recover_cancellable_orders(envelope.account_alias)
    except BrokerPortError as error:
        report.record(
            "cancellable_recovery",
            False,
            f"same-client recovery refused — {describe_broker_error(error)};"
            " nothing may be cancelled while the recovered identity is ambiguous",
        )
        return (), error.code.value
    owned = tuple(
        o for o in recovered if o.client_order_id.startswith(CLIENT_ORDER_PREFIX)
    )
    report.record(
        "cancellable_recovery",
        True,
        f"{len(owned)} lifecycle-owned same-client order(s) cancellable"
        f" (of {len(recovered)} recovered for this client id)",
    )
    return owned, None


def _run_cleanup(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_seconds: float,
) -> None:
    """Cancel every lifecycle-owned order this client may cancel. Never flattens.

    Cleanup goes straight to the port: cancelling is the one broker call that
    REDUCES exposure, so it must not depend on minting a new authorization
    (which would be a submit permission). It is CANCEL-ONLY, not a dry run.

    Every eligible order gets exactly ONE cancellation attempt, and a failure
    on one order never prevents the attempt on the next — leaving a second
    order working because the first one failed would be the worst outcome.
    There is still no automatic retry. Cleanup reads clean only when a FRESH
    broker snapshot shows no targeted working order left.
    """
    report.mode_label = CLEANUP_MODE_LABEL
    observed = broker.get_open_orders(envelope.account_alias)
    observed_mine = tuple(
        o for o in observed if o.client_order_id.startswith(CLIENT_ORDER_PREFIX)
    )
    report.record(
        "cleanup_observed_orders",
        True,
        f"{len(observed)} order(s) visible at the broker;"
        f" {len(observed_mine)} carry the PoI prefix",
    )
    targets, recovery_refusal = _cancellable_lifecycle_orders(broker, envelope, report)
    report.record(
        "cleanup_scope",
        True,
        f"{len(targets)} lifecycle order(s) eligible for cancellation by this client",
    )

    attempted: list[str] = []
    confirmed: list[str] = []
    unresolved: list[str] = []
    for receipt in targets:
        client_order_id = receipt.client_order_id
        attempted.append(client_order_id)
        try:
            cancel = broker.cancel_order(
                CancelOrderRequest(
                    client_order_id=client_order_id,
                    correlation_id=f"{CLIENT_ORDER_PREFIX}-cleanup",
                )
            )
        except BrokerPortError as error:
            # One order failing must not abort the sweep: keep making the
            # exposure-reducing attempt for every other eligible order.
            report.record(
                f"cleanup_cancel[{client_order_id}]",
                False,
                f"cancel failed — {describe_broker_error(error)}; continuing"
                " with the remaining eligible orders",
            )
            unresolved.append(client_order_id)
            continue
        report.record(
            f"cleanup_cancel[{client_order_id}]",
            cancel.state in (OrderState.CANCELLED, OrderState.CANCEL_PENDING),
            f"state={cancel.state.value}",
        )
        # CANCEL_PENDING is accepted-but-not-final: wait for the terminal
        # CANCELLED before claiming this order is gone.
        if cancel.state is OrderState.CANCELLED:
            confirmed.append(client_order_id)
            continue
        settled, _seen = _await_state(
            broker,
            client_order_id,
            frozenset({OrderState.CANCELLED}),
            timeout_seconds=timeout_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
        report.record(
            f"cleanup_cancel_confirmed[{client_order_id}]",
            settled is not None,
            "cancellation confirmed by the broker"
            if settled is not None
            else "no CANCELLED confirmation before timeout",
        )
        (confirmed if settled is not None else unresolved).append(client_order_id)

    report.record(
        "cleanup_attempts",
        True,
        f"attempted={attempted or 'none'} confirmed={confirmed or 'none'}"
        f" unresolved={unresolved or 'none'}",
    )

    # Fresh snapshot: PASS only when NO targeted working order remains.
    remaining = broker.get_open_orders(envelope.account_alias)
    remaining_mine = tuple(
        o for o in remaining if o.client_order_id.startswith(CLIENT_ORDER_PREFIX)
    )
    report.record(
        "cleanup_no_working_order_remains",
        not remaining_mine,
        "no PoI working order remains after cleanup"
        if not remaining_mine
        else f"{len(remaining_mine)} PoI order(s) still working:"
        f" {[o.client_order_id for o in remaining_mine]}",
    )
    positions = tuple(broker.get_positions(envelope.account_alias))
    nonzero = _record_position_breakdown(
        report, positions, symbol=envelope.symbol, label="cleanup_position_breakdown"
    )
    if nonzero:
        # Cleanup cancels orders; it NEVER flattens. A surviving position is
        # the owner's to resolve and must be impossible to miss.
        _owner_action_required(
            report,
            "cleanup does not flatten and a position is still open: "
            + ", ".join(f"{p.symbol}:{p.quantity}" for p in nonzero),
            "inspect the position in the IB Gateway/TWS UI (VNC)",
            "close it or place a protective stop by hand",
        )
    if recovery_refusal or unresolved or remaining_mine:
        stuck = sorted({*unresolved, *(o.client_order_id for o in remaining_mine)})
        _owner_action_required(
            report,
            "CLEANUP DID NOT PROVE THE ACCOUNT IS CLEAR — "
            + (
                f"same-client recovery was refused ({recovery_refusal})"
                if recovery_refusal
                else f"one or more PoI orders may still be working: {stuck}"
            ),
            "open the IB Gateway/TWS UI (VNC) and cancel any remaining PoI order"
            " by hand",
            "do NOT re-run a submitting scenario until cleanup reads clean",
            "re-run --scenario cleanup, then --scenario verify-clean, to re-inspect",
        )
        return
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=(),
        # Reconciliation reflects the ACTUAL post-cancel snapshot: a surviving
        # position is reported as held (and flagged above) rather than being
        # asserted away as zero.
        positions={p.symbol: p.quantity for p in nonzero},
        protected={},
        # Cleanup reads the whole event stream, so it has genuinely seen
        # every event it reports as processed.
        processed_event_ids=frozenset(_event_id(e) for e in broker.order_events()),
        now=now,
        trigger=ReconciliationTrigger.EVENT_DRIVEN,
    )


def _run_verify_clean(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
) -> None:
    """Read-only proof that this account has nothing outstanding.

    The scenario-correct verifier for a CANCEL-PATH operation. ``reconnect``
    is the FILL-PATH verifier: it proves that a position and its exact
    protective stop survived a restart, which is why it requires a fill-path
    operation reference. Using it to check a cancel-path run conflates two
    different questions, so cancel-path cleanliness gets its own check here.

    Makes no submission, no cancellation and no flatten call — it only reads.
    """
    report.mode_label = READ_ONLY_MODE_LABEL
    # Each read-only probe is recorded INDEPENDENTLY. A single failing probe
    # used to abort the whole scenario, so a real owner-run reported nothing
    # but `broker error BROKER_UNHEALTHY; no retry` and the owner could not see
    # that the account actually held an unrelated position. A failed probe
    # still records FAIL (fail-closed is unchanged) — it just no longer erases
    # the answers the other probes already obtained.
    open_orders = _probe(
        report, "verify_open_orders_read", lambda: broker.get_open_orders(envelope.account_alias)
    )
    if open_orders is None:
        working: tuple[BrokerOrderReceipt, ...] = ()
        report.record(
            "verify_no_working_lifecycle_order",
            False,
            "UNPROVEN: the open-order snapshot could not be read",
        )
    else:
        lifecycle = tuple(
            o for o in open_orders if o.client_order_id.startswith(CLIENT_ORDER_PREFIX)
        )
        working = tuple(o for o in lifecycle if o.state in _WORKING_ORDER_STATES)
        report.record(
            "verify_no_working_lifecycle_order",
            not working,
            "no PoI lifecycle order is working"
            if not working
            else f"{len(working)} still working: "
            + ", ".join(f"{o.client_order_id}:{o.state.value}" for o in working),
        )
        report.record(
            "verify_foreign_orders_untouched",
            True,
            f"{len(open_orders) - len(lifecycle)} non-PoI order(s) visible; reported"
            " for the owner, never cancelled or modified by this harness",
        )

    positions = _probe(
        report, "verify_positions_read", lambda: tuple(broker.get_positions(envelope.account_alias))
    )
    if positions is None:
        nonzero: tuple[BrokerPosition, ...] = ()
        report.record(
            "verify_no_open_position",
            False,
            "UNPROVEN: the position snapshot could not be read",
        )
    else:
        nonzero = _record_position_breakdown(
            report, positions, symbol=envelope.symbol, label="verify_position_breakdown"
        )
        report.record(
            "verify_no_open_position",
            not nonzero,
            "flat"
            if not nonzero
            # Name every open position explicitly: an unrelated holding must be
            # visible to the owner, not hidden behind a generic broker error.
            else "open: " + ", ".join(f"{p.symbol}:{p.quantity}" for p in nonzero),
        )

    result = _probe(
        report,
        "verify_reconciliation_read",
        lambda: _reconcile(
            broker,
            envelope,
            report,
            open_orders=(),
            positions={},
            protected={},
            processed_event_ids=frozenset(_event_id(e) for e in broker.order_events()),
            now=now,
            trigger=ReconciliationTrigger.PERIODIC,
        ),
    )
    report.record(
        "verify_no_unresolved_broker_state",
        result is not None and not result.findings,
        "UNPROVEN: reconciliation could not be run"
        if result is None
        else "no unresolved broker operation state"
        if not result.findings
        else f"{len(result.findings)} unresolved finding(s): "
        + ", ".join(sorted({f.kind.value for f in result.findings})),
    )
    if working or nonzero or open_orders is None or positions is None:
        _owner_action_required(
            report,
            "verify-clean could not prove the account is clean"
            + (
                f" — open position(s): {', '.join(f'{p.symbol}:{p.quantity}' for p in nonzero)}"
                if nonzero
                else ""
            ),
            "inspect open orders and positions in the IB Gateway/TWS UI (VNC)",
            "run --scenario cleanup to cancel any remaining PoI order",
            "close or protect any open position by hand — this harness never flattens",
        )


def run_lifecycle(
    broker_factory: Callable[[], BrokerPort],
    envelope: LifecycleEnvelope,
    *,
    execute: bool,
    confirm_token: str | None,
    run_ref: str | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    timeout_seconds: float = 30.0,
    journal: OwnerRunJournal | None = None,
    quote_provider: Callable[[], QuoteObservation] | None = None,
) -> LifecycleReport:
    """Run one scenario end to end, fail-closed, with cleanup on error."""
    now_fn = clock or (lambda: datetime.now(tz=UTC))
    sleep_fn = sleep or time.sleep
    # Durations are measured monotonically so a system-clock jump can
    # neither abandon a live order early nor wait far past the timeout.
    # Evidence timestamps stay UTC wall-clock (``now_fn``) — the two are
    # never mixed.
    monotonic_fn = monotonic or time.monotonic
    expected_token = confirmation_token(envelope)
    params_hash = parameters_hash(envelope)

    if execute and envelope.scenario in SUBMITTING_SCENARIOS:
        if confirm_token is None or confirm_token.strip().upper() != expected_token:
            raise LifecycleRefused(
                "--execute requires --confirm with the token printed by a dry run"
                " of these exact parameters; the supplied token does not match"
            )
    if execute and envelope.scenario not in SUBMITTING_SCENARIOS:
        raise LifecycleRefused(
            f"scenario {envelope.scenario!r} never submits; --execute is refused"
        )
    if execute and journal is None:
        # The confirmation token is deterministic, so on its own it is NOT
        # single-use across processes. The durable journal is what makes an
        # execute attempt unrepeatable; without it, refuse to execute.
        raise LifecycleRefused(
            "--execute requires a durable owner-run journal (--journal-dir): the"
            " confirmation token alone can be replayed in a new process"
        )

    report = LifecycleReport(scenario=envelope.scenario, executed=execute)
    if journal is not None and envelope.scenario in SUBMITTING_SCENARIOS:
        # BEFORE any broker call: atomically claim the single execute attempt
        # (or record the dry-run approval). A crash never resets this.
        try:
            if execute:
                record = journal.begin_execute(
                    operation_reference=envelope.run_ref,
                    parameters_hash=params_hash,
                    started_at=now_fn(),
                    harness_version=HARNESS_VERSION,
                )
                report.record(
                    "execute_attempt_claimed",
                    True,
                    f"durable state={record.state.value} for operation"
                    f" {envelope.run_ref}; a second --execute is now refused",
                )
            else:
                journal.record_dry_run(
                    operation_reference=envelope.run_ref,
                    parameters_hash=params_hash,
                    approved_at=now_fn(),
                    harness_version=HARNESS_VERSION,
                )
        except OwnerRunReplayRefused as refusal:
            raise LifecycleRefused(str(refusal)) from refusal
        except OwnerRunJournalError as error:
            raise LifecycleRefused(f"owner-run journal unusable: {error}") from error

    broker = broker_factory()
    connected = False
    agent = ExecutionAgent(broker, clock=now_fn)
    try:
        broker.connect()
        connected = True
        report.record("connect", True, f"{envelope.host}:{envelope.port} (paper, localhost)")
        _preflight(broker, envelope, report)
        if not report.passed and envelope.scenario not in READ_ONLY_SCENARIOS:
            report.note("preflight failed; nothing was submitted (fail closed)")
            return report
        if not report.passed:
            # A READ-ONLY scenario continues after a failed preflight: it can
            # send nothing, and the owner needs the full picture (D-CLOCK-02).
            # The run still reports FAIL — gathering more evidence never turns
            # a failure into a pass.
            report.note(
                "preflight reported a failure; this scenario is read-only, so the"
                " remaining inspections still ran to give the owner the complete"
                " picture. NOTHING was submitted, cancelled or flattened."
            )

        now = now_fn()
        if envelope.scenario == "cleanup":
            _run_cleanup(
                broker,
                envelope,
                report,
                now=now,
                sleep=sleep_fn,
                monotonic=monotonic_fn,
                timeout_seconds=timeout_seconds,
            )
        elif envelope.scenario == "verify-clean":
            _run_verify_clean(broker, envelope, report, now=now)
        elif envelope.scenario == "reconnect":
            _run_reconnect(broker, envelope, report, now=now, run_ref=run_ref)
        elif envelope.scenario == "preflight":
            # Read-only by definition: no what-if, no intent, nothing built.
            report.note("preflight is read-only; no order intent was constructed")
        elif not execute:
            # What-if only. preview_order never places anything, and the
            # intent below is discarded — no authorization is ever minted.
            preview = broker.preview_order(
                OrderIntent(
                    client_order_id=envelope.client_order_id,
                    authorization_ref="dry-run:what-if-only",
                    correlation_id=f"{CLIENT_ORDER_PREFIX}-dry-run",
                    environment=Environment.PAPER,
                    account_alias=envelope.account_alias,
                    symbol=envelope.symbol,
                    side=envelope.side,
                    order_type=OrderType.LIMIT,
                    quantity=envelope.quantity,
                    limit_price=envelope.limit_price,
                )
            )
            report.record(
                "what_if_preview",
                preview.estimated_notional <= MAX_NOTIONAL,
                f"estimated_notional={preview.estimated_notional}"
                f" warnings={list(preview.warnings)}",
            )
            report.note(f"NOTHING WAS SENT. Confirmation token: {expected_token}")
            report.note(
                "To execute, re-run the identical command with"
                f" --execute --confirm {expected_token}"
            )
        elif envelope.scenario == "cancel-path":
            _run_cancel_path(
                broker,
                agent,
                envelope,
                report,
                now=now,
                sleep=sleep_fn,
                monotonic=monotonic_fn,
                timeout_seconds=timeout_seconds,
                quote_provider=quote_provider,
            )
        elif envelope.scenario == "fill-path":
            _run_fill_path(
                broker,
                agent,
                envelope,
                report,
                now=now,
                sleep=sleep_fn,
                monotonic=monotonic_fn,
                timeout_seconds=timeout_seconds,
            )
    except BrokerPortError as error:
        report.record("fail_closed", False, describe_broker_error(error))
        _emergency_cleanup(agent, envelope, report, execute=execute)
    except LifecycleRefused as refusal:
        report.record("fail_closed", False, str(refusal))
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        report.record(
            "fail_closed", False, f"unknown error, failing closed: {type(error).__name__}"
        )
        _emergency_cleanup(agent, envelope, report, execute=execute)
    finally:
        if connected:
            try:
                broker.disconnect()
                report.record("disconnect", True, "clean disconnect")
            except Exception:  # noqa: BLE001
                report.record("disconnect", False, "disconnect failed")
        if journal is not None and execute:
            _journal_outcome(journal, envelope, report, broker, now=now_fn())
    return report


def _journal_outcome(
    journal: OwnerRunJournal,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    broker: BrokerPort,
    *,
    now: datetime,
) -> None:
    """Persist the last known canonical state of an execute attempt.

    Best-effort and never fatal: the exclusivity marker is already durable, so
    a journal write failure here can only lose detail, never re-open the
    operation. Records no token, account id or authorization material.
    """
    try:
        broker_ref: str | None = None
        last_state: str | None = None
        for event in broker.order_events():
            if event.client_order_id == envelope.client_order_id:
                broker_ref = event.broker_order_ref
                last_state = event.state.value
        journal.record_progress(
            operation_reference=envelope.run_ref,
            observed_at=now,
            broker_order_ref=broker_ref,
            last_known_state=last_state,
            # Anything short of a fully passing run leaves the operation
            # requiring reconciliation before it can be considered closed.
            reconciliation_required=not report.passed,
        )
    except (OwnerRunJournalError, Exception):  # noqa: BLE001 - never fatal
        report.note(
            "the owner-run journal could not record the final state; the durable"
            " execute-attempt guard is unaffected and still blocks a replay"
        )


def _emergency_cleanup(
    agent: ExecutionAgent,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    execute: bool,
) -> None:
    """Best-effort cancel of anything this run submitted. Never flattens."""
    if not execute:
        return
    try:
        outcome = agent.cancel(
            envelope.client_order_id, correlation_id=f"{CLIENT_ORDER_PREFIX}-abort"
        )
        report.note(f"abort cleanup: cancel status={outcome.status.value}")
        confirmed = outcome.status is ExecutionStatus.CANCEL_SENT
    except Exception:  # noqa: BLE001
        confirmed = False
    if not confirmed:
        # The harness could not prove it left nothing behind. Everything
        # after this point is the owner's call, by hand.
        _owner_action_required(
            report,
            "the run aborted and the harness could NOT confirm its order was cancelled",
            "inspect open orders AND positions in the IB Gateway/TWS UI (VNC)",
            "cancel any working order from this run by hand",
            "if a position was opened, close it or place a protective stop manually",
        )


def _resolve_paper_account_id(secret_name: str) -> str:
    """Resolve the DU account id for the alias from the PAPER secret store.

    The adapter maps alias -> real broker account id and refuses anything
    that is not a ``DU`` paper account. That id must never reach argv (it
    would be visible in ``ps`` and in shell history), so it is read from
    the environment-scoped secret store the repository already uses —
    ``SECRETS_ROOT`` with no default, PAPER and LIVE physically separate
    (see scripts/poi_tg_user.py for the same wiring). The value is used
    only to construct the adapter and is never printed; every report line
    goes through ``mask_account``.
    """
    from trading_config.secrets import FileSecretStore, SecretError

    secrets_root = os.environ.get("SECRETS_ROOT")
    if not secrets_root:
        raise LifecycleRefused(
            "SECRETS_ROOT is required so the paper account id is read from the"
            " secret store instead of the command line (no default; secrets"
            " never live in the repo)"
        )
    try:
        store = FileSecretStore(Path(secrets_root), Environment.PAPER)
        return store.get(secret_name).reveal()
    except SecretError as error:
        # The message names the reference, never the value.
        raise LifecycleRefused(f"could not resolve the paper account id: {error}") from error


def _load_broker_factory(
    module_name: str,
    *,
    account_alias: str,
    account_secret: str,
    host: str,
    port: int,
    client_id: int,
) -> Callable[[], BrokerPort]:
    """Late, isolated import of the run-time adapter module (never at top).

    The module must expose ``create_broker`` or ``build_paper_adapter``
    with the canonical keyword signature. No broker SDK is imported by
    this harness itself; tests inject a fake factory instead.
    """
    import importlib

    module = importlib.import_module(module_name)
    builder = getattr(module, "create_broker", None) or getattr(module, "build_paper_adapter", None)
    if builder is None or not callable(builder):
        raise LifecycleRefused(
            f"broker module {module_name!r} must expose create_broker() or build_paper_adapter()"
        )
    account_id = _resolve_paper_account_id(account_secret)
    if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
        # Fail here with a masked message rather than letting the adapter
        # raise: this harness must never be pointed at a non-paper account.
        raise LifecycleRefused(
            f"secret {account_secret!r} does not hold a paper account"
            f" ({mask_account(account_id)}); {PAPER_ACCOUNT_PREFIX}* only"
        )

    def factory() -> BrokerPort:
        broker: BrokerPort = builder(
            account_aliases={account_alias: account_id},
            host=host,
            port=port,
            client_id=client_id,
        )
        return broker

    return factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "POI-IBKR-001 Paper order-lifecycle harness. Owner-run only,"
            " localhost + paper port 4002 only, one minimal order at a time."
            " Dry-run by default: --execute additionally requires the"
            " confirmation token printed by a dry run of identical parameters."
        )
    )
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument(
        "--account-alias",
        required=True,
        help=(
            "alias, never a broker account id; the real DU account id is read"
            " from the PAPER secret store via SECRETS_ROOT"
        ),
    )
    parser.add_argument(
        "--account-secret",
        default=DEFAULT_ACCOUNT_SECRET,
        help=(
            "name of the secret under $SECRETS_ROOT/PAPER holding the DU paper"
            f" account id (default: {DEFAULT_ACCOUNT_SECRET})"
        ),
    )
    parser.add_argument("--symbol", default="F", help=f"one of {sorted(ALLOWED_SYMBOLS)}")
    parser.add_argument("--quantity", default="1", help=f"<= {MAX_QUANTITY}")
    parser.add_argument("--limit-price", default=None, help="required for submitting scenarios")
    parser.add_argument(
        "--reference-price",
        default=None,
        help=(
            "current market price. MANDATORY for the cancel path: it bounds how"
            " far the resting limit sits from the market"
        ),
    )
    parser.add_argument(
        "--quote-bid",
        default=None,
        help="observed BID. Mandatory for the cancel path (with --quote-ask/-at/-session)",
    )
    parser.add_argument("--quote-ask", default=None, help="observed ASK")
    parser.add_argument(
        "--quote-at",
        default=None,
        help=(
            "ISO-8601 UTC timestamp of the quote observation; a quote older than"
            f" {MAX_QUOTE_AGE_SECONDS}s may not authorize a submission"
        ),
    )
    parser.add_argument(
        "--quote-session",
        default=None,
        choices=QUOTE_SESSIONS,
        help="market session the quote was observed in; 'closed' never authorizes a submit",
    )
    parser.add_argument(
        "--journal-dir",
        default=None,
        help=(
            "directory holding the DURABLE owner-run attempt journal. Required"
            " for --execute: it makes an execute attempt single-use across"
            " processes and crashes. Never store it in the repository"
        ),
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help=(
            "UTC YYYY-MM-DD scoping the token and the client_order_id"
            " (default: today). Two owner-runs on different days never collide,"
            " and a token from an earlier day cannot be replayed"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1/localhost only")
    parser.add_argument("--port", type=int, default=PAPER_API_PORT, help="paper API port only")
    parser.add_argument("--client-id", type=int, default=7, help="IBKR API client id")
    parser.add_argument("--broker-module", required=True, help="module exposing create_broker()")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="ACTUALLY SEND to the paper broker (requires --confirm)",
    )
    parser.add_argument("--confirm", default=None, help="token printed by the dry run")
    parser.add_argument(
        "--run-ref",
        default=None,
        help=(
            "reconnect only: the fill-path OPERATION REFERENCE printed in the"
            " fill-path evidence (a non-authorizing id, NOT the confirmation"
            " token) that binds this reconnect to that fill-path operation's"
            " protective stop; without it the reconnect fails closed"
        ),
    )
    parser.add_argument("--evidence-out", default=None, help="write the sanitized artifact here")
    return parser


def _decimal(value: str | None, *, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(value)
    except (ValueError, ArithmeticError) as error:
        raise LifecycleRefused(f"not a valid decimal: {value!r}") from error


def _build_quote(args: argparse.Namespace) -> QuoteObservation | None:
    """Build the owner's market observation from the CLI, or ``None``.

    All four quote flags travel together: a partial quote is refused rather
    than silently defaulted, so nothing can be authorized against a half-
    specified market.
    """
    supplied = [args.quote_bid, args.quote_ask, args.quote_at, args.quote_session]
    if all(value is None for value in supplied):
        return None
    if any(value is None for value in supplied):
        raise LifecycleRefused(
            "--quote-bid, --quote-ask, --quote-at and --quote-session must all be"
            " supplied together (a partial quote authorizes nothing)"
        )
    try:
        observed_at = datetime.fromisoformat(str(args.quote_at))
    except ValueError as error:
        raise LifecycleRefused(
            f"--quote-at must be an ISO-8601 timestamp: {args.quote_at!r}"
        ) from error
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return QuoteObservation(
        bid=_decimal(args.quote_bid, default=Decimal("0")),
        ask=_decimal(args.quote_ask, default=Decimal("0")),
        observed_at=observed_at,
        session=str(args.quote_session),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        limit_price = _decimal(args.limit_price, default=Decimal("0"))
        if args.scenario in SUBMITTING_SCENARIOS and limit_price <= 0:
            raise LifecycleRefused("--limit-price is required for a submitting scenario")
        reference = (
            _decimal(args.reference_price, default=Decimal("0"))
            if args.reference_price is not None
            else None
        )
        envelope = LifecycleEnvelope(
            scenario=args.scenario,
            symbol=args.symbol,
            side=OrderSide.BUY,
            quantity=_decimal(args.quantity, default=Decimal("1")),
            limit_price=limit_price if limit_price > 0 else Decimal("1"),
            account_alias=args.account_alias,
            host=args.host,
            port=args.port,
            run_date=args.run_date or datetime.now(tz=UTC).strftime("%Y-%m-%d"),
            reference_price=reference,
            quote=_build_quote(args),
        )
        journal = OwnerRunJournal(Path(args.journal_dir)) if args.journal_dir else None
        factory = _load_broker_factory(
            args.broker_module,
            account_alias=args.account_alias,
            account_secret=args.account_secret,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
        )
    except (LifecycleRefused, ImportError, OwnerRunJournalError) as error:
        print(f"REFUSED: {error}")
        return 2

    try:
        report = run_lifecycle(
            factory,
            envelope,
            execute=args.execute,
            confirm_token=args.confirm,
            run_ref=args.run_ref,
            timeout_seconds=args.timeout_seconds,
            journal=journal,
        )
    except LifecycleRefused as error:
        # Refusals raised before any broker call (token mismatch, replayed
        # execute attempt, missing journal) must not look like a broker result.
        print(f"REFUSED: {error}")
        return 2
    rendered = report.render()
    print(rendered)
    if args.evidence_out is not None:
        Path(args.evidence_out).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
