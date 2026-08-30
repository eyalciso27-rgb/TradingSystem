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
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from trading_broker_core.port import OrderSide
from trading_execution_agent import __main__ as probe
from trading_execution_agent.execution_service import ExecutionEvaluation
from trading_execution_agent.safe_logging import (
    NOISY_BROKER_LOGGERS,
    SecretRedactingFilter,
    clear_sensitive_values,
    install_owner_output_protection,
    owner_print,
    redact,
    register_sensitive_value,
)

#: A synthetic paper account. NOT "shaped exactly like a real one" -- that
#: claim was in this file until the first owner-host run disproved it: the real
#: identifier contains an alphabetic character after the prefix, which the
#: original digits-only pattern did not match. Real-shaped values live in
#: REAL_SHAPED_ACCOUNTS below; this one exercises the simplest case.
PAPER_ACCOUNT = "DU" + "1234567"
ALIAS = "paper-poi"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No inherited configuration; each test states what it needs."""
    for name in (
        "SECRETS_ROOT",
        "TRADING_PAPER_ACCOUNT_ID",
        "DATABASE_URL",
        "TRADING_OBJECT_STORE",
    ):
        monkeypatch.delenv(name, raising=False)
    # Registered literals are process-global by design; no test may inherit
    # another's, and none may leak into a later one.
    clear_sensitive_values()
    yield
    clear_sensitive_values()


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


def _bootstrap_code() -> str:
    """The executable lines of the shared Paper DB/env bootstrap library.

    The loopback publish, the readiness wait, the DATABASE_URL construction, the
    authenticated preflight, the schema-to-head step and the cleanup now live in
    ONE library both owner wrappers source, so the owner-safety properties are
    asserted where the logic lives rather than duplicated per wrapper.
    """
    return _code_lines(
        (REPO_ROOT / "scripts" / "lib" / "paper_probe_bootstrap.sh").read_text(
            encoding="utf-8"
        )
    )


def test_the_probe_wrapper_exists_and_is_owner_invoked() -> None:
    """One documented command, and it is not part of paper startup."""
    wrapper = REPO_ROOT / "scripts" / "run_execution_probe_paper.sh"
    assert wrapper.is_file()
    code = _code_lines(wrapper.read_text(encoding="utf-8"))
    lib = _bootstrap_code()

    # Never trace the shell: the DSN is built for this run.
    assert "set -x" not in code and "set -x" not in lib
    # The DSN is exported to the child and never echoed or logged, in either.
    for leak in ("echo $DATABASE_URL", 'echo "$DATABASE_URL"', "log $DATABASE_URL"):
        assert leak not in code and leak not in lib, leak
    # It resolves the account from the secret store (now in the shared library)
    # and never asks the owner for it.
    assert "SECRETS_ROOT" in lib
    assert "TRADING_PAPER_ACCOUNT_ID" not in code and "TRADING_PAPER_ACCOUNT_ID" not in lib
    # The wrapper sources the shared bootstrap and always cleans up the publish.
    assert "paper_probe_bootstrap.sh" in code
    assert "trap 'paper_probe_close_db' EXIT" in code


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


# == the account shape the real run actually produced ========================
#
# The original pattern was ``(?:DU|DF|U)\d{5,}`` -- digits only -- and the test
# fixture called ``DU1234567`` "shaped exactly like a real Paper account". The
# first owner-host run disproved that: the real identifier contains an
# ALPHABETIC character after the prefix. The owner's actual id appears nowhere
# in this repository; these are synthetic values of the same SHAPE.

REAL_SHAPED_ACCOUNTS = (
    "DUA12345",       # letter immediately after the prefix
    "DU12A345",       # letter in the middle
    "DU1234A",        # letter at the end
    "DFX99887",       # the DF family, same story
)


@pytest.mark.parametrize("account", REAL_SHAPED_ACCOUNTS)
def test_alphanumeric_account_shapes_are_redacted(account: str) -> None:
    """A digits-only pattern would miss every one of these."""
    cleaned = redact(f"ANSWER managedAccounts {{'accountsList': '{account}'}}")

    assert account not in cleaned, cleaned
    assert "[account]" in cleaned


@pytest.mark.parametrize(
    "ordinary",
    ["UNKNOWN", "DURATION", "Underlying", "USD", "DUE", "UTC"],
)
def test_ordinary_words_are_not_mistaken_for_accounts(ordinary: str) -> None:
    """Broadening the pattern must not start eating English."""
    assert redact(f"status {ordinary} here") == f"status {ordinary} here"


# == the exact resolved secret, whatever its format =========================


def test_the_resolved_account_is_registered_as_an_exact_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recognition is not the load-bearing defence; the literal is.

    A format no pattern anticipates -- which is precisely what the first real
    run produced -- is still removed, because the exact value is registered the
    moment it is resolved.
    """
    unrecognisable = "QQ-paper-9f3a-tenant"
    monkeypatch.setenv(
        "SECRETS_ROOT",
        str(_secret_store(tmp_path / "secrets", account_id=unrecognisable)),
    )
    assert unrecognisable in redact(f"account {unrecognisable}"), (
        "precondition: no pattern matches this shape"
    )

    resolved = probe._resolve_account_id(ALIAS)  # noqa: SLF001

    assert resolved == unrecognisable
    assert unrecognisable not in redact(f"account {unrecognisable}")
    assert "[redacted]" in redact(f"account {unrecognisable}")


