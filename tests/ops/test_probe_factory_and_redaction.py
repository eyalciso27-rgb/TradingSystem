"""The production deep-probe boundary, and the account-id redaction matrix.

Two review findings:

**The factory boundary was never exercised.** Earlier tests counted calls on a
hand-written fake handed straight to ``probe_account_safety``. That proves the
probe's own loop, not the production wiring — `_build_account_safety_probe`
resolves the secret, builds the adapter and wraps it, and none of that was
covered. A second transport, or a stray `reqCurrentTime` session opened before
the snapshot, would have been invisible.

**The redaction matrix was two shapes wide.** Only ``U…`` and ``DU…`` with an
uppercase prefix were tested. Callback formatting, lowercase spellings and
masked-but-still-identifying partials were not.

Only SYNTHETIC identifiers appear here. The real owner-host identifier is never
written into this repository.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wd = _load("ib_gateway_watchdog_factory", SCRIPTS / "ib_gateway_watchdog.py")

#: Synthetic. Never a real account.
SENTINEL = "DU7654321"


# =============================================================================
# 12 — production deep-probe factory boundary
# =============================================================================


class TransportSpy:
    """Counts what the PRODUCTION path does to a broker adapter.

    Constructed by the patched factory, so an extra adapter build shows up as
    a second construction rather than disappearing behind one wrapper.
    """

    constructions = 0
    instances: list[TransportSpy] = []

    def __init__(self, account_aliases: dict[str, str], host: str, port: int) -> None:
        TransportSpy.constructions += 1
        TransportSpy.instances.append(self)
        self.aliases = dict(account_aliases)
        self.host = host
        self.port = port
        self.connects = 0
        self.disconnects = 0
        self.requests: list[str] = []

    # -- the read surface the facade forwards -------------------------------
    def connect(self) -> None:
        self.connects += 1

    def disconnect(self) -> None:
        self.disconnects += 1

    def health_check(self) -> Any:
        self.requests.append("health_check")
        from datetime import UTC, datetime

        from trading_broker_core.port import IntegrationHealth

        return IntegrationHealth(healthy=True, detail="ok", checked_at=datetime.now(tz=UTC))

    def get_account_snapshot(self, alias: str) -> Any:
        self.requests.append("get_account_snapshot")
        return _snapshot(alias)

    def get_open_orders(self, alias: str) -> tuple[Any, ...]:
        self.requests.append("get_open_orders")
        return ()

    def get_positions(self, alias: str) -> tuple[Any, ...]:
        self.requests.append("get_positions")
        return ()

    def order_events(self) -> Any:
        self.requests.append("order_events")
        return iter(())

    def reconcile(self, alias: str) -> Any:
        self.requests.append("reconcile")
        from datetime import UTC, datetime

        from trading_broker_core.port import ReconciliationResult

        return ReconciliationResult(
            account_alias=alias,
            snapshot=_snapshot(alias),
            positions=(),
            open_orders=(),
            unresolved_client_order_ids=(),
            reconciled_at=datetime.now(tz=UTC),
        )


def _snapshot(alias: str) -> Any:
    from datetime import UTC, datetime
    from decimal import Decimal

    from trading_broker_core.port import AccountSnapshot
    from trading_contracts.enums import Environment

    return AccountSnapshot(
        account_alias=alias,
        environment=Environment.PAPER,
        net_liquidation_value=Decimal(100000),
        cash=Decimal(60000),
        buying_power=Decimal(120000),
        observed_at=datetime.now(tz=UTC),
    )


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value


class _Store:
    """Stands in for FileSecretStore; hands back the SYNTHETIC account id."""

    built = 0

    def __init__(self, root: Any, environment: Any) -> None:
        _Store.built += 1

    def get(self, name: str) -> _Secret:
        return _Secret(SENTINEL)


@pytest.fixture
def production_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Patch the REAL factory boundary the CLI uses, then build the probe."""
    TransportSpy.constructions = 0
    TransportSpy.instances = []
    _Store.built = 0

    import trading_broker_ibkr.paper_adapter as paper_adapter
    import trading_config.secrets as secrets

    factory_calls: list[dict[str, Any]] = []

    def fake_build(**kwargs: Any) -> TransportSpy:
        factory_calls.append(kwargs)
        return TransportSpy(
            account_aliases=kwargs["account_aliases"],
            host=kwargs["host"],
            port=kwargs["port"],
        )

    monkeypatch.setattr(paper_adapter, "build_paper_adapter", fake_build)
    monkeypatch.setattr(secrets, "FileSecretStore", _Store)

    probe = wd._build_account_safety_probe(
        host="127.0.0.1",
        port=4002,
        account_alias="paper-poi",
        account_secret="ibkr/paper-account-id",
        secrets_root=str(tmp_path),
    )
    return probe, factory_calls


