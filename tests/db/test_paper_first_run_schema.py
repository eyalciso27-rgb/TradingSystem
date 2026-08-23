"""A TRUE first database initialisation, against a real empty PostgreSQL.

The owner host has no PAPER container, no volume, no ``trading-paper`` network
and no Compose project, so the next run creates the cluster from nothing. A
cluster created moments ago authenticates and answers ``SELECT 1`` perfectly
while holding none of the application tables -- so the probe's preflight, which
covered reachability and authentication, would have passed it straight through
to the broker. The first evidence of an empty schema would then have come from
``DurableExecutionHistory``, with two IBKR sessions already open.

These tests use a genuinely empty database, not a fixture-migrated one: the
shared ``schema_at_head`` fixture in this directory keeps the configured
database at head, which is exactly the state a first-run test must not start
from. Each test therefore creates its own database and drops it afterwards.

Nothing here touches the configured database's schema, and nothing here drops a
database it did not create.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from trading_persistence.schema_preparation import (
    SchemaPreparationError,
    current_revision,
    head_revision,
    prepare_database,
    verify_ready,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs a real PostgreSQL server (CI's postgres service)",
)


@pytest.fixture
def empty_database() -> Iterator[str]:
    """A brand-new, completely empty database. Dropped afterwards.

    ``CREATE DATABASE`` cannot run inside a transaction, hence AUTOCOMMIT. The
    name is unique per test so a leftover from a crashed run never collides.
    """
    admin_url = make_url(os.environ["DATABASE_URL"])
    name = f"firstrun_{uuid.uuid4().hex[:16]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            # FORCE detaches any connection this test left behind. Only ever
            # applied to the database this fixture itself created.
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_a_brand_new_database_starts_with_no_schema_at_all(
    empty_database: str,
) -> None:
    """The precondition. Without this the rest could pass against a migrated DB."""
    engine = create_engine(empty_database)
    try:
        assert current_revision(engine) is None
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_preparation_brings_a_fresh_database_to_alembic_head(
    empty_database: str,
) -> None:
    """The owner's first run: nothing, then the canonical schema."""
    result = prepare_database(empty_database)

    assert result.first_run is True
    assert result.upgraded is True
    assert result.previous_revision is None
    assert result.revision == head_revision()

    engine = create_engine(empty_database)
    try:
        assert current_revision(engine) == head_revision()
        assert "audit_events" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_the_durable_audit_dependency_is_genuinely_queryable(
    empty_database: str,
) -> None:
    """Not "a table exists" -- the actual production reads must succeed.

    ``verify_ready`` runs the same query ``DurableExecutionAuditSink.can_persist``
    performs and the same ``load_decision_records`` the history reader uses. A
    check that only inspected ``information_schema`` would accept a schema at
    the wrong revision or one a partial migration left inconsistent.
    """
    prepare_database(empty_database)

    engine = create_engine(empty_database)
    try:
        verify_ready(engine)  # must not raise

        from trading_persistence.execution_audit import load_decision_records

        with engine.connect() as connection:
            assert load_decision_records(connection, "no-such-decision") == []
    finally:
        engine.dispose()


def test_an_unmigrated_database_is_refused_rather_than_accepted(
    empty_database: str,
) -> None:
    """The exact state that used to reach the broker: authenticated, no schema."""
    engine = create_engine(empty_database)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1

        with pytest.raises(SchemaPreparationError):
            verify_ready(engine)
    finally:
        engine.dispose()


def test_a_second_preparation_is_idempotent_and_changes_nothing(
    empty_database: str,
) -> None:
    """Re-running must be a no-op, not a re-migration and never a reset."""
    first = prepare_database(empty_database)

    engine = create_engine(empty_database)
    try:
        tables_before = sorted(inspect(engine).get_table_names())
        with engine.connect() as connection:
            versions_before = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
    finally:
        engine.dispose()

    second = prepare_database(empty_database)

    assert second.upgraded is False
    assert second.first_run is False
    assert second.previous_revision == first.revision
    assert second.revision == first.revision

    engine = create_engine(empty_database)
    try:
        assert sorted(inspect(engine).get_table_names()) == tables_before
        with engine.connect() as connection:
            versions_after = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
        assert versions_after == versions_before
        assert len(versions_after) == 1
    finally:
        engine.dispose()


def test_preparation_does_not_touch_the_configured_database(
    empty_database: str,
) -> None:
    """Preparing one database must not reach into another.

    ``env.py`` reads DATABASE_URL, so an explicit URL has to be injected into
    the environment for the duration of the upgrade. That injection must be
    scoped and restored, or a later caller silently migrates the wrong database.
    """
    before = os.environ.get("DATABASE_URL")

    prepare_database(empty_database)

    assert os.environ.get("DATABASE_URL") == before
