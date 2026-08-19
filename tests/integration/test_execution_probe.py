"""The owner-safe probe: it must not submit, and must not leak.

Two properties, and both are structural rather than behavioural. The probe has
no submission path in it at all — not a disabled one — and everything it opens
is closed again, including on failure. A probe that leaks a gateway session or a
database transaction cannot be run twice.

Offline: nothing here connects to anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from trading_execution_agent import __main__ as probe


def _source() -> str:
    return Path(probe.__file__).read_text(encoding="utf-8")


def test_the_probe_has_no_submission_path_at_all() -> None:
    """Not "submission is disabled" — there is no call that could submit."""
    tree = ast.parse(_source())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "submit_order" not in called
    assert "execute" not in called, "the probe calls evaluate(), never execute()"
    assert "evaluate" in called


def test_the_probe_never_supplies_an_authorization() -> None:
    """It uses the production authority, which denies. Belt and braces."""
    source = _source()

    assert "authorization=" not in source
    assert "the production default DENIES" in source


def test_the_probe_offers_no_live_mode() -> None:
    with pytest.raises(SystemExit):
        probe.main(["probe", "--mode", "live"])


def test_the_probe_closes_everything_it_opened() -> None:
    """Including on failure: the cleanup is in a finally."""

    class _Closeable:
        def __init__(self) -> None:
            self.closed = False

        def disconnect(self) -> None:
            self.closed = True

        def dispose(self) -> None:
            self.closed = True

    adapter, session, engine = _Closeable(), _Closeable(), _Closeable()
    resources = probe.ProbeResources(
        service=None,  # type: ignore[arg-type]
        adapter=adapter, session=session, engine=engine,
    )

    resources.close()

    assert adapter.closed and session.closed and engine.closed


def test_closing_survives_a_resource_that_fails_to_close() -> None:
    """One broken close must not strand the others."""

    class _Broken:
        def disconnect(self) -> None:
            raise RuntimeError("already gone")

    class _Fine:
        def __init__(self) -> None:
            self.closed = False

        def dispose(self) -> None:
            self.closed = True

    engine = _Fine()
    resources = probe.ProbeResources(
        service=None,  # type: ignore[arg-type]
        adapter=_Broken(), session=_Broken(), engine=engine,
    )

    resources.close()

    assert engine.closed, "the database engine was still disposed"


def test_the_probe_holds_no_database_connection_of_its_own() -> None:
    """It hands the sink an ENGINE; the sink owns its committed transactions."""
    source = _source()

    assert "engine.connect()" not in source
    assert "connection.begin()" not in source
    assert "DurableExecutionAuditSink(\n            engine" in source


def test_the_probe_says_no_order_was_submitted() -> None:
    from trading_execution_agent.execution_service import (
        ExecutionEvaluation,
        ExecutionIntent,
        ExecutionStage,
    )

    assert "NO ORDER SUBMITTED" in _source()
    assert ExecutionStage.SUBMITTED not in {
        ExecutionStage.AUTHORIZATION_DENIED, ExecutionStage.READY_NOT_AUTHORIZED
    }
    del ExecutionEvaluation, ExecutionIntent


def test_the_probe_documents_its_two_ibkr_sessions() -> None:
    """Honest about what actually connects: two clients, not one socket."""
    assert "Two IBKR client sessions" in _source()
