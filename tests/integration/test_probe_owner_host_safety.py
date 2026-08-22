"""What the FIRST real owner-host probe run exposed.

Three defects, none of which any existing test could have caught, because all
three live in the seam between this repository and the owner host:

1. the promise that the paper account id is "read, never written, never
   printed" was false -- the IBKR SDK's own ``ibapi.utils`` logger echoed the
   raw ``managedAccounts`` payload at INFO;
2. an unset ``DATABASE_URL`` was discovered only AFTER two real IBKR sessions
   had been opened, which aborted startup mid-flight and left the SDK thread
   tearing down through ``serverVersion() == None``;
3. the probe demanded ``TRADING_PAPER_ACCOUNT_ID`` even though the owner host
   already keeps that value in the canonical PAPER secret store.

Nothing here connects to anything.
"""

from __future__ import annotations

import inspect
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from trading_execution_agent import __main__ as probe
from trading_execution_agent.safe_logging import (
    NOISY_BROKER_LOGGERS,
    SecretRedactingFilter,
    install_owner_output_protection,
    redact,
)

#: Shaped exactly like a real IBKR paper account, and never a real one.
PAPER_ACCOUNT = "DU" + "1234567"
ALIAS = "paper-poi"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited configuration; each test states what it needs."""
    for name in (
        "SECRETS_ROOT",
        "TRADING_PAPER_ACCOUNT_ID",
        "DATABASE_URL",
        "TRADING_OBJECT_STORE",
    ):
        monkeypatch.delenv(name, raising=False)


def _secret_store(root: Path, account_id: str = PAPER_ACCOUNT) -> Path:
    """The canonical PAPER secret layout the owner host already has."""
    secret = root / "PAPER" / "ibkr" / "paper-account-id"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(account_id + "\n", encoding="utf-8")
    return root


# == 1. the account id must not reach owner-visible output ==================


def test_the_managed_accounts_payload_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """THE regression. This is the exact line the real run printed.

    Not our logger: ``ibapi.utils`` emitted it. Redacting ``render()`` could
    never have caught this, which is why the filter sits on the root handlers.
    """
    install_owner_output_protection()
    ibapi_logger = logging.getLogger("ibapi.utils")
    ibapi_logger.setLevel(logging.INFO)

    with caplog.at_level(logging.INFO):
        caplog.handler.addFilter(SecretRedactingFilter())
        ibapi_logger.info("ANSWER managedAccounts {'accountsList': '%s'}", PAPER_ACCOUNT)

    printed = caplog.text
    assert PAPER_ACCOUNT not in printed, (
        f"the raw paper account id reached owner-visible output: {printed}"
    )
    assert "[account]" in printed, printed


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("paper account", f"accountsList: {PAPER_ACCOUNT}"),
        ("live account", "managedAccounts U7654321"),
        ("account list", f"{PAPER_ACCOUNT},DU7654321"),
        (
            "database dsn",
            "could not connect to postgresql://trading:hunter2@127.0.0.1:5432/paper",
        ),
        ("password kv", "POSTGRES_PASSWORD=hunter2 in environment"),
        ("token kv", "authorization: Bearer abc123def"),
    ],
)
def test_secret_shapes_never_survive_redaction(name: str, line: str) -> None:
    """Whoever emitted it, and whatever logger it came from."""
    cleaned = redact(line)

    for secret in (PAPER_ACCOUNT, "U7654321", "DU7654321", "hunter2", "abc123def"):
        assert secret not in cleaned, f"{name}: {secret} survived -> {cleaned}"


def test_the_broker_protocol_loggers_are_silenced_by_default() -> None:
    """Defence one: the payload is not emitted at all."""
    for name in NOISY_BROKER_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    install_owner_output_protection()

    for name in NOISY_BROKER_LOGGERS:
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_debug_broker_logging_keeps_the_redaction() -> None:
    """Defence two survives even when the owner asks for SDK chatter."""
    for name in NOISY_BROKER_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    install_owner_output_protection(debug=True)

    assert logging.getLogger("ibapi.utils").level == logging.NOTSET, (
        "debug mode leaves the SDK loggers alone"
    )
    root = logging.getLogger()
    assert any(isinstance(f, SecretRedactingFilter) for f in root.filters), (
        "the redaction filter is installed regardless"
    )


def test_the_probe_render_shows_the_alias_and_never_the_account() -> None:
    """Normal output names the configured alias, not the broker account."""
    import inspect

    source = inspect.getsource(probe.render)

    assert "account_alias" not in source or "account_id" not in source
    assert "TRADING_PAPER_ACCOUNT_ID" not in source
    assert PAPER_ACCOUNT not in source


# == 2. preflight before any socket =========================================


class _ConnectionTripwire:
    """Fails the test loudly if anything tries to connect."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append("connect")
        raise AssertionError(
            "a network resource was opened before local configuration was"
            " validated"
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


def test_a_missing_database_url_opens_zero_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE empirical failure, in test form.

    The real run opened the broker adapter AND the market-data session before
    discovering DATABASE_URL was unset. Nothing about that needed a socket.
    """
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    tripwire = _no_connections(monkeypatch)

    with pytest.raises(probe.ProbePreflightError, match="DATABASE_URL"):
        probe.build_service(
            symbol="AAPL",
            account_alias=ALIAS,
            host="127.0.0.1",
            port=4002,
            equity_cap=Decimal("50000"),
        )

    assert tripwire.calls == [], "a connection was attempted"


def test_a_missing_account_secret_opens_zero_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Canonical secret absent and no override: clean refusal, no sockets."""
    empty = tmp_path / "secrets"
    (empty / "PAPER" / "ibkr").mkdir(parents=True)
    monkeypatch.setenv("SECRETS_ROOT", str(empty))
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost/paper")
    tripwire = _no_connections(monkeypatch)

    with pytest.raises(probe.ProbePreflightError) as refused:
        probe.build_service(
            symbol="AAPL",
            account_alias=ALIAS,
            host="127.0.0.1",
            port=4002,
            equity_cap=Decimal("50000"),
        )

    assert tripwire.calls == []
    assert PAPER_ACCOUNT not in str(refused.value)


def test_a_live_port_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The paper-only endpoint check is local, so it happens first too."""
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost/paper")
    tripwire = _no_connections(monkeypatch)

    with pytest.raises(probe.ProbePreflightError, match="4001"):
        probe.build_service(
            symbol="AAPL",
            account_alias=ALIAS,
            host="127.0.0.1",
            port=4001,
            equity_cap=Decimal("50000"),
        )

    assert tripwire.calls == []


def test_preflight_runs_before_any_import_of_a_connecting_component() -> None:
    """Structural: preflight is called before the first resource is built."""
    import inspect

    source = inspect.getsource(probe.build_service)
    preflight_at = source.index("preflight(")
    for later in ("build_paper_adapter(", "create_session(", "create_sync_engine("):
        assert source.index(later) > preflight_at, later


# == 3. the account comes from the canonical secret store ===================


def test_the_account_resolves_from_the_canonical_secret_without_any_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The owner exports no account id. This is the normal path."""
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))

    assert probe._resolve_account_id(ALIAS) == PAPER_ACCOUNT  # noqa: SLF001


