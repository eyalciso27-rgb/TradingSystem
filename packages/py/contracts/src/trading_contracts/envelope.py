"""Canonical event envelope — SINGLE SOURCE OF TRUTH (IMP-0102).

Every event in the system is wrapped in this envelope. Field set is exactly
the IMP-0102 scope (event_id, type, version, occurred/observed/available/
ingested, correlation, causation) — no convenience fields.

Authorities:

- Identifier types: Data_Model_and_Retention_Specification_v0.6 section 4
  (uuid primary keys; UUIDv7 is generated at the application layer per the
  Technology specification — envelope validation accepts any RFC 4122/9562
  UUID and does not pin the version).
- Point-in-time semantics: Data_Model_and_Retention_Specification_v0.6
  section 24.2 (available_at, ingested_at) plus the four-timestamp model
  mandated by IMP-0102: occurred_at = when the event happened in the world,
  observed_at = when the source noticed it, available_at = first moment the
  system was allowed to use it in a decision, ingested_at = when the system
  actually ingested it. Equal timestamps are allowed; the baseline defines
  no ordering rule, so none is enforced here.
- type format: open SCREAMING_SNAKE string per the canonical event_type
  example in Integration_Specification_v0.6 section 2.4 (open list — not an
  enum by design).
- version format: '<major>.<minor>' per the canonical schema_version example
  ("1.0") in Integration_Specification_v0.6 section 2.4. This is the version
  of the event type's payload schema — distinct from the envelope JSON-Schema
  version (the $id suffix) and from the baseline pack version.
- correlation_id is required (Data Model: correlation_id uuid NOT NULL;
  Security section 12: correlation_id in structured logs). causation_id is
  the event_id of the direct cause; root events omit it (the baseline fixes
  no rule here — documented decision).

JSON Schema and TypeScript are GENERATED from this model by
scripts/generate_contracts.py — never edit the generated files.
"""

from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TYPE_PATTERN: Final[str] = r"^[A-Z][A-Z0-9_]*$"
VERSION_PATTERN: Final[str] = r"^[0-9]+\.[0-9]+$"

ENVELOPE_SCHEMA_ID: Final[str] = "trading-system/events/event_envelope/v1"


class EventEnvelope(BaseModel):
    """Canonical envelope wrapping every event in the system.

    Immutable (frozen) and strict: unknown fields are rejected, all four
    point-in-time fields are mandatory and must be timezone-aware.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    type: str = Field(pattern=TYPE_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    occurred_at: AwareDatetime
    observed_at: AwareDatetime
    available_at: AwareDatetime
    ingested_at: AwareDatetime
    correlation_id: UUID
    causation_id: UUID | None = None