def test_an_override_account_is_registered_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exceptional path gets the same protection as the normal one."""
    override = "ZZ-override-7712"
    monkeypatch.setenv("SECRETS_ROOT", str(_secret_store(tmp_path / "secrets")))
    monkeypatch.setenv("TRADING_PAPER_ACCOUNT_ID", override)

    probe._resolve_account_id(ALIAS)  # noqa: SLF001

    assert override not in redact(f"connected to {override}")


@pytest.mark.parametrize(
    "emit",
    ["plain", "percent-arg", "dict-arg", "managed-accounts", "tuple-arg"],
)
def test_the_resolved_account_never_reaches_a_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    emit: str,
) -> None:
    """Every shape the SDK actually logs, including containers."""
    account = "DU7A31942"
    monkeypatch.setenv(
        "SECRETS_ROOT", str(_secret_store(tmp_path / "secrets", account_id=account))
    )
    probe._resolve_account_id(ALIAS)  # noqa: SLF001
    install_owner_output_protection()
    sdk = logging.getLogger("ibapi.utils")
    sdk.setLevel(logging.INFO)

    with caplog.at_level(logging.INFO):
        caplog.handler.addFilter(SecretRedactingFilter())
        if emit == "plain":
            sdk.info(f"managedAccounts {account}")
        elif emit == "percent-arg":
            sdk.info("managedAccounts %s", account)
        elif emit == "dict-arg":
            sdk.info("ANSWER %s", {"accountsList": account, "reqId": 1})
        elif emit == "managed-accounts":
            sdk.info("ANSWER managedAccounts {'accountsList': '%s'}", account)
        else:
            sdk.info("fields %s", (account, "other"))

    assert account not in caplog.text, f"{emit}: {caplog.text}"


def test_debug_mode_still_removes_the_resolved_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """--debug-broker-logging is not permission to print the account."""
    account = "DU8B44210"
    monkeypatch.setenv(
        "SECRETS_ROOT", str(_secret_store(tmp_path / "secrets", account_id=account))
    )
    probe._resolve_account_id(ALIAS)  # noqa: SLF001
    install_owner_output_protection(debug=True)
    sdk = logging.getLogger("ibapi.utils")
    sdk.setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG):
        caplog.handler.addFilter(SecretRedactingFilter())
        sdk.debug("raw payload %s", {"accountsList": account})

    assert account not in caplog.text, caplog.text


# == arbitrary exception text must not reach the terminal ===================


class _Leaky(Exception):
    """A dependency exception whose message carries a secret, as they do."""


LEAKY_MESSAGES = {
    "account": "connection failed for account DU7A31942",
    "dsn": (
        "connection to server at postgresql://trading:hunter2@127.0.0.1:55432"
        "/trading_paper failed"
    ),
    "password": 'FATAL: password authentication failed; password="hunter2"',
}


@pytest.mark.parametrize("kind", sorted(LEAKY_MESSAGES))
def test_a_startup_failure_prints_a_code_and_a_type_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """``print`` bypasses every logging filter, so nothing uncontrolled is printed.

    The previous code did ``print(f"...: {type(error).__name__}: {error}")``,
    which put a psycopg DSN -- password included -- straight on the terminal.
    """
    message = LEAKY_MESSAGES[kind]

    def exploding(**kwargs: object) -> object:
        raise _Leaky(message)

    monkeypatch.setattr(probe, "build_service", exploding)

    status = probe.main(["probe", "--symbol", "AAPL"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert status == probe.EXIT_ERROR
    assert probe.PROBE_START_FAILED in output
    assert "_Leaky" in output, "the exception TYPE is still reported"
    for secret in ("DU7A31942", "hunter2", "postgresql://", "trading:hunter2"):
        assert secret not in output, f"{kind}: {secret} reached the terminal"
    assert "Traceback" not in output


@pytest.mark.parametrize("kind", sorted(LEAKY_MESSAGES))
def test_an_evidence_failure_prints_a_code_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """collect_evidence was guarded for cleanup but not for OUTPUT.

    An exception there escaped main and Python printed the traceback -- message
    included -- which is dependency-controlled text on the owner's terminal.
    """
    message = LEAKY_MESSAGES[kind]
    closed: list[str] = []

    class _Service:
        def collect_evidence(self, intent: object) -> object:
            raise _Leaky(message)

    class _Resources:
        service = _Service()

        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(probe, "build_service", lambda **kwargs: _Resources())

    status = probe.main(["probe", "--symbol", "AAPL"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert status == probe.EXIT_ERROR
    assert probe.PROBE_RUN_FAILED in output
    assert closed == ["closed"], "resources are still released"
    for secret in ("DU7A31942", "hunter2", "postgresql://"):
        assert secret not in output, f"{kind}: {secret} reached the terminal"
    assert "Traceback" not in output


def test_a_preflight_failure_prints_our_own_safe_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Our messages name what to configure and are shown -- redacted anyway."""

    def refusing(**kwargs: object) -> object:
        raise probe.ProbePreflightError(
            "SECRETS_ROOT is not set, so the paper account for alias"
            " 'paper-poi' cannot be resolved"
        )

    monkeypatch.setattr(probe, "build_service", refusing)

    status = probe.main(["probe", "--symbol", "AAPL"])

    output = capsys.readouterr().out
    assert status == probe.EXIT_ERROR
    assert probe.PREFLIGHT_FAILED in output
    assert "SECRETS_ROOT" in output, "the owner is told what to configure"
    assert "Traceback" not in output