def test_the_explicit_override_takes_precedence_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Retained for unusual cases, with a stated precedence rule."""
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", "DU" + "7654321")

    assert probe._resolve_account_id(ALIAS) == "DU" + "7654321"  # noqa: SLF001


def test_an_empty_canonical_secret_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blank file is not an account; connecting anyway is guessing."""
    root = tmp_path / "secrets"
    secret = root / "PAPER" / "ibkr" / "paper-account-id"
    secret.parent.mkdir(parents=True)
    secret.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("SECRETS_ROOT", str(root))

    with pytest.raises(probe.ProbePreflightError, match="empty"):
        probe._resolve_account_id(ALIAS)  # noqa: SLF001


def test_no_secret_root_and_no_override_names_the_reference_not_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal tells the owner what to configure, and leaks nothing."""
    with pytest.raises(probe.ProbePreflightError) as refused:
        probe._resolve_account_id(ALIAS)  # noqa: SLF001

    message = str(refused.value)
    assert "SECRETS_ROOT" in message
    assert "ibkr/paper-account-id" in message
    assert PAPER_ACCOUNT not in message


def test_the_probe_never_hard_codes_an_owner_host_path() -> None:
    """The concrete owner path is evidence, not configuration to embed."""
    import inspect

    source = inspect.getsource(probe)

    assert "/home/opc" not in source
    assert ".config/tradingsystem" not in source


# == 4. teardown after a partial connect =====================================


def test_an_abort_after_connecting_leaves_no_live_thread_and_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The real run aborted mid-startup and the SDK thread printed a traceback.

    Startup is interrupted AFTER the broker adapter opened, so the ExitStack has
    to unwind a partially established set of resources. Nothing may escape, and
    nothing may be left running.
    """
    import threading

    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost/paper")
    monkeypatch.setenv("TRADING_OBJECT_STORE", str(tmp_path / "objects"))

    closed: list[str] = []
    before = set(threading.enumerate())

    class _Adapter:
        def connect(self) -> None:
            closed.append("adapter-connected")

        def disconnect(self) -> None:
            closed.append("adapter-disconnected")

    def build_adapter(**kwargs: object) -> _Adapter:
        return _Adapter()

    def create_session_that_fails() -> object:
        raise RuntimeError("gateway went away mid-startup")

    monkeypatch.setattr(
        "trading_broker_ibkr.paper_adapter.build_paper_adapter", build_adapter
    )
    monkeypatch.setattr(
        "trading_broker_ibkr.poi_market_data_session.create_session",
        create_session_that_fails,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="mid-startup"):
        probe.build_service(
            symbol="AAPL",
            account_alias=ALIAS,
            host="127.0.0.1",
            port=4002,
            equity_cap=Decimal("50000"),
        )

    assert closed == ["adapter-connected", "adapter-disconnected"], closed
    leaked = set(threading.enumerate()) - before
    assert not leaked, f"a background thread outlived the aborted startup: {leaked}"
    assert "Traceback" not in caplog.text


