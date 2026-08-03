"""Read-only account-safety classification for unattended supervision.

Authority: Integration_Specification_v0.6 section 9.5 (reconciliation);
Decision_Contract 12.5 (UNKNOWN is resolved only by reconciliation, never
guessed); Security and Operations spec section 6 (fail closed).

**Why this exists.** The IB Gateway watchdog used to restart the Gateway on
`PROCESS_DOWN` / `API_DOWN` without knowing anything about the ACCOUNT. A
restart while an order is working, or while a position is open, or while the
broker state is unresolved, can orphan that order, sever a run mid-flight and
destroy the evidence needed to reconcile it. A process-liveness signal is not
an account-safety signal, and the two must never be conflated.

This module produces the missing signal — one canonical classification derived
from ONE complete read-only snapshot:

``CLEAN``
    Every check below passed. Only this may allow a policy to consider an
    unattended action.
``NOT_CLEAN``
    A **complete, valid** snapshot proves that an order, a position, an
    unresolved operation state or a blocking reconciliation finding exists.
``UNKNOWN``
    The proof could not be completed: timeout, connection error, partial
    callback set, malformed data, account mismatch, permission problem or any
    inconsistency.

**Connectivity failure is UNKNOWN. It is never CLEAN.** Absence of evidence is
not evidence of absence, and that distinction is the whole point of this
module: a watchdog that cannot reach the broker knows *less*, not more.

**Strictly read-only.** The probe is handed a :class:`ReadOnlyBrokerView` — a
facade that forwards only read methods. ``submit_order``, ``cancel_order`` and
any modify surface are not merely unused, they are **not reachable**: the
facade does not define them and holds its broker privately.

Nothing here connects to anything by itself; the caller injects the view, so
the whole classification is deterministic and testable with fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from trading_broker_core.port import (
    AccountSnapshot,
    BrokerOrderEvent,
    BrokerOrderReceipt,
    BrokerPortError,
    BrokerPosition,
    IntegrationHealth,
)
from trading_contracts.enums import Environment, OrderState

from trading_execution_agent.reconciliation import (
    FindingKind,
    InternalState,
    ReconciliationEngine,
    ReconciliationPolicy,
    ReconciliationReport,
    ReconciliationTrigger,
)

ACCOUNT_SAFETY_VERSION = "account-safety-probe-1.0.0"

#: Endpoint envelope. Identical to the adapter's, restated here so the probe
#: refuses a bad endpoint BEFORE it connects to anything.
ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
LIVE_API_PORT = 4001

#: Order states in which an order is genuinely still working at the broker.
#: Canonical states only — no parallel vocabulary is introduced here.
WORKING_ORDER_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_PENDING,
    }
)

#: Hard bound on any detail string copied into evidence.
DETAIL_MAX_LENGTH = 200


class AccountSafety(StrEnum):
    """The canonical read-only account-safety classification."""

    CLEAN = "CLEAN"
    NOT_CLEAN = "NOT_CLEAN"
    UNKNOWN = "UNKNOWN"


class SafetyReason(StrEnum):
    """Sanitized reason codes. Never contains an account id or a secret."""

    # -- CLEAN --------------------------------------------------------------
    ALL_CHECKS_PASSED = "ALL_CHECKS_PASSED"

    # -- NOT_CLEAN (a complete snapshot proved something exists) ------------
    WORKING_ORDER_PRESENT = "WORKING_ORDER_PRESENT"
    NON_ZERO_POSITION_PRESENT = "NON_ZERO_POSITION_PRESENT"
    UNRESOLVED_STATE_PRESENT = "UNRESOLVED_STATE_PRESENT"
    RECONCILIATION_BLOCKING_FINDING = "RECONCILIATION_BLOCKING_FINDING"

    # -- UNKNOWN (the proof could not be completed) -------------------------
    ENDPOINT_NOT_LOCALHOST = "ENDPOINT_NOT_LOCALHOST"
    ENDPOINT_NOT_PAPER_PORT = "ENDPOINT_NOT_PAPER_PORT"
    ENDPOINT_IS_LIVE_PORT = "ENDPOINT_IS_LIVE_PORT"
    CONNECT_FAILED = "CONNECT_FAILED"
    HEALTH_CHECK_FAILED = "HEALTH_CHECK_FAILED"
    API_ROUND_TRIP_FAILED = "API_ROUND_TRIP_FAILED"
    CURRENT_TIME_FAILED = "CURRENT_TIME_FAILED"
    ACCOUNT_NOT_PAPER = "ACCOUNT_NOT_PAPER"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    OPEN_ORDERS_UNREADABLE = "OPEN_ORDERS_UNREADABLE"
    POSITIONS_UNREADABLE = "POSITIONS_UNREADABLE"
    RECONCILIATION_UNREADABLE = "RECONCILIATION_UNREADABLE"
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
    SNAPSHOT_INCONSISTENT = "SNAPSHOT_INCONSISTENT"
    MALFORMED_DATA = "MALFORMED_DATA"
    DISCONNECT_FAILED = "DISCONNECT_FAILED"
    PROBE_EXCEPTION = "PROBE_EXCEPTION"
    #: The state itself makes the proof impossible (process down, API down,
    #: login incomplete, 2FA pending). Nothing was queried.
    NOT_PROBEABLE_IN_THIS_STATE = "NOT_PROBEABLE_IN_THIS_STATE"


#: Reasons that mean "a complete snapshot proved a problem" rather than
#: "the proof failed". Used to classify and to assert the two never mix.
NOT_CLEAN_REASONS: frozenset[SafetyReason] = frozenset(
    {
        SafetyReason.WORKING_ORDER_PRESENT,
        SafetyReason.NON_ZERO_POSITION_PRESENT,
        SafetyReason.UNRESOLVED_STATE_PRESENT,
        SafetyReason.RECONCILIATION_BLOCKING_FINDING,
    }
)


def sanitize_detail(text: object) -> str:
    """Bound and scrub free text before it reaches evidence or a journal."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > DETAIL_MAX_LENGTH:
        return collapsed[: DETAIL_MAX_LENGTH - 1] + "…"
    return collapsed


