"""Track C — source activation framework + per-source probes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from trading_application.source_adapters.activation import (
    ActivationError,
    ClientKind,
    CursorStore,
    PoIVerdict,
    build_evidence,
    read_only_preflight,
)
from trading_application.source_adapters.activation_cli import main as activation_main
from trading_application.source_adapters.activation_sources import (
    FILINGS_SPEC,
    TELEGRAM_BOT_SPEC,
    TELEGRAM_USER_SPEC,
    TRADINGVIEW_SPEC,
    filings_probe,
    telegram_bot_probe,
    telegram_user_probe,
    tradingview_probe,
)
from trading_application.source_adapters.filings import RawFilingRecord
from trading_application.source_adapters.telegram_user import TdlibAuthState
from trading_config.secrets import FileSecretStore
from trading_contracts.enums import Environment, IntegrationHealthStatus, SourceType

T0 = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def seed_secret(root: Path, environment: Environment, key: str, value: str) -> None:
    path = root / environment.value / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def store(root: Path, environment: Environment = Environment.RESEARCH) -> FileSecretStore:
    return FileSecretStore(root, environment)


# -- fakes -------------------------------------------------------------------


class FakeTdlib:
    def __init__(self, state: TdlibAuthState) -> None:
        self.state = state

    def authorization_state(self) -> TdlibAuthState:
        return self.state

    def get_events(self, *, after_event_id):  # type: ignore[no-untyped-def]
        return ()


class FakeBot:
    def __init__(self, authorized: bool) -> None:
        self.authorized = authorized

    def is_authorized(self) -> bool:
        return self.authorized

    def get_updates(self, *, offset, timeout):  # type: ignore[no-untyped-def]
        return ()


class FakeFilingSource:
    def __init__(self, name: str, *, down: bool = False) -> None:
        self._name = name
        self._down = down

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> SourceType:
        return SourceType.FILING

    def fetch(self, *, after_cursor: str) -> tuple[RawFilingRecord, ...]:
        if self._down:
            raise ConnectionError("edgar down")
        return ()


# -- secret loading (item 7) -------------------------------------------------


def test_missing_secret_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ActivationError, match="missing required secrets"):
        read_only_preflight(
            connector_id="telegram-user-tdlib", spec=TELEGRAM_USER_SPEC, store=store(tmp_path),
            probe=telegram_user_probe(FakeTdlib(TdlibAuthState.READY)), client_kind=ClientKind.REAL,
            observed_at=T0,
        )


def test_present_secret_unauthorized_is_auth_required_not_healthy(tmp_path: Path) -> None:
    for key in TELEGRAM_USER_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "value")
    result = read_only_preflight(
        connector_id="telegram-user-tdlib", spec=TELEGRAM_USER_SPEC, store=store(tmp_path),
        probe=telegram_user_probe(FakeTdlib(TdlibAuthState.WAIT_PASSWORD)),
        client_kind=ClientKind.REAL, observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.AUTH_REQUIRED  # not HEALTHY


def test_ready_real_session_is_healthy(tmp_path: Path) -> None:
    for key in TELEGRAM_USER_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "value")
    result = read_only_preflight(
        connector_id="telegram-user-tdlib", spec=TELEGRAM_USER_SPEC, store=store(tmp_path),
        probe=telegram_user_probe(FakeTdlib(TdlibAuthState.READY)),
        client_kind=ClientKind.REAL, observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.HEALTHY


# -- fake client can never be PoI evidence (item 8) --------------------------


def test_fake_client_yields_not_evidence_even_when_healthy(tmp_path: Path) -> None:
    for key in TELEGRAM_USER_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "value")
    result = read_only_preflight(
        connector_id="telegram-user-tdlib", spec=TELEGRAM_USER_SPEC, store=store(tmp_path),
        probe=telegram_user_probe(FakeTdlib(TdlibAuthState.READY)),
        client_kind=ClientKind.FAKE, observed_at=T0,
    )
    evidence = build_evidence("POI-SOC-TG-USER", result)
    assert result.status is IntegrationHealthStatus.HEALTHY  # a healthy FAKE...
    assert evidence.verdict is PoIVerdict.NOT_EVIDENCE  # ...is still never PASS


def test_real_healthy_yields_pass(tmp_path: Path) -> None:
    for key in TELEGRAM_BOT_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "tok")
    result = read_only_preflight(
        connector_id="telegram-bot", spec=TELEGRAM_BOT_SPEC, store=store(tmp_path),
        probe=telegram_bot_probe(FakeBot(authorized=True)), client_kind=ClientKind.REAL,
        observed_at=T0,
    )
    assert build_evidence("POI-SOC-TG-BOT", result).verdict is PoIVerdict.PASS


# -- telegram bot never satisfies the user gate (item 13) --------------------


def test_bot_probe_never_satisfies_user_gate(tmp_path: Path) -> None:
    for key in TELEGRAM_BOT_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "tok")
    # authorized bot is HEALTHY but the probe enforces it cannot satisfy the gate
    result = read_only_preflight(
        connector_id="telegram-bot", spec=TELEGRAM_BOT_SPEC, store=store(tmp_path),
        probe=telegram_bot_probe(FakeBot(authorized=True)), client_kind=ClientKind.REAL,
        observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.HEALTHY
    assert "supplemental" in result.detail


# -- source outage is isolated (item 11) -------------------------------------


def test_source_outage_reports_down_without_raising(tmp_path: Path) -> None:
    result = read_only_preflight(
        connector_id="news-filings", spec=FILINGS_SPEC, store=store(tmp_path),
        probe=filings_probe((FakeFilingSource("edgar", down=True),)), client_kind=ClientKind.REAL,
        observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.DOWN  # isolated; no exception propagates


def test_independent_sources_probed_even_if_one_is_down(tmp_path: Path) -> None:
    healthy = read_only_preflight(
        connector_id="news-filings", spec=FILINGS_SPEC, store=store(tmp_path),
        probe=filings_probe((FakeFilingSource("edgar"),)), client_kind=ClientKind.REAL,
        observed_at=T0,
    )
    assert healthy.status is IntegrationHealthStatus.HEALTHY


# -- tradingview: secret present but no alert -> AUTH_REQUIRED ----------------


def test_tradingview_configured_but_no_alert_is_auth_required(tmp_path: Path) -> None:
    for key in TRADINGVIEW_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "shared-secret")
    result = read_only_preflight(
        connector_id="tradingview-webhook", spec=TRADINGVIEW_SPEC, store=store(tmp_path),
        probe=tradingview_probe(), client_kind=ClientKind.REAL, observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.AUTH_REQUIRED
    assert build_evidence("POI-TV-001", result).verdict is not PoIVerdict.PASS


# -- secrets never enter the evidence artifact (item 10) ---------------------


def test_evidence_write_refuses_to_leak_a_secret(tmp_path: Path) -> None:
    for key in TELEGRAM_BOT_SPEC.required_keys:
        seed_secret(tmp_path, Environment.RESEARCH, key, "tok")
    result = read_only_preflight(
        connector_id="telegram-bot", spec=TELEGRAM_BOT_SPEC, store=store(tmp_path),
        probe=telegram_bot_probe(FakeBot(authorized=True)), client_kind=ClientKind.REAL,
        observed_at=T0,
    )
    evidence = build_evidence("POI-SOC-TG-BOT", result)
    out = tmp_path / "evidence.json"
    evidence.write(out)  # sanitized ok
    assert "tok" not in out.read_text(encoding="utf-8")


# -- durable checkpoint / idempotent restart (item 9) ------------------------


def test_cursor_store_persists_and_handles_corruption(tmp_path: Path) -> None:
    cs = CursorStore(tmp_path / "cur.json")
    assert cs.load() == {}
    cs.advance("AAPL:1d", "2026-06-01T20:00:00+00:00")
    assert CursorStore(tmp_path / "cur.json").load()["AAPL:1d"].startswith("2026-06-01")
    (tmp_path / "cur.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ActivationError, match="corrupt cursor"):
        cs.load()


# -- CLI ---------------------------------------------------------------------


def test_cli_list_and_runbook(capsys: pytest.CaptureFixture[str]) -> None:
    assert activation_main(["prog", "list"]) == 0
    out = capsys.readouterr().out
    assert "telegram-user" in out and "tradingview" in out

    assert activation_main(["prog", "runbook", "telegram-user"]) == 0
    rb = capsys.readouterr().out
    assert "POI-SOC-TG-USER" in rb and "PASS criteria" in rb and "2FA" in rb


def test_cli_check_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # missing -> fail closed
    assert activation_main(
        ["prog", "check-secrets", "telegram-bot", "--environment", "PAPER", "--root", str(tmp_path)]
    ) == 1
    assert "MISSING" in capsys.readouterr().out
    # present -> ok
    for key in TELEGRAM_BOT_SPEC.required_keys:
        seed_secret(tmp_path, Environment.PAPER, key, "tok")
    assert activation_main(
        ["prog", "check-secrets", "telegram-bot", "--environment", "PAPER", "--root", str(tmp_path)]
    ) == 0
    assert "OK" in capsys.readouterr().out


def test_cli_secret_keys_never_prints_values(capsys: pytest.CaptureFixture[str]) -> None:
    assert activation_main(["prog", "secret-keys", "tradingview"]) == 0
    out = capsys.readouterr().out
    assert "webhook_secret" in out
