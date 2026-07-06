"""Canonical source/social contracts — SINGLE SOURCE OF TRUTH (IMP-0103).

Models the unified user/bot observation model of the baseline:

- SocialConnectionProfile — Data_Model v0.6 section 31
  (social_connection_profile: provider, access_mode, identity_ref,
  required_capability, enabled, health_status). identity_ref is an opaque
  reference; session/token/cookie/QR material is never part of a contract.
- CanonicalSocialMessage — Data_Model v0.6 sections 31, 7.1 (source_events)
  and 30.2 (telegram_event_metadata); Integration v0.6 sections 3-4
  (WhatsApp/Discord capture: replies as links, external author id separate
  from display name, attachments as references, edits as revisions,
  deletions as tombstones).
- SocialMessageObservation — Data_Model v0.6 section 31 ("a separate
  observation per User/Bot with external IDs and received_at") and
  System_Functional v0.7 section 22 (dedup by access_mode, account/bot
  identity, chat_id, message_id and revision).
- SocialAttachment — Data_Model v0.6 sections 6.2/26 (raw_objects: URI,
  MIME, size, hash; source_attachments link) and Integration v0.6
  (attachments are metadata + reference, downloaded separately through
  Trust/Quarantine). Never raw bytes, never local filesystem paths.

Deduplication (explicit, deterministic — the IMP-0103 done criterion):

    dedup_key = "sha256:" + SHA256(
        source_type US channel_external_id US "id" US external_message_id)
    fallback (no external id — Integration v0.6 section 3.3):
    dedup_key = "sha256:" + SHA256(
        source_type US channel_external_id US "content" US content_hash)

(US = the ASCII unit separator 0x1F.) Per Data_Model section 31 the key is
"provider + canonical chat/source + message/post ID; fallback content
cluster". Access mode, connector identity and ingestion timestamps are
deliberately NOT part of the key, so user-mode and bot-mode observations of
the same post deduplicate to one canonical message
(policy social.cross_mode_dedup_required=true, HARD DATA RULE). Edits and
deletions keep the key (revision / tombstone); a forward is a new message
whose content cluster is linked via content_hash.

JSON Schemas and TypeScript are GENERATED from these models by
scripts/generate_contracts.py — never edit the generated files.
"""

import hashlib
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trading_contracts.enums import IntegrationHealthStatus, SocialAccessMode, SourceType

SHA256_REF_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"
_MEDIA_TOKEN = r"[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*"
MEDIA_TYPE_PATTERN: Final[str] = rf"^{_MEDIA_TOKEN}/{_MEDIA_TOKEN}$"

# Provider implied by each canonical access mode (Data_Model v0.6 sections
# 30-31). Any other combination is illegal and rejected.
SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE: Final[dict[SocialAccessMode, SourceType]] = {
    SocialAccessMode.WHATSAPP_USER: SourceType.WHATSAPP,
    SocialAccessMode.DISCORD_USER: SourceType.DISCORD,
    SocialAccessMode.TELEGRAM_USER_TDLIB: SourceType.TELEGRAM,
    SocialAccessMode.TELEGRAM_BOT: SourceType.TELEGRAM,
}

SOCIAL_SOURCE_TYPES: Final[frozenset[SourceType]] = frozenset(
    SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE.values()
)

_UNIT_SEPARATOR: Final[str] = "\x1f"