@dataclass(frozen=True, slots=True)
class AccountSafetyReport:
    """One complete read-only account-safety determination."""

    safety: AccountSafety
    reasons: tuple[SafetyReason, ...]
    checked_at: datetime
    #: Counts are ``None`` when that part of the snapshot was never obtained —
    #: deliberately NOT 0, so "unknown" can never be read as "none".
    working_orders: int | None = None
    non_zero_positions: int | None = None
    unresolved_states: int | None = None
    reconciled: bool | None = None
    blocking_findings: int | None = None
    connect_ok: bool = False
    api_round_trip_ok: bool = False
    disconnect_ok: bool = False
    detail: str = ""
    probe_version: str = ACCOUNT_SAFETY_VERSION

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware")

    @property
    def permits_unattended_action(self) -> bool:
        """Only a fully proven CLEAN account may allow a policy to act."""
        return self.safety is AccountSafety.CLEAN

    def as_dict(self) -> dict[str, object]:
        """Deterministic, sanitized mapping suitable for JSON evidence."""
        return {
            "safety": self.safety.value,
            "reasons": [reason.value for reason in self.reasons],
            "checked_at": self.checked_at.astimezone(UTC).isoformat(),
            "working_orders": self.working_orders,
            "non_zero_positions": self.non_zero_positions,
            "unresolved_states": self.unresolved_states,
            "reconciled": self.reconciled,
            "blocking_findings": self.blocking_findings,
            "connect_ok": self.connect_ok,
            "api_round_trip_ok": self.api_round_trip_ok,
            "disconnect_ok": self.disconnect_ok,
            "permits_unattended_action": self.permits_unattended_action,
            "detail": self.detail,
            "probe_version": self.probe_version,
        }


def unknown_report(
    reason: SafetyReason,
    *,
    now: datetime,
    detail: str = "",
    **fields: object,
) -> AccountSafetyReport:
    """Build an UNKNOWN report. The fail-closed constructor."""
    return AccountSafetyReport(
        safety=AccountSafety.UNKNOWN,
        reasons=(reason,),
        checked_at=now,
        detail=sanitize_detail(detail),
        **fields,  # type: ignore[arg-type]
    )


# -- the read-only view ------------------------------------------------------


class ReadOnlyBrokerView(Protocol):
    """The ONLY broker surface the probe may touch.

    Deliberately omits ``submit_order``, ``cancel_order``, ``preview_order``
    and every modify/flatten verb. A conforming implementation must not expose
    them either — see :class:`ReadOnlyBrokerFacade`.
    """

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def health_check(self) -> IntegrationHealth: ...

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot: ...

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]: ...

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]: ...

    def order_events(self) -> Iterator[BrokerOrderEvent]: ...

    def reconcile(self, account_alias: str) -> object: ...


