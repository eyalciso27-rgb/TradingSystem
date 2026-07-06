"""Replay / as-of visibility tests (IMP-0202).

Done criterion: a replay query at ``as_of`` never sees a record whose
``available_at`` is in the future, and never sees a record already superseded.

The logic tests run everywhere on an in-memory SQLite engine (the predicate is
dialect-neutral); the real-PostgreSQL test runs in CI where DATABASE_URL is set.
"""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import DateTime, Text, create_engine, insert, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from trading_persistence.point_in_time import NaiveAsOfError, as_of_clause, select_as_of

AS_OF = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
BEFORE = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
EARLY = datetime(2026, 7, 5, 9, 30, tzinfo=UTC)
AFTER = datetime(2026, 7, 5, 11, 0, tzinfo=UTC)


class _LogicBase(DeclarativeBase):
    pass


class _Record(_LogicBase):
    """Minimal point-in-time row for dialect-neutral logic tests."""

    __tablename__ = "pit_records"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@pytest.fixture()
def logic_conn() -> Iterator[Any]:
    engine = create_engine("sqlite://")
    _LogicBase.metadata.create_all(engine)
    with engine.connect() as conn:
        rows = [
            {"id": "visible", "available_at": BEFORE, "superseded_at": None},
            {"id": "future", "available_at": AFTER, "superseded_at": None},
            {"id": "superseded", "available_at": BEFORE, "superseded_at": EARLY},
            {"id": "superseded-later", "available_at": BEFORE, "superseded_at": AFTER},
        ]
        conn.execute(insert(_Record), rows)
        yield conn
    engine.dispose()


def test_replay_never_sees_future_or_superseded_records(logic_conn: Any) -> None:
    visible = {row.id for row in logic_conn.execute(select_as_of(_Record, AS_OF))}
    assert visible == {"visible", "superseded-later"}
    assert "future" not in visible  # available_at > as_of is invisible
    assert "superseded" not in visible  # superseded before as_of is invisible


def test_record_becomes_visible_exactly_at_available_at(logic_conn: Any) -> None:
    at_availability = {row.id for row in logic_conn.execute(select_as_of(_Record, AFTER))}
    assert "future" in at_availability  # visible once as_of reaches available_at
    assert "superseded-later" not in at_availability  # superseded at that moment


def test_as_of_clause_composes_with_other_filters(logic_conn: Any) -> None:
    stmt = select(_Record.id).where(as_of_clause(_Record, AS_OF), _Record.id == "visible")
    assert [r.id for r in logic_conn.execute(stmt)] == ["visible"]


def test_naive_as_of_rejected() -> None:
    with pytest.raises(NaiveAsOfError):
        select_as_of(_Record, datetime(2026, 7, 5, 10, 0))  # noqa: DTZ001


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="PostgreSQL only in CI")
def test_source_events_as_of_on_postgresql(connection: Any) -> None:
    from trading_persistence import models as m

    hash_ = "sha256:" + "e" * 64
    workspace_id = uuid4()
    connection.execute(
        insert(m.Workspace).values(id=workspace_id, name="ws", created_at=AS_OF, updated_at=AS_OF)
    )

    def event(available: datetime, superseded: datetime | None) -> Any:
        event_id = uuid4()
        connection.execute(
            insert(m.SourceEvent).values(
                id=event_id,
                workspace_id=workspace_id,
                source_type="TELEGRAM",
                event_type="MESSAGE_CREATED",
                observed_at=BEFORE,
                ingested_at=BEFORE,
                available_at=available,
                superseded_at=superseded,
                content_hash=hash_,
                created_at=BEFORE,
            )
        )
        return event_id

    visible_id = event(BEFORE, None)
    future_id = event(AFTER, None)
    superseded_id = event(BEFORE, EARLY)

    ids = {row.id for row in connection.execute(select_as_of(m.SourceEvent, AS_OF))}
    assert visible_id in ids
    assert future_id not in ids
    assert superseded_id not in ids

    # Once as_of reaches its availability moment, the record becomes visible.
    later_ids = {row.id for row in connection.execute(select_as_of(m.SourceEvent, AFTER))}
    assert future_id in later_ids
    assert superseded_id not in later_ids  # superseded stays invisible

    # Timezone-aware round trip on PostgreSQL.
    stored = connection.execute(
        select(m.SourceEvent.available_at).where(m.SourceEvent.id == future_id)
    ).scalar_one()
    assert stored.tzinfo is not None
    assert stored == AFTER
