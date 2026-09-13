"""Bounded read-only MVP orchestration with immutable audit evidence.

This is the single runnable entry point above MvpTradingPolicy.evaluate_read_only.
It accepts a frozen, explicitly supplied snapshot and has no broker, execution-agent,
authorization, operating-mode, or executable-plan dependency. Persistence is a
structural port so the application layer remains independent of adapters.

The execution lifecycle audit record is intentionally not reused: its PREPARED and
OUTCOME states describe a path that may contact a broker. This domain-neutral run
record instead describes deterministic evaluation lineage only; the durable adapter
reuses the repository's existing audit chain and content-addressed object store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol
from uuid import UUID

from trading_domain.calendar import TradingCalendar
from trading_market_data_core.types import BarSeries

from trading_application.emergency import HaltRecord
from trading_application.execution_identity import (
    allocation_identity,
    decision_identity,
    risk_result_identity,
)
from trading_application.mvp_trading_policy import (
    MvpAccountSnapshot,
    MvpCandidateFacts,
    MvpExecutionInputs,
    MvpPolicyOutcome,
    MvpReadOnlySeal,
    MvpResult,
    MvpStage,
)
from trading_application.position_attribution import BrokerHeldPosition, SystemPositionRecord
from trading_application.universe_eligibility import UniverseFacts

READ_ONLY_RUN_RECORD_VERSION = "mvp-read-only-run-1.0.0"


class MvpReadOnlyRuntimeError(Exception):
    """Read-only evidence is invalid, conflicting, or unavailable."""


@dataclass(frozen=True, slots=True)
class MvpInputEvidence:
    """Qualification status supplied with one bounded input dataset."""

    input_name: str
    quality_status: str
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.input_name.strip() or not self.quality_status.strip():
            raise MvpReadOnlyRuntimeError(
                "input evidence name and quality status are required"
            )


@dataclass(frozen=True, slots=True)
class MvpReadOnlyInputSnapshot:
    """All bounded inputs consumed by one evaluate-only policy run.

    Evidence status is recorded verbatim. The runtime never upgrades a fixture
    or current value to PIT-qualified evidence and never projects it backward.
    """

    instrument_id: UUID
    bars: BarSeries
    candidate: MvpCandidateFacts
    universe: UniverseFacts
    account: MvpAccountSnapshot
    held_positions: tuple[BrokerHeldPosition, ...]
    system_records: tuple[SystemPositionRecord, ...]
    execution: MvpExecutionInputs
    as_of: datetime
    calendar: TradingCalendar | None = None
    active_halts: tuple[HaltRecord, ...] = ()
    input_evidence: tuple[MvpInputEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise MvpReadOnlyRuntimeError("snapshot as_of must be timezone-aware")
        immutable_fields = (
            "held_positions",
            "system_records",
            "active_halts",
            "input_evidence",
        )
        for name in immutable_fields:
            if not isinstance(getattr(self, name), tuple):
                raise MvpReadOnlyRuntimeError(
                    f"snapshot {name} must be an immutable tuple"
                )
        names = [item.input_name for item in self.input_evidence]
        if len(names) != len(set(names)):
            raise MvpReadOnlyRuntimeError("input evidence names must be unique")


class MvpReadOnlyAuditStore(Protocol):
    """Immutable persistence port implemented by fakes or the durable adapter."""

    def put_if_absent(
        self, identity: str, record: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def load(self, identity: str) -> Mapping[str, object] | None: ...


class MvpReadOnlyPolicy(Protocol):
    """The runtime's deliberately narrow, non-authorizing policy surface."""

    @property
    def strategy_id(self) -> str: ...

    @property
    def strategy_version(self) -> str: ...

    @property
    def strategy_fingerprint(self) -> str: ...

    def evaluate_read_only(
        self,
        instrument_id: UUID,
        *,
        bars: BarSeries,
        candidate: MvpCandidateFacts,
        universe: UniverseFacts,
        account: MvpAccountSnapshot,
        held_positions: Sequence[BrokerHeldPosition],
        system_records: Sequence[SystemPositionRecord],
        execution: MvpExecutionInputs,
        as_of: datetime,
        calendar: TradingCalendar | None = None,
        active_halts: Sequence[HaltRecord] = (),
    ) -> MvpPolicyOutcome: ...


@dataclass(frozen=True, slots=True)
class MvpReadOnlyRun:
    """Canonical policy result together with its durable audit record."""

    outcome: MvpPolicyOutcome
    read_only_seal: MvpReadOnlySeal | None
    audit_record: Mapping[str, object]