#: Verbs that must never be reachable from the probe.
FORBIDDEN_BROKER_VERBS: frozenset[str] = frozenset(
    {
        "submit_order",
        "cancel_order",
        "preview_order",
        "modify_order",
        "replace_order",
        "flatten",
        "flatten_position",
        "recover_cancellable_orders",
    }
)


class ReadOnlyBrokerFacade:
    """Forwards read methods only; the broker itself stays private.

    The point is structural, not conventional: ``facade.cancel_order`` raises
    ``AttributeError`` because the attribute does not exist. Handing the probe
    a full adapter would leave a live order surface one attribute lookup away,
    and "we simply don't call it" is not a safety property.
    """

    __slots__ = ("_broker",)

    def __init__(self, broker: object) -> None:
        for verb in FORBIDDEN_BROKER_VERBS:
            # A view is only read-only if the thing behind it is the adapter we
            # expect; this asserts we are wrapping, not accidentally aliasing.
            if verb in type(self).__dict__:  # pragma: no cover - defensive
                raise TypeError(f"read-only facade must not define {verb}")
        self._broker = broker

    def connect(self) -> None:
        self._broker.connect()  # type: ignore[attr-defined]

    def disconnect(self) -> None:
        self._broker.disconnect()  # type: ignore[attr-defined]

    def health_check(self) -> IntegrationHealth:
        return self._broker.health_check()  # type: ignore[attr-defined,no-any-return]

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        return self._broker.get_account_snapshot(account_alias)  # type: ignore[attr-defined,no-any-return]

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        return self._broker.get_open_orders(account_alias)  # type: ignore[attr-defined,no-any-return]

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        return self._broker.get_positions(account_alias)  # type: ignore[attr-defined,no-any-return]

    def order_events(self) -> Iterator[BrokerOrderEvent]:
        return self._broker.order_events()  # type: ignore[attr-defined,no-any-return]

    def reconcile(self, account_alias: str) -> object:
        return self._broker.reconcile(account_alias)  # type: ignore[attr-defined]


def validate_endpoint(host: str, port: int) -> SafetyReason | None:
    """Endpoint envelope, checked BEFORE any connection is attempted."""
    if port == LIVE_API_PORT:
        return SafetyReason.ENDPOINT_IS_LIVE_PORT
    if host not in ALLOWED_HOSTS:
        return SafetyReason.ENDPOINT_NOT_LOCALHOST
    if port != PAPER_API_PORT:
        return SafetyReason.ENDPOINT_NOT_PAPER_PORT
    return None


def count_working_orders(orders: tuple[BrokerOrderReceipt, ...]) -> int:
    """Working orders only — a cancelled/filled receipt is not exposure."""
    return sum(1 for order in orders if order.state in WORKING_ORDER_STATES)


