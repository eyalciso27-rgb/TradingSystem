"""The owner-safe probe: it must not submit, and must not leak.

Two properties, and both are structural rather than behavioural. The probe has
no submission path in it at all — not a disabled one — and everything it opens
is closed again, including on failure. A probe that leaks a gateway session or a
database transaction cannot be run twice.

Offline: nothing here connects to anything.
"""

from __future__ import annotations

import ast
from decimal import Decimal
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
    assert "execute" not in called, "the probe never calls execute()"
    # It calls collect_evidence(), which gathers independent read-only
    # observations and internally runs evaluate() — neither can submit.
    assert "collect_evidence" in called
    assert "process" not in called, "the agent's submit path is never entered"


def test_the_probe_never_supplies_an_authorization() -> None:
    """It uses the production authority, which denies. Belt and braces."""
    source = _source()

    assert "authorization=" not in source
    assert "the production default DENIES" in source


def test_the_probe_offers_no_live_mode() -> None:
    with pytest.raises(SystemExit):
        probe.main(["probe", "--mode", "live"])


class _Closeable:
    """Stands in for a gateway session, an adapter or an engine."""

    def __init__(self, *, fails: bool = False) -> None:
        self.closed = False
        self._fails = fails

    def connect(self, *args: object, **kw: object) -> None:
        return None

    def disconnect(self) -> None:
        if self._fails:
            raise RuntimeError("already gone")
        self.closed = True

    def dispose(self) -> None:
        if self._fails:
            raise RuntimeError("already gone")
        self.closed = True


def test_a_failure_after_the_adapter_opens_still_disconnects_it(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged ownership: what opened first is released when a later step fails.

    The earlier design built every resource and only then returned something
    closeable, so a failure in the second step stranded the first. Each resource
    is now registered for cleanup the moment it opens.
    """
    adapter = _Closeable()
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", "DU" + "1234567")
    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter",
        lambda **kw: adapter,
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session",
        _raise("the market data session could not be created"),
    )

    with pytest.raises(RuntimeError, match="market data session"):
        probe.build_service(
            symbol="AAPL", account_alias="paper-poi", host="127.0.0.1",
            port=4002, equity_cap=Decimal("50000"),
        )

    assert adapter.closed, "the adapter opened, so the adapter was closed"


def test_a_failure_after_the_session_opens_releases_both(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, session = _Closeable(), _Closeable()
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", "DU" + "1234567")
    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter", lambda **kw: adapter
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session",
        lambda: session,
    )
    monkeypatch.setattr(
        "trading_persistence.db.create_sync_engine",
        _raise("the database engine could not be created"),
    )

    with pytest.raises(RuntimeError, match="database engine"):
        probe.build_service(
            symbol="AAPL", account_alias="paper-poi", host="127.0.0.1",
            port=4002, equity_cap=Decimal("50000"),
        )

    assert session.closed and adapter.closed, "both were released"


def test_a_failure_building_the_service_releases_the_engine_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, session, engine = _Closeable(), _Closeable(), _Closeable()
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", "DU" + "1234567")
    monkeypatch.setenv("TRADING_OBJECT_STORE", str(tmp_path / "objects"))
    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter", lambda **kw: adapter
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session", lambda: session
    )
    monkeypatch.setattr("trading_persistence.db.create_sync_engine", lambda: engine)
    monkeypatch.setattr(
        probe, "_build", _raise("the service could not be assembled")
    )

    with pytest.raises(RuntimeError, match="service could not be assembled"):
        probe.build_service(
            symbol="AAPL", account_alias="paper-poi", host="127.0.0.1",
            port=4002, equity_cap=Decimal("50000"),
        )

    assert engine.closed and session.closed and adapter.closed


def test_a_successful_construction_releases_everything_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, session, engine = _Closeable(), _Closeable(), _Closeable()
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", "DU" + "1234567")
    monkeypatch.setenv("TRADING_OBJECT_STORE", str(tmp_path / "objects"))
    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter", lambda **kw: adapter
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session", lambda: session
    )
    monkeypatch.setattr("trading_persistence.db.create_sync_engine", lambda: engine)
    monkeypatch.setattr(probe, "_build", lambda *a, **kw: object())

    resources = probe.build_service(
        symbol="AAPL", account_alias="paper-poi", host="127.0.0.1",
        port=4002, equity_cap=Decimal("50000"),
    )
    resources.close()
    resources.close()  # idempotent: an ExitStack unwinds once

    assert engine.closed and session.closed and adapter.closed


def test_one_resource_failing_to_close_never_strands_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, session, engine = _Closeable(), _Closeable(fails=True), _Closeable()
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", "DU" + "1234567")
    monkeypatch.setenv("TRADING_OBJECT_STORE", str(tmp_path / "objects"))
    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter", lambda **kw: adapter
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session", lambda: session
    )
    monkeypatch.setattr("trading_persistence.db.create_sync_engine", lambda: engine)
    monkeypatch.setattr(probe, "_build", lambda *a, **kw: object())

    probe.build_service(
        symbol="AAPL", account_alias="paper-poi", host="127.0.0.1",
        port=4002, equity_cap=Decimal("50000"),
    ).close()

    assert engine.closed and adapter.closed, "a broken close did not strand these"


def _raise(message: str) -> object:
    def boom(*args: object, **kw: object) -> object:
        raise RuntimeError(message)

    return boom


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