def test_debug_detail_passes_through_the_same_redactor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Debug means more detail, never permission to print a secret."""

    def exploding(**kwargs: object) -> object:
        raise _Leaky(LEAKY_MESSAGES["dsn"])

    monkeypatch.setattr(probe, "build_service", exploding)

    probe.main(["probe", "--symbol", "AAPL", "--debug-broker-logging"])

    output = capsys.readouterr().out
    assert "detail (redacted)" in output, "debug does add detail"
    for secret in ("hunter2", "postgresql://", "trading:hunter2"):
        assert secret not in output, output
    assert "Traceback" not in output


def test_the_probe_never_prints_a_raw_exception_string() -> None:
    """Structural: exception text can only reach the terminal through the boundary.

    ``{error}`` interpolation is allowed ONLY inside an ``owner_print`` call,
    which redacts. A bare ``print`` of it bypasses every filter -- that is what
    put a psycopg DSN on the terminal.
    """
    source = inspect.getsource(probe.main)
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "{error}" in line and "owner_print(" not in line
    ]

    assert offenders == [], offenders
    # And the only exception text that is interpolated at all is our own
    # preflight message; everything else reports a code and a type.
    interpolating = [line.strip() for line in source.splitlines() if "{error}" in line]
    assert len(interpolating) == 1, interpolating
    assert "PREFLIGHT_FAILED" in interpolating[0], interpolating


# == the wrapper: readiness must fail ========================================


def test_the_wrapper_fails_when_the_database_never_becomes_ready() -> None:
    """A bounded wait that falls through is not a check.

    Thirty failed pg_isready calls used to be followed by exporting
    DATABASE_URL and starting Python anyway -- configuration present, database
    absent, discovered only after two IBKR sessions were open.
    """
    lib = _bootstrap_code()

    assert "db_ready=0" in lib and "db_ready=1" in lib
    ready_check = lib.index('[ "$db_ready" -ne 1 ]')
    dsn_build = lib.index("build_paper_database_url.py")
    assert ready_check < dsn_build, (
        "readiness must be asserted before the DSN is built"
    )
    # And the wrapper only runs the probe AFTER the bootstrap returns.
    wrapper = _code_lines(
        (REPO_ROOT / "scripts" / "run_execution_probe_paper.sh").read_text(
            encoding="utf-8"
        )
    )
    assert wrapper.index("paper_probe_open_db") < wrapper.index(
        "python -m trading_execution_agent probe"
    )


def test_the_wrapper_arms_cleanup_before_it_mutates_anything() -> None:
    """A compose call can change state and still fail."""
    lib = _bootstrap_code()

    armed = lib.index("PAPER_PROBE_PUBLISH_STARTED=1")
    # The MUTATING call, not the restore inside the cleanup function: only the
    # open path's own compose up carries the published port.
    mutated = lib.index('PAPER_PROBE_DB_PORT="$db_port" docker compose')
    assert armed < mutated, (
        "the cleanup flag is set BEFORE the compose up, so a partial recreate"
        " cannot leave the publish open"
    )
    # And the wrapper arms the EXIT trap before it calls the bootstrap at all.
    wrapper = _code_lines(
        (REPO_ROOT / "scripts" / "run_execution_probe_paper.sh").read_text(
            encoding="utf-8"
        )
    )
    assert wrapper.index("trap 'paper_probe_close_db' EXIT") < wrapper.index(
        "paper_probe_open_db"
    )


# == the wrapper: credentials must be encoded ================================


@pytest.mark.parametrize(
    ("name", "password"),
    [
        ("at sign", "p@ssword"),
        ("colon", "pass:word"),
        ("slash", "pass/word"),
        ("hash", "pass#word"),
        ("percent", "pass%2Fword"),
        ("space", "pass word"),
        ("everything", "p@ss:w/rd#1 %2F+&?=x"),
    ],
)
def test_reserved_characters_round_trip_through_the_database_url(
    name: str, password: str
) -> None:
    """The secret must not have to avoid characters so our string works."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from build_paper_database_url import build_url
    from sqlalchemy.engine import make_url

    url = build_url(
        {
            "PAPER_POSTGRES_USER": "trading@paper",
            "PAPER_POSTGRES_PASSWORD": password,
            "PAPER_POSTGRES_DB": "trading_paper",
            "PAPER_PROBE_DB_PORT": "55432",
        }
    )
    parsed = make_url(url.render_as_string(hide_password=False))

    assert parsed.password == password, f"{name}: {parsed.password!r}"
    assert parsed.username == "trading@paper"
    assert parsed.database == "trading_paper"
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 55432