def count_non_zero_positions(positions: tuple[BrokerPosition, ...]) -> int:
    """Zero-quantity rows are NOT positions (D-CLOCK-02 lesson)."""
    return sum(1 for position in positions if position.quantity != 0)


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Everything the probe needs that is not the broker view itself."""

    account_alias: str
    host: str = "127.0.0.1"
    port: int = PAPER_API_PORT
    policy_ref: str = "account-safety-probe-policy-1"
    #: Optional literal ``reqCurrentTime`` round trip. When supplied it MUST
    #: succeed. When absent, the account-summary round trip below is used —
    #: it is a strictly stronger liveness proof, because it requires the API
    #: to answer a real request and deliver its terminating callback.
    current_time: Callable[[], object] | None = field(default=None, compare=False)


def probe_account_safety(
    view: ReadOnlyBrokerView,
    config: ProbeConfig,
    *,
    now: datetime,
) -> AccountSafetyReport:
    """Classify the account from ONE complete read-only snapshot.

    Every failure path returns UNKNOWN. Only a snapshot that completed every
    check can return CLEAN, and only a snapshot that completed every check but
    found something can return NOT_CLEAN. There is no path from a failed read
    to CLEAN.
    """
    endpoint_problem = validate_endpoint(config.host, config.port)
    if endpoint_problem is not None:
        return unknown_report(
            endpoint_problem,
            now=now,
            detail=f"endpoint refused: {config.host}:{config.port}",
        )

    connect_ok = False
    try:
        view.connect()
        connect_ok = True
    except BrokerPortError as error:
        return unknown_report(
            SafetyReason.CONNECT_FAILED, now=now, detail=f"connect: {error.code.value}"
        )
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        return unknown_report(
            SafetyReason.CONNECT_FAILED, now=now, detail=type(error).__name__
        )

    try:
        return _probe_connected(view, config, now=now, connect_ok=connect_ok)
    finally:
        # A disconnect failure is recorded inside _probe_connected; this is the
        # last-resort guarantee that we never leave a session open.
        try:
            view.disconnect()
        except Exception:  # noqa: BLE001 - best effort, already reported
            pass


def _probe_connected(  # noqa: PLR0911 - one explicit return per failure mode
    view: ReadOnlyBrokerView,
    config: ProbeConfig,
    *,
    now: datetime,
    connect_ok: bool,
) -> AccountSafetyReport:
    alias = config.account_alias
    base: dict[str, object] = {"connect_ok": connect_ok}

    # 1. health
    try:
        health = view.health_check()
    except Exception as error:  # noqa: BLE001
        return unknown_report(
            SafetyReason.HEALTH_CHECK_FAILED, now=now, detail=type(error).__name__, **base
        )
    if not health.healthy:
        return unknown_report(
            SafetyReason.HEALTH_CHECK_FAILED,
            now=now,
            detail=sanitize_detail(health.detail),
            **base,
        )

    # 2. optional literal current-time round trip
    if config.current_time is not None:
        try:
            stamp = config.current_time()
        except Exception as error:  # noqa: BLE001
            return unknown_report(
                SafetyReason.CURRENT_TIME_FAILED, now=now, detail=type(error).__name__, **base
            )
        if stamp is None or not str(stamp).strip():
            return unknown_report(
                SafetyReason.CURRENT_TIME_FAILED, now=now, detail="empty response", **base
            )

    # 3. account snapshot — a real API round trip AND the Paper/DU gate
    try:
        snapshot = view.get_account_snapshot(alias)
    except BrokerPortError as error:
        return unknown_report(
            SafetyReason.API_ROUND_TRIP_FAILED,
            now=now,
            detail=f"account snapshot: {error.code.value}",
            **base,
        )
    except Exception as error:  # noqa: BLE001
        return unknown_report(
            SafetyReason.API_ROUND_TRIP_FAILED, now=now, detail=type(error).__name__, **base
        )
    base["api_round_trip_ok"] = True

    if snapshot.environment is not Environment.PAPER:
        return unknown_report(
            SafetyReason.ACCOUNT_NOT_PAPER,
            now=now,
            detail=f"environment={snapshot.environment.value}",
            **base,
        )
    if snapshot.account_alias != alias:
        # Answering for a different alias means we are not looking at the
        # account we are about to make a decision about.
        return unknown_report(
            SafetyReason.ACCOUNT_MISMATCH, now=now, detail="alias mismatch", **base
        )

    # 4. open orders
    try:
        orders = tuple(view.get_open_orders(alias))
    except BrokerPortError as error:
        return unknown_report(
            SafetyReason.OPEN_ORDERS_UNREADABLE,
            now=now,
            detail=error.code.value,
            **base,
        )
    except Exception as error:  # noqa: BLE001
        return unknown_report(
            SafetyReason.OPEN_ORDERS_UNREADABLE, now=now, detail=type(error).__name__, **base
        )
    try:
        working = count_working_orders(orders)
    except (AttributeError, TypeError) as error:
        return unknown_report(
            SafetyReason.MALFORMED_DATA, now=now, detail=type(error).__name__, **base
        )
    base["working_orders"] = working

    # 5. positions
    try:
        positions = tuple(view.get_positions(alias))
    except BrokerPortError as error:
        return unknown_report(
            SafetyReason.POSITIONS_UNREADABLE, now=now, detail=error.code.value, **base
        )
    except Exception as error:  # noqa: BLE001
        return unknown_report(
            SafetyReason.POSITIONS_UNREADABLE, now=now, detail=type(error).__name__, **base
        )
    try:
        non_zero = count_non_zero_positions(positions)
    except (AttributeError, TypeError) as error:
        return unknown_report(
            SafetyReason.MALFORMED_DATA, now=now, detail=type(error).__name__, **base
        )
    base["non_zero_positions"] = non_zero

    # 6. reconciliation — the only thing that resolves UNKNOWN broker state
    try:
        report = _reconcile(view, config, now=now)
    except BrokerPortError as error:
        return unknown_report(
            SafetyReason.RECONCILIATION_UNREADABLE,
            now=now,
            detail=error.code.value,
            **base,
        )
    except Exception as error:  # noqa: BLE001
        return unknown_report(
            SafetyReason.RECONCILIATION_UNREADABLE,
            now=now,
            detail=type(error).__name__,
            **base,
        )
    unresolved = sum(
        1 for finding in report.findings if finding.kind.value == "UNRESOLVED_UNKNOWN_ORDER"
    )
    blocking = len(report.findings) if report.entry_block_required else 0
    base["unresolved_states"] = unresolved
    base["reconciled"] = report.reconciled
    base["blocking_findings"] = blocking

    # 7. clean disconnect
    disconnect_ok = True
    try:
        view.disconnect()
    except Exception as error:  # noqa: BLE001
        disconnect_ok = False
        detail = type(error).__name__
    base["disconnect_ok"] = disconnect_ok
    if not disconnect_ok:
        return unknown_report(
            SafetyReason.DISCONNECT_FAILED, now=now, detail=detail, **base
        )

    # -- the snapshot is complete; now classify what it proves ---------------
    problems: list[SafetyReason] = []
    if working > 0:
        problems.append(SafetyReason.WORKING_ORDER_PRESENT)
    if non_zero > 0:
        problems.append(SafetyReason.NON_ZERO_POSITION_PRESENT)
    if unresolved > 0:
        problems.append(SafetyReason.UNRESOLVED_STATE_PRESENT)
    if not report.reconciled or report.findings:
        problems.append(SafetyReason.RECONCILIATION_BLOCKING_FINDING)

    if problems:
        return AccountSafetyReport(
            safety=AccountSafety.NOT_CLEAN,
            reasons=tuple(problems),
            checked_at=now,
            detail=sanitize_detail(
                f"working_orders={working} non_zero_positions={non_zero}"
                f" unresolved={unresolved} findings={len(report.findings)}"
            ),
            **base,  # type: ignore[arg-type]
        )

    return AccountSafetyReport(
        safety=AccountSafety.CLEAN,
        reasons=(SafetyReason.ALL_CHECKS_PASSED,),
        checked_at=now,
        detail="complete read-only snapshot; nothing outstanding",
        **base,  # type: ignore[arg-type]
    )


def _reconcile(
    view: ReadOnlyBrokerView, config: ProbeConfig, *, now: datetime
) -> ReconciliationReport:
    """Reconcile against an EMPTY internal expectation.

    The watchdog expects the account to hold nothing, so anything the broker
    reports surfaces as a finding rather than being silently accepted.
    """
    engine = ReconciliationEngine(
        ReconciliationPolicy(
            policy_version=config.policy_ref,
            # Every canonical mismatch blocks: an unattended supervisor has no
            # judgement to apply, so nothing is tolerated.
            blocking_kinds=frozenset(FindingKind),
        )
    )
    internal = InternalState(
        account_alias=config.account_alias,
        environment=Environment.PAPER,
        open_orders=(),
        positions={},
        protected_quantities={},
        processed_fill_event_ids=frozenset(
            f"{event.sequence}:{event.client_order_id}:{event.state.value}"
            for event in view.order_events()
        ),
        as_of=now,
    )
    report: ReconciliationReport = engine.run(
        view,  # type: ignore[arg-type]  # the engine only reads
        internal,
        trigger=ReconciliationTrigger.PERIODIC,
        as_of=now,
    )
    return report


def positions_mapping(positions: tuple[BrokerPosition, ...]) -> Mapping[str, Decimal]:
    """Non-zero positions as ``symbol -> quantity`` (evidence helper)."""
    return {p.symbol: p.quantity for p in positions if p.quantity != 0}


__all__ = [
    "ACCOUNT_SAFETY_VERSION",
    "ALLOWED_HOSTS",
    "DETAIL_MAX_LENGTH",
    "FORBIDDEN_BROKER_VERBS",
    "LIVE_API_PORT",
    "NOT_CLEAN_REASONS",
    "PAPER_API_PORT",
    "WORKING_ORDER_STATES",
    "AccountSafety",
    "AccountSafetyReport",
    "ProbeConfig",
    "ReadOnlyBrokerFacade",
    "ReadOnlyBrokerView",
    "SafetyReason",
    "count_non_zero_positions",
    "count_working_orders",
    "positions_mapping",
    "probe_account_safety",
    "sanitize_detail",
    "unknown_report",
    "validate_endpoint",
]
