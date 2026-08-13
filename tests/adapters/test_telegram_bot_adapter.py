"""IMP-0604 Telegram bot adapter — supplemental only; cannot satisfy user gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_application.source_adapters.telegram_bot import (
    BotUpdate,
    TelegramBotAdapter,
)
from trading_contracts.enums import IntegrationHealthStatus, SocialAccessMode, SourceType

T0 = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


class FakeBotClient:
    def __init__(self, batches: list[tuple[BotUpdate, ...]], *, authorized: bool = True) -> None:
        self._batches = batches
        self._i = 0
        self.authorized = authorized
        self.raise_next = False

    def is_authorized(self) -> bool:
        return self.authorized

    def get_updates(self, *, offset: int, timeout: int) -> tuple[BotUpdate, ...]:
        if self.raise_next:
            raise ConnectionError("network")
        if self._i >= len(self._batches):
            return ()
        batch = tuple(u for u in self._batches[self._i] if u.update_id >= offset)
        self._i += 1
        return batch


def upd(uid: int, msg: str, text: str, *, minute: int = 0, edit: bool = False) -> BotUpdate:
    return BotUpdate(
        update_id=uid, chat_external_id="chan-1", message_external_id=msg, text=text,
        date=T0 + timedelta(minutes=minute), is_edit=edit, author_external_id="u1",
        author_display_name="Trader",
    )


def make(batches: list[tuple[BotUpdate, ...]], **kw: object) -> TelegramBotAdapter:
    return TelegramBotAdapter(
        FakeBotClient(batches, **kw),  # type: ignore[arg-type]
        connector_id="tg-bot-1", source_account_id="bot-123", allowlist=["chan-1"],
    )


def test_supplemental_only_never_satisfies_the_user_gate() -> None:
    adapter = make([()])
    assert TelegramBotAdapter.SUPPLEMENTAL_ONLY is True
    assert adapter.satisfies_user_requirement() is False


def test_not_authorized_is_auth_required_and_yields_nothing() -> None:
    adapter = make([(upd(1, "m1", "hi"),)], authorized=False)
    assert adapter.poll() == ()
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.AUTH_REQUIRED


def test_message_is_normalized_with_bot_provenance() -> None:
    adapter = make([(upd(1, "m1", "AAPL breaking out"),)])
    out = adapter.poll()
    assert len(out) == 1
    obs = out[0].observation
    assert obs.access_mode is SocialAccessMode.TELEGRAM_BOT
    assert obs.source_type is SourceType.TELEGRAM
    assert out[0].canonical.content_text == "AAPL breaking out"
    assert out[0].dedup_key.startswith("sha256:")
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.HEALTHY


def test_non_allowlisted_channel_is_ignored() -> None:
    foreign = BotUpdate(
        update_id=1, chat_external_id="other", message_external_id="m1", text="x", date=T0,
    )
    adapter = make([(foreign,)])
    assert adapter.poll() == ()


def test_cursor_advances_and_resume_never_duplicates() -> None:
    adapter = make([(upd(1, "m1", "a"), upd(2, "m2", "b")), (upd(2, "m2", "b"),)])
    first = adapter.poll()
    assert {m.observation.external_message_id for m in first} == {"m1", "m2"}
    assert adapter.cursor == 3
    second = adapter.poll()  # redelivers update 2 -> deduped
    assert second == ()


def test_edit_bumps_the_canonical_revision() -> None:
    adapter = make([(upd(1, "m1", "first"),), (upd(2, "m1", "edited", edit=True),)])
    adapter.poll()
    edited = adapter.poll()
    assert len(edited) == 1
    assert edited[0].canonical.revision == 2
    assert edited[0].canonical.is_deleted_at_source is False


def test_provider_failure_degrades_and_can_open_circuit() -> None:
    client = FakeBotClient([(upd(1, "m1", "a"),)])
    adapter = TelegramBotAdapter(
        client, connector_id="c", source_account_id="a", allowlist=["chan-1"],
    )
    client.raise_next = True
    assert adapter.poll() == ()
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.DEGRADED


def test_adapter_has_no_order_surface() -> None:
    adapter = make([()])
    for surface in ("submit", "cancel", "execute", "place_order", "flatten"):
        assert not hasattr(adapter, surface)
