"""Tamper-evident audit tests (IMP-0304).

Done criteria: mutation breaks verification; checkpoint restore verifies the
chain. Chain logic is tested locally on plain records; persistence against
real PostgreSQL runs in CI.
"""

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from trading_persistence.audit import (
    GENESIS_HASH,
    AuditIntegrityError,
    compute_audit_hash,
    verify_records,
)

NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)


@dataclass(frozen=True)
class FakeRecord:
    sequence_no: int
    actor_ref: str
    action: str
    target_ref: str | None
    environment: str
    detail: str | None
    payload_ref: str | None
    correlation_id: UUID | None
    occurred_at: datetime
    previous_hash: str
    current_hash: str


def _chain(count: int) -> list[FakeRecord]:
    records: list[FakeRecord] = []
    previous = GENESIS_HASH
    for sequence_no in range(1, count + 1):
        fields: dict[str, Any] = dict(
            sequence_no=sequence_no,
            actor_ref="user-1",
            action=f"ACTION_{sequence_no}",
            target_ref=None,
            environment="PAPER",
            detail=None,
            payload_ref=None,
            correlation_id=None,
            occurred_at=NOW,
            previous_hash=previous,
        )
        current = compute_audit_hash(**fields)
        records.append(FakeRecord(current_hash=current, **fields))
        previous = current
    return records


def test_intact_chain_verifies() -> None:
    assert verify_records(_chain(5)) == []


def test_mutated_historical_record_detected() -> None:
    records = _chain(5)
    records[2] = replace(records[2], actor_ref="attacker")
    problems = verify_records(records)
    assert any("hash mismatch at sequence 3" in p for p in problems)


def test_deleted_record_detected() -> None:
    records = _chain(5)
    del records[2]  # remove sequence 3
    problems = verify_records(records)
    assert any("sequence gap" in p for p in problems)
    assert any("broken linkage" in p for p in problems)


def test_reordered_records_detected() -> None:
    records = _chain(5)
    records[1], records[2] = records[2], records[1]
    assert verify_records(records) != []


def test_forged_insertion_detected() -> None:
    records = _chain(3)
    forged_fields: dict[str, Any] = dict(
        sequence_no=4,
        actor_ref="attacker",
        action="FORGED",
        target_ref=None,
        environment="PAPER",
        detail=None,
        payload_ref=None,
        correlation_id=None,
        occurred_at=NOW,
        previous_hash="sha256:" + "f" * 64,  # not the real tail hash
    )
    forged = FakeRecord(current_hash=compute_audit_hash(**forged_fields), **forged_fields)
    problems = verify_records([*records, forged])
    assert any("broken linkage at sequence 4" in p for p in problems)


def test_wrong_previous_hash_detected() -> None:
    records = _chain(2)
    records[1] = replace(records[1], previous_hash=GENESIS_HASH)
    assert any("broken linkage" in p for p in verify_records(records))


# --- Real PostgreSQL (CI) ---

pg = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="PostgreSQL only in CI"
)


@pg
def test_append_verify_checkpoint_on_postgresql(engine: Any) -> None:
    from sqlalchemy import delete, update
    from trading_contracts.enums import Environment
    from trading_persistence import models as m
    from trading_persistence.audit import (
        append_audit_record,
        export_checkpoint,
        verify_against_checkpoint,
        verify_audit_chain,
    )

    with engine.begin() as conn:
        conn.execute(delete(m.AuditEvent))
        for index in range(3):
            append_audit_record(
                conn,
                actor_ref="owner",
                action=f"PROMOTE_{index}",
                environment=Environment.PAPER,
                occurred_at=NOW,
            )
    with engine.connect() as conn:
        assert verify_audit_chain(conn) == []
        checkpoint = export_checkpoint(conn)
        assert checkpoint.last_sequence_no == 3
        assert verify_against_checkpoint(conn, checkpoint) == []

    # Out-of-band mutation breaks verification (the DoD).
    with engine.begin() as conn:
        conn.execute(
            update(m.AuditEvent)
            .where(m.AuditEvent.sequence_no == 2)
            .values(actor_ref="attacker")
        )
    with engine.connect() as conn:
        assert verify_audit_chain(conn) != []

    # Deleting the tail is caught by the offsite checkpoint after restore.
    with engine.begin() as conn:
        conn.execute(
            update(m.AuditEvent)
            .where(m.AuditEvent.sequence_no == 2)
            .values(actor_ref="owner")  # undo mutation
        )
        conn.execute(delete(m.AuditEvent).where(m.AuditEvent.sequence_no == 3))
    with engine.connect() as conn:
        problems = verify_against_checkpoint(conn, checkpoint)
        assert any("truncated" in p for p in problems)
    with engine.begin() as conn:
        conn.execute(delete(m.AuditEvent))


@pg
def test_rbac_sink_appends_chained_records(engine: Any) -> None:
    from dataclasses import dataclass as dc

    from sqlalchemy import delete
    from trading_contracts.enums import Environment
    from trading_persistence import models as m
    from trading_persistence.audit import PersistentAuditSink, verify_audit_chain

    @dc(frozen=True)
    class Decision:
        allowed: bool
        principal_id: str
        action: str
        reason: str

    with engine.begin() as conn:
        conn.execute(delete(m.AuditEvent))
        sink = PersistentAuditSink(conn, Environment.PAPER)
        sink.record(Decision(False, "viewer", "CHANGE_SECRETS", "no role permits"))
        sink.record(Decision(True, "owner", "VIEW_DASHBOARD", "granted"))
    with engine.connect() as conn:
        assert verify_audit_chain(conn) == []
    with engine.begin() as conn:
        conn.execute(delete(m.AuditEvent))


def test_secret_material_rejected_in_detail() -> None:
    from unittest.mock import MagicMock

    from trading_contracts.enums import Environment
    from trading_persistence.audit import append_audit_record

    with pytest.raises(AuditIntegrityError):
        append_audit_record(
            MagicMock(),
            actor_ref="user",
            action="X",
            environment=Environment.PAPER,
            occurred_at=NOW,
            detail="password=hunter2-fake",
        )
