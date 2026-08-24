"""The object store must be proven usable before any IBKR socket.

``preflight()`` promised to validate every purely local precondition before a
single network resource opened. It did not keep that promise for the object
store: it only did ``Path(raw_store)``, which proves that Python can hold a
string. The real ``FilesystemObjectStorage`` creates its root on construction
and writes a temporary file on ``put``, and that construction happened in
``build_service`` AFTER ``adapter.connect()`` and ``session.connect()`` -- so an
unwritable or invalid object-store root failed with two real IBKR sessions
already open. That is the exact ordering violation the DATABASE_URL fix existed
to remove, in a different dependency.

Now ``preflight()`` builds the real adapter, writes a fixed content-addressed
probe payload, reads it back, and RETURNS that proven instance so the service
uses the very object that was verified -- not a fresh one created after the
sockets.

Nothing here connects to anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from trading_execution_agent import __main__ as probe
from trading_execution_agent.safe_logging import clear_sensitive_values
from trading_persistence.object_storage import FilesystemObjectStorage

ALIAS = "paper-poi"
PAPER_ACCOUNT = "DU" + "1234567"
HOST = "127.0.0.1"
PAPER_PORT = 4002


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "SECRETS_ROOT",
        "TRADING_PAPER_ACCOUNT_ID",
        "DATABASE_URL",
        "TRADING_OBJECT_STORE",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_sensitive_values()
    yield
    clear_sensitive_values()


def _secret_store(root: Path, account_id: str = PAPER_ACCOUNT) -> Path:
    secret = root / "PAPER" / "ibkr" / "paper-account-id"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(account_id + "\n", encoding="utf-8")
    return root


class _ConnectionTripwire:
    """Fails the test loudly if anything tries to construct or connect."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append("connect")
        raise AssertionError(
            "a network resource was opened before the object store was proven"
        )


def _no_connections(monkeypatch: pytest.MonkeyPatch) -> _ConnectionTripwire:
    tripwire = _ConnectionTripwire()
    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter", tripwire
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session", tripwire
    )
    monkeypatch.setattr("trading_persistence.db.create_sync_engine", tripwire)
    return tripwire


def _configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str) -> None:
    """A shell where only the object store is in question."""
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost/paper")
    monkeypatch.setenv("TRADING_OBJECT_STORE", store)


# == A. an unusable object store opens zero IBKR sessions ====================


def test_an_unusable_object_store_opens_zero_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A root that cannot be created must be caught before a single socket.

    A regular file where a directory must be: ``mkdir(parents=True)`` on a path
    under it fails, on POSIX and on Windows alike.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    _configured(monkeypatch, tmp_path, str(blocker / "objects"))
    tripwire = _no_connections(monkeypatch)

    with pytest.raises(probe.ProbePreflightError) as refused:
        probe.build_service(
            symbol="AAPL",
            account_alias=ALIAS,
            host=HOST,
            port=PAPER_PORT,
            equity_cap=Decimal("50000"),
        )

    assert tripwire.calls == [], "a network resource was opened"
    assert "object store" in str(refused.value)


def test_the_object_store_is_proven_after_the_database_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ordering within preflight: a missing DATABASE_URL is reported as itself,
    not masked by an object-store failure, and neither opens a socket."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("TRADING_OBJECT_STORE", str(blocker / "objects"))
    # DATABASE_URL deliberately unset.
    tripwire = _no_connections(monkeypatch)

    with pytest.raises(probe.ProbePreflightError, match="DATABASE_URL"):
        probe.build_service(
            symbol="AAPL",
            account_alias=ALIAS,
            host=HOST,
            port=PAPER_PORT,
            equity_cap=Decimal("50000"),
        )

    assert tripwire.calls == []


# == B. a writable object store passes, and the proven instance is returned ==


def test_a_writable_object_store_passes_preflight_and_is_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """preflight returns the REAL, proven adapter, holding the probe payload."""
    store = tmp_path / "objects"
    _configured(monkeypatch, tmp_path, str(store))

    config = probe.preflight(
        account_alias=ALIAS, host=HOST, port=PAPER_PORT, equity_cap=Decimal("50000")
    )

    assert isinstance(config.object_store, FilesystemObjectStorage)
    # The exact payload preflight wrote is retrievable through the real adapter.
    ref = config.object_store.put(probe._OBJECT_STORE_PROBE_PAYLOAD).ref
    assert config.object_store.get(ref) == probe._OBJECT_STORE_PROBE_PAYLOAD
    # And it actually reached disk under the configured root.
    assert any(store.rglob("*")), "nothing was written under the object-store root"


def test_the_probe_payload_write_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Content-addressed and idempotent: proving readiness never grows the store."""
    store = tmp_path / "objects"
    _configured(monkeypatch, tmp_path, str(store))

    probe.preflight(
        account_alias=ALIAS, host=HOST, port=PAPER_PORT, equity_cap=Decimal("50000")
    )
    files_after_first = sorted(p for p in store.rglob("*") if p.is_file())
    probe.preflight(
        account_alias=ALIAS, host=HOST, port=PAPER_PORT, equity_cap=Decimal("50000")
    )
    files_after_second = sorted(p for p in store.rglob("*") if p.is_file())

    assert files_after_first == files_after_second
    assert len(files_after_first) == 1, "one content-addressed object, not many"


# == C. the default wrapper location works ===================================


def test_the_default_object_store_location_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wrapper sets TRADING_OBJECT_STORE=var/objects/paper; unset, the code
    falls back to its own default. Both are relative, so run from a temp cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost/paper")
    monkeypatch.setenv("TRADING_OBJECT_STORE", "var/objects/paper")

    config = probe.preflight(
        account_alias=ALIAS, host=HOST, port=PAPER_PORT, equity_cap=Decimal("50000")
    )

    assert isinstance(config.object_store, FilesystemObjectStorage)
    assert (tmp_path / "var" / "objects" / "paper").is_dir()


def test_the_built_in_default_is_used_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost/paper")
    # TRADING_OBJECT_STORE unset by the autouse fixture.

    config = probe.preflight(
        account_alias=ALIAS, host=HOST, port=PAPER_PORT, equity_cap=Decimal("50000")
    )

    assert isinstance(config.object_store, FilesystemObjectStorage)
    assert (tmp_path / probe.DEFAULT_OBJECT_STORE).is_dir()


# == D. an object-store failure never leaks the path or a traceback ==========


def test_an_object_store_failure_prints_no_path_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure names TRADING_OBJECT_STORE and the type, never the path."""
    distinctive = tmp_path / "blocker-9f3a-tenant"
    distinctive.write_text("x", encoding="utf-8")
    _configured(monkeypatch, tmp_path, str(distinctive / "secret-looking-subdir"))
    _no_connections(monkeypatch)

    status = probe.main(["probe", "--symbol", "AAPL"])

    output = capsys.readouterr().out
    assert status == probe.EXIT_ERROR
    assert probe.PREFLIGHT_FAILED in output
    assert "Traceback" not in output
    # The configured path -- which on a real host can embed a home directory --
    # must not be echoed. The variable NAME is fine; the value is not.
    assert "blocker-9f3a-tenant" not in output
    assert "secret-looking-subdir" not in output
    assert "TRADING_OBJECT_STORE" in output, "the owner is told what to configure"