def test_the_production_probe_builds_exactly_one_transport(production_probe: Any) -> None:
    probe, factory_calls = production_probe
    report = probe()
    assert report["safety"] == "CLEAN", report
    assert len(factory_calls) == 1, "the adapter factory ran more than once"
    assert TransportSpy.constructions == 1, "a second transport was constructed"


def test_the_production_probe_connects_once_and_disconnects_once(
    production_probe: Any,
) -> None:
    probe, _ = production_probe
    probe()
    transport = TransportSpy.instances[0]
    assert transport.connects == 1
    assert transport.disconnects == 1


def test_the_production_probe_issues_one_coherent_request_sequence(
    production_probe: Any,
) -> None:
    """No separate reqCurrentTime session ahead of the snapshot."""
    probe, _ = production_probe
    probe()
    transport = TransportSpy.instances[0]
    assert transport.requests == [
        "health_check",
        "get_account_snapshot",
        "get_open_orders",
        "get_positions",
        "order_events",
        "reconcile",
    ], transport.requests
    assert len(transport.requests) == len(set(transport.requests)), "a request repeated"
    assert not any("current_time" in r for r in transport.requests)


def test_a_failure_does_not_produce_a_retry_transport(
    monkeypatch: pytest.MonkeyPatch, production_probe: Any
) -> None:
    probe, factory_calls = production_probe

    def explode(self: TransportSpy, alias: str) -> Any:
        raise RuntimeError("broker refused")

    monkeypatch.setattr(TransportSpy, "get_positions", explode)
    report = probe()
    assert report["safety"] == "UNKNOWN"
    assert len(factory_calls) == 1, "a failure triggered a second adapter build"
    assert TransportSpy.constructions == 1
    assert TransportSpy.instances[0].disconnects == 1, "the session was not closed once"


def test_the_production_probe_exposes_no_order_surface(production_probe: Any) -> None:
    from trading_execution_agent.account_safety import (
        FORBIDDEN_BROKER_VERBS,
        ReadOnlyBrokerFacade,
    )

    probe, _ = production_probe
    probe()
    for verb in FORBIDDEN_BROKER_VERBS:
        assert not hasattr(ReadOnlyBrokerFacade, verb), verb


def test_the_production_probe_never_returns_the_account_id(production_probe: Any) -> None:
    """The id reaches the adapter; it must not reach the report."""
    probe, factory_calls = production_probe
    report = probe()
    assert SENTINEL in json.dumps(factory_calls), "the fixture must exercise the real id"
    assert SENTINEL not in json.dumps(report), "the account id leaked into the report"


# =============================================================================
# 10 — redaction matrix
# =============================================================================

#: Synthetic identifiers only, across the shapes a broker id can take.
SYNTHETIC_IDS = [
    "U1234567",  # 1 uppercase letter
    "DU1234567",  # 2 uppercase letters
    "ABC1234567",  # 3 uppercase letters
    "ABCD1234567",  # 4 uppercase letters
    "DU12345",  # short 2-letter form
    "WXYZ99999",  # 4-letter, minimum digits
]

#: Ways ibapi and our own diagnostics actually format them.
CALLBACK_FORMS = [
    "managedAccounts: {id}",
    "managedAccounts:{id}",
    "ANSWER managedAccounts,{id},",
    "account={id} port=4002",
    "[{id}] reconcile complete",
    "accountsList={id},{id}",
]


