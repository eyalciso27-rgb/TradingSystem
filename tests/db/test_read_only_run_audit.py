"""Real-PostgreSQL durability tests for immutable read-only run evidence."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from trading_persistence.audit import verify_audit_chain
from trading_persistence.object_storage import FilesystemObjectStorage
from trading_persistence.read_only_run_audit import (
    DurableReadOnlyRunAuditStore,
    ReadOnlyRunAuditError,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a real PostgreSQL server"
)


def _record(
    identity: str, *, run_input_ref: str = "mvp-runtime-input:sha256:abc"
) -> dict[str, object]:
    return {
        "record_version": "mvp-read-only-run-1.0.0",
        "task_id": "M7-READONLY-RUNTIME-RUNNER-AUDIT",
        "run_id": identity,
        "run_identity": identity,
        "run_input_ref": run_input_ref,
        "as_of": datetime(2026, 9, 9, 12, tzinfo=UTC).isoformat(),
        "environment": "PAPER",
        "strategy_id": "baseline-trend-breakout-v1",
        "strategy_version": "v1",
        "strategy_fingerprint": "sha256:frozen",
        "result": "NO_TRADE",
        "terminal_stage": "DECISION",
        "reason_codes": ["DECISION_REJECT"],
        "input_snapshot_ref": run_input_ref,
        "decision_ref": "decision:example",
        "risk_ref": None,
        "allocation_ref": None,
        "evaluated_quantity": None,
        "read_only": True,
        "seal_ref": None,
        "input_evidence": [{"input_name": "bars", "quality_status": "FIXTURE"}],
    }


@pytest.fixture
def store(engine: Engine, tmp_path: Path) -> DurableReadOnlyRunAuditStore:
    return DurableReadOnlyRunAuditStore(
        engine, FilesystemObjectStorage(tmp_path / "read-only-objects")
    )


def test_record_survives_and_replays_identically(
    store: DurableReadOnlyRunAuditStore,
) -> None:
    identity = f"read-only-run:M7:{uuid4()}"
    record = _record(identity)

    first = store.put_if_absent(identity, record)
    second = store.put_if_absent(identity, record)

    assert first == second == store.load(identity)


def test_conflicting_immutable_identity_fails_closed(
    store: DurableReadOnlyRunAuditStore,
) -> None:
    identity = f"read-only-run:M7:{uuid4()}"
    store.put_if_absent(identity, _record(identity))

    with pytest.raises(
        ReadOnlyRunAuditError, match="conflicting payload or provenance"
    ):
        store.put_if_absent(
            identity, _record(identity, run_input_ref="changed")
        )


def test_read_only_record_preserves_tamper_evident_chain(
    store: DurableReadOnlyRunAuditStore, engine: Engine
) -> None:
    identity = f"read-only-run:M7:{uuid4()}"
    store.put_if_absent(identity, _record(identity))

    with engine.connect() as connection:
        assert verify_audit_chain(connection) == []


def test_live_evidence_is_refused(store: DurableReadOnlyRunAuditStore) -> None:
    identity = f"read-only-run:M7:{uuid4()}"
    record = _record(identity)
    record["environment"] = "LIVE"

    with pytest.raises(ReadOnlyRunAuditError, match="may not target LIVE"):
        store.put_if_absent(identity, record)


def test_adapter_reuses_existing_audit_and_object_storage() -> None:
    from trading_persistence import read_only_run_audit

    source = Path(read_only_run_audit.__file__).read_text(encoding="utf-8")
    assert "__tablename__" not in source
    assert "CREATE TABLE" not in source.upper()