def test_the_rendered_url_masks_the_password_by_default() -> None:
    """The only way the real password leaves the helper is stdout, on purpose."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from build_paper_database_url import build_url

    url = build_url(
        {
            "PAPER_POSTGRES_USER": "trading",
            "PAPER_POSTGRES_PASSWORD": "hunter2",
            "PAPER_POSTGRES_DB": "trading_paper",
        }
    )

    assert "hunter2" not in url.render_as_string()
    assert "hunter2" in url.render_as_string(hide_password=False)


def test_the_wrapper_no_longer_interpolates_credentials_into_a_url() -> None:
    """Structural: the string-concatenated DSN is gone."""
    lib = _bootstrap_code()

    assert "${PAPER_POSTGRES_PASSWORD}@" not in lib
    assert "$PAPER_POSTGRES_PASSWORD@" not in lib
    assert "build_paper_database_url.py" in lib


# == the ONE owner-visible output boundary ===================================
#
# The redaction filter covers ``logging``. It cannot cover ``print``. So the
# SUCCESSFUL path -- the evidence lines and the rendered evaluation -- wrote
# dynamic text straight to the terminal: quote details, readiness gate details,
# blockers, evaluation reasons, the authorization reason. Most are safe today,
# but "safe today" is not the claim being made.
#
# The literal used here is deliberately unrecognisable to every pattern, so
# these tests prove the EXACT-SECRET registry reaches the successful path --
# not that the DU regex happens to catch something.

UNRECOGNISABLE = "QQ-paper-9f3a-tenant"


def test_the_synthetic_literal_is_invisible_to_every_pattern() -> None:
    """Precondition. If a pattern matched it, these tests would prove nothing."""
    clear_sensitive_values()

    assert redact(f"detail {UNRECOGNISABLE}") == f"detail {UNRECOGNISABLE}"


def test_owner_print_redacts_registered_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The boundary itself."""
    register_sensitive_value(UNRECOGNISABLE)

    owner_print(f"quote detail: {UNRECOGNISABLE}")

    captured = capsys.readouterr()
    assert UNRECOGNISABLE not in captured.out + captured.err
    assert "[redacted]" in captured.out


