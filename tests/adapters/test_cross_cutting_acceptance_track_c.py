"""Cross-cutting acceptance — Track C subset (items 7-13).

Consolidated proof of the source-activation invariants, complementing the
Track-A acceptance (items 1-6, 14).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading_application.source_adapters.activation import (
    ClientKind,
    PoIVerdict,
    assert_no_execution_surface,
    build_evidence,
    read_only_preflight,
)
from trading_application.source_adapters.activation_sources import (
    TELEGRAM_BOT_SPEC,
    TELEGRAM_USER_SPEC,
    telegram_bot_probe,
    telegram_user_probe,
)
from trading_application.source_adapters.filings import FilingsAdapter, RawFilingRecord
from trading_application.source_adapters.telegram_bot import TelegramBotAdapter
from trading_application.source_adapters.telegram_user import (
    TdlibAuthState,
    TdlibEvent,
    TdlibEventKind,
    TelegramUserAdapter,
)
from trading_application.source_adapters.tradingview import TradingViewAdapter
from trading_config.secrets import FileSecretStore
from trading_contracts.enums import Environment, IntegrationHealthStatus, SourceType

T0 = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _seed(root: Path, keys: tuple[str, ...]) -> FileSecretStore:
    for key in keys:
        path = root / Environment.RESEARCH.value / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value", encoding="utf-8")
    return FileSecretStore(root, Environment.RESEARCH)


class _Tdlib:
    def __init__(self, state: TdlibAuthState, events: tuple[TdlibEvent, ...] = ()) -> None:
        self.state = state
        self._events = events
        self._sent = False

    def authorization_state(self) -> TdlibAuthState:
        return self.state

    def get_events(self, *, after_event_id: int) -> tuple[TdlibEvent, ...]:
        if self._sent:
            return ()
        self._sent = True
        return tuple(e for e in self._events if e.event_id > after_event_id)


class _Bot:
    def is_authorized(self) -> bool:
        return True

    def get_updates(self, *, offset, timeout):  # type: ignore[no-untyped-def]
        return ()


class _Source:
    @property
    def name(self) -> str:
        return "edgar"

    @property
    def source_type(self) -> SourceType:
        return SourceType.FILING

    def fetch(self, *, after_cursor: str) -> tuple[RawFilingRecord, ...]:
        return ()


# 7. missing credentials => AUTH_REQUIRED / fail-closed, never HEALTHY
def test_acceptance_7_missing_credentials_never_healthy(tmp_path: Path) -> None:
    # unauthorized real session -> AUTH_REQUIRED, not HEALTHY
    store = _seed(tmp_path, TELEGRAM_USER_SPEC.required_keys)
    result = read_only_preflight(
        connector_id="telegram-user-tdlib", spec=TELEGRAM_USER_SPEC, store=store,
        probe=telegram_user_probe(_Tdlib(TdlibAuthState.WAIT_CODE)), client_kind=ClientKind.REAL,
        observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.AUTH_REQUIRED


# 8. a fixture/fake source cannot yield a PoI PASS
def test_acceptance_8_fake_never_pass(tmp_path: Path) -> None:
    store = _seed(tmp_path, TELEGRAM_USER_SPEC.required_keys)
    result = read_only_preflight(
        connector_id="telegram-user-tdlib", spec=TELEGRAM_USER_SPEC, store=store,
        probe=telegram_user_probe(_Tdlib(TdlibAuthState.READY)), client_kind=ClientKind.FAKE,
        observed_at=T0,
    )
    assert build_evidence("POI-SOC-TG-USER", result).verdict is PoIVerdict.NOT_EVIDENCE


# 9. reconnect creates no duplicate observation/candidate
def test_acceptance_9_reconnect_no_duplicate() -> None:
    event = TdlibEvent(event_id=1, kind=TdlibEventKind.NEW, chat_external_id="c",
                       message_external_id="m1", text="hi", date=T0)
    client = _Tdlib(TdlibAuthState.READY, (event,))
    adapter = TelegramUserAdapter(client, connector_id="tg", source_account_id="o", allowlist=["c"])
    first = adapter.poll()
    # simulate a reconnect redelivering the same event id
    client._sent = False
    second = adapter.poll()
    assert len(first) == 1 and second == ()  # cursor prevents a duplicate


# 10. secrets never enter logs/artifacts
def test_acceptance_10_secrets_never_in_artifact(tmp_path: Path) -> None:
    store = _seed(tmp_path, TELEGRAM_BOT_SPEC.required_keys)
    result = read_only_preflight(
        connector_id="telegram-bot", spec=TELEGRAM_BOT_SPEC, store=store,
        probe=telegram_bot_probe(_Bot()), client_kind=ClientKind.REAL, observed_at=T0,
    )
    out = tmp_path / "e.json"
    build_evidence("POI-SOC-TG-BOT", result).write(out)
    assert "value" not in out.read_text(encoding="utf-8").replace('"activation_version"', "")


# 11. a source outage does not block independent discovery
def test_acceptance_11_outage_isolated(tmp_path: Path) -> None:
    class Down(_Source):
        def fetch(self, *, after_cursor: str):  # type: ignore[no-untyped-def]
            raise ConnectionError("down")

    from trading_application.source_adapters.activation_sources import FILINGS_SPEC, filings_probe

    result = read_only_preflight(
        connector_id="news-filings", spec=FILINGS_SPEC, store=FileSecretStore(
            tmp_path, Environment.RESEARCH),
        probe=filings_probe((Down(),)), client_kind=ClientKind.REAL, observed_at=T0,
    )
    assert result.status is IntegrationHealthStatus.DOWN  # no exception escaped


# 12. no external source can touch execution
def test_acceptance_12_no_execution_surface_on_any_adapter() -> None:
    tg_user = TelegramUserAdapter(_Tdlib(TdlibAuthState.READY), connector_id="u",
                                  source_account_id="o", allowlist=[])
    tg_bot = TelegramBotAdapter(_Bot(), connector_id="b", source_account_id="o", allowlist=[])
    filings = FilingsAdapter((_Source(),))
    tv = TradingViewAdapter(secret="s")
    for adapter in (tg_user, tg_bot, filings, tv):
        assert_no_execution_surface(adapter)  # raises if any order surface exists


# 13. the Telegram bot never substitutes for the Telegram user
def test_acceptance_13_bot_never_substitutes_user() -> None:
    bot = TelegramBotAdapter(_Bot(), connector_id="b", source_account_id="o", allowlist=[])
    assert bot.satisfies_user_requirement() is False
    assert TelegramBotAdapter.SUPPLEMENTAL_ONLY is True
    user = TelegramUserAdapter(_Tdlib(TdlibAuthState.READY), connector_id="u",
                               source_account_id="o", allowlist=[])
    assert user.satisfies_user_requirement() is True