def test_the_market_data_transport_teardown_tolerates_a_partial_connection(
) -> None:
    """``serverVersion() == None`` during disconnect must not escape.

    This is the exact shape the owner host produced: a connection established
    far enough to have a thread, not far enough to answer ``isConnected()``.
    """
    from trading_broker_ibkr import poi_market_data_client as client_module

    source = inspect.getsource(client_module)
    stop_body = source[source.index("        def stop(self) -> None:"):]
    stop_body = stop_body[: stop_body.index("        def request_current_time")]

    # isConnected() itself may raise on a half-open connection.
    assert "except Exception" in stop_body, stop_body
    assert "join(timeout=" in stop_body, "the SDK thread is joined, not abandoned"
    assert "bind(None)" in stop_body, "the sink is released so late callbacks stop"


# == 3. the canonical paper probe invocation =================================


def _code_lines(text: str) -> str:
    """Executable lines only. A comment explaining what is forbidden is not
    an occurrence of the forbidden thing."""
    return chr(10).join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_probe_wrapper_exists_and_is_owner_invoked() -> None:
    """One documented command, and it is not part of paper startup."""
    wrapper = REPO_ROOT / "scripts" / "run_execution_probe_paper.sh"
    assert wrapper.is_file()
    code = _code_lines(wrapper.read_text(encoding="utf-8"))

    # Never trace the shell: the DSN is built in this script.
    assert "set -x" not in code
    # The DSN is exported to the child and never echoed or logged.
    for leak in ("echo $DATABASE_URL", 'echo "$DATABASE_URL"', "log $DATABASE_URL"):
        assert leak not in code, leak
    # It resolves the account from the secret store and never asks for it.
    assert "SECRETS_ROOT" in code
    assert "TRADING_PAPER_ACCOUNT_ID" not in code
    # And it always cleans up the temporary publish.
    assert "trap cleanup EXIT" in code


def test_the_diagnostic_publish_is_loopback_only_and_not_in_paper_startup() -> None:
    """PostgreSQL must never become reachable beyond the host."""
    override = REPO_ROOT / "deploy" / "compose.paper-probe.yaml"
    assert override.is_file()
    code = _code_lines(override.read_text(encoding="utf-8"))

    assert "127.0.0.1:" in code, "the publish is loopback-bound"
    assert "0.0.0.0" not in code, "the paper database must never bind all interfaces"
    # No literal credential in a compose file; only the PAPER_* references the
    # paper stack already requires from the secret store.
    assert "hunter" not in code.lower()
    assert "password:" not in code.lower()

    paper = _code_lines(
        (REPO_ROOT / "deploy" / "compose.paper.yaml").read_text(encoding="utf-8")
    )
    assert "ports:" not in paper, "normal paper startup must publish nothing"
    assert "compose.paper-probe.yaml" not in paper, (
        "the diagnostic override must not be pulled into normal startup"
    )
