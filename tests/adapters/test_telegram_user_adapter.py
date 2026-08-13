"""IMP-0603 Telegram user (TDLib) adapter — required-primary path; auth/2FA."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_application.source_adapters.telegram_user import (
    TdlibAuthState,
    TdlibEvent,
    TdlibEventKind,
    TelegramUserAdapter,
)
from trading_contracts.enums import IntegrationHealthStatus, SocialAccessMode, SourceType

T0 = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)


class FakeTdlibClient:
    def __init__(
        self, batches: list[tuple[TdlibEvent, ...]], *, state: TdlibAuthState = TdlibAuthState.READY
    ) -> None:
        self._batches = batches
        self._i = 0
        self.state = state
        self.raise_next = False

    def authorization_state(self) -> TdlibAuthState:
        return self.state

    def get_events(self, *, after_event_id: int) -> tuple[TdlibEvent, ...]:
        if self.raise_next:
            raise ConnectionError("tdlib reconnecting")
        if self._i >= len(self._batches):
            return ()
        batch = tuple(e for e in self._batches[self._i] if e.event_id > after_event_id)
        self._i += 1
        return batch


def ev(
    eid: int, msg: str, text: str, *, kind: TdlibEventKind = TdlibEventKind.NEW, minute: int = 0
) -> TdlibEvent:
    return TdlibEvent(
        event_id=eid, kind=kind, chat_external_id="chan-1", message_external_id=msg, text=text,
        date=T0 + timedelta(minutes=minute), author_external_id="u1", author_display_name="Owner",
        edited_at=(T0 + timedelta(minutes=minute)) if kind is TdlibEventKind.EDITED else None,
    )


def make(batches: list[tuple[TdlibEvent, ...]], **kw: object) -> TelegramUserAdapter:
    return TelegramUserAdapter(
        FakeTdlibClient(batches, **kw),  # type: ignore[arg-type]
        connector_id="tg-user-1", source_account_id="owner-42", allowlist=["chan-1"],
    )


def test_waiting_for_phone_is_auth_required_and_not_the_user_gate() -> None:
    adapter = make([(ev(1, "m1", "hi"),)], state=TdlibAuthState.WAIT_PHONE)
    assert adapter.poll() == ()
    assert adapter.satisfies_user_requirement() is False
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.AUTH_REQUIRED


def test_waiting_for_2fa_password_is_auth_required() -> None:
    adapter = make([(ev(1, "m1", "hi"),)], state=TdlibAuthState.WAIT_PASSWORD)
    assert adapter.poll() == ()
    assert adapter.satisfies_user_requirement() is False
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.AUTH_REQUIRED


def test_ready_session_satisfies_the_mandatory_user_gate() -> None:
    adapter = make([()], state=TdlibAuthState.READY)
    assert adapter.SUPPLEMENTAL_ONLY is False
    assert adapter.satisfies_user_requirement() is True


def test_new_message_is_normalized_with_user_tdlib_provenance() -> None:
    adapter = make([(ev(1, "m1", "AAPL setup"),)])
    out = adapter.poll()
    assert len(out) == 1
    assert out[0].observation.access_mode is SocialAccessMode.TELEGRAM_USER_TDLIB
    assert out[0].observation.source_type is SourceType.TELEGRAM
    assert out[0].canonical.content_text == "AAPL setup"
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.HEALTHY


def test_edit_bumps_revision_and_delete_is_a_tombstone() -> None:
    adapter = make([
        (ev(1, "m1", "first"),),
        (ev(2, "m1", "edited", kind=TdlibEventKind.EDITED, minute=1),),
        (ev(3, "m1", "", kind=TdlibEventKind.DELETED, minute=2),),
    ])
    first = adapter.poll()
    assert first[0].canonical.revision == 1
    edited = adapter.poll()
    assert edited[0].canonical.revision == 2
    assert edited[0].canonical.is_deleted_at_source is False
    deleted = adapter.poll()
    assert deleted[0].canonical.is_deleted_at_source is True


def test_reconnect_replay_never_duplicates() -> None:
    # Second batch redelivers event 1 (a restart replay) plus a genuinely new one.
    adapter = make([(ev(1, "m1", "a"),), (ev(1, "m1", "a"), ev(2, "m2", "b", minute=1))])
    first = adapter.poll()
    assert {m.observation.external_message_id for m in first} == {"m1"}
    assert adapter.cursor == 1
    second = adapter.poll()
    assert {m.observation.external_message_id for m in second} == {"m2"}  # no dup m1


def test_non_allowlisted_chat_is_ignored() -> None:
    foreign = TdlibEvent(
        event_id=1, kind=TdlibEventKind.NEW, chat_external_id="other",
        message_external_id="m1", text="x", date=T0,
    )
    adapter = make([(foreign,)])
    assert adapter.poll() == ()


def test_provider_failure_degrades() -> None:
    client = FakeTdlibClient([(ev(1, "m1", "a"),)])
    adapter = TelegramUserAdapter(
        client, connector_id="c", source_account_id="a", allowlist=["chan-1"],
    )
    client.raise_next = True
    assert adapter.poll() == ()
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.DEGRADED


def test_closed_session_is_down() -> None:
    adapter = make([()], state=TdlibAuthState.CLOSED)
    assert adapter.poll() == ()
    assert adapter.health(observed_at=T0).status is IntegrationHealthStatus.DOWN


def test_adapter_has_no_order_surface() -> None:
    adapter = make([()])
    for surface in ("submit", "cancel", "execute", "place_order", "flatten"):
        assert not hasattr(adapter, surface)