def test_the_probe_has_no_second_output_path() -> None:
    """Structural: nothing in the probe writes to the terminal directly.

    A second unredacted path is the whole defect. ``print(`` must not appear in
    the module at all -- ``owner_print`` is the only writer.
    """
    source = inspect.getsource(probe)
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "print(" in line
        and "owner_print(" not in line
        and not line.strip().startswith("#")
        and "def owner_print" not in line
    ]

    assert offenders == [], offenders


PROBE_NOW = datetime(2026, 8, 23, 15, 30, tzinfo=UTC)


def _evaluation_carrying(poison: str) -> ExecutionEvaluation:
    """A REAL ExecutionEvaluation with the secret in exactly one dynamic field.

    ``render()`` is the production one, so whichever field is poisoned reaches
    the terminal exactly as it would on the owner host.
    """
    from trading_application.trading_readiness_runtime import (
        GateOutcome,
        MarketDataState,
        QuoteState,
        TradingReadinessRuntimeReport,
    )
    from trading_execution_agent.execution_service import (
        AuthorizationDecision,
        ExecutionEvaluation,
        ExecutionIntent,
        ExecutionStage,
    )
    from trading_market_data_core.trading_readiness import TradingMode, TradingReadiness

    intent = ExecutionIntent(
        strategy_id="owner-host-probe",
        decision=probe.probe_decision("AAPL", PROBE_NOW),
        symbol="AAPL",
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
        correlation_id="corr-1",
        client_order_id="probe-1",
        source_id="owner-host-probe",
    )
    quote = QuoteState(
        Decimal("100.00"),
        Decimal("100.05"),
        PROBE_NOW,
        bid_size=500,
        ask_size=400,
        market_data_state=MarketDataState.REALTIME,
        detail=(
            f"served from {UNRECOGNISABLE}" if poison == "quote_detail" else "healthy"
        ),
    )
    readiness = TradingReadinessRuntimeReport(
        mode=TradingMode.PAPER,
        strategy_id="owner-host-probe",
        readiness=TradingReadiness.BLOCKED,
        security_id="conid:265598",
        symbol="AAPL",
        gates=(
            GateOutcome(
                name="market_data_usable",
                satisfied=False,
                detail=(
                    f"feed {UNRECOGNISABLE}"
                    if poison == "gate_detail"
                    else "no subscription"
                ),
            ),
        ),
        blockers=(
            (f"blocked by {UNRECOGNISABLE}",) if poison == "blocker" else ("blocked",)
        ),
        universe_status=None,
        optional_context={},
        evaluated_at=PROBE_NOW,
        runtime_refs={},
        notes=(),
    )
    return ExecutionEvaluation(
        intent=intent,
        stage=ExecutionStage.READY_NOT_AUTHORIZED,
        reasons=(
            (f"reason mentions {UNRECOGNISABLE}",)
            if poison == "reason"
            else ("readiness satisfied and authorization granted",)
        ),
        quote=quote,
        readiness=readiness,
        authorization=AuthorizationDecision(
            allowed=False,
            reason=(
                f"denied for {UNRECOGNISABLE}"
                if poison == "authorization"
                else "autonomous submission is not enabled"
            ),
        ),
        evaluated_at=PROBE_NOW,
        intent_mode="PAPER",
    )