def compute_social_message_dedup_key(
    source_type: SourceType,
    channel_external_id: str,
    external_message_id: str | None,
    content_hash: str,
) -> str:
    """Deterministic cross-mode dedup key (Data_Model v0.6 section 31).

    Uses SHA-256 from the stdlib (never Python's built-in hash(), which is
    not stable across processes). The external message id is the primary
    key material; the content hash is the fallback content cluster.
    """
    if external_message_id is not None:
        parts = (source_type.value, channel_external_id, "id", external_message_id)
    else:
        parts = (source_type.value, channel_external_id, "content", content_hash)
    material = _UNIT_SEPARATOR.join(parts)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class SocialConnectionProfile(BaseModel):
    """A configured social connection (Data_Model v0.6 section 31)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: SourceType
    access_mode: SocialAccessMode
    identity_ref: str = Field(min_length=1)
    required_capability: bool
    enabled: bool
    health_status: IntegrationHealthStatus

    @model_validator(mode="after")
    def _validate_provider_mode(self) -> "SocialConnectionProfile":
        expected = SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE[self.access_mode]
        if self.provider is not expected:
            raise ValueError(
                f"access_mode {self.access_mode.value} requires provider "
                f"{expected.value}, not {self.provider.value}"
            )
        return self


class SocialAttachment(BaseModel):
    """Attachment metadata + storage reference; never raw bytes.

    Data_Model v0.6: raw_objects carry URI, MIME, size and hash;
    source_attachments link an event to a file. Integration v0.6: the file
    is downloaded separately through Trust/Quarantine, so hash and storage
    reference may be absent until the download completes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    media_type: str = Field(pattern=MEDIA_TYPE_PATTERN)
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: str | None = Field(default=None, pattern=SHA256_REF_PATTERN)
    raw_object_ref: str | None = None


class CanonicalSocialMessage(BaseModel):
    """Adapter-independent canonical social message (Data_Model section 31).

    Identity does not depend on a single adapter or access mode: the same
    post captured by user mode and bot mode must map to the same dedup_key.
    Edits create revisions; deletion at the source is a tombstone flag; a
    reply/quote is stored as a link, not a copy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: SourceType
    channel_external_id: str = Field(min_length=1)
    external_message_id: str | None = None
    author_external_id: str | None = None
    author_display_name: str | None = None
    content_text: str | None = None
    content_language: str | None = None
    content_hash: str = Field(pattern=SHA256_REF_PATTERN)
    raw_object_ref: str | None = None
    reply_to_external_message_id: str | None = None
    thread_external_id: str | None = None
    media_group_id: str | None = None
    forward_origin_json: str | None = None
    attachments: tuple[SocialAttachment, ...] = ()
    revision: int = Field(default=1, ge=1)
    is_deleted_at_source: bool = False
    source_edited_at: AwareDatetime | None = None
    dedup_key: str = Field(pattern=SHA256_REF_PATTERN)

    @model_validator(mode="after")
    def _validate_message(self) -> "CanonicalSocialMessage":
        if self.source_type not in SOCIAL_SOURCE_TYPES:
            raise ValueError(
                f"source_type {self.source_type.value} is not a social source; "
                f"allowed: {sorted(t.value for t in SOCIAL_SOURCE_TYPES)}"
            )
        expected = compute_social_message_dedup_key(
            self.source_type,
            self.channel_external_id,
            self.external_message_id,
            self.content_hash,
        )
        if self.dedup_key != expected:
            raise ValueError(
                "dedup_key does not match the canonical computation "
                "(provider + chat/source + message id, content-hash fallback)"
            )
        return self


class SocialMessageObservation(BaseModel):
    """One adapter's observation of a canonical message (Data_Model 31).

    A separate observation exists per user/bot capture with its external IDs
    and received_at (the adapter-side receive time — source-native, distinct
    from the envelope's ingested_at, which is when the system stored the
    event). message_dedup_key ties the observation to the canonical message,
    so user and bot observations of the same post never count twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: SourceType
    access_mode: SocialAccessMode
    connector_id: str = Field(min_length=1)
    source_account_id: str = Field(min_length=1)
    channel_external_id: str = Field(min_length=1)
    external_message_id: str | None = None
    external_update_id: str | None = None
    revision: int = Field(default=1, ge=1)
    received_at: AwareDatetime
    message_dedup_key: str = Field(pattern=SHA256_REF_PATTERN)

    @model_validator(mode="after")
    def _validate_observation(self) -> "SocialMessageObservation":
        expected = SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE[self.access_mode]
        if self.source_type is not expected:
            raise ValueError(
                f"access_mode {self.access_mode.value} requires source_type "
                f"{expected.value}, not {self.source_type.value}"
            )
        return self


SOCIAL_MODELS: Final[tuple[type[BaseModel], ...]] = (
    SocialAttachment,
    SocialConnectionProfile,
    CanonicalSocialMessage,
    SocialMessageObservation,
)
