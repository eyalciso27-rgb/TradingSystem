"""Durable pre/post-submit audit for one execution decision.

The readiness design has always had an ``AuditSink`` seam and no production
implementation behind it, which made "the decision was recorded" a claim about
a test double. This binds it to the audit infrastructure the repository already
has, rather than growing a parallel JSON-file subsystem beside it:

* the **tamper-evident chain** (``audit_events``) gets one appended, hash-linked
  record per persisted decision, carrying the decision identity and a redacted
  one-line summary. Append-only, and any later edit or deletion is detectable;
* the **content-addressed object store** (``raw_objects`` semantics) holds the
  full decision payload — every gate outcome, the quote, the risk result, the
  plan — keyed by its own sha256. The chain row references it, so the heavy
  payload never bloats the chain and the reference cannot silently point at
  different bytes than were audited.

**This sink owns its transactions, and that is the whole point.** It takes an
``Engine``, not a connection, and every append runs inside its own
``engine.begin()`` block. ``persist`` therefore returns a reference only after
a COMMIT has succeeded.

An earlier version took a caller's ``Connection`` and appended "inside the
caller's transaction" — which meant ``persist`` could return a chain hash, the
order could go to the broker, and the process could die before anyone committed,
losing the supposedly durable pre-submit record. The invariant is *no broker
submit until the pre-submit audit is durably committed*, and a reference handed
back from an uncommitted transaction does not establish it. Nothing about the
append-only chain semantics changed; only who owns the commit.

The split between payload and chain matters too: the payload is written *first*,
then the chain row is committed. A crash between them leaves an unreferenced
object, which is harmless. The reverse order would leave a committed chain row
pointing at nothing, which is indistinguishable from tampering.

``can_persist`` is a real probe, not a constant, and it leaves no transaction
open. Readiness must be able to refuse *before* a decision is made because audit
is down, and a sink that answers "yes" and then throws has told the gate
nothing.

No secrets: the chain's own validation rejects credential-shaped detail, and
only references, identifiers and market observations are ever written here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, select
from trading_contracts.enums import Environment
from trading_contracts.execution_fingerprint import (
    MANDATORY_PREPARED_FIELDS,
    PREPARED_ORDER_FIELDS,
    PROTECTIVE_DISCRIMINATOR_FIELD,
    PROTECTIVE_ORDER_FIELDS,
    REQUEST_IDENTITY_FIELDS,
)

from trading_persistence.audit import append_audit_record
from trading_persistence.models import AuditEvent

EXECUTION_AUDIT_VERSION = "execution-audit-1.0.0"

#: Chain actions, one per lifecycle stage. The distinction is load-bearing.
#:
#: An earlier version recorded EVERY evaluation as PRE_SUBMIT, including
#: decisions refused long before submission -- a halt, a risk veto, an
#: authorization denial. On restart, "PRE_SUBMIT with no outcome" was then read
#: as "the broker may already hold this order", so a decision that was refused
#: before anything was sent replayed as NEEDS_RECONCILIATION. That is false, and
#: it turns every routine refusal into an operational incident.
#:
#: Only ``ACTION_SUBMIT_PREPARED`` is the point of no return: it is written
#: after authorization and immediately before the submission path, so a crash
#: between it and an outcome genuinely means the broker state is unknown.
ACTION_EVALUATED = "EXECUTION_EVALUATED"
ACTION_SUBMIT_PREPARED = "EXECUTION_SUBMIT_PREPARED"
ACTION_OUTCOME = "EXECUTION_OUTCOME"

#: Retained so an existing chain written by the previous naming still reads
#: back. Never written any more.
ACTION_PRE_SUBMIT = "EXECUTION_PRE_SUBMIT"
ACTION_POST_SUBMIT = "EXECUTION_POST_SUBMIT"

#: Which actions mean "the broker may already hold this order".
AMBIGUOUS_ACTIONS: frozenset[str] = frozenset(
    {ACTION_SUBMIT_PREPARED, ACTION_PRE_SUBMIT}
)

#: Which actions carry a terminal broker answer.
TERMINAL_ACTIONS: frozenset[str] = frozenset({ACTION_OUTCOME, ACTION_POST_SUBMIT})

#: The chain's ``detail`` column is a short operator-readable summary; the full
#: record lives in the object store. Bounded so one decision cannot dominate the
#: chain, and truncation is visible rather than silent.
MAX_DETAIL_CHARS = 400


class ObjectStore(Protocol):
    """Content-addressed payload storage (``FilesystemObjectStorage`` and co.)."""

    def put(self, data: bytes) -> object: ...

    def get(self, ref: str) -> bytes: ...

    def exists(self, ref: str) -> bool: ...


class ExecutionAuditError(Exception):
    """The decision could not be durably recorded. Never a reason to submit."""


def canonical_payload(record: Mapping[str, object]) -> bytes:
    """Deterministic bytes for one decision record.

    Sorted keys and tight separators so the same decision always hashes to the
    same reference — the property that makes the object store idempotent and a
    retry cheap instead of duplicative.
    """
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")


def summarize(record: Mapping[str, object]) -> str:
    """A one-line, bounded, secret-free summary for the chain row."""
    parts = [
        f"readiness={record.get('trading_readiness', 'UNKNOWN')}",
        f"mode={record.get('mode', 'UNKNOWN')}",
        f"symbol={record.get('symbol', 'UNKNOWN')}",
        f"checkpoint={record.get('checkpoint', 'UNKNOWN')}",
    ]
    blockers = record.get("blockers")
    if isinstance(blockers, list | tuple) and blockers:
        parts.append(f"blockers={list(blockers)}")
    outcome = record.get("submission_outcome")
    if outcome:
        parts.append(f"outcome={outcome}")
    detail = " ".join(" ".join(str(part).split()) for part in parts)
    if len(detail) > MAX_DETAIL_CHARS:
        detail = detail[: MAX_DETAIL_CHARS - 3] + "..."
    return detail


class DurableExecutionAuditSink:
    """``AuditSink`` backed by the tamper-evident chain and the object store.

    One instance serves one decision path. ``persist`` returns the chain hash,
    which is the audit reference the execution path carries forward: it
    identifies the record, and it changes if anything in the record changes.
    """

    def __init__(
        self,
        engine: Engine,
        objects: ObjectStore,
        *,
        environment: Environment,
        actor_ref: str = "execution-service",
    ) -> None:
        # An ENGINE, deliberately. Handing this a live connection would put the
        # commit in someone else's hands, and "durable" would mean "durable if
        # the caller remembers".
        self._engine = engine
        self._objects = objects
        self._environment = environment
        self._actor_ref = actor_ref

    # -- the readiness AuditSink protocol -------------------------------------

    def can_persist(self) -> bool:
        """Probe the real dependencies. A guess here is worth nothing.

        Both halves are checked because either can be down independently, and
        discovering that at submit time is exactly what this must prevent.
        """
        try:
            # `with` closes the connection and its implicit transaction; a probe
            # that leaks an open transaction would hold locks for the life of
            # the process.
            with self._engine.connect() as connection:
                connection.execute(
                    select(AuditEvent.sequence_no)
                    .order_by(AuditEvent.sequence_no.desc())
                    .limit(1)
                ).one_or_none()
            probe = canonical_payload({"probe": EXECUTION_AUDIT_VERSION})
            stored = self._objects.put(probe)
        except Exception:  # noqa: BLE001 - any failure means "cannot persist"
            return False
        return bool(getattr(stored, "ref", None))

    def persist(self, record: Mapping[str, object]) -> str:
        """Record an evaluation. COMMITTED before it returns.

        This is the ordinary path and it covers refusals too: a decision that
        was halted, vetoed or denied is still a decision worth recording. It is
        deliberately NOT the point of no return -- see ``persist_prepared``.
        """
        return self._append(record, action=ACTION_EVALUATED)

    def persist_prepared(self, record: Mapping[str, object]) -> str:
        """THE point of no return: authorized, and about to reach the broker.

        Committed before the submission path is entered, so a crash after this
        and before an outcome means exactly one thing: the broker may hold the
        order, and only reconciliation can say.
        """
        return self._append(record, action=ACTION_SUBMIT_PREPARED)

    # -- post-submit ----------------------------------------------------------

    def persist_outcome(self, record: Mapping[str, object]) -> str:
        """Record what the broker answered. NEVER replaces the pre-submit row.

        Appended as its own chained record: the earlier records stated what we
        decided before acting, and rewriting them afterwards would destroy the
        one thing they exist to prove.
        """
        return self._append(record, action=ACTION_OUTCOME)

    # -- internals ------------------------------------------------------------

    def _append(self, record: Mapping[str, object], *, action: str) -> str:
        payload = canonical_payload(record)
        try:
            stored = self._objects.put(payload)
            payload_ref = str(getattr(stored, "ref", "") or "")
        except Exception as error:  # noqa: BLE001
            raise ExecutionAuditError(
                f"decision payload could not be stored ({type(error).__name__})"
            ) from error
        if not payload_ref:
            raise ExecutionAuditError("object store returned no reference")

        occurred_raw = record.get("evaluated_at")
        occurred_at = _aware(occurred_raw) or datetime.now(tz=UTC)
        try:
            # One append, one transaction, committed on exit. A failure to
            # commit propagates as ExecutionAuditError, which the execution path
            # treats as "not recorded" -- and therefore as "do not submit".
            with self._engine.begin() as connection:
                chain_hash = append_audit_record(
                    connection,
                    actor_ref=self._actor_ref,
                    action=action,
                    environment=self._environment,
                    occurred_at=occurred_at,
                    target_ref=_target_ref(record),
                    detail=summarize(record),
                    payload_ref=payload_ref,
                    correlation_id=_uuid_or_none(record.get("correlation_id")),
                )
                # A second chained record under the ORDER id, in the SAME
                # transaction, so the lifecycle is findable from either
                # identity and the two can never disagree about existing.
                order_ref = order_index_ref(record)
                if order_ref and order_ref != _target_ref(record):
                    append_audit_record(
                        connection,
                        actor_ref=self._actor_ref,
                        action=action,
                        environment=self._environment,
                        occurred_at=occurred_at,
                        target_ref=order_ref,
                        detail=summarize(record),
                        payload_ref=payload_ref,
                        correlation_id=_uuid_or_none(record.get("correlation_id")),
                    )
                return chain_hash
        except Exception as error:  # noqa: BLE001
            raise ExecutionAuditError(
                f"decision could not be committed ({type(error).__name__})"
            ) from error


def _target_ref(record: Mapping[str, object]) -> str | None:
    """The decision this record belongs to, so failures stay queryable."""
    for key in ("decision_id", "client_order_id", "security_id"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def order_index_ref(record: Mapping[str, object]) -> str | None:
    """The ORDER id this record belongs to, indexed alongside the decision.

    Written as a second chained record so a lookup by order id finds the same
    lifecycle. Without it, a client order id reused by a DIFFERENT decision
    after a restart is invisible: querying under the new decision finds nothing,
    and the collision only becomes apparent at the broker.
    """
    value = record.get("client_order_id")
    return str(value) if value else None


def _aware(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def _uuid_or_none(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


class ExecutionHistoryUnreadableError(Exception):
    """A durable record exists and its content could not be read.

    Distinct from "no history": the difference between "nothing happened" and
    "something happened and we cannot see what" is the difference between
    proceeding safely and submitting a duplicate.
    """


@dataclass(frozen=True, slots=True)
class ExecutionHistoryRecord:
    """One durable lifecycle record, with the payload that produced it."""

    action: str
    chain_hash: str
    payload: Mapping[str, object]


class DurableExecutionHistoryReader:
    """The read side of the chain: what happened to a decision, across restarts.

    In-process idempotency dies with the process, so a replayed decision after a
    crash would otherwise submit a second time.

    It returns the stored PAYLOADS, not just action names. An earlier version
    returned only the actions, and the caller then treated the existence of a
    terminal record as "SUBMITTED" -- but a terminal record may say REFUSED,
    FAILED or NEEDS_REVIEW just as easily, and converting all of them into
    SUBMITTED invents an order that never existed.

    It opens and closes its own connection per query for the same reason the
    sink owns its own transactions: a long-lived connection held by a
    long-running service is a lock waiting to happen.
    """

    def __init__(self, engine: Engine, objects: ObjectStore) -> None:
        self._engine = engine
        self._objects = objects

    def records_for(self, decision_id: str) -> tuple[ExecutionHistoryRecord, ...]:
        return self._records(decision_id)

    def records_for_order(self, client_order_id: str) -> tuple[ExecutionHistoryRecord, ...]:
        """Records filed under an ORDER id, whatever decision produced them.

        Looking up only by decision cannot see a collision: decision A prepares
        order X, the process restarts, decision B arrives reusing order X, and a
        query under B finds nothing. The order id is indexed too, so the
        collision is visible from either side.
        """
        return self._records(client_order_id)

    def _records(self, target_ref: str) -> tuple[ExecutionHistoryRecord, ...]:
        with self._engine.connect() as connection:
            rows = load_decision_records(connection, target_ref)
        records: list[ExecutionHistoryRecord] = []
        for action, chain_hash, payload_ref in rows:
            if not payload_ref:
                raise ExecutionHistoryUnreadableError(
                    f"{action} record {chain_hash} references no payload;"
                    " the execution it describes cannot be identified"
                )
            # An unreadable payload used to become an empty dict, which then
            # looked like a record whose fingerprint and outcome were simply
            # absent -- so a terminal record with a missing object would have
            # been reconstructed as a refusal, and a prepared record would have
            # lost the identity that makes a replay safe. Missing object,
            # failed integrity check and undecodable JSON are all the same
            # answer: we cannot read this, so we must not act on it.
            try:
                raw = self._objects.get(payload_ref)
            except Exception as error:  # noqa: BLE001
                raise ExecutionHistoryUnreadableError(
                    f"{action} record {chain_hash} references payload"
                    f" {payload_ref} which could not be retrieved"
                    f" ({type(error).__name__})"
                ) from error
            try:
                payload = json.loads(raw)
            except ValueError as error:
                raise ExecutionHistoryUnreadableError(
                    f"{action} record {chain_hash} has an undecodable payload"
                ) from error
            if not isinstance(payload, dict):
                raise ExecutionHistoryUnreadableError(
                    f"{action} record {chain_hash} payload is not an object"
                )
            _require_readable(action, chain_hash, payload)
            records.append(
                ExecutionHistoryRecord(
                    action=action, chain_hash=chain_hash, payload=payload
                )
            )
        # The lifecycle must agree with itself before anyone reads it: a
        # terminal record that resolves a different order than the one prepared
        # is corruption, and a corrupt lifecycle is unreadable, not resolvable.
        require_consistent_lifecycle(records)
        return tuple(records)


#: Every lifecycle record that names an execution must carry these. NOT
#: restated here: the writer's fingerprint is built from the SAME contract, and
#: two tuples that happen to agree today are not a contract. A field added to
#: the writer's identity and forgotten here would mean this reader validated an
#: identity the writer no longer produces, and nothing would notice.
REQUIRED_IDENTITY_FIELDS: tuple[str, ...] = REQUEST_IDENTITY_FIELDS

#: A record that describes a prepared submission additionally describes the
#: frozen broker-bound order. Without these a replay cannot distinguish a
#: repeat from a change.
REQUIRED_PREPARED_FIELDS: tuple[str, ...] = MANDATORY_PREPARED_FIELDS


def _require_readable(
    action: str, chain_hash: str, payload: Mapping[str, object]
) -> None:
    """A lifecycle record must carry the facts its action promises.

    A prepared record without an identity cannot be compared to a replay, and a
    terminal record without an outcome cannot be reconstructed -- reading either
    as "field absent" would turn an integrity failure into a confident wrong
    answer.
    """
    if action in AMBIGUOUS_ACTIONS or action in TERMINAL_ACTIONS:
        fingerprint = payload.get("execution_fingerprint")
        if not isinstance(fingerprint, dict):
            raise ExecutionHistoryUnreadableError(
                f"{action} record {chain_hash} carries no execution fingerprint;"
                " the execution it describes cannot be identified"
            )
        # A NON-EMPTY dict is not a fingerprint. ``{"junk": "x"}`` used to pass
        # here, then fail to parse into an ExecutionFingerprint, and be silently
        # skipped by the conflict check -- so a malformed identity behaved
        # exactly like no identity at all, which is the failure mode this
        # validation exists to prevent.
        missing = [
            field
            for field in REQUIRED_IDENTITY_FIELDS
            if not str(fingerprint.get(field, "")).strip()
        ]
        if missing:
            raise ExecutionHistoryUnreadableError(
                f"{action} record {chain_hash} has an incomplete execution"
                f" fingerprint (missing {missing}); a malformed identity must"
                " never be treated as an absent one"
            )
        if action in AMBIGUOUS_ACTIONS or _describes_a_submission(payload):
            # The point of no return must carry the FROZEN order, or a replay
            # cannot tell a repeat from a changed one -- and so must the
            # terminal record that resolves it, or the lifecycle ends without
            # ever stating which order it was that resolved.
            missing_order = [
                field
                for field in REQUIRED_PREPARED_FIELDS
                if not str(fingerprint.get(field, "")).strip()
            ]
            if missing_order:
                raise ExecutionHistoryUnreadableError(
                    f"{action} record {chain_hash} does not describe the frozen"
                    f" order (missing {missing_order}); the prepared execution"
                    " cannot be compared to a replay"
                )
            # A record that declares a protective mechanism (ADR-0019) MUST name
            # the protective order and its quantity, or its claim of protection
            # cannot be compared to a replay -- and a bracket that cannot be
            # verified is exactly the one that must be reconciled, not replayed.
            if str(fingerprint.get(PROTECTIVE_DISCRIMINATOR_FIELD, "")).strip():
                missing_protective = [
                    field
                    for field in PROTECTIVE_ORDER_FIELDS
                    if not str(fingerprint.get(field, "")).strip()
                ]
                if missing_protective:
                    raise ExecutionHistoryUnreadableError(
                        f"{action} record {chain_hash} declares a protective"
                        f" mechanism but is missing {missing_protective}; a"
                        " protected entry whose structure cannot be identified"
                        " must be reconciled, never replayed"
                    )
    if action in TERMINAL_ACTIONS and not payload.get("submission_outcome"):
        raise ExecutionHistoryUnreadableError(
            f"terminal record {chain_hash} carries no submission outcome; it"
            " must never be reconstructed as a refusal just because the"
            " outcome string is absent"
        )


#: Outcomes that mean a submission was actually attempted, so the record must
#: describe the order that was attempted. A record whose outcome says nothing
#: was ever sent is exempt only when no order was prepared at all.
_ATTEMPTED_OUTCOMES = frozenset({"SUBMITTED", "REFUSED", "FAILED", "NEEDS_REVIEW", "NOT_SENT"})


def _describes_a_submission(payload: Mapping[str, object]) -> bool:
    """Does this terminal record resolve a prepared submission?"""
    outcome = str(payload.get("submission_outcome", "")).strip()
    return outcome in _ATTEMPTED_OUTCOMES


#: The frozen order AND its protective structure: both are sealed at
#: SUBMIT_PREPARED, and a terminal record that resolves either differently than
#: was prepared is corruption. A protective stop that moved, resized or vanished
#: between prepared and terminal is the safety property this exists to catch.
_SEALED_ORDER_FIELDS: tuple[str, ...] = (*PREPARED_ORDER_FIELDS, *PROTECTIVE_ORDER_FIELDS)


def _prepared_order(fingerprint: Mapping[str, object]) -> dict[str, str]:
    """The frozen-order half of a fingerprint (order + protection), as strings."""
    return {
        field: str(fingerprint.get(field, "")).strip()
        for field in _SEALED_ORDER_FIELDS
    }


def require_consistent_lifecycle(
    records: Sequence[ExecutionHistoryRecord],
) -> None:
    """PREPARED and its terminal resolution must describe the SAME order.

    The writer carries the prepared fingerprint into the outcome record, which
    is correct -- and not something a reader may depend on. A durable store
    whose correctness rests on the current writer always behaving is a store
    that cannot detect the writer misbehaving, and "the outcome says 11 shares
    where the prepared marker said 10" is exactly the corruption a replay must
    refuse rather than resolve.

    Raises rather than returning a verdict: an inconsistent lifecycle is
    unreadable, which means reconciliation and zero submits.
    """
    prepared: dict[str, str] | None = None
    for record in records:
        if record.action not in AMBIGUOUS_ACTIONS:
            continue
        fingerprint = record.payload.get("execution_fingerprint")
        if isinstance(fingerprint, dict):
            prepared = _prepared_order(fingerprint)
            break
    if prepared is None:
        return
    for record in records:
        if record.action not in TERMINAL_ACTIONS:
            continue
        fingerprint = record.payload.get("execution_fingerprint")
        if not isinstance(fingerprint, dict):
            continue
        terminal = _prepared_order(fingerprint)
        differing = [
            field
            for field in _SEALED_ORDER_FIELDS
            if prepared[field] != terminal[field]
        ]
        if differing:
            raise ExecutionHistoryUnreadableError(
                f"terminal record {record.chain_hash} resolves a DIFFERENT order"
                f" than the one that was prepared (differs in {differing});"
                " a lifecycle that cannot agree with itself about which order"
                " it was must be reconciled, never replayed"
            )


def load_decision_records(
    connection: Connection, decision_id: str
) -> list[tuple[str, str, str | None]]:
    """Every chained record for one decision: (action, chain hash, payload ref).

    Queryable whether or not the decision ever reached the broker — a refusal is
    a decision, and the whole point of recording one is being able to find it.
    """
    rows = connection.execute(
        select(AuditEvent.action, AuditEvent.current_hash, AuditEvent.payload_ref)
        .where(AuditEvent.target_ref == decision_id)
        .order_by(AuditEvent.sequence_no)
    ).all()
    return [(row.action, row.current_hash, row.payload_ref) for row in rows]


__all__ = [
    "ACTION_EVALUATED",
    "ACTION_OUTCOME",
    "ACTION_POST_SUBMIT",
    "ACTION_PRE_SUBMIT",
    "ACTION_SUBMIT_PREPARED",
    "AMBIGUOUS_ACTIONS",
    "TERMINAL_ACTIONS",
    "ExecutionHistoryRecord",
    "ExecutionHistoryUnreadableError",
    "REQUIRED_IDENTITY_FIELDS",
    "require_consistent_lifecycle",
    "REQUIRED_PREPARED_FIELDS",
    "order_index_ref",
    "EXECUTION_AUDIT_VERSION",
    "MAX_DETAIL_CHARS",
    "DurableExecutionAuditSink",
    "DurableExecutionHistoryReader",
    "ExecutionAuditError",
    "ObjectStore",
    "canonical_payload",
    "load_decision_records",
    "summarize",
]
