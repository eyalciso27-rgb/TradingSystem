"""Corporate action workflow (IMP-0705).

Authority: System_Functional_Specification_v0.7 section 40.6 (Split, Reverse
Split, Symbol Change, Delisting and Merger create a CorporateActionEvent;
the affected position moves to SYMBOL_HALT and RECONCILIATION_REQUIRED;
stops/targets/quantity/average price never change without Broker
Reconciliation and an Adjustment Record; new entries in the affected
instrument stay blocked until an explicit Resume), Security spec section
23.6 (a corporate-action conflict triggers SYMBOL_HALT; no silent manual
adjustment; Resume requires Broker Snapshot, Provider Evidence and an
Adjustment Record), Integration spec section 20.3 (corporate actions come
from an official/broker/reference provider; conflict -> halt) and Data
Model sections 24.1 (corporate_actions: type, effective_at, source, status)
and 24.2 (available_at point-in-time semantics).

Workflow states (derived from the authority lifecycle, not invented
behavior): OBSERVED (raw provider event) -> VALIDATED (complete, no
conflict) -> RESOLVED (explicit resume with evidence). CONFLICT marks
diverging reports and routes to review. The DoD invariant: an unresolved
action blocks the AFFECTED instrument only — never any other instrument.

This module never mutates positions, prices or the Instrument Master: a
symbol change is resolved only against an explicit mapping reference, and
adjustments exist solely as attributed AdjustmentRecords (exact ``Fraction``
ratios — no precision loss) for the later reconciliation tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from typing import Protocol
from uuid import UUID, uuid4

from trading_contracts.enums import CorporateActionType, HaltScope


class CorporateActionError(Exception):
    """Base class for fail-closed corporate-action failures."""


class UnknownInstrumentError(CorporateActionError):
    """The event references an instrument the master does not know."""


class InvalidActionError(CorporateActionError):
    """Mandatory fields are missing/inconsistent for the action type."""


class ConflictError(CorporateActionError):
    """Diverging reports for the same action: review required."""


class ResolutionEvidenceError(CorporateActionError):
    """Resume attempted without the mandatory evidence (23.6)."""


class CorporateActionStatus(StrEnum):
    """Workflow status (Data Model 24.1 ``status`` column values)."""

    OBSERVED = "OBSERVED"
    VALIDATED = "VALIDATED"
    CONFLICT = "CONFLICT"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    """One provider-attributed corporate action for one internal instrument."""

    id: UUID
    instrument_id: str  # internal Instrument Master id, never a raw ticker
    action_type: CorporateActionType
    source: str  # provider attribution (official/broker/reference), mandatory
    effective_at: datetime
    available_at: datetime  # first moment the event may influence anything
    announced_at: datetime | None = None
    ratio: Fraction | None = None  # new_shares/old_shares (exact)
    new_symbol: str | None = None
    status: CorporateActionStatus = CorporateActionStatus.OBSERVED

    def __post_init__(self) -> None:
        for stamp in (self.effective_at, self.available_at, self.announced_at):
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError("corporate action timestamps must be timezone-aware")
        if not self.source.strip():
            raise InvalidActionError("source attribution is mandatory")
        if self.action_type in (CorporateActionType.SPLIT, CorporateActionType.REVERSE_SPLIT):
            if self.ratio is None or self.ratio <= 0:
                raise InvalidActionError(f"{self.action_type.value} requires a positive ratio")
            if self.action_type is CorporateActionType.SPLIT and self.ratio <= 1:
                raise InvalidActionError("SPLIT ratio must be > 1 (new/old shares)")
            if self.action_type is CorporateActionType.REVERSE_SPLIT and self.ratio >= 1:
                raise InvalidActionError("REVERSE_SPLIT ratio must be < 1 (new/old shares)")
        if self.action_type is CorporateActionType.SYMBOL_CHANGE and not self.new_symbol:
            raise InvalidActionError("SYMBOL_CHANGE requires new_symbol")

    def material_terms(self) -> tuple[object, ...]:
        """The fields two sources must agree on to corroborate each other."""
        return (self.action_type, self.effective_at, self.ratio, self.new_symbol)


@dataclass(frozen=True, slots=True)
class AdjustmentRecord:
    """Attributed, versioned adjustment plan — never applied silently (40.6).

    Recording the factors is all this workflow does; applying them to
    positions/stops happens only through the later broker-reconciliation
    tasks, referencing this record.
    """

    id: UUID
    event_id: UUID
    price_factor: Fraction
    quantity_factor: Fraction
    formula_version: str
    created_by: str
    broker_snapshot_ref: str
    provider_evidence_ref: str

    def __post_init__(self) -> None:
        if self.price_factor <= 0 or self.quantity_factor <= 0:
            raise InvalidActionError("adjustment factors must be positive")
        if not self.formula_version:
            raise InvalidActionError("formula_version is mandatory")


class HaltSink(Protocol):
    """Receives the SYMBOL_HALT demanded by a conflict/unresolved action.

    Wired to the IMP-0404 emergency control service by the caller; the
    workflow itself only ever requests a SYMBOL_HALT scoped to the affected
    instrument (DoD: blocks the affected instrument only).
    """

    def halt_symbol(self, instrument_id: str, *, reason_code: str, reason_text: str) -> None: ...


class CollectingHaltSink:
    """Test/bootstrap sink recording requested symbol halts."""

    def __init__(self) -> None:
        self.halts: list[tuple[str, str]] = []

    def halt_symbol(self, instrument_id: str, *, reason_code: str, reason_text: str) -> None:
        self.halts.append((instrument_id, reason_code))


SPLIT_ADJUSTMENT_V1 = "corporate-action-split-v1"


def split_adjustment(event: CorporateActionEvent, *, created_by: str,
                     broker_snapshot_ref: str, provider_evidence_ref: str) -> AdjustmentRecord:
    """Exact split adjustment plan: price x old/new, quantity x new/old."""
    if event.ratio is None:
        raise InvalidActionError("split adjustment requires a ratio")
    return AdjustmentRecord(
        id=uuid4(),
        event_id=event.id,
        price_factor=1 / event.ratio,
        quantity_factor=event.ratio,
        formula_version=SPLIT_ADJUSTMENT_V1,
        created_by=created_by,
        broker_snapshot_ref=broker_snapshot_ref,
        provider_evidence_ref=provider_evidence_ref,
    )


class CorporateActionWorkflow:
    """Deterministic in-memory workflow over corporate-action events."""

    def __init__(
        self,
        *,
        instrument_exists: Callable[[str], bool],
        halt_sink: HaltSink,
    ) -> None:
        self._instrument_exists = instrument_exists
        self._halt_sink = halt_sink
        self._events: dict[UUID, CorporateActionEvent] = {}
        self._adjustments: dict[UUID, AdjustmentRecord] = {}

    # -- intake ---------------------------------------------------------------

    def observe(self, event: CorporateActionEvent) -> CorporateActionEvent:
        """Record a raw provider event; detect conflicts immediately."""
        if not self._instrument_exists(event.instrument_id):
            raise UnknownInstrumentError(
                f"corporate action references unknown instrument "
                f"{event.instrument_id!r} (fail closed)"
            )
        if event.id in self._events:
            return self._events[event.id]  # idempotent re-delivery
        stored = event
        for other in self._events.values():
            if (
                other.instrument_id == event.instrument_id
                and other.action_type == event.action_type
                and other.effective_at == event.effective_at
                and other.status is not CorporateActionStatus.RESOLVED
                and other.material_terms() != event.material_terms()
            ):
                # Diverging reports: both go to CONFLICT and the affected
                # instrument (only) is halted (23.6).
                self._events[other.id] = replace(
                    other, status=CorporateActionStatus.CONFLICT
                )
                stored = replace(event, status=CorporateActionStatus.CONFLICT)
                self._halt_sink.halt_symbol(
                    event.instrument_id,
                    reason_code="CORPORATE_ACTION_CONFLICT",
                    reason_text=(
                        f"conflicting {event.action_type.value} reports from "
                        f"{other.source!r} and {event.source!r}"
                    ),
                )
        self._events[stored.id] = stored
        return stored

    # -- validation -----------------------------------------------------------

    def validate(self, event_id: UUID) -> CorporateActionEvent:
        """OBSERVED -> VALIDATED (complete fields, no conflict)."""
        event = self._require(event_id)
        if event.status is CorporateActionStatus.CONFLICT:
            raise ConflictError(
                f"event {event_id} is in CONFLICT; review is required before "
                "any further processing"
            )
        if event.status is CorporateActionStatus.RESOLVED:
            return event
        validated = replace(event, status=CorporateActionStatus.VALIDATED)
        self._events[event_id] = validated
        return validated

    # -- resolution (explicit resume, 23.6/40.6) --------------------------------

    def resolve(
        self,
        event_id: UUID,
        *,
        adjustment: AdjustmentRecord | None,
        approved_by: str,
        symbol_mapping_ref: str | None = None,
    ) -> CorporateActionEvent:
        """Explicit resume with mandatory evidence. Never silent.

        Splits/mergers/delistings require an AdjustmentRecord (which itself
        carries the broker snapshot + provider evidence refs). A symbol
        change additionally requires an explicit Instrument Master mapping
        reference — the workflow never creates or merges instruments itself.
        """
        event = self._require(event_id)
        if not approved_by.strip():
            raise ResolutionEvidenceError("explicit approver is required for resume")
        if event.action_type is CorporateActionType.SYMBOL_CHANGE and not symbol_mapping_ref:
            raise ResolutionEvidenceError(
                "SYMBOL_CHANGE resume requires an explicit instrument-master "
                "mapping reference (no automatic instrument merge)"
            )
        if adjustment is None:
            raise ResolutionEvidenceError(
                "resume requires an AdjustmentRecord with broker snapshot and "
                "provider evidence (23.6); nothing is adjusted silently"
            )
        if adjustment.event_id != event_id:
            raise ResolutionEvidenceError("adjustment record belongs to another event")
        if not adjustment.broker_snapshot_ref or not adjustment.provider_evidence_ref:
            raise ResolutionEvidenceError(
                "resume requires broker_snapshot_ref and provider_evidence_ref"
            )
        self._adjustments[adjustment.id] = adjustment
        resolved = replace(event, status=CorporateActionStatus.RESOLVED)
        self._events[event_id] = resolved
        return resolved

    # -- point-in-time queries (DoD) --------------------------------------------

    def actions_visible(
        self, instrument_id: str, *, as_of: datetime
    ) -> tuple[CorporateActionEvent, ...]:
        """Events usable at ``as_of`` (available_at <= as_of; no lookahead)."""
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return tuple(
            sorted(
                (
                    event
                    for event in self._events.values()
                    if event.instrument_id == instrument_id
                    and event.available_at <= as_of
                ),
                key=lambda event: (event.available_at, str(event.id)),
            )
        )

    def is_instrument_blocked(self, instrument_id: str, *, as_of: datetime) -> bool:
        """DoD: an unresolved action blocks the AFFECTED instrument only.

        True iff a visible (available_at <= as_of) action for THIS instrument
        is not yet RESOLVED. Other instruments are never affected.
        """
        return any(
            event.status is not CorporateActionStatus.RESOLVED
            for event in self.actions_visible(instrument_id, as_of=as_of)
        )

    def adjustment_for(self, event_id: UUID) -> AdjustmentRecord | None:
        for record in self._adjustments.values():
            if record.event_id == event_id:
                return record
        return None

    def _require(self, event_id: UUID) -> CorporateActionEvent:
        event = self._events.get(event_id)
        if event is None:
            raise CorporateActionError(f"unknown corporate action event {event_id}")
        return event


# The halt this workflow requests is the canonical SYMBOL_HALT scope.
CORPORATE_ACTION_HALT_SCOPE = HaltScope.SYMBOL_HALT

__all__ = [
    "CORPORATE_ACTION_HALT_SCOPE",
    "SPLIT_ADJUSTMENT_V1",
    "AdjustmentRecord",
    "CollectingHaltSink",
    "ConflictError",
    "CorporateActionError",
    "CorporateActionEvent",
    "CorporateActionStatus",
    "CorporateActionWorkflow",
    "HaltSink",
    "InvalidActionError",
    "ResolutionEvidenceError",
    "UnknownInstrumentError",
    "split_adjustment",
]
