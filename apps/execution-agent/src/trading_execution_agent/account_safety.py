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
from dataclasses import dataclass, field, replace
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
    ReconciliationResult,
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
    ORDER_EVENTS_UNREADABLE = "ORDER_EVENTS_UNREADABLE"
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


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """ONE instant of broker truth, captured once and never re-read.

    Every count and every reconciliation finding in a report is derived from a
    single instance of this class. That is the point: if the counts came from
    one round trip and the reconciliation from another, a `CLEAN` verdict would
    describe a moment that never existed — orders could have appeared between
    the two reads, and the probe would have proven nothing.
    """

    account: AccountSnapshot
    open_orders: tuple[BrokerOrderReceipt, ...]
    positions: tuple[BrokerPosition, ...]
    order_events: tuple[BrokerOrderEvent, ...]
    reconciliation: ReconciliationResult


class SnapshotMismatchError(Exception):
    """A consumer asked the replay view for data it did not capture."""


class _SnapshotBrokerView:
    """Replays one captured :class:`BrokerSnapshot`. Holds no connection.

    :class:`ReconciliationEngine` calls ``reconcile()`` and ``order_events()``
    itself. Handing it the live view would issue a SECOND round trip, so the
    findings could describe a different instant from the counts. This view is
    what the engine gets instead: it serves the already-captured data and
    cannot reach the broker, so reconciliation is provably free of extra reads.

    Like :class:`ReadOnlyBrokerFacade` it defines no order verb.
    """

    __slots__ = ("_alias", "_snapshot")

    def __init__(self, snapshot: BrokerSnapshot, account_alias: str) -> None:
        self._snapshot = snapshot
        self._alias = account_alias

    def _check(self, account_alias: str) -> None:
        if account_alias != self._alias:
            raise SnapshotMismatchError(
                "replay asked for an alias that was never captured"
            )

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        self._check(account_alias)
        return self._snapshot.account

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        self._check(account_alias)
        return self._snapshot.open_orders

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        self._check(account_alias)
        return self._snapshot.positions

    def order_events(self) -> Iterator[BrokerOrderEvent]:
        return iter(self._snapshot.order_events)

    def reconcile(self, account_alias: str) -> ReconciliationResult:
        self._check(account_alias)
        return self._snapshot.reconciliation


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

    try:
        view.connect()
    except BrokerPortError as error:
        # Never connected => nothing to close. disconnect() is NOT called here.
        return unknown_report(
            SafetyReason.CONNECT_FAILED, now=now, detail=f"connect: {error.code.value}"
        )
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        return unknown_report(
            SafetyReason.CONNECT_FAILED, now=now, detail=type(error).__name__
        )

    # One connection is open. Exactly one disconnect attempt follows on EVERY
    # path below, including an unexpected exception — and exactly one, because
    # nothing under here disconnects on its own.
    try:
        report = _snapshot_and_classify(view, config, now=now)
    except _ProbeAborted as aborted:
        report = aborted.report
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        report = unknown_report(
            SafetyReason.PROBE_EXCEPTION,
            now=now,
            detail=type(error).__name__,
            connect_ok=True,
        )
    return _close_session(view, report, now=now)


class _ProbeAborted(Exception):
    """Internal control flow: carries the UNKNOWN report that ends the probe.

    Using an exception keeps every read on the happy path readable while still
    guaranteeing that the single disconnect in :func:`probe_account_safety`
    runs — a plain early ``return`` from a helper could not do both.
    """

    def __init__(self, report: AccountSafetyReport) -> None:
        super().__init__(report.reasons[0].value if report.reasons else "aborted")
        self.report = report


def _close_session(
    view: ReadOnlyBrokerView, report: AccountSafetyReport, *, now: datetime
) -> AccountSafetyReport:
    """The ONE disconnect attempt per successful connection.

    A session we could not close is a session we cannot reason about, so a
    disconnect failure downgrades the verdict to UNKNOWN even when every other
    check passed. There is deliberately no path from a failed disconnect to
    CLEAN. The counts already gathered are kept in the structured fields; only
    the reason vocabulary changes, so an UNKNOWN report never carries a
    NOT_CLEAN reason code.
    """
    try:
        view.disconnect()
    except Exception as error:  # noqa: BLE001 - any failure => fail closed
        return replace(
            report,
            safety=AccountSafety.UNKNOWN,
            reasons=(SafetyReason.DISCONNECT_FAILED,),
            disconnect_ok=False,
            detail=sanitize_detail(
                f"disconnect failed after {report.safety.value}: {type(error).__name__}"
            ),
        )
    return replace(report, disconnect_ok=True)