def _canonical_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        converted = [_canonical_value(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(item, sort_keys=True, default=str),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value


def canonical_run_payload(value: object) -> bytes:
    """Stable JSON bytes used for immutable input and record identities."""
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _snapshot_ref(snapshot: MvpReadOnlyInputSnapshot) -> str:
    digest = hashlib.sha256(canonical_run_payload(snapshot)).hexdigest()
    return f"mvp-runtime-input:sha256:{digest}"


def _policy_content_ref(namespace: str, payload: object) -> str:
    """Reproduce the canonical policy seal hash for load-only verification."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )
    return f"{namespace}:sha256:{hashlib.sha256(canonical.encode('ascii')).hexdigest()}"


def _optional_lineage(
    outcome: MvpPolicyOutcome,
) -> tuple[str | None, str | None, str | None]:
    decision_ref = (
        decision_identity(outcome.decision) if outcome.decision is not None else None
    )
    risk_ref = (
        risk_result_identity(outcome.risk_result)
        if outcome.risk_result is not None
        else None
    )
    allocation_ref = (
        allocation_identity(outcome.allocation)
        if outcome.allocation is not None
        else None
    )
    return decision_ref, risk_ref, allocation_ref


def _require_read_only_outcome(outcome: MvpPolicyOutcome) -> None:
    if outcome.executable_plan is not None or outcome.pre_submit is not None:
        raise MvpReadOnlyRuntimeError("evaluate_read_only returned execution authority")
    seal = outcome.read_only_seal
    if outcome.result is MvpResult.READ_ONLY_INTENT:
        if (
            seal is None
            or outcome.pre_submit_evaluation is None
            or outcome.stage is not MvpStage.READ_ONLY_INTENT
        ):
            raise MvpReadOnlyRuntimeError(
                "READ_ONLY_INTENT is missing its canonical seal"
            )
    elif seal is not None:
        raise MvpReadOnlyRuntimeError(
            "an unsealed terminal must not carry a read-only seal"
        )


def _validate_stored_lineage(record: Mapping[str, object]) -> None:
    result = record.get("result")
    seal_ref = record.get("seal_ref")
    if result == MvpResult.READ_ONLY_INTENT.value:
        if record.get("read_only") is not True or not isinstance(seal_ref, str):
            raise MvpReadOnlyRuntimeError(
                "stored READ_ONLY_INTENT has no seal reference"
            )
        try:
            lineage = {
                "strategy_id": record["strategy_id"],
                "strategy_version": record["strategy_version"],
                "strategy_fingerprint": record["strategy_fingerprint"],
                "input_snapshot_ref": record["input_snapshot_ref"],
                "decision_ref": record["decision_ref"],
                "risk_ref": record["risk_ref"],
                "allocation_ref": record["allocation_ref"],
                "evaluated_quantity": Decimal(str(record["evaluated_quantity"])),
                "as_of": datetime.fromisoformat(str(record["as_of"])),
            }
        except (InvalidOperation, KeyError, ValueError) as error:
            raise MvpReadOnlyRuntimeError(
                "stored sealed lineage is incomplete"
            ) from error
        if not all(
            lineage[key] for key in ("decision_ref", "risk_ref", "allocation_ref")
        ):
            raise MvpReadOnlyRuntimeError("stored sealed lineage is incomplete")
        if seal_ref != _policy_content_ref("mvp-read-only-lineage", lineage):
            raise MvpReadOnlyRuntimeError(
                "stored seal does not match canonical lineage"
            )
    elif result in (MvpResult.NO_TRADE.value, MvpResult.NO_NEW_ENTRY.value):
        if record.get("read_only") is not True or seal_ref is not None:
            raise MvpReadOnlyRuntimeError(
                "stored unsealed terminal fabricates a seal"
            )
    else:
        raise MvpReadOnlyRuntimeError(
            "stored read-only evidence has an invalid result"
        )


def _identity(task_id: str, run_id: str) -> str:
    if not task_id.strip() or not run_id.strip():
        raise MvpReadOnlyRuntimeError("task_id and run_id are required")
    material = json.dumps([task_id, run_id], separators=(",", ":"), ensure_ascii=True)
    return f"read-only-run:sha256:{hashlib.sha256(material.encode('ascii')).hexdigest()}"


class MvpReadOnlyRuntime:
    """Execute, persist and replay one physically evaluate-only policy path."""

    def __init__(
        self, policy: MvpReadOnlyPolicy, audit_store: MvpReadOnlyAuditStore
    ) -> None:
        self._policy = policy
        self._audit_store = audit_store

    def run(
        self, *, task_id: str, run_id: str, snapshot: MvpReadOnlyInputSnapshot
    ) -> MvpReadOnlyRun:
        """Evaluate the frozen snapshot and immutably append its canonical record."""
        identity = _identity(task_id, run_id)
        run_input_ref = _snapshot_ref(snapshot)
        outcome = self._policy.evaluate_read_only(
            snapshot.instrument_id,
            bars=snapshot.bars,
            candidate=snapshot.candidate,
            universe=snapshot.universe,
            account=snapshot.account,
            held_positions=snapshot.held_positions,
            system_records=snapshot.system_records,
            execution=snapshot.execution,
            as_of=snapshot.as_of,
            calendar=snapshot.calendar,
            active_halts=snapshot.active_halts,
        )
        _require_read_only_outcome(outcome)
        decision_ref, risk_ref, allocation_ref = _optional_lineage(outcome)
        seal = outcome.read_only_seal
        if seal is not None and (
            seal.decision_ref != decision_ref
            or seal.risk_ref != risk_ref
            or seal.allocation_ref != allocation_ref
            or seal.strategy_id != self._policy.strategy_id
            or seal.strategy_version != self._policy.strategy_version
            or seal.strategy_fingerprint != self._policy.strategy_fingerprint
            or seal.as_of != snapshot.as_of
            or (
                outcome.pre_submit_evaluation is not None
                and seal.evaluated_quantity
                != outcome.pre_submit_evaluation.evaluated_quantity
            )
        ):
            raise MvpReadOnlyRuntimeError(
                "canonical seal does not match evaluated lineage"
            )
        input_snapshot_ref = (
            seal.input_snapshot_ref if seal is not None else run_input_ref
        )
        if seal is not None:
            evaluated_quantity = seal.evaluated_quantity
        elif outcome.pre_submit_evaluation is not None:
            evaluated_quantity = outcome.pre_submit_evaluation.evaluated_quantity
        else:
            evaluated_quantity = None
        record: Mapping[str, object] = {
            "record_version": READ_ONLY_RUN_RECORD_VERSION,
            "task_id": task_id,
            "run_id": run_id,
            "run_identity": identity,
            "run_input_ref": run_input_ref,
            "as_of": snapshot.as_of.isoformat(),
            "environment": snapshot.execution.environment.value,
            "strategy_id": self._policy.strategy_id,
            "strategy_version": self._policy.strategy_version,
            "strategy_fingerprint": self._policy.strategy_fingerprint,
            "result": outcome.result.value,
            "terminal_stage": outcome.stage.value,
            "reason_codes": list(outcome.reason_codes),
            "input_snapshot_ref": input_snapshot_ref,
            "decision_ref": decision_ref,
            "risk_ref": risk_ref,
            "allocation_ref": allocation_ref,
            "evaluated_quantity": (
                str(evaluated_quantity) if evaluated_quantity is not None else None
            ),
            "read_only": True,
            "seal_ref": seal.seal_ref if seal is not None else None,
            "input_evidence": [asdict(item) for item in snapshot.input_evidence],
        }
        _validate_stored_lineage(record)
        try:
            stored = self._audit_store.put_if_absent(identity, record)
        except Exception as error:
            if isinstance(error, MvpReadOnlyRuntimeError):
                raise
            raise MvpReadOnlyRuntimeError(
                f"read-only audit persistence failed ({type(error).__name__})"
            ) from error
        if canonical_run_payload(stored) != canonical_run_payload(record):
            raise MvpReadOnlyRuntimeError(
                "immutable run identity has conflicting evidence"
            )
        return MvpReadOnlyRun(
            outcome=outcome, read_only_seal=seal, audit_record=stored
        )

    def replay(self, *, task_id: str, run_id: str) -> Mapping[str, object]:
        """Load and validate lineage without evaluation or broker contact."""
        identity = _identity(task_id, run_id)
        try:
            record = self._audit_store.load(identity)
        except Exception as error:
            raise MvpReadOnlyRuntimeError(
                f"read-only audit replay failed ({type(error).__name__})"
            ) from error
        if record is None:
            raise MvpReadOnlyRuntimeError("read-only run evidence was not found")
        required = {
            "task_id",
            "run_id",
            "run_identity",
            "run_input_ref",
            "as_of",
            "environment",
            "strategy_id",
            "strategy_version",
            "strategy_fingerprint",
            "result",
            "terminal_stage",
            "reason_codes",
            "input_snapshot_ref",
            "decision_ref",
            "risk_ref",
            "allocation_ref",
            "evaluated_quantity",
            "read_only",
            "seal_ref",
        }
        if (
            not required.issubset(record)
            or record.get("run_identity") != identity
            or record.get("task_id") != task_id
            or record.get("run_id") != run_id
        ):
            raise MvpReadOnlyRuntimeError(
                "stored read-only evidence is incomplete or misidentified"
            )
        _validate_stored_lineage(record)
        return record


__all__ = [
    "MvpInputEvidence",
    "MvpReadOnlyAuditStore",
    "MvpReadOnlyInputSnapshot",
    "MvpReadOnlyPolicy",
    "MvpReadOnlyRun",
    "MvpReadOnlyRuntime",
    "MvpReadOnlyRuntimeError",
    "READ_ONLY_RUN_RECORD_VERSION",
    "canonical_run_payload",
]
