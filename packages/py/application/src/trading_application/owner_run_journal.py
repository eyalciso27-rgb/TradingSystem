"""Durable owner-run execute-attempt journal (D-CANCEL-05).

Authority: Integration_Specification_v0.6 section 9.2 (execution isolation),
9.4 (append-only, idempotent broker events), 9.5 (reconciliation);
Decision_Contract 12.5 (UNKNOWN is resolved only by reconciliation).

**Why this exists.** The owner-run harness gates ``--execute`` behind a
confirmation token derived deterministically from the run parameters. That
token is reproducible by design (the owner copies it from a dry run), so it is
*not* single-use across processes: re-running the identical command with the
same token would pass the gate again, and a crash between "order sent" and
"status received" would leave the owner able to re-submit a duplicate order.

This journal makes an execute attempt durable and single-use:

- the transition into ``EXECUTE_STARTED`` is an ATOMIC, exclusive filesystem
  create (``O_CREAT | O_EXCL``), so two processes racing the same operation
  cannot both proceed, and a crash leaves the marker in place;
- once started, a second ``--execute`` for the same operation is refused
  regardless of any freshly minted in-process authorization;
- reconciliation/cleanup/read-only inspection are unaffected — they never
  begin an execute attempt;
- closing an operation as terminal-clean records the fact but never erases
  the audit record or the exclusivity marker.

The journal deliberately stores NO confirmation token, account id, credential
or authorization material — only a non-authorizing operation reference, a
parameters hash, timestamps, the broker's own opaque order reference and the
last known canonical state.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

#: Schema version of a journal record; bumped if the shape ever changes.
JOURNAL_SCHEMA_VERSION = 1

#: Field names that must NEVER appear in a journal record. Enforced on write
#: so a future caller cannot accidentally persist authorization material.
FORBIDDEN_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "confirm",
        "confirm_token",
        "confirmation_token",
        "account",
        "account_id",
        "account_alias",
        "secret",
        "password",
        "credential",
        "authorization",
        "authorization_ref",
    }
)


class OwnerRunReplayRefused(Exception):
    """A durable execute attempt already exists for this operation."""


class OwnerRunJournalError(Exception):
    """The journal could not be read or written; always fail closed."""


class OperationState(StrEnum):
    """Lifecycle of one durable owner-run operation."""

    #: A dry run approved these exact parameters; nothing has been sent.
    APPROVED = "APPROVED"
    #: An execute attempt began. No second attempt may ever start.
    EXECUTE_STARTED = "EXECUTE_STARTED"
    #: The broker outcome is not provably terminal; reconciliation required.
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    #: Reconciliation proved the account clean for this operation. The audit
    #: record and the exclusivity marker both remain.
    CLOSED_CLEAN = "CLOSED_CLEAN"


@dataclass(frozen=True, slots=True)
class OwnerRunRecord:
    """One durable owner-run operation record (audit; append-only in spirit)."""

    operation_reference: str
    parameters_hash: str
    state: OperationState
    harness_version: str = ""
    dry_run_approved_at: str | None = None
    execute_started_at: str | None = None
    broker_order_ref: str | None = None
    last_known_state: str | None = None
    reconciliation_required: bool = False
    terminal_clean_at: str | None = None
    history: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "operation_reference": self.operation_reference,
            "parameters_hash": self.parameters_hash,
            "state": self.state.value,
            "harness_version": self.harness_version,
            "dry_run_approved_at": self.dry_run_approved_at,
            "execute_started_at": self.execute_started_at,
            "broker_order_ref": self.broker_order_ref,
            "last_known_state": self.last_known_state,
            "reconciliation_required": self.reconciliation_required,
            "terminal_clean_at": self.terminal_clean_at,
            "history": list(self.history),
        }

    @staticmethod
    def from_json(raw: object) -> OwnerRunRecord:
        if not isinstance(raw, dict):
            raise OwnerRunJournalError("journal record is not an object")
        try:
            return OwnerRunRecord(
                operation_reference=str(raw["operation_reference"]),
                parameters_hash=str(raw["parameters_hash"]),
                state=OperationState(str(raw["state"])),
                harness_version=str(raw.get("harness_version") or ""),
                dry_run_approved_at=_optional_str(raw.get("dry_run_approved_at")),
                execute_started_at=_optional_str(raw.get("execute_started_at")),
                broker_order_ref=_optional_str(raw.get("broker_order_ref")),
                last_known_state=_optional_str(raw.get("last_known_state")),
                reconciliation_required=bool(raw.get("reconciliation_required", False)),
                terminal_clean_at=_optional_str(raw.get("terminal_clean_at")),
                history=tuple(str(item) for item in raw.get("history", ()) or ()),
            )
        except (KeyError, ValueError) as error:
            raise OwnerRunJournalError(f"unreadable journal record: {error}") from error


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise OwnerRunJournalError("journal timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


def _safe_component(operation_reference: str) -> str:
    """Reject anything that is not a plain reference (no path traversal)."""
    reference = operation_reference.strip()
    if not reference or not reference.replace("-", "").replace("_", "").isalnum():
        raise OwnerRunJournalError(
            f"operation reference {operation_reference!r} is not a plain"
            " alphanumeric reference"
        )
    return reference


class OwnerRunJournal:
    """Filesystem-backed, crash-safe journal of owner-run execute attempts."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise OwnerRunJournalError(f"cannot create journal directory: {error}") from error

    # -- paths ---------------------------------------------------------------
    def _record_path(self, operation_reference: str) -> Path:
        return self._root / f"{_safe_component(operation_reference)}.json"

    def _marker_path(self, operation_reference: str) -> Path:
        return self._root / f"{_safe_component(operation_reference)}.execute-started"

    # -- io ------------------------------------------------------------------
    def get(self, operation_reference: str) -> OwnerRunRecord | None:
        path = self._record_path(operation_reference)
        if not path.exists():
            return None
        try:
            return OwnerRunRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise OwnerRunJournalError(f"cannot read journal record: {error}") from error

    def _write(self, record: OwnerRunRecord) -> OwnerRunRecord:
        payload = record.to_json()
        leaked = FORBIDDEN_RECORD_KEYS.intersection(payload)
        if leaked:
            raise OwnerRunJournalError(
                f"refusing to persist authorization material: {sorted(leaked)}"
            )
        path = self._record_path(record.operation_reference)
        try:
            # Atomic publish: write a sibling temp file, fsync, then replace.
            # A crash therefore never leaves a half-written record.
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._root),
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            )
            with handle as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, path)
        except OSError as error:
            raise OwnerRunJournalError(f"cannot write journal record: {error}") from error
        return record

    # -- transitions ---------------------------------------------------------
    def record_dry_run(
        self,
        *,
        operation_reference: str,
        parameters_hash: str,
        approved_at: datetime,
        harness_version: str = "",
    ) -> OwnerRunRecord:
        """Record that a dry run approved these exact parameters.

        Never overwrites an operation that already began executing — a dry run
        after an execute attempt must not reset the guard.
        """
        existing = self.get(operation_reference)
        stamp = _stamp(approved_at)
        if existing is not None:
            if existing.parameters_hash != parameters_hash:
                raise OwnerRunReplayRefused(
                    f"operation {operation_reference} already exists with different"
                    " canonical parameters; refusing to reuse the operation identity"
                )
            if existing.state is not OperationState.APPROVED:
                # An execute attempt (or later state) is durable; keep it.
                return existing
            return self._write(
                replace(
                    existing,
                    dry_run_approved_at=stamp,
                    history=(*existing.history, f"{stamp} dry-run-approved"),
                )
            )
        return self._write(
            OwnerRunRecord(
                operation_reference=operation_reference,
                parameters_hash=parameters_hash,
                state=OperationState.APPROVED,
                harness_version=harness_version,
                dry_run_approved_at=stamp,
                history=(f"{stamp} dry-run-approved",),
            )
        )

    def begin_execute(
        self,
        *,
        operation_reference: str,
        parameters_hash: str,
        started_at: datetime,
        harness_version: str = "",
    ) -> OwnerRunRecord:
        """Atomically claim the single execute attempt for this operation.

        Raises ``OwnerRunReplayRefused`` if an attempt already began — in this
        process or any earlier one, including after a crash, a submit timeout
        or a broker rejection. This runs BEFORE any broker call.
        """
        existing = self.get(operation_reference)
        if existing is not None and existing.parameters_hash != parameters_hash:
            raise OwnerRunReplayRefused(
                f"operation {operation_reference} is journalled with different"
                " canonical parameters; refusing to execute"
            )
        marker = self._marker_path(operation_reference)
        try:
            # O_EXCL is the atomic test-and-set: exactly one process wins,
            # and the marker survives a crash (it is never removed).
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            state = existing.state.value if existing is not None else "EXECUTE_STARTED"
            last = existing.last_known_state if existing is not None else None
            raise OwnerRunReplayRefused(
                f"operation {operation_reference} already had an execute attempt"
                f" (durable state {state}, last known order state {last});"
                " the confirmation token cannot be replayed. Reconcile or clean"
                " up instead, or start a new operation with new parameters"
            ) from error
        except OSError as error:
            raise OwnerRunJournalError(f"cannot claim execute attempt: {error}") from error
        stamp = _stamp(started_at)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(stamp)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise OwnerRunJournalError(f"cannot record execute attempt: {error}") from error
        base = existing or OwnerRunRecord(
            operation_reference=operation_reference,
            parameters_hash=parameters_hash,
            state=OperationState.APPROVED,
            harness_version=harness_version,
        )
        return self._write(
            replace(
                base,
                state=OperationState.EXECUTE_STARTED,
                harness_version=harness_version or base.harness_version,
                execute_started_at=stamp,
                reconciliation_required=True,
                history=(*base.history, f"{stamp} execute-started"),
            )
        )

    def record_progress(
        self,
        *,
        operation_reference: str,
        observed_at: datetime,
        broker_order_ref: str | None = None,
        last_known_state: str | None = None,
        reconciliation_required: bool | None = None,
    ) -> OwnerRunRecord:
        """Record the broker reference / last known canonical state."""
        existing = self.get(operation_reference)
        if existing is None:
            raise OwnerRunJournalError(
                f"no journal record for operation {operation_reference}"
            )
        stamp = _stamp(observed_at)
        note = f"{stamp} progress state={last_known_state} ref={broker_order_ref}"
        return self._write(
            replace(
                existing,
                broker_order_ref=broker_order_ref or existing.broker_order_ref,
                last_known_state=last_known_state or existing.last_known_state,
                reconciliation_required=(
                    existing.reconciliation_required
                    if reconciliation_required is None
                    else reconciliation_required
                ),
                history=(*existing.history, note),
            )
        )

    def close_clean(
        self, *, operation_reference: str, reconciled_at: datetime
    ) -> OwnerRunRecord:
        """Terminal-clean reconciliation closes the operation.

        The exclusivity marker and the whole audit history are preserved: a
        closed operation can never be executed again.
        """
        existing = self.get(operation_reference)
        if existing is None:
            raise OwnerRunJournalError(
                f"no journal record for operation {operation_reference}"
            )
        stamp = _stamp(reconciled_at)
        return self._write(
            replace(
                existing,
                state=OperationState.CLOSED_CLEAN,
                reconciliation_required=False,
                terminal_clean_at=stamp,
                history=(*existing.history, f"{stamp} terminal-clean"),
            )
        )


__all__ = [
    "FORBIDDEN_RECORD_KEYS",
    "JOURNAL_SCHEMA_VERSION",
    "OperationState",
    "OwnerRunJournal",
    "OwnerRunJournalError",
    "OwnerRunRecord",
    "OwnerRunReplayRefused",
]