def _snapshot_and_classify(
    view: ReadOnlyBrokerView,
    config: ProbeConfig,
    *,
    now: datetime,
) -> AccountSafetyReport:
    """Liveness, then ONE snapshot, then classification. Never disconnects."""
    alias = config.account_alias
    base: dict[str, object] = {"connect_ok": True}

    _check_liveness(view, config, now=now, base=base)
    snapshot = _capture_snapshot(view, alias, now=now, base=base)
    base["api_round_trip_ok"] = True
    _assert_coherent(snapshot, alias, now=now, base=base)

    # Counts come from the captured snapshot, never from a fresh read.
    try:
        working = count_working_orders(snapshot.open_orders)
        non_zero = count_non_zero_positions(snapshot.positions)
    except (AttributeError, TypeError) as error:
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.MALFORMED_DATA, now=now, detail=type(error).__name__, **base
            )
        ) from error
    base["working_orders"] = working
    base["non_zero_positions"] = non_zero

    # Reconciliation reads the SAME snapshot through a replay view, so it
    # cannot reach the broker and cannot observe a later instant.
    try:
        report = _reconcile(snapshot, config, now=now)
    except SnapshotMismatchError as error:
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.SNAPSHOT_INCONSISTENT, now=now, detail=str(error), **base
            )
        ) from error
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.RECONCILIATION_UNREADABLE,
                now=now,
                detail=type(error).__name__,
                **base,
            )
        ) from error

    unresolved = sum(
        1 for finding in report.findings if finding.kind.value == "UNRESOLVED_UNKNOWN_ORDER"
    )
    base["unresolved_states"] = unresolved
    base["reconciled"] = report.reconciled
    base["blocking_findings"] = len(report.findings) if report.entry_block_required else 0

    # -- the snapshot is complete and coherent; classify what it proves ------
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
        detail="one complete read-only snapshot; nothing outstanding",
        **base,  # type: ignore[arg-type]
    )


def _check_liveness(
    view: ReadOnlyBrokerView,
    config: ProbeConfig,
    *,
    now: datetime,
    base: dict[str, object],
) -> None:
    """Health, then the optional literal current-time round trip."""
    try:
        health = view.health_check()
    except Exception as error:  # noqa: BLE001
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.HEALTH_CHECK_FAILED,
                now=now,
                detail=type(error).__name__,
                **base,
            )
        ) from error
    if not health.healthy:
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.HEALTH_CHECK_FAILED,
                now=now,
                detail=sanitize_detail(health.detail),
                **base,
            )
        )

    if config.current_time is None:
        return
    try:
        stamp = config.current_time()
    except Exception as error:  # noqa: BLE001
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.CURRENT_TIME_FAILED,
                now=now,
                detail=type(error).__name__,
                **base,
            )
        ) from error
    if stamp is None or not str(stamp).strip():
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.CURRENT_TIME_FAILED, now=now, detail="empty response", **base
            )
        )


