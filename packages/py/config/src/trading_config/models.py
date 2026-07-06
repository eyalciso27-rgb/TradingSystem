"""Versioned configuration contracts (IMP-0105).

Source of truth for the config loader's typed models. These are internal
loader contracts (Python backend / workers), not cross-language domain
contracts, so they are intentionally NOT part of scripts/generate_contracts.py
(no JSON-Schema/TypeScript generation). See README for that decision.

Authorities:

- Config repository and Active Version Pointer: Decision_Contract_v0.8
  section 7 (config/ tree; "Active Version Pointer קובע איזו גרסה פעילה";
  "Rollback מחזיר Pointer לגרסה קודמת"; version change needs validation/audit,
  not a build).
- Versioned rows immutable, active_version_pointers mutable+audit per
  Context/Environment: Data_Model_and_Retention_Specification_v0.6 sections 4
  and 15 (active_version_pointers).
- Version token and entry shape (version, environment, valid_from, owner,
  status): Policy_Parameters_and_Initial_Assumptions_v0.5 section 1.1.
- Environment/OperatingMode axes and their mapping: reused from
  trading_contracts (IMP-0101). No secrets in config; secret material is a
  reference only (Security_and_Operations_Specification_v0.6).
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from trading_contracts.enums import (
    OPERATING_MODE_TO_ENVIRONMENT,
    Environment,
    OperatingMode,
    SocialAccessMode,
    SourceType,
)

SEMVER_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"
SEGMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
TOKEN_PATTERN = r"^[A-Z][A-Z0-9_]*$"
SHA256_REF_PATTERN = r"^sha256:[0-9a-f]{64}$"
# An opaque secret reference (e.g. "sops://age/whatsapp"), never a value.
SECRET_REF_PATTERN = r"^[A-Za-z][A-Za-z0-9._:/-]*$"

SemVer = Annotated[str, Field(pattern=SEMVER_PATTERN)]
Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
Token = Annotated[str, Field(pattern=TOKEN_PATTERN)]
Sha256Ref = Annotated[str, Field(pattern=SHA256_REF_PATTERN)]
SecretRef = Annotated[str, Field(pattern=SECRET_REF_PATTERN, max_length=200)]

CONFIG_DIR_BY_TYPE: dict[str, str] = {
    "POLICY": "policies",
    "STRATEGY": "strategies",
    "PROVIDER": "providers",
    "SOURCE": "sources",
}


class ConfigModelError(ValueError):
    """A configuration value violates its contract."""


def _reject_inline_secret(value: str | None) -> str | None:
    if value is None:
        return value
    lowered = value.lower()
    for marker in ("-----begin", "private key", "password=", "bearer "):
        if marker in lowered:
            raise ConfigModelError("secret_ref must be an opaque reference, not a secret value")
    return value


class _ConfigBase(BaseModel):
    """Shared metadata for every versioned config (frozen, strict)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: Segment
    version: SemVer
    schema_version: SemVer
    environments: tuple[OperatingMode, ...] = Field(min_length=1)
    enabled: bool
    owner: str = Field(min_length=1)
    status: Token
    valid_from: AwareDatetime
    content_hash: Sha256Ref | None = None

    @property
    def resolved_environments(self) -> frozenset[Environment]:
        """The environment-axis values implied by the operating modes."""
        return frozenset(OPERATING_MODE_TO_ENVIRONMENT[mode] for mode in self.environments)


class PolicyConfig(_ConfigBase):
    """Policy config (Decision Contract §7: policies/{decision,risk,latency,...})."""

    config_type: Literal["POLICY"]
    policy_kind: Token
    parameter_refs: tuple[str, ...] = ()


class StrategyConfig(_ConfigBase):
    """Strategy manifest reference (strategy_versions)."""

    config_type: Literal["STRATEGY"]
    strategy_id: Segment
    strategy_version: SemVer
    policy_version_ref: str | None = None
    allowed_operating_modes: tuple[OperatingMode, ...] = Field(min_length=1)
    assessment_requirement_refs: tuple[str, ...] = ()


class ProviderConfig(_ConfigBase):
    """Market-data / broker provider config; adapters are out of scope here."""

    config_type: Literal["PROVIDER"]
    provider_id: Segment
    provider_kind: Token
    capability_ref: str | None = None
    access_mode: SocialAccessMode | None = None
    health_policy_ref: str | None = None
    rate_policy_ref: str | None = None
    secret_ref: SecretRef | None = None

    _no_inline_secret = field_validator("secret_ref")(_reject_inline_secret)


class SourceConfig(_ConfigBase):
    """Source config (WhatsApp/Discord/Telegram/InvestingPro/news/market)."""

    config_type: Literal["SOURCE"]
    source_id: Segment
    source_type: SourceType
    access_mode: SocialAccessMode | None = None
    health_policy_ref: str | None = None
    secret_ref: SecretRef | None = None

    _no_inline_secret = field_validator("secret_ref")(_reject_inline_secret)


VersionedConfig = Annotated[
    PolicyConfig | StrategyConfig | ProviderConfig | SourceConfig,
    Field(discriminator="config_type"),
]

CONFIG_MODEL_BY_TYPE: dict[str, type[_ConfigBase]] = {
    "POLICY": PolicyConfig,
    "STRATEGY": StrategyConfig,
    "PROVIDER": ProviderConfig,
    "SOURCE": SourceConfig,
}


class ActivePointer(BaseModel):
    """Mutable pointer to the active version of one config, per environment.

    Holds no config content; rollback is a change of `version` only. Paper and
    Live pointers live in separate environment namespaces and never share a
    pointer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_type: Literal["POLICY", "STRATEGY", "PROVIDER", "SOURCE"]
    config_id: Segment
    version: SemVer
    environment: Environment
    activated_at: AwareDatetime
    activated_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    content_hash: Sha256Ref | None = None

    @field_validator("activated_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ConfigModelError("activated_at must be timezone-aware")
        return value
