"""Telegram user + bot coexistence — one canonical candidate, no substitution.

Proves at the CODE level (deterministic fakes) the IMP-0605 property "same
Telegram post via user/bot creates one candidate" and the CLAUDE.md rule that
the bot path can never substitute for the mandatory user path. The REAL proof
(POI-SOC-TG-COEX) still requires the owner's live accounts and stays BLOCKED.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading_application.source_adapters.telegram_bot import BotUpdate, TelegramBotAdapter
from trading_application.source_adapters.telegram_user import (
    TdlibAuthState,
    TdlibEvent,
    TdlibEventKind,
    TelegramUserAdapter,
)

T0 = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
CHANNEL = "chan-1"
MSG_ID = "42"
TEXT = "AAPL breaking out over 230"


class _ReadyTdlib:
    def __init__(self, events: tuple[TdlibEvent, ...]) -> None:
        self._events = events
        self._sent = False

    def authorization_state(self) -> TdlibAuthState:
        return TdlibAuthState.READY

    def get_events(self, *, after_event_id: int) -> tuple[TdlibEvent, ...]:
        if self._sent:
            return ()
        self._sent = True
        return self._events


class _Bot:
    def __init__(self, updates: tuple[BotUpdate, ...]) -> None:
        self._updates = updates
        self._sent = False

    def is_authorized(self) -> bool:
        return True

    def get_updates(self, *, offset: int, timeout: int) -> tuple[BotUpdate, ...]:
        if self._sent:
            return ()
        self._sent = True
        return self._updates


def test_user_and_bot_capture_of_one_post_share_the_dedup_key() -> None:
    user = TelegramUserAdapter(
        _ReadyTdlib((TdlibEvent(
            event_id=1, kind=TdlibEventKind.NEW, chat_external_id=CHANNEL,
            message_external_id=MSG_ID, text=TEXT, date=T0,
        ),)),
        connector_id="tg-user", source_account_id="owner", allowlist=[CHANNEL],
    )
    bot = TelegramBotAdapter(
        _Bot((BotUpdate(
            update_id=5, chat_external_id=CHANNEL, message_external_id=MSG_ID,
            text=TEXT, date=T0,
        ),)),
        connector_id="tg-bot", source_account_id="bot", allowlist=[CHANNEL],
    )
    user_msg = user.poll()[0]
    bot_msg = bot.poll()[0]

    # Same underlying post -> identical canonical dedup key -> ONE candidate.
    assert user_msg.dedup_key == bot_msg.dedup_key
    # ...even though they are distinct observations via distinct access modes.
    assert user_msg.observation.access_mode is not bot_msg.observation.access_mode


def test_the_bot_alone_never_satisfies_the_user_gate() -> None:
    bot = TelegramBotAdapter(
        _Bot(()), connector_id="tg-bot", source_account_id="bot", allowlist=[CHANNEL],
    )
    # No matter what the bot ingests, it cannot satisfy the mandatory user gate.
    assert bot.satisfies_user_requirement() is False
    assert TelegramBotAdapter.SUPPLEMENTAL_ONLY is True

    user = TelegramUserAdapter(
        _ReadyTdlib(()), connector_id="tg-user", source_account_id="owner", allowlist=[CHANNEL],
    )
    assert user.satisfies_user_requirement() is True  # only the READY user session does