def _capture_snapshot(
    view: ReadOnlyBrokerView,
    alias: str,
    *,
    now: datetime,
    base: dict[str, object],
) -> BrokerSnapshot:
    """Read every part of the broker state EXACTLY ONCE.

    Each read appears here once and nowhere else in the module; nothing
    downstream is given the live view again. That is what makes "one snapshot"
    a structural property rather than a convention.
    """

    def read(reason: SafetyReason, call: Callable[[], object]) -> object:
        try:
            return call()
        except BrokerPortError as error:
            raise _ProbeAborted(
                unknown_report(reason, now=now, detail=error.code.value, **base)
            ) from error
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise _ProbeAborted(
                unknown_report(reason, now=now, detail=type(error).__name__, **base)
            ) from error

    account = read(
        SafetyReason.API_ROUND_TRIP_FAILED, lambda: view.get_account_snapshot(alias)
    )
    assert isinstance(account, AccountSnapshot)  # noqa: S101 - port contract

    # The Paper/DU gate runs before anything else is read: if this is not the
    # account we think it is, further reads would only add false confidence.
    if account.environment is not Environment.PAPER:
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.ACCOUNT_NOT_PAPER,
                now=now,
                detail=f"environment={account.environment.value}",
                **base,
            )
        )
    if account.account_alias != alias:
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.ACCOUNT_MISMATCH, now=now, detail="alias mismatch", **base
            )
        )

    orders = read(
        SafetyReason.OPEN_ORDERS_UNREADABLE, lambda: tuple(view.get_open_orders(alias))
    )
    positions = read(
        SafetyReason.POSITIONS_UNREADABLE, lambda: tuple(view.get_positions(alias))
    )
    events = read(SafetyReason.ORDER_EVENTS_UNREADABLE, lambda: tuple(view.order_events()))
    reconciliation = read(
        SafetyReason.RECONCILIATION_UNREADABLE, lambda: view.reconcile(alias)
    )
    if not isinstance(reconciliation, ReconciliationResult):
        raise _ProbeAborted(
            unknown_report(
                SafetyReason.MALFORMED_DATA,
                now=now,
                detail=f"reconcile returned {type(reconciliation).__name__}",
                **base,
            )
        )

    return BrokerSnapshot(
        account=account,
        open_orders=orders,  # type: ignore[arg-type]
        positions=positions,  # type: ignore[arg-type]
        order_events=events,  # type: ignore[arg-type]
        reconciliation=reconciliation,
    )


def _assert_coherent(
    snapshot: BrokerSnapshot,
    alias: str,
    *,
    now: datetime,
    base: dict[str, object],
) -> None:
    """Prove the captured parts describe ONE instant of ONE account.

    ``reconcile()`` returns its own account/orders/positions, so the two reads
    overlap — and that overlap is a free consistency check. If the broker
    changed between them, or answered for a different account, the parts
    disagree and the snapshot is not a snapshot. Mixed-time data must never be
    classified; it becomes UNKNOWN.
    """

    def abort(reason: SafetyReason, detail: str) -> _ProbeAborted:
        return _ProbeAborted(unknown_report(reason, now=now, detail=detail, **base))

    result = snapshot.reconciliation
    if result.account_alias != alias or result.snapshot.account_alias != alias:
        raise abort(SafetyReason.ACCOUNT_MISMATCH, "reconciliation alias mismatch")
    if result.snapshot.environment is not Environment.PAPER:
        raise abort(
            SafetyReason.ACCOUNT_NOT_PAPER,
            f"reconciliation environment={result.snapshot.environment.value}",
        )

    order_ids = [order.client_order_id for order in snapshot.open_orders]
    if len(set(order_ids)) != len(order_ids):
        raise abort(SafetyReason.SNAPSHOT_INCONSISTENT, "duplicate order in snapshot")
    symbols = [position.symbol for position in snapshot.positions]
    if len(set(symbols)) != len(symbols):
        raise abort(SafetyReason.SNAPSHOT_INCONSISTENT, "duplicate position in snapshot")

    # The cross-check that catches a broker mutating mid-probe.
    direct_orders = {(o.client_order_id, o.state) for o in snapshot.open_orders}
    reconciled_orders = {(o.client_order_id, o.state) for o in result.open_orders}
    if direct_orders != reconciled_orders:
        raise abort(
            SafetyReason.SNAPSHOT_INCONSISTENT,
            "open orders differ between reads: the broker changed mid-probe",
        )
    direct_positions = {p.symbol: p.quantity for p in snapshot.positions if p.quantity != 0}
    reconciled_positions = {
        p.symbol: p.quantity for p in result.positions if p.quantity != 0
    }
    if direct_positions != reconciled_positions:
        raise abort(
            SafetyReason.SNAPSHOT_INCONSISTENT,
            "positions differ between reads: the broker changed mid-probe",
        )


def _reconcile(
    snapshot: BrokerSnapshot, config: ProbeConfig, *, now: datetime
) -> ReconciliationReport:
    """Reconcile the CAPTURED snapshot against an EMPTY internal expectation.

    The watchdog expects the account to hold nothing, so anything the broker
    reports surfaces as a finding rather than being silently accepted.

    The engine is handed :class:`_SnapshotBrokerView`, not the live view: it
    calls ``reconcile()`` and ``order_events()`` itself, and those must resolve
    to the data already captured. No second broker read happens here.
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
            for event in snapshot.order_events
        ),
        as_of=now,
    )
    report: ReconciliationReport = engine.run(
        _SnapshotBrokerView(snapshot, config.account_alias),  # type: ignore[arg-type]
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
