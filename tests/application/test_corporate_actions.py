"""Corporate action workflow: validation, conflict, blocking, resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from uuid import uuid4

import pytest
from trading_application.corporate_actions import (
    CollectingHaltSink,
    ConflictError,
    CorporateActionEvent,
    CorporateActionStatus,
    CorporateActionWorkflow,
    InvalidActionError,
    ResolutionEvidenceError,
    UnknownInstrumentError,
    split_adjustment,
)
from trading_contracts.enums import CorporateActionType

EFFECTIVE = datetime(2026, 7, 10, 13, 30, tzinfo=UTC)
AVAILABLE = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
AS_OF = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

KNOWN = {"inst-1", "inst-2"}


def make_workflow() -> tuple[CorporateActionWorkflow, CollectingHaltSink]:
    sink = CollectingHaltSink()
    workflow = CorporateActionWorkflow(
        instrument_exists=lambda iid: iid in KNOWN, halt_sink=sink
    )
    return workflow, sink


def split_event(
    *,
    instrument: str = "inst-1",
    source: str = "official",
    ratio: Fraction = Fraction(10, 1),
    available_at: datetime = AVAILABLE,
) -> CorporateActionEvent:
    return CorporateActionEvent(
        id=uuid4(),
        instrument_id=instrument,
        action_type=CorporateActionType.SPLIT,
        source=source,
        effective_at=EFFECTIVE,
        available_at=available_at,
        ratio=ratio,
    )


def test_valid_split_observed_and_validated() -> None:
    workflow, sink = make_workflow()
    event = workflow.observe(split_event())
    assert event.status is CorporateActionStatus.OBSERVED
    validated = workflow.validate(event.id)
    assert validated.status is CorporateActionStatus.VALIDATED
    assert sink.halts == []


def test_split_ratio_exact_fraction_adjustment() -> None:
    event = split_event(ratio=Fraction(10, 1))
    record = split_adjustment(
        event,
        created_by="op-1",
        broker_snapshot_ref="broker-snap-1",
        provider_evidence_ref="prov-ev-1",
    )
    assert record.price_factor == Fraction(1, 10)  # 100 -> 10, exactly
    assert record.quantity_factor == Fraction(10, 1)
    assert record.price_factor * record.quantity_factor == 1  # value preserved exactly


def test_invalid_ratios_fail_closed() -> None:
    with pytest.raises(InvalidActionError, match="SPLIT ratio"):
        split_event(ratio=Fraction(1, 2))  # split must be > 1
    with pytest.raises(InvalidActionError, match="REVERSE_SPLIT"):
        CorporateActionEvent(
            id=uuid4(),
            instrument_id="inst-1",
            action_type=CorporateActionType.REVERSE_SPLIT,
            source="official",
            effective_at=EFFECTIVE,
            available_at=AVAILABLE,
            ratio=Fraction(5, 1),  # reverse split must be < 1
        )
    with pytest.raises(InvalidActionError, match="new_symbol"):
        CorporateActionEvent(
            id=uuid4(),
            instrument_id="inst-1",
            action_type=CorporateActionType.SYMBOL_CHANGE,
            source="official",
            effective_at=EFFECTIVE,
            available_at=AVAILABLE,
        )


def test_unknown_instrument_fails_closed() -> None:
    workflow, _sink = make_workflow()
    with pytest.raises(UnknownInstrumentError):
        workflow.observe(split_event(instrument="ghost"))


def test_conflicting_reports_go_to_review_and_halt_affected_only() -> None:
    workflow, sink = make_workflow()
    first = workflow.observe(split_event(source="official", ratio=Fraction(10, 1)))
    second = workflow.observe(split_event(source="broker", ratio=Fraction(5, 1)))
    assert second.status is CorporateActionStatus.CONFLICT
    assert workflow.actions_visible("inst-1", as_of=AS_OF)[0].status is (
        CorporateActionStatus.CONFLICT
    )
    # SYMBOL_HALT requested for the affected instrument only
    assert sink.halts == [("inst-1", "CORPORATE_ACTION_CONFLICT")]
    with pytest.raises(ConflictError):
        workflow.validate(first.id)
    # DoD: the affected instrument is blocked; any other instrument is not
    assert workflow.is_instrument_blocked("inst-1", as_of=AS_OF)
    assert not workflow.is_instrument_blocked("inst-2", as_of=AS_OF)


def test_corroborating_report_is_not_a_conflict() -> None:
    workflow, sink = make_workflow()
    workflow.observe(split_event(source="official"))
    second = workflow.observe(split_event(source="broker"))
    assert second.status is CorporateActionStatus.OBSERVED
    assert sink.halts == []


def test_available_at_after_as_of_is_invisible_no_lookahead() -> None:
    workflow, _sink = make_workflow()
    workflow.observe(split_event(available_at=AS_OF + timedelta(hours=1)))
    assert workflow.actions_visible("inst-1", as_of=AS_OF) == ()
    assert not workflow.is_instrument_blocked("inst-1", as_of=AS_OF)
    # once time passes availability, it becomes visible and blocking
    later = AS_OF + timedelta(hours=2)
    assert workflow.is_instrument_blocked("inst-1", as_of=later)


def test_unresolved_blocks_until_explicit_resolution_with_evidence() -> None:
    workflow, _sink = make_workflow()
    event = workflow.observe(split_event())
    workflow.validate(event.id)
    assert workflow.is_instrument_blocked("inst-1", as_of=AS_OF)

    with pytest.raises(ResolutionEvidenceError, match="AdjustmentRecord"):
        workflow.resolve(event.id, adjustment=None, approved_by="op-1")

    record = split_adjustment(
        event,
        created_by="op-1",
        broker_snapshot_ref="broker-snap-9",
        provider_evidence_ref="prov-ev-9",
    )
    resolved = workflow.resolve(event.id, adjustment=record, approved_by="op-1")
    assert resolved.status is CorporateActionStatus.RESOLVED
    assert not workflow.is_instrument_blocked("inst-1", as_of=AS_OF)
    stored = workflow.adjustment_for(event.id)
    assert stored is not None and stored.broker_snapshot_ref == "broker-snap-9"


def test_symbol_change_requires_explicit_mapping_no_auto_merge() -> None:
    workflow, _sink = make_workflow()
    event = workflow.observe(
        CorporateActionEvent(
            id=uuid4(),
            instrument_id="inst-1",
            action_type=CorporateActionType.SYMBOL_CHANGE,
            source="official",
            effective_at=EFFECTIVE,
            available_at=AVAILABLE,
            new_symbol="NEWT",
        )
    )
    record = split_adjustment(
        split_event(), created_by="op-1",
        broker_snapshot_ref="b", provider_evidence_ref="p",
    )
    # wrong event's adjustment is rejected, and mapping ref is mandatory
    with pytest.raises(ResolutionEvidenceError, match="mapping reference"):
        workflow.resolve(event.id, adjustment=record, approved_by="op-1")


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        split_event(available_at=datetime(2026, 7, 8, 21, 0))  # noqa: DTZ001
    workflow, _sink = make_workflow()
    workflow.observe(split_event())
    with pytest.raises(ValueError, match="timezone-aware"):
        workflow.is_instrument_blocked("inst-1", as_of=datetime(2026, 7, 9))  # noqa: DTZ001


def test_replay_determinism_and_idempotent_redelivery() -> None:
    event = split_event()
    runs = []
    for _ in range(2):
        workflow, _sink = make_workflow()
        workflow.observe(event)
        workflow.observe(event)  # duplicate delivery: no double record
        runs.append(workflow.actions_visible("inst-1", as_of=AS_OF))
    assert runs[0] == runs[1]
    assert len(runs[0]) == 1


def test_workflow_creates_no_candidate_decision_or_order() -> None:
    import trading_application.corporate_actions as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in ("trading_persistence", "broker_ibkr", "httpx"):
            assert forbidden not in line, line
