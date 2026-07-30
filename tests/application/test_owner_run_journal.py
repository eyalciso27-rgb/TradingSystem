"""D-CANCEL-05: the durable owner-run execute-attempt guard.

The harness's confirmation token is a deterministic digest of the run
parameters — reproducible by design, so on its own it is NOT single-use across
processes. Re-running the same command with the same token would pass the gate
again, and a crash between "order sent" and "status received" would leave the
owner able to submit a duplicate order.

These tests pin the durable guard: one execute attempt per operation, surviving
a new process and a crash, and never storing authorization material.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from trading_application.owner_run_journal import (
    OperationState,
    OwnerRunJournal,
    OwnerRunJournalError,
    OwnerRunRecord,
    OwnerRunReplayRefused,
)

OPERATION = "05D0E19B631C2548"
PARAMS = "a" * 64
OTHER_PARAMS = "b" * 64
T0 = datetime(2026, 7, 21, 14, 30, tzinfo=UTC)
T1 = datetime(2026, 7, 21, 14, 31, tzinfo=UTC)
T2 = datetime(2026, 7, 21, 14, 35, tzinfo=UTC)


def _journal(tmp_path: Path) -> OwnerRunJournal:
    return OwnerRunJournal(tmp_path / "journal")


def _begin(journal: OwnerRunJournal, *, started_at: datetime = T1) -> OwnerRunRecord:
    return journal.begin_execute(
        operation_reference=OPERATION, parameters_hash=PARAMS, started_at=started_at
    )


def test_a_dry_run_records_an_approved_operation(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    record = journal.record_dry_run(
        operation_reference=OPERATION, parameters_hash=PARAMS, approved_at=T0
    )
    assert record.state is OperationState.APPROVED
    assert record.execute_started_at is None


def test_begin_execute_claims_the_single_attempt(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.record_dry_run(
        operation_reference=OPERATION, parameters_hash=PARAMS, approved_at=T0
    )
    record = _begin(journal)
    assert record.state is OperationState.EXECUTE_STARTED
    assert record.execute_started_at is not None
    assert record.reconciliation_required is True


def test_a_second_execute_is_refused_in_the_same_process(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    with pytest.raises(OwnerRunReplayRefused):
        _begin(journal)


def test_a_token_cannot_be_replayed_in_a_new_process(tmp_path: Path) -> None:
    """The core property: a brand-new journal object over the same directory."""
    _begin(_journal(tmp_path))

    fresh_process = _journal(tmp_path)  # simulates a new CLI invocation
    with pytest.raises(OwnerRunReplayRefused) as refusal:
        _begin(fresh_process, started_at=T2)
    assert "cannot be replayed" in str(refusal.value)


def test_a_crash_before_any_status_does_not_reset_the_guard(tmp_path: Path) -> None:
    """No progress was ever recorded — the attempt is still spent."""
    _begin(_journal(tmp_path))
    # Nothing else happens: the process dies right after placeOrder.
    with pytest.raises(OwnerRunReplayRefused):
        _begin(_journal(tmp_path), started_at=T2)


def test_a_submit_timeout_still_blocks_a_replay(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    journal.record_progress(
        operation_reference=OPERATION,
        observed_at=T1,
        last_known_state="UNKNOWN",
        reconciliation_required=True,
    )
    with pytest.raises(OwnerRunReplayRefused) as refusal:
        _begin(_journal(tmp_path), started_at=T2)
    assert "UNKNOWN" in str(refusal.value)


def test_a_broker_rejection_still_blocks_a_replay(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    journal.record_progress(
        operation_reference=OPERATION, observed_at=T1, last_known_state="REJECTED"
    )
    with pytest.raises(OwnerRunReplayRefused):
        _begin(_journal(tmp_path), started_at=T2)


def test_different_parameters_cannot_reuse_an_operation_identity(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.record_dry_run(
        operation_reference=OPERATION, parameters_hash=PARAMS, approved_at=T0
    )
    with pytest.raises(OwnerRunReplayRefused) as refusal:
        journal.begin_execute(
            operation_reference=OPERATION,
            parameters_hash=OTHER_PARAMS,
            started_at=T1,
        )
    assert "different canonical parameters" in str(refusal.value)


def test_a_dry_run_after_an_execute_never_resets_the_state(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    record = journal.record_dry_run(
        operation_reference=OPERATION, parameters_hash=PARAMS, approved_at=T2
    )
    assert record.state is OperationState.EXECUTE_STARTED


def test_progress_records_the_broker_reference_and_last_state(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    record = journal.record_progress(
        operation_reference=OPERATION,
        observed_at=T1,
        broker_order_ref="ibkr-paper:2",
        last_known_state="ACKNOWLEDGED",
    )
    assert record.broker_order_ref == "ibkr-paper:2"
    assert record.last_known_state == "ACKNOWLEDGED"


def test_terminal_clean_closes_the_operation_without_erasing_the_audit(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    journal.record_dry_run(
        operation_reference=OPERATION, parameters_hash=PARAMS, approved_at=T0
    )
    _begin(journal)
    journal.record_progress(
        operation_reference=OPERATION, observed_at=T1, last_known_state="CANCELLED"
    )
    record = journal.close_clean(operation_reference=OPERATION, reconciled_at=T2)

    assert record.state is OperationState.CLOSED_CLEAN
    assert record.reconciliation_required is False
    assert record.terminal_clean_at is not None
    # The audit trail survives in full.
    assert len(record.history) >= 4
    assert record.execute_started_at is not None
    # And a closed operation still cannot be executed again.
    with pytest.raises(OwnerRunReplayRefused):
        _begin(_journal(tmp_path), started_at=T2)


def test_reconciliation_and_cleanup_paths_are_unaffected(tmp_path: Path) -> None:
    """Reading the record never claims an attempt."""
    journal = _journal(tmp_path)
    journal.record_dry_run(
        operation_reference=OPERATION, parameters_hash=PARAMS, approved_at=T0
    )
    assert journal.get(OPERATION) is not None
    assert journal.get(OPERATION) is not None
    # Still claimable exactly once afterwards.
    _begin(journal)


def test_the_journal_stores_no_authorization_material(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    journal.record_progress(
        operation_reference=OPERATION,
        observed_at=T1,
        broker_order_ref="ibkr-paper:2",
        last_known_state="ACKNOWLEDGED",
    )
    raw = (tmp_path / "journal" / f"{OPERATION}.json").read_text(encoding="utf-8")
    payload = json.loads(raw)

    for forbidden in ("token", "confirm", "account", "secret", "password", "credential"):
        assert forbidden not in raw.lower(), forbidden
    assert set(payload) == {
        "schema_version",
        "operation_reference",
        "parameters_hash",
        "state",
        "harness_version",
        "dry_run_approved_at",
        "execute_started_at",
        "broker_order_ref",
        "last_known_state",
        "reconciliation_required",
        "terminal_clean_at",
        "history",
    }


def test_a_path_traversal_reference_is_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    for bad in ("../escape", "a/b", "", "  ", "op;rm"):
        with pytest.raises(OwnerRunJournalError):
            journal.get(bad)


def test_naive_timestamps_are_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(OwnerRunJournalError):
        journal.record_dry_run(
            operation_reference=OPERATION,
            parameters_hash=PARAMS,
            approved_at=datetime(2026, 7, 21, 14, 30),  # noqa: DTZ001 - the point
        )


def test_progress_on_an_unknown_operation_fails_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    with pytest.raises(OwnerRunJournalError):
        journal.record_progress(
            operation_reference=OPERATION, observed_at=T1, last_known_state="X"
        )


def test_a_corrupt_record_fails_closed(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    (tmp_path / "journal" / f"{OPERATION}.json").write_text("not json", encoding="utf-8")
    with pytest.raises(OwnerRunJournalError):
        journal.get(OPERATION)


def test_the_write_is_atomic_leaving_no_partial_records(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    _begin(journal)
    leftovers = [p.name for p in (tmp_path / "journal").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