def _successful_probe(
    monkeypatch: pytest.MonkeyPatch, *, poison: str
) -> tuple[int, str]:
    """Run the NORMAL successful output path with a secret in one field.

    Nothing connects: the service is replaced, but ``render`` and
    ``evidence.lines()`` are the real ones, so the text that reaches the
    terminal is produced exactly as it is in production.
    """
    from trading_execution_agent.execution_service import ExecutionEvidence

    register_sensitive_value(UNRECOGNISABLE)
    evaluation = _evaluation_carrying(poison)

    class _Service:
        def collect_evidence(self, _intent: object) -> ExecutionEvidence:
            evidence = ExecutionEvidence(evaluated_at=PROBE_NOW)
            evidence.record(
                "quote",
                True,
                f"served from {UNRECOGNISABLE}"
                if poison == "observation"
                else "healthy",
            )
            evidence.evaluation = evaluation
            return evidence

    class _Resources:
        service = _Service()

        def close(self) -> None:
            return None

    monkeypatch.setattr(probe, "build_service", lambda **kwargs: _Resources())
    return probe.main(["probe", "--symbol", "AAPL"]), poison


@pytest.mark.parametrize(
    "poison",
    ["observation", "quote_detail", "gate_detail", "blocker", "reason", "authorization"],
)
def test_no_dynamic_field_can_leak_through_the_successful_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], poison: str
) -> None:
    """One secret, six different dynamic fields, the normal success path."""
    status, field = _successful_probe(monkeypatch, poison=poison)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert status in (probe.EXIT_OK, probe.EXIT_BLOCKED), output
    assert UNRECOGNISABLE not in output, f"{field} leaked: {output}"
    assert "[redacted]" in output, f"{field}: the field was not actually rendered"


# == the host-side authenticated database preflight ==========================


def test_the_database_preflight_reports_a_code_and_a_type_only() -> None:
    """Driver text embeds the DSN, so it is never printed."""
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_paper_probe_database import UNUSABLE, check

    # Nothing listening on port 1.
    status, line = check(
        "postgresql+psycopg://trading:hunter2@127.0.0.1:1/trading_paper"
    )

    assert status != 0
    assert line.startswith(UNUSABLE)
    for secret in ("hunter2", "trading:hunter2", "postgresql://", "127.0.0.1"):
        assert secret not in line, line


def test_an_unparseable_url_is_refused_without_echoing_it() -> None:
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_paper_probe_database import UNUSABLE, check

    status, line = check("::::not a url::::")

    assert status != 0
    assert line.startswith(UNUSABLE)
    assert "not a url" not in line


def test_a_missing_url_is_refused() -> None:
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_paper_probe_database as checker

    status = checker.main([])

    assert status != 0


def test_the_preflight_runs_before_the_probe_and_gates_it() -> None:
    """Ordering, and that a failure stops the run.

    ``pg_isready`` inside the container proves the server accepts connections.
    It does not prove the loopback publish is reachable from the host, that the
    URL parses, or that these credentials authenticate -- and the PostgreSQL
    image does not reset an existing role's password when the container is
    recreated, so an old volume can expect an older one.
    """
    lib = _bootstrap_code()

    dsn_build = lib.index("build_paper_database_url.py")
    db_check = lib.index("check_paper_probe_database.py")
    schema = lib.index("prepare_paper_database.py")
    assert dsn_build < db_check < schema, (
        "the authenticated check must use the built URL, before schema prep"
    )
    # A failed check returns non-zero (fails closed) and says so, so the caller
    # never opens a broker socket.
    guarded = lib[db_check : db_check + 400]
    assert "return 1" in guarded, "a failed database preflight must abort the run"
    assert "zero IBKR connections" in guarded
    # And the wrapper gates the probe on the bootstrap succeeding.
    wrapper = _code_lines(
        (REPO_ROOT / "scripts" / "run_execution_probe_paper.sh").read_text(
            encoding="utf-8"
        )
    )
    gate = wrapper.index("paper_probe_open_db")
    assert wrapper.index("|| fail", gate) < wrapper.index(
        "python -m trading_execution_agent probe"
    )


