"""Real PostgreSQL migration and constraint tests (IMP-0201).

Skipped unless DATABASE_URL is set (CI provides a postgres service).
"""

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError
from trading_persistence import models as m
from trading_persistence.base import Base

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL not set; PostgreSQL migration tests run in CI only.",
        allow_module_level=True,
    )

NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
HASH = "sha256:" + "d" * 64


def _insert(conn: Connection, model: type[Any], **values: Any) -> Any:
    row_id = values.setdefault("id", uuid4())
    conn.execute(insert(model).values(**values))
    return row_id


def _full_chain(conn: Connection) -> dict[str, Any]:
    """Insert one valid row per core table in dependency order."""
    ids: dict[str, Any] = {}
    ids["workspace"] = _insert(conn, m.Workspace, name="ws", created_at=NOW, updated_at=NOW)
    ids["instrument"] = _insert(
        conn,
        m.Instrument,
        symbol="AAPL",
        venue="NASDAQ",
        asset_class="STOCK",
        currency="USD",
        status="ACTIVE",
        created_at=NOW,
        updated_at=NOW,
    )
    ids["raw_object"] = _insert(
        conn,
        m.RawObject,
        uri="obj://a",
        mime_type="text/plain",
        size_bytes=10,
        content_hash=HASH,
        pinned=False,
        created_at=NOW,
    )
    ids["source_event"] = _insert(
        conn,
        m.SourceEvent,
        workspace_id=ids["workspace"],
        source_type="TELEGRAM",
        external_event_id="evt-1",
        event_type="MESSAGE_CREATED",
        observed_at=NOW,
        ingested_at=NOW,
        available_at=NOW,
        content_hash=HASH,
        raw_object_id=ids["raw_object"],
        created_at=NOW,
    )
    _insert(
        conn,
        m.SocialMessageObservation,
        source_event_id=ids["source_event"],
        source_type="TELEGRAM",
        access_mode="TELEGRAM_USER_TDLIB",
        connector_id="conn-1",
        source_account_id="acct-1",
        channel_external_id="-100",
        external_message_id="55",
        received_at=NOW,
        message_dedup_key=HASH,
    )
    ids["candidate"] = _insert(
        conn,
        m.TradeCandidate,
        workspace_id=ids["workspace"],
        instrument_id=ids["instrument"],
        strategy_id="breakout",
        strategy_version="1.0.0",
        direction="LONG",
        status="NEW",
        created_at=NOW,
        updated_at=NOW,
    )
    _insert(
        conn,
        m.SignalAssessment,
        candidate_id=ids["candidate"],
        instrument_id=ids["instrument"],
        signal_family="TECHNICAL",
        model_id="technical.breakout",
        model_version="1.0.0",
        direction="LONG",
        status="TRIGGERED",
        score=Decimal("0.72"),
        calibrated_probability=Decimal("0.6"),
        calibration_status="HEURISTIC",
        observed_at=NOW,
        ingested_at=NOW,
        available_at=NOW,
        created_at=NOW,
    )
    ids["decision"] = _insert(
        conn,
        m.DecisionRun,
        candidate_id=ids["candidate"],
        strategy_version="1.0.0",
        decision_policy_version="1.0.0",
        processing_profile="STANDARD",
        outcome="APPROVE",
        action_tier="MANUAL",
        data_quality=Decimal("0.9"),
        started_at=NOW,
        completed_at=NOW,
        latency_ms=12,
        reason_codes=[],
        decision_hash=HASH,
        created_at=NOW,
    )
    ids["plan"] = _insert(
        conn,
        m.SystemTradePlan,
        decision_run_id=ids["decision"],
        instrument_id=ids["instrument"],
        direction="LONG",
        environment="PAPER",
        plan_version=1,
        status="APPROVED",
        thesis="breakout",
        invalidation_summary="close below 24.5",
        max_loss_amount=Decimal("200.00"),
        currency="USD",
        valid_from=NOW,
        created_at=NOW,
    )
    ids["risk"] = _insert(
        conn,
        m.RiskEvaluation,
        decision_run_id=ids["decision"],
        trade_plan_id=ids["plan"],
        outcome="APPROVE",
        approved_quantity=Decimal("100"),
        approved_risk_amount=Decimal("200.00"),
        currency="USD",
        risk_policy_version="1.0.0",
        evaluated_at=NOW,
        created_at=NOW,
    )
    ids["broker"] = _insert(conn, m.Broker, name="mock", enabled=True, created_at=NOW)
    ids["account"] = _insert(
        conn,
        m.BrokerAccount,
        broker_id=ids["broker"],
        account_alias="paper-1",
        environment="PAPER",
        currency="USD",
        created_at=NOW,
        updated_at=NOW,
    )
    ids["intent"] = _insert(
        conn,
        m.OrderIntent,
        trade_plan_id=ids["plan"],
        risk_evaluation_id=ids["risk"],
        broker_account_id=ids["account"],
        environment="PAPER",
        operating_mode="PAPER_MANUAL",
        order_role="ENTRY",
        side="BUY",
        order_type="STOP",
        quantity=Decimal("100"),
        stop_price=Decimal("25.00"),
        time_in_force="DAY",
        idempotency_key="idem-1",
        status="CREATED",
        created_at=NOW,
    )
    ids["broker_order"] = _insert(
        conn,
        m.BrokerOrder,
        order_intent_id=ids["intent"],
        broker_account_id=ids["account"],
        external_order_ref="ext-1",
        current_status="ACKNOWLEDGED",
        updated_at=NOW,
    )
    _insert(
        conn,
        m.BrokerOrderEvent,
        broker_order_id=ids["broker_order"],
        order_state="PARTIALLY_FILLED",
        filled_quantity=Decimal("40"),
        remaining_quantity=Decimal("60"),
        last_fill_quantity=Decimal("40"),
        average_fill_price=Decimal("25.10"),
        currency="USD",
        event_at=NOW,
        created_at=NOW,
    )
    _insert(
        conn,
        m.Fill,
        broker_order_id=ids["broker_order"],
        external_execution_ref="exec-1",
        executed_at=NOW,
        quantity=Decimal("40"),
        price=Decimal("25.10"),
        currency="USD",
    )
    ids["position"] = _insert(
        conn,
        m.Position,
        broker_account_id=ids["account"],
        instrument_id=ids["instrument"],
        trade_plan_id=ids["plan"],
        environment="PAPER",
        strategy_version="1.0.0",
        side="BUY",
        quantity_open=Decimal("40"),
        average_entry_price=Decimal("25.10"),
        status="OPEN",
        opened_at=NOW,
        updated_at=NOW,
    )
    return ids


