"""Telegram bot adapter (IMP-0604) — SUPPLEMENTAL Bot API ingestion.

Canonical DoD: "Works alongside user adapter and cannot satisfy user gate."
Authority (CLAUDE.md): Telegram user/TDLib is the mandatory PRIMARY path; the
Bot API is supplemental and MUST NEVER silently substitute for it. This adapter
therefore hard-codes ``satisfies_user_requirement() is False`` and marks every
observation ``TELEGRAM_BOT`` — a bot capture of a post shares the canonical
dedup key with the user capture, so coexistence yields ONE candidate, but the
bot alone can never satisfy the required user session.

The real Bot API client is injected as a ``TelegramBotClient`` port (official
long-poll ``getUpdates`` shape); tests drive a deterministic fake. Read-only for
trading: there is no order/submit surface anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
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

TELEGRAM_BOT_ADAPTER_VERSION = "telegram-bot-adapter-1.0.0"


@dataclass(frozen=True, slots=True)
class BotUpdate:
    """One official Bot-API update (channel post / message / edit)."""

    update_id: int
    chat_external_id: str
    message_external_id: str
    text: str
    date: datetime
    is_edit: bool = False
    author_external_id: str | None = None
    author_display_name: str | None = None


@runtime_checkable
class TelegramBotClient(Protocol):
    """The official Bot-API surface the adapter needs. Read-only ingestion."""

    def is_authorized(self) -> bool: ...

    def get_updates(self, *, offset: int, timeout: int) -> tuple[BotUpdate, ...]: ...


class TelegramBotAdapter:
    """Fail-closed supplemental Telegram bot ingestion. Never the user gate."""

    #: Binding: the bot path can never satisfy the mandatory user requirement.
    SUPPLEMENTAL_ONLY = True

    def __init__(
        self,
        client: TelegramBotClient,
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
        self._offset = 0  # cursor: next update_id to fetch (checkpoint/resume)
        self._status = IntegrationHealthStatus.INITIALIZING
        self._detail = "initialized"

    @staticmethod
    def satisfies_user_requirement() -> bool:
        """A bot NEVER satisfies the mandatory Telegram user-session gate."""
        return False

    @property
    def cursor(self) -> int:
        return self._offset

    def poll(self, *, timeout: int = 0) -> tuple[IngestedSocialMessage, ...]:
        """One ingestion cycle. Fail-closed; deterministic; idempotent.

        AUTH_REQUIRED when the bot session is not authorized. Allowlisted
        channels only. An edit bumps the canonical revision; a redelivered
        identical update is deduplicated; the cursor only ever advances.
        """
        if not self._client.is_authorized():
            self._set(IntegrationHealthStatus.AUTH_REQUIRED, "bot not authorized")
            return ()
        if self._breaker.is_open:
            self._set(IntegrationHealthStatus.CIRCUIT_OPEN, "circuit open")
            return ()
        try:
            updates = self._client.get_updates(offset=self._offset, timeout=timeout)
        except Exception as error:  # noqa: BLE001 - provider failure -> DEGRADED
            self._breaker.record_failure()
            self._set(
                IntegrationHealthStatus.DEGRADED,
                f"get_updates failed: {type(error).__name__}",
            )
            return ()

        out: list[IngestedSocialMessage] = []
        for update in sorted(updates, key=lambda u: u.update_id):
            self._offset = max(self._offset, update.update_id + 1)  # cursor advances only
            if update.chat_external_id not in self._allowlist:
                continue
            revision = 2 if update.is_edit else 1
            ingested = normalize_social_message(
                access_mode=SocialAccessMode.TELEGRAM_BOT,
                connector_id=self._connector_id,
                source_account_id=self._source_account_id,
                channel_external_id=update.chat_external_id,
                external_message_id=update.message_external_id,
                external_update_id=str(update.update_id),
                text=update.text,
                received_at=update.date,
                author_external_id=update.author_external_id,
                author_display_name=update.author_display_name,
                revision=revision,
                source_edited_at=update.date if update.is_edit else None,
            )
            # Idempotent on the OBSERVATION identity (update id + revision); a
            # true redelivery is dropped, an edit is a new revision.
            obs_key = f"{ingested.dedup_key}:{update.update_id}:{revision}"
            if not self._dedup.register(obs_key, ingested.canonical.content_hash):
                continue
            out.append(ingested)
        self._breaker.record_success()
        self._set(IntegrationHealthStatus.HEALTHY, f"ingested {len(out)} message(s)")
        return tuple(out)

    def health(self, *, observed_at: datetime) -> AdapterHealth:
        return AdapterHealth(status=self._status, detail=self._detail, observed_at=observed_at)

    def _set(self, status: IntegrationHealthStatus, detail: str) -> None:
        self._status = status
        self._detail = detail


__all__ = [
    "TELEGRAM_BOT_ADAPTER_VERSION",
    "BotUpdate",
    "TelegramBotAdapter",
    "TelegramBotClient",
]