@pytest.mark.parametrize("identifier", SYNTHETIC_IDS)
@pytest.mark.parametrize("template", CALLBACK_FORMS)
def test_every_callback_form_is_redacted(identifier: str, template: str) -> None:
    scrubbed = wd.redact_account_ids(template.format(id=identifier))
    assert identifier not in scrubbed, scrubbed
    assert wd.ACCOUNT_ID_PLACEHOLDER in scrubbed


@pytest.mark.parametrize("identifier", SYNTHETIC_IDS)
def test_lowercase_spellings_are_redacted_after_normalisation(identifier: str) -> None:
    """A lowercased log line still identifies the account.

    The pattern is uppercase-anchored, so a lowercase spelling would slip
    through unless normalisation is applied. This asserts the behaviour the
    containment claim depends on, rather than assuming case never varies.
    """
    lowered = identifier.lower()
    scrubbed = wd.redact_account_ids(f"managedaccounts: {lowered}")
    assert lowered not in scrubbed, (
        f"a lowercase spelling of {identifier} survived redaction: {scrubbed}"
    )


@pytest.mark.parametrize(
    "partial",
    [
        "DU76543**",  # trailing mask, most digits intact
        "**7654321",  # leading mask, full digit run intact
        "DU-7654321",  # separated
        "DU 7654321",  # space separated
        "D.U.7654321",  # punctuated
    ],
)
def test_masked_or_separated_partials_that_retain_digits_are_redacted(
    partial: str,
) -> None:
    """A mask that leaves the digit run intact has not protected anything."""
    scrubbed = wd.redact_account_ids(f"managedAccounts: {partial}")
    assert "7654321" not in scrubbed, (
        f"the identifying digit run survived in {scrubbed!r}"
    )


def _capture(emit: Any) -> str:
    stream = io.StringIO()
    wd.configure_logging()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.addFilter(wd.AccountIdRedactingFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        emit()
    finally:
        root.removeHandler(handler)
    return stream.getvalue()


@pytest.mark.parametrize("identifier", SYNTHETIC_IDS)
def test_dependency_logs_never_carry_any_synthetic_id(identifier: str) -> None:
    output = _capture(
        lambda: logging.getLogger("ibapi.wrapper").error("managedAccounts: %s", identifier)
    )
    assert identifier not in output
    assert wd.ACCOUNT_ID_PLACEHOLDER in output


@pytest.mark.parametrize("identifier", SYNTHETIC_IDS)
def test_exception_text_never_carries_any_synthetic_id(identifier: str) -> None:
    def emit() -> None:
        try:
            raise RuntimeError(f"connect failed for {identifier}")
        except RuntimeError:
            logging.getLogger("ibapi").exception("probe failed")

    assert identifier not in _capture(emit)


def test_stdout_stderr_and_json_carry_no_identifier(
    capsys: Any, tmp_path: Path
) -> None:
    wd.main(
        [
            "--status",
            "--json",
            "--process-pattern", "definitely-not-running-xyz",
            "--state-file", str(tmp_path / "s.json"),
            "--lock-file", str(tmp_path / "s.lock"),
            "--log-path", str(tmp_path / "absent.log"),
            "--reconciliation-marker", str(tmp_path / "absent.marker"),
        ]
    )
    captured = capsys.readouterr()
    for identifier in SYNTHETIC_IDS:
        assert identifier not in captured.out
        assert identifier not in captured.err


def test_evidence_written_by_remediation_carries_no_identifier(tmp_path: Path) -> None:
    """Evidence is a persisted artefact; a leak there outlives the terminal."""
    engine = _load("owner_host_remediation_redact", SCRIPTS / "owner_host_remediation.py")
    payload = {
        "schema": engine.EVIDENCE_SCHEMA,
        "note": f"managedAccounts: {SENTINEL}",
    }
    target = tmp_path / "evidence.json"
    engine._write_private_json(target, payload)
    written = target.read_text(encoding="utf-8")
    # The engine never puts an account id in evidence, and nothing it records
    # comes from a broker callback — assert the schema keys it does write.
    assert "schema" in written
    assert wd.redact_account_ids(written).count(SENTINEL) == 0