# --- Schema existence ---

def test_all_tables_created(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for name in Base.metadata.tables:
        assert name in tables


def test_key_constraints_present(engine: Engine) -> None:
    inspector = inspect(engine)
    uniques = {u["name"] for u in inspector.get_unique_constraints("order_intents")}
    assert "uq_order_intents_environment_broker_account_id_idempotency_key" in uniques
    checks = {c["name"] for c in inspector.get_check_constraints("order_intents")}
    assert "ck_order_intents_order_intents_quantity_positive" in checks


def test_key_indexes_present(engine: Engine) -> None:
    inspector = inspect(engine)
    indexes = {i["name"] for i in inspector.get_indexes("broker_orders")}
    assert "ix_broker_orders_external_order_ref" in indexes


# --- Positive flow ---

def test_full_chain_inserts(connection: Connection) -> None:
    ids = _full_chain(connection)
    assert ids["position"] is not None


def test_decimal_precision_round_trip(connection: Connection) -> None:
    ids = _full_chain(connection)
    row = connection.execute(
        select(m.Fill.quantity, m.Fill.price).where(m.Fill.broker_order_id == ids["broker_order"])
    ).one()
    assert row.quantity == Decimal("40.0000000000")
    assert row.price == Decimal("25.10000000")


def test_timestamps_are_timezone_aware(connection: Connection) -> None:
    ids = _full_chain(connection)
    observed = connection.execute(
        select(m.SourceEvent.observed_at).where(m.SourceEvent.id == ids["source_event"])
    ).scalar_one()
    assert observed.tzinfo is not None


def test_config_version_and_active_pointer_rollback(connection: Connection) -> None:
    common = dict(
        config_type="POLICY",
        config_id="risk.core",
        schema_version="1.0.0",
        environments=["PAPER_MANUAL"],
        enabled=True,
        owner="RISK",
        status="ACTIVE",
        content={},
        valid_from=NOW,
        created_at=NOW,
    )
    _insert(connection, m.ConfigVersion, version="0.1.0", **common)
    _insert(connection, m.ConfigVersion, version="0.2.0", **common)
    _insert(
        connection,
        m.ActiveVersionPointer,
        config_type="POLICY",
        config_id="risk.core",
        version="0.2.0",
        environment="PAPER",
        activated_at=NOW,
        activated_by="owner",
        reason="init",
        updated_at=NOW,
    )
    # Rollback = repoint version only.
    connection.execute(
        update(m.ActiveVersionPointer)
        .where(m.ActiveVersionPointer.environment == "PAPER")
        .values(version="0.1.0", reason="rollback")
    )
    active = connection.execute(
        select(m.ActiveVersionPointer.version).where(
            m.ActiveVersionPointer.environment == "PAPER"
        )
    ).scalar_one()
    assert active == "0.1.0"


def test_paper_and_live_pointers_separate(connection: Connection) -> None:
    for env, ver in (("PAPER", "0.1.0"), ("LIVE", "0.2.0")):
        _insert(
            connection,
            m.ActiveVersionPointer,
            config_type="POLICY",
            config_id="risk.core",
            version=ver,
            environment=env,
            activated_at=NOW,
            activated_by="owner",
            reason="init",
            updated_at=NOW,
        )
    rows = connection.execute(
        select(m.ActiveVersionPointer.environment, m.ActiveVersionPointer.version)
    ).all()
    assert {(r.environment, r.version) for r in rows} == {("PAPER", "0.1.0"), ("LIVE", "0.2.0")}


# --- Negative constraints ---

def test_duplicate_idempotency_key_rejected(connection: Connection) -> None:
    ids = _full_chain(connection)
    with pytest.raises(IntegrityError):
        _insert(
            connection,
            m.OrderIntent,
            trade_plan_id=ids["plan"],
            risk_evaluation_id=ids["risk"],
            broker_account_id=ids["account"],
            environment="PAPER",
            operating_mode="PAPER_MANUAL",
            order_role="ENTRY",
            side="BUY",
            order_type="STOP",
            quantity=Decimal("50"),
            time_in_force="DAY",
            idempotency_key="idem-1",  # duplicate within same env+account
            status="CREATED",
            created_at=NOW,
        )


def test_non_positive_quantity_rejected(connection: Connection) -> None:
    ids = _full_chain(connection)
    with pytest.raises(IntegrityError):
        _insert(
            connection,
            m.OrderIntent,
            trade_plan_id=ids["plan"],
            broker_account_id=ids["account"],
            environment="PAPER",
            operating_mode="PAPER_MANUAL",
            order_role="ENTRY",
            side="BUY",
            order_type="STOP",
            quantity=Decimal("0"),
            time_in_force="DAY",
            idempotency_key="idem-2",
            status="CREATED",
            created_at=NOW,
        )


def test_invalid_enum_value_rejected(connection: Connection) -> None:
    ids = _full_chain(connection)
    with pytest.raises(IntegrityError):
        _insert(
            connection,
            m.SourceEvent,
            workspace_id=ids["workspace"],
            source_type="MYSPACE",  # not a canonical SourceType
            event_type="X",
            observed_at=NOW,
            ingested_at=NOW,
            available_at=NOW,
            content_hash=HASH,
            created_at=NOW,
        )


def test_null_required_field_rejected(connection: Connection) -> None:
    with pytest.raises(IntegrityError):
        _insert(
            connection,
            m.Instrument,
            symbol=None,
            asset_class="STOCK",
            currency="USD",
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )


def test_broken_foreign_key_rejected(connection: Connection) -> None:
    with pytest.raises(IntegrityError):
        _insert(
            connection,
            m.InstrumentAlias,
            instrument_id=uuid4(),  # no such instrument
            alias="AAPL",
            alias_type="TICKER",
            valid_from=NOW,
        )


def test_duplicate_active_pointer_rejected(connection: Connection) -> None:
    for _ in range(2):
        try:
            _insert(
                connection,
                m.ActiveVersionPointer,
                config_type="POLICY",
                config_id="risk.core",
                version="0.1.0",
                environment="PAPER",
                activated_at=NOW,
                activated_by="o",
                reason="r",
                updated_at=NOW,
            )
        except IntegrityError:
            return
    pytest.fail("duplicate active pointer was not rejected")


def test_duplicate_config_version_rejected(connection: Connection) -> None:
    common = dict(
        config_type="POLICY",
        config_id="risk.core",
        version="0.1.0",
        schema_version="1.0.0",
        environments=["PAPER_MANUAL"],
        enabled=True,
        owner="RISK",
        status="ACTIVE",
        content={},
        valid_from=NOW,
        created_at=NOW,
    )
    _insert(connection, m.ConfigVersion, **common)
    with pytest.raises(IntegrityError):
        _insert(connection, m.ConfigVersion, **common)


def test_negative_filled_quantity_rejected(connection: Connection) -> None:
    ids = _full_chain(connection)
    with pytest.raises(IntegrityError):
        _insert(
            connection,
            m.BrokerOrderEvent,
            broker_order_id=ids["broker_order"],
            order_state="PARTIALLY_FILLED",
            filled_quantity=Decimal("-1"),
            remaining_quantity=Decimal("0"),
            event_at=NOW,
            created_at=NOW,
        )


# --- Migration lifecycle ---

def test_upgrade_downgrade_upgrade_cycle(engine: Engine, alembic_config: Any) -> None:
    from alembic import command

    command.downgrade(alembic_config, "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()
    command.upgrade(alembic_config, "head")
    tables = set(inspect(engine).get_table_names())
    assert "positions" in tables and "order_intents" in tables


def test_no_metadata_drift(engine: Engine, alembic_config: Any) -> None:
    from alembic import command
    from alembic.util.exc import AutogenerateDiffsDetected

    command.upgrade(alembic_config, "head")
    try:
        command.check(alembic_config)
    except AutogenerateDiffsDetected as error:  # pragma: no cover - failure path
        pytest.fail(f"metadata drift detected: {error}")
