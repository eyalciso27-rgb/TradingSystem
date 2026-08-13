"""Shared social-adapter normalization (reuses IMP-0605 contracts).

Both the Telegram user (TDLib) and bot adapters map their provider updates to
the SAME canonical pair — a ``CanonicalSocialMessage`` (adapter-independent
identity, so user and bot observations of one post share a dedup key) and a
per-observation ``SocialMessageObservation``. This is the IMP-0605 normalization
contract; adapters never invent their own message schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading_contracts.enums import SocialAccessMode, SourceType
from trading_contracts.social import (
    SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE,
    CanonicalSocialMessage,
    SocialMessageObservation,
    compute_social_message_dedup_key,
)

from trading_application.source_adapters.framework import AdapterError, content_hash


@dataclass(frozen=True, slots=True)
class IngestedSocialMessage:
    """One normalized capture: the canonical message + this adapter's observation."""

    canonical: CanonicalSocialMessage
    observation: SocialMessageObservation

    @property
    def dedup_key(self) -> str:
        return self.canonical.dedup_key


def normalize_social_message(
    *,
    access_mode: SocialAccessMode,
    connector_id: str,
    source_account_id: str,
    channel_external_id: str,
    external_message_id: str | None,
    external_update_id: str | None,
    text: str,
    received_at: datetime,
    author_external_id: str | None = None,
    author_display_name: str | None = None,
    revision: int = 1,
    is_deleted_at_source: bool = False,
    source_edited_at: datetime | None = None,
    reply_to_external_message_id: str | None = None,
    thread_external_id: str | None = None,
) -> IngestedSocialMessage:
    """Build the canonical message + observation for one provider update.

    Identity is adapter-independent: the dedup key is derived from
    (provider, channel, message id | content-hash), so a user-mode and a
    bot-mode capture of the same post collapse to one candidate downstream.
    """
    if received_at.tzinfo is None:
        raise AdapterError("received_at must be timezone-aware")
    source_type: SourceType = SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE[access_mode]
    chash = content_hash(text)
    dedup_key = compute_social_message_dedup_key(
        source_type, channel_external_id, external_message_id, chash
    )
    canonical = CanonicalSocialMessage(
        source_type=source_type,
        channel_external_id=channel_external_id,
        external_message_id=external_message_id,
        author_external_id=author_external_id,
        author_display_name=author_display_name,
        content_text=text,
        content_hash=chash,
        reply_to_external_message_id=reply_to_external_message_id,
        thread_external_id=thread_external_id,
        revision=revision,
        is_deleted_at_source=is_deleted_at_source,
        source_edited_at=source_edited_at,
        dedup_key=dedup_key,
    )
    observation = SocialMessageObservation(
        source_type=source_type,
        access_mode=access_mode,
        connector_id=connector_id,
        source_account_id=source_account_id,
        channel_external_id=channel_external_id,
        external_message_id=external_message_id,
        external_update_id=external_update_id,
        revision=revision,
        received_at=received_at,
        message_dedup_key=dedup_key,
    )
    return IngestedSocialMessage(canonical=canonical, observation=observation)


__all__ = ["IngestedSocialMessage", "normalize_social_message"]
