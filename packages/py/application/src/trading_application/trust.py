"""Trust / quarantine service (IMP-0305).

Authority: Trust_Adversarial_Input_and_Market_Manipulation_Controls_v0.3 —
section 2 (pipeline: Trust Assessment -> Quarantine or Eligible Evidence),
section 3 (canonical source classes; QUARANTINED content is forbidden in any
decision), section 4 (prompt-injection isolation: content is data only; the
analysis model never gets broker/shell/secrets/policy-write tools), section 7
(attachments: MIME allowlist, size limits, no active content) and section 10
(acceptance: an injection fixture never creates an OrderIntent or changes
policy; quarantined content never reaches Fusion; every evidence points to
provenance and content hash).

Separation of concerns:

- ``assess`` produces a TrustAssessment (pure, no state) — flags found in
  untrusted content/attachments. Detection inputs (injection markers, MIME
  allowlist, size limits) are POLICY-provided parameters, not invented
  constants.
- ``QuarantineService.gate`` turns an assessment into a quarantine decision
  and stores state. Fail-closed: flagged, unverifiable or unknown content is
  blocked; only explicitly eligible content may continue toward Candidate/
  Decision/OrderIntent/broker.
- Manual release is explicit, RBAC-protected (an allowed
  AuthorizationDecision from IMP-0301), audited with actor and reason.
- Permanent rejection keeps the record forever — suspicious events are never
  silently deleted.

Quarantine state stores references and flags only (content_ref hash,
raw_object_ref) — never inline payloads or secrets. PAPER and LIVE use
separate service instances.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trading_contracts.enums import Environment

from trading_application.auth import AuditSink, AuthorizationDecision


class SourceClass(StrEnum):
    """Canonical source classes (Trust spec section 3)."""

    OFFICIAL = "OFFICIAL"
    LICENSED_MARKET_DATA = "LICENSED_MARKET_DATA"
    REPUTABLE_NEWS = "REPUTABLE_NEWS"
    EXTERNAL_SIGNAL = "EXTERNAL_SIGNAL"
    SOCIAL_IDENTIFIED = "SOCIAL_IDENTIFIED"
    SOCIAL_UNKNOWN = "SOCIAL_UNKNOWN"
    QUARANTINED = "QUARANTINED"


class TrustFlag(StrEnum):
    """Canonical quarantine causes, one per documented control."""

    PROMPT_INJECTION = "PROMPT_INJECTION"  # section 1/4
    DISALLOWED_MIME = "DISALLOWED_MIME"  # section 7 allowlist
    OVERSIZED_ATTACHMENT = "OVERSIZED_ATTACHMENT"  # section 7 size limits
    ACTIVE_CONTENT = "ACTIVE_CONTENT"  # section 7 no macros/executables
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"  # section 10 content hash
    UNVERIFIABLE_SOURCE = "UNVERIFIABLE_SOURCE"  # section 3 QUARANTINED class


class QuarantineStatus(StrEnum):
    QUARANTINED = "QUARANTINED"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"


class QuarantineBlockedError(PermissionError):
    """Content may not continue toward trading processing."""


class ReleaseDeniedError(PermissionError):
    """The manual release was not authorized."""


@dataclass(frozen=True)
class AttachmentMeta:
    media_type: str
    size_bytes: int


@dataclass(frozen=True)
class TrustAssessment:
    """Pure output of trust assessment: references + flags, no payload."""

    content_ref: str
    source_class: SourceClass
    flags: tuple[TrustFlag, ...]
    cluster_key: str | None = None


@dataclass
class QuarantineRecord:
    content_ref: str
    source_class: SourceClass
    flags: tuple[TrustFlag, ...]
    status: QuarantineStatus
    history: list[str] = field(default_factory=list)


def assess(
    *,
    content_ref: str,
    claimed_hash: str,
    source_class: SourceClass,
    content_text: str | None,
    attachments: Sequence[AttachmentMeta] = (),
    injection_markers: Sequence[str] = (),
    mime_allowlist: Sequence[str] = (),
    max_attachment_bytes: int | None = None,
    cluster_key: str | None = None,
) -> TrustAssessment:
    """Trust assessment (pipeline step, section 2). Pure and deterministic.

    Detection inputs come from policy configuration; the content itself is
    treated as data only and no tool is ever invoked here (section 4).
    """
    flags: list[TrustFlag] = []
    if content_ref != claimed_hash:
        flags.append(TrustFlag.INTEGRITY_FAILURE)
    if source_class is SourceClass.QUARANTINED:
        flags.append(TrustFlag.UNVERIFIABLE_SOURCE)
    lowered = (content_text or "").lower()
    if any(marker.lower() in lowered for marker in injection_markers):
        flags.append(TrustFlag.PROMPT_INJECTION)
    for attachment in attachments:
        if mime_allowlist and attachment.media_type not in mime_allowlist:
            flags.append(TrustFlag.DISALLOWED_MIME)
        if max_attachment_bytes is not None and attachment.size_bytes > max_attachment_bytes:
            flags.append(TrustFlag.OVERSIZED_ATTACHMENT)
        if attachment.media_type in _ACTIVE_CONTENT_MIME:
            flags.append(TrustFlag.ACTIVE_CONTENT)
    return TrustAssessment(
        content_ref=content_ref,
        source_class=source_class,
        flags=tuple(dict.fromkeys(flags)),
        cluster_key=cluster_key,
    )


# Executable/active content types per section 7 ("no macros, no active HTML,
# no embedded executables").
_ACTIVE_CONTENT_MIME = frozenset(
    {"application/x-msdownload", "application/x-sh", "text/html"}
)


class QuarantineService:
    """Quarantine decision + state for ONE environment (PAPER/LIVE separate)."""

    def __init__(self, environment: Environment, audit: AuditSink) -> None:
        self._environment = environment
        self._audit = audit
        self._records: dict[str, QuarantineRecord] = {}
        self._eligible: set[str] = set()

    def _audit_event(self, actor: str, action: str, ref: str, reason: str) -> None:
        self._audit.record(
            AuthorizationDecision(
                allowed=True, principal_id=actor, action=action, reason=f"{ref}: {reason}"
            )
        )

    def gate(self, assessment: TrustAssessment, *, now: datetime) -> QuarantineRecord | None:
        """Quarantine-or-eligible decision (section 2). Idempotent.

        Returns the quarantine record when blocked, None when eligible.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        existing = self._records.get(assessment.content_ref)
        if existing is not None:
            return existing  # duplicate quarantine is a no-op
        if not assessment.flags:
            self._eligible.add(assessment.content_ref)
            return None
        record = QuarantineRecord(
            content_ref=assessment.content_ref,
            source_class=assessment.source_class,
            flags=assessment.flags,
            status=QuarantineStatus.QUARANTINED,
            history=[f"quarantined at {now.isoformat()}"],
        )
        self._records[assessment.content_ref] = record
        self._eligible.discard(assessment.content_ref)
        flag_names = ",".join(flag.value for flag in assessment.flags)
        self._audit_event("trust-service", "QUARANTINE", assessment.content_ref, flag_names)
        return record

    def admit_for_trading(self, content_ref: str) -> None:
        """Server-side gate before Candidate/Decision/OrderIntent/broker.

        Fail-closed: quarantined, rejected and UNKNOWN content are all
        blocked; only content explicitly assessed as eligible (or released)
        may pass. Quarantined content never reaches Fusion (section 10).
        """
        record = self._records.get(content_ref)
        if record is not None and record.status is not QuarantineStatus.RELEASED:
            raise QuarantineBlockedError(
                f"content {content_ref} is {record.status.value}; "
                "it may not reach trading processing"
            )
        if record is None and content_ref not in self._eligible:
            raise QuarantineBlockedError(
                f"content {content_ref} was never trust-assessed; fail-closed"
            )

    def release(
        self,
        content_ref: str,
        *,
        authorization: AuthorizationDecision,
        reason: str,
        now: datetime,
    ) -> QuarantineRecord:
        """Explicit, RBAC-protected, audited manual release."""
        if not authorization.allowed:
            self._audit.record(authorization)  # the denial itself is audited
            raise ReleaseDeniedError(
                f"release of {content_ref} denied: {authorization.reason}"
            )
        if not reason.strip():
            raise ReleaseDeniedError("release requires an explicit reason")
        record = self._records.get(content_ref)
        if record is None:
            raise QuarantineBlockedError(f"content {content_ref} is not quarantined")
        if record.status is QuarantineStatus.REJECTED:
            raise ReleaseDeniedError("permanently rejected content cannot be released")
        record.status = QuarantineStatus.RELEASED
        record.history.append(
            f"released at {now.isoformat()} by {authorization.principal_id}: {reason}"
        )
        self._audit_event(
            authorization.principal_id, "QUARANTINE_RELEASE", content_ref, reason
        )
        return record

    def reject_permanently(
        self,
        content_ref: str,
        *,
        authorization: AuthorizationDecision,
        reason: str,
        now: datetime,
    ) -> QuarantineRecord:
        """Permanent rejection; the record is kept, never silently deleted."""
        if not authorization.allowed:
            self._audit.record(authorization)
            raise ReleaseDeniedError(f"rejection of {content_ref} denied")
        record = self._records.get(content_ref)
        if record is None:
            raise QuarantineBlockedError(f"content {content_ref} is not quarantined")
        record.status = QuarantineStatus.REJECTED
        record.history.append(
            f"rejected at {now.isoformat()} by {authorization.principal_id}: {reason}"
        )
        self._audit_event(
            authorization.principal_id, "QUARANTINE_REJECT", content_ref, reason
        )
        return record

    @property
    def environment(self) -> Environment:
        return self._environment