def test_a_failing_database_preflight_invokes_zero_probes(tmp_path: Path) -> None:
    """End to end over the real script fragment, with the checker forced to fail.

    Deterministic: the wrapper's own gating lines are executed by bash with the
    checker and the probe replaced by recorders, so the assertion is about the
    script's control flow rather than a re-implementation of it.
    """
    import subprocess

    marker = tmp_path / "probe-was-invoked"
    script = tmp_path / "fragment.sh"
    script.write_text(
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        'log() { :; }\n'
        "check_db() { return 1; }\n"
        f'run_probe() {{ touch "{marker.as_posix()}"; }}\n'
        'if ! check_db; then\n'
        '  fail "the paper database is not usable from this host; not starting'
        ' the probe (zero IBKR connections were opened)"\n'
        "fi\n"
        "run_probe\n",
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603 - a local bash fragment, no input
        ["bash", str(script)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 2, result.stderr
    assert not marker.exists(), "the execution probe was invoked anyway"
    assert "zero IBKR connections" in result.stderr


def test_a_passing_database_preflight_allows_the_probe(tmp_path: Path) -> None:
    """The control: the same control flow, with the check succeeding."""
    import subprocess

    marker = tmp_path / "probe-was-invoked"
    script = tmp_path / "fragment.sh"
    script.write_text(
        "set -Eeuo pipefail\n"
        'fail() { echo "ERROR: $*" >&2; exit 2; }\n'
        "check_db() { return 0; }\n"
        f'run_probe() {{ touch "{marker.as_posix()}"; }}\n'
        'if ! check_db; then fail "db"; fi\n'
        "run_probe\n",
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603 - a local bash fragment, no input
        ["bash", str(script)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists(), "the probe should run once the database is proven"


def test_the_paper_postgres_compose_contract_is_unchanged() -> None:
    """The compose interface is still the two PAPER_POSTGRES_* variables.

    The second owner-host run changed WHO supplies them -- the wrapper now
    loads them from the canonical store instead of demanding them from the
    owner's shell -- and deliberately did not change WHAT the paper stack
    consumes. The deployment contract in ``compose.paper.yaml`` is untouched.
    """
    compose = (REPO_ROOT / "deploy" / "compose.paper.yaml").read_text(encoding="utf-8")

    assert "PAPER_POSTGRES_USER" in compose
    assert "PAPER_POSTGRES_PASSWORD" in compose
    # The exporting itself now lives in the one shared loader, so the wrapper,
    # the paper stack launcher and the compose check cannot drift apart.
    loader = _code_lines(
        (REPO_ROOT / "scripts" / "lib" / "paper_env.sh").read_text(encoding="utf-8")
    )
    assert "export PAPER_POSTGRES_USER=" in loader
    assert "export PAPER_POSTGRES_PASSWORD=" in loader

    # The shared bootstrap loads them; the wrapper sources it and neither file
    # REQUIRES them from the caller's environment (the fresh-shell regression).
    lib = _bootstrap_code()
    assert "load_paper_environment" in lib
    code = _code_lines(
        (REPO_ROOT / "scripts" / "run_execution_probe_paper.sh").read_text(
            encoding="utf-8"
        )
    )
    assert "paper_probe_bootstrap.sh" in code
    for forbidden in ("PAPER_POSTGRES_USER:?", "PAPER_POSTGRES_PASSWORD:?"):
        assert forbidden not in code and forbidden not in lib
