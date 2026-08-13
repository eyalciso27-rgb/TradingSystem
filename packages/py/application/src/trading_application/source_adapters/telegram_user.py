"""Telegram user adapter (IMP-0603) — REQUIRED-PRIMARY TDLib user connector.

Canonical DoD: "Required-primary TDLib user connector; Login/QR/2FA, restart,
allowlist, events and encrypted session pass." Authority (CLAUDE.md): the
Telegram personal-user path through TDLib is mandatory and PRIMARY; the Bot API
(IMP-0604) is only supplemental. This adapter is therefore the one that can
satisfy the mandatory user-session gate — but ONLY when TDLib reports a fully
authorized ``READY`` session; every pre-auth state (phone / code / 2FA
password) is surfaced as ``AUTH_REQUIRED`` and yields nothing.

The real ``tdlib`` client is injected as a ``TelegramUserClient`` port; the
encrypted TDLib session lives behind that port (this module never reads or logs
it). Tests drive a deterministic fake. Read-only for trading — no order surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trading_contracts.enums import IntegrationHealthStatus, SocialAccessMode

from trading_application.source_adapters.framework import (
    AdapterError,
    AdapterHealth,
    CircuitBreaker,
    DedupRegistry,
)
from trading_application.source_adapters.social_base import (
    IngestedSocialMessage,
    normalize_social_message,
)

TELEGRAM_USER_ADAPTER_VERSION = "telegram-user-tdlib-adapter-1.0.0"


class TdlibAuthState(StrEnum):
    """The TDLib authorization states this adapter distinguishes."""

    WAIT_PHONE = "WAIT_PHONE"
    WAIT_CODE = "WAIT_CODE"
    WAIT_PASSWORD = "WAIT_PASSWORD"  # 2FA
    READY = "READY"
    CLOSED = "CLOSED"


class TdlibEventKind(StrEnum):
    NEW = "NEW"
    EDITED = "EDITED"
    DELETED = "DELETED"


@dataclass(frozen=True, slots=True)
class TdlibEvent:
    """One TDLib update: a new message, an edit, or a deletion (tombstone)."""

    event_id: int
    kind: TdlibEventKind
    chat_external_id: str
    message_external_id: str
    date: datetime
    text: str = ""
    revision: int = 1
    author_external_id: str | None = None
    author_display_name: str | None = None
    edited_at: datetime | None = None


@runtime_checkable
class TelegramUserClient(Protocol):
    """The TDLib user surface the adapter needs. Read-only ingestion.

    The encrypted session is owned by the TDLib client behind this port; the
    adapter only observes the authorization state and consumes events.
    """

    def authorization_state(self) -> TdlibAuthState: ...

    def get_events(self, *, after_event_id: int) -> tuple[TdlibEvent, ...]: ...


class TelegramUserAdapter:
    """Fail-closed REQUIRED-PRIMARY Telegram user (TDLib) ingestion."""

    #: This is the mandatory primary path (unlike the supplemental bot adapter).
    SUPPLEMENTAL_ONLY = False

    def __init__(
        self,
        client: TelegramUserClient,
        *,
        connector_id: str,
        source_account_id: str,
        allowlist: Iterable[str],
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if not connector_id.strip() or not source_account_id.strip():
            raise AdapterError("connector_id and source_account_id are mandatory")
        self._client = client
        self._connector_id = connector_id
        self._source_account_id = source_account_id
        self._allowlist = frozenset(allowlist)
        self._breaker = breaker or CircuitBreaker(threshold=5)
        self._dedup = DedupRegistry()
        self._cursor = 0  # last consumed event_id (checkpoint/resume across restart)
        self._status = IntegrationHealthStatus.INITIALIZING
        self._detail = "initialized"

    def satisfies_user_requirement(self) -> bool:
        """The user gate is satisfied ONLY by a fully authorized TDLib session."""
        return self._client.authorization_state() is TdlibAuthState.READY

    @property
    def cursor(self) -> int:
        return self._cursor

    def poll(self) -> tuple[IngestedSocialMessage, ...]:
        """One ingestion cycle. Fail-closed; deterministic; idempotent.

        Any pre-``READY`` authorization state (phone / login code / 2FA
        password) is ``AUTH_REQUIRED`` and yields nothing. New messages
        normalize; edits bump the canonical revision; deletions become
        tombstones (``is_deleted_at_source=True``). The cursor only advances,
        so a reconnect/restart that redelivers old events produces no duplicate.
        """
        state = self._client.authorization_state()
        if state is not TdlibAuthState.READY:
            detail = {
                TdlibAuthState.WAIT_PHONE: "awaiting phone number",
                TdlibAuthState.WAIT_CODE: "awaiting login code",
                TdlibAuthState.WAIT_PASSWORD: "awaiting 2FA password",
                TdlibAuthState.CLOSED: "session closed",
            }.get(state, "not authorized")
            status = (
                IntegrationHealthStatus.DOWN
                if state is TdlibAuthState.CLOSED
                else IntegrationHealthStatus.AUTH_REQUIRED
            )
            self._set(status, detail)
            return ()
        if self._breaker.is_open:
            self._set(IntegrationHealthStatus.CIRCUIT_OPEN, "circuit open")
            return ()
        try:
            events = self._client.get_events(after_event_id=self._cursor)
        except Exception as error:  # noqa: BLE001 - provider failure -> DEGRADED
            self._breaker.record_failure()
            self._set(
                IntegrationHealthStatus.DEGRADED,
                f"get_events failed: {type(error).__name__}",
            )
            return ()

        out: list[IngestedSocialMessage] = []
        for event in sorted(events, key=lambda e: e.event_id):
            self._cursor = max(self._cursor, event.event_id)  # advances only
            if event.chat_external_id not in self._allowlist:
                continue
            is_deleted = event.kind is TdlibEventKind.DELETED
            revision = (
                event.revision if event.kind is TdlibEventKind.NEW else max(2, event.revision)
            )
            ingested = normalize_social_message(
                access_mode=SocialAccessMode.TELEGRAM_USER_TDLIB,
                connector_id=self._connector_id,
                source_account_id=self._source_account_id,
                channel_external_id=event.chat_external_id,
                external_message_id=event.message_external_id,
                external_update_id=str(event.event_id),
                text=event.text,
                received_at=event.date,
                author_external_id=event.author_external_id,
                author_display_name=event.author_display_name,
                revision=revision,
                is_deleted_at_source=is_deleted,
                source_edited_at=event.edited_at if event.kind is TdlibEventKind.EDITED else None,
            )
            obs_key = f"{ingested.dedup_key}:{event.event_id}:{event.kind.value}:{revision}"
            if not self._dedup.register(obs_key, ingested.canonical.content_hash):
                continue
            out.append(ingested)
        self._breaker.record_success()
        self._set(IntegrationHealthStatus.HEALTHY, f"ingested {len(out)} event(s)")
        return tuple(out)

    def health(self, *, observed_at: datetime) -> AdapterHealth:
        return AdapterHealth(status=self._status, detail=self._detail, observed_at=observed_at)

    def _set(self, status: IntegrationHealthStatus, detail: str) -> None:
        self._status = status
        self._detail = detail


__all__ = [
    "TELEGRAM_USER_ADAPTER_VERSION",
    "TdlibAuthState",
    "TdlibEvent",
    "TdlibEventKind",
    "TelegramUserAdapter",
    "TelegramUserClient",
]
