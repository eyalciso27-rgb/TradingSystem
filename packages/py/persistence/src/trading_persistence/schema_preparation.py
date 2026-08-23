"""Bring a database to the canonical schema, and prove it is actually usable.

The gap this closes: a freshly created PostgreSQL cluster authenticates
perfectly and answers ``SELECT 1`` while containing none of the application
tables. The owner host has no PAPER container, volume, network or Compose
project, so the next run is a TRUE first initialisation -- and the probe's
preflight, which had grown to cover reachability and authentication, would have
passed it straight through to the broker. The first symptom of an empty schema
would then arrive from ``DurableExecutionHistory`` with two IBKR sessions
already open, which is precisely the ordering this work exists to prevent.

Alembic is the canonical mechanism and stays the only one. There is deliberately
no ``Base.metadata.create_all`` here: a second way to build the schema produces
databases that no migration path can reach, and the difference only shows up
when someone tries to upgrade one.

Only ``upgrade head``. No downgrade, no reset, no drop -- this runs against a
database that may already hold the durable execution history, and the safe
direction is the only direction offered.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, select
from sqlalchemy.engine import make_url

from trading_persistence.db import create_sync_engine, get_database_url
from trading_persistence.models import AuditEvent

#: ``.../packages/py/persistence``: this file is ``<pkg>/src/trading_persistence/``.
PERSISTENCE_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PERSISTENCE_DIR / "alembic.ini"
ALEMBIC_SCRIPTS = PERSISTENCE_DIR / "alembic"

#: A reference no decision will ever have. The production history query is run
#: against it so the check exercises the real read path without inventing a row.
_ABSENT_DECISION_REF = "schema-readiness-probe:no-such-decision"


class SchemaPreparationError(RuntimeError):
    """The schema could not be brought to, or proven at, the canonical head."""


@dataclass(frozen=True, slots=True)
class PreparationResult:
    """What preparation did. Carries revisions, never a URL or a credential."""

    previous_revision: str | None
    revision: str
    #: True when this call moved the database; False when it was already at head.
    upgraded: bool

    @property
    def first_run(self) -> bool:
        """No ``alembic_version`` at all before this: a true first initialisation."""
        return self.previous_revision is None


def alembic_config() -> Config:
    """The repository's own Alembic configuration.

    ``script_location`` is set explicitly rather than relying on the process
    working directory, because this runs from a wrapper that has already
    ``cd``-ed and from tests that have not.
    """
    if not ALEMBIC_INI.is_file():
        raise SchemaPreparationError(
            f"the Alembic configuration is missing at {ALEMBIC_INI}"
        )
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    return config


def head_revision() -> str:
    """The revision this repository considers canonical."""
    script = ScriptDirectory.from_config(alembic_config())
    head = script.get_current_head()
    if head is None:
        raise SchemaPreparationError("the migration history has no head revision")
    return head


def current_revision(engine: Engine) -> str | None:
    """The database's own revision, or None when it has never been migrated."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


#: Seconds. Bounded so an unreachable host fails as a failure rather than as a
#: hang: this runs in the owner's startup path, ahead of the broker, and a step
#: that never returns is worse than one that reports trouble.
CONNECT_TIMEOUT_SECONDS = 10


def _with_connect_timeout(url: str) -> str:
    """The same URL, with a bounded libpq connect timeout unless one is set.

    Applied through the URL rather than ``connect_args`` because Alembic's
    ``env.py`` builds its own engine from ``DATABASE_URL``; a timeout passed
    only to our engine would leave the migration's own connection unbounded.
    """
    parsed = make_url(url)
    if "connect_timeout" in parsed.query:
        return url
    return parsed.update_query_dict(
        {"connect_timeout": str(CONNECT_TIMEOUT_SECONDS)}
    ).render_as_string(hide_password=False)


@contextmanager
def _database_url(url: str) -> Iterator[None]:
    """Alembic's ``env.py`` reads DATABASE_URL, so the effective URL is set here.

    Restored afterwards: a helper that permanently rewrites the environment of
    its caller is a trap for the next reader, and here it would mean a later
    call silently migrating a different database.
    """
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def verify_ready(engine: Engine) -> None:
    """Prove the schema is at head AND that the durable dependencies read.

    Two separate questions. ``alembic_version`` says what someone intended;
    running the real queries says what the database can actually do. Checking
    only the version table would accept a schema that a partial or manually
    edited migration left inconsistent, and checking only that some table exists
    would accept a schema at the wrong revision.
    """
    expected = head_revision()
    actual = current_revision(engine)
    if actual != expected:
        raise SchemaPreparationError(
            "the database schema is not at the canonical revision"
            f" (found {actual!r}, expected {expected!r})"
        )
    # The exact read DurableExecutionAuditSink.can_persist performs, and the
    # exact production query DurableExecutionHistoryReader runs. Both are
    # read-only; neither invents a row to prove the table works.
    from trading_persistence.execution_audit import load_decision_records

    with engine.connect() as connection:
        connection.execute(
            select(AuditEvent.sequence_no)
            .order_by(AuditEvent.sequence_no.desc())
            .limit(1)
        ).one_or_none()
        load_decision_records(connection, _ABSENT_DECISION_REF)


def prepare_database(url: str | None = None) -> PreparationResult:
    """Upgrade to head if needed, then prove it. Idempotent by construction.

    A second run finds the database already at head, applies nothing, and still
    verifies -- so "already prepared" and "freshly prepared" end in the same
    proven state rather than in an assumption.
    """
    effective = _with_connect_timeout(url or get_database_url())
    with _database_url(effective):
        engine = create_sync_engine(effective)
        try:
            previous = current_revision(engine)
            expected = head_revision()
            if previous != expected:
                command.upgrade(alembic_config(), "head")
            verify_ready(engine)
            return PreparationResult(
                previous_revision=previous,
                revision=expected,
                upgraded=previous != expected,
            )
        finally:
            engine.dispose()


__all__ = [
    "ALEMBIC_INI",
    "ALEMBIC_SCRIPTS",
    "PERSISTENCE_DIR",
    "PreparationResult",
    "SchemaPreparationError",
    "alembic_config",
    "current_revision",
    "head_revision",
    "prepare_database",
    "verify_ready",
]
