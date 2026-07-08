"""Quant/ML model registry scaffolding (IMP-0806).

Authority: System_Functional_Specification_v0.7 section 30 (Model Registry
and Governance: model versions, calibration, drift, approval and rollback),
Data_Model_and_Retention_Specification_v0.6 section 18 (model_registry:
model id, family, artifact, status, calibration and approval; the model
artifact lives in object storage with a hash — the registry stores metadata
and approval state only) and Decision_Contract 12.2 (CalibrationStatus is
the canonical calibration axis; PAPER_AUTO gates require CALIBRATED — that
gate is enforced downstream, not silently here).

Governance invariants (the DoD):

- Every newly registered model starts **SHADOW / UNVERIFIED / NOT_EVALUATED**
  regardless of what the caller supplies — shadow-only by construction.
- **No unapproved model can become ACTIVE**: promotion is an explicit,
  RBAC-guarded (PROMOTE_STRATEGY_POLICY), audited action carrying the
  approver; there is no automatic promotion and no fallback that activates
  an unapproved model (``resolve_active`` returns only approved ACTIVE
  models — never a shadow substitute).
- A DRIFTING model cannot be promoted; rollback is explicit and audited.
- ``artifact_ref`` is an OPAQUE reference (object-storage key + hash per
  Data Model 18.1) — never an inline binary and never secret material.
- Registry metadata never contains secrets/tokens/credentials (validated).

Per ADR-0014 this is scaffolding over an in-memory store port: no ML
platform, no network, no real calibration/drift evaluation (those arrive
with the research/governance tasks and POI-AI-001).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from trading_contracts.enums import CalibrationStatus

from trading_application.auth import (
    AuditSink,
    AuthAction,
    AuthorizationDecision,
    Principal,
    require,
)

_SECRET_MARKERS = ("secret", "token", "password", "api_key", "apikey", "credential")
_MAX_ARTIFACT_REF_LENGTH = 512


class ModelRegistryError(Exception):
    """Fail-closed registry refusal."""


class PromotionDeniedError(ModelRegistryError):
    """Promotion refused: approval/RBAC/drift gate not satisfied."""


class ModelStatus(StrEnum):
    """Lifecycle status (SFS section 30: shadow/active + rollback)."""

    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class DriftStatus(StrEnum):
    """Drift evaluation state (SFS section 30: drift governance)."""

    NOT_EVALUATED = "NOT_EVALUATED"
    STABLE = "STABLE"
    DRIFTING = "DRIFTING"


@dataclass(frozen=True, slots=True)
class ModelRecord:
    """Registry metadata for one model version (never the artifact itself)."""

    model_id: str
    family: str
    version: str
    artifact_ref: str  # opaque object-storage reference
    artifact_hash: str  # content hash of the stored artifact (18.1)
    status: ModelStatus
    calibration_status: CalibrationStatus
    drift_status: DriftStatus
    registered_by: str
    registered_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None

    def __post_init__(self) -> None:
        for stamp in (self.registered_at, self.approved_at):
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError("registry timestamps must be timezone-aware")
        if not self.model_id or not self.family or not self.version:
            raise ModelRegistryError("model_id, family and version are mandatory")
        _validate_artifact_ref(self.artifact_ref)
        if not self.artifact_hash:
            raise ModelRegistryError("artifact_hash is mandatory (Data Model 18.1)")
        for field_value in (self.model_id, self.family, self.version, self.artifact_ref):
            lowered = field_value.lower()
            for marker in _SECRET_MARKERS:
                if marker in lowered:
                    raise ModelRegistryError(
                        "registry metadata may not contain secret material"
                    )

    @property
    def key(self) -> tuple[str, str]:
        return (self.model_id, self.version)


def _validate_artifact_ref(artifact_ref: str) -> None:
    """The artifact stays in object storage; the registry holds a reference."""
    if not artifact_ref:
        raise ModelRegistryError("artifact_ref is mandatory")
    if len(artifact_ref) > _MAX_ARTIFACT_REF_LENGTH:
        raise ModelRegistryError(
            "artifact_ref must be an opaque reference, not inline artifact content"
        )
    if any(ord(ch) < 32 for ch in artifact_ref):
        raise ModelRegistryError("artifact_ref must be printable (no binary content)")


class ModelRegistry:
    """Deterministic in-memory registry enforcing the governance gates."""

    def __init__(self, audit: AuditSink) -> None:
        self._audit = audit
        self._records: dict[tuple[str, str], ModelRecord] = {}

    # -- registration ---------------------------------------------------------

    def register(
        self,
        *,
        model_id: str,
        family: str,
        version: str,
        artifact_ref: str,
        artifact_hash: str,
        registered_by: str,
        now: datetime,
    ) -> ModelRecord:
        """Register a model version — ALWAYS shadow-only/unverified at birth."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        record = ModelRecord(
            model_id=model_id,
            family=family,
            version=version,
            artifact_ref=artifact_ref,
            artifact_hash=artifact_hash,
            status=ModelStatus.SHADOW,  # never caller-controlled
            calibration_status=CalibrationStatus.UNVERIFIED,
            drift_status=DriftStatus.NOT_EVALUATED,
            registered_by=registered_by,
            registered_at=now,
        )
        existing = self._records.get(record.key)
        if existing is not None:
            if (
                existing.artifact_ref == record.artifact_ref
                and existing.artifact_hash == record.artifact_hash
                and existing.family == record.family
            ):
                return existing  # idempotent re-registration of the same version
            raise ModelRegistryError(
                f"model {model_id!r} version {version!r} already registered with "
                "different artifact metadata (versions are immutable)"
            )
        self._records[record.key] = record
        self._audit_event(registered_by, f"MODEL_REGISTER:{model_id}:{version}", "shadow")
        return record

    # -- governance transitions ------------------------------------------------

    def promote(
        self,
        principal: Principal,
        *,
        model_id: str,
        version: str,
        now: datetime,
    ) -> ModelRecord:
        """SHADOW -> ACTIVE: explicit, RBAC-guarded, audited approval only."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        # Server-side RBAC (deny by default): promotion is a strategy/policy
        # promotion action; the denial is audited by `require`.
        require(principal, AuthAction.PROMOTE_STRATEGY_POLICY, self._audit)
        record = self._require(model_id, version)
        if record.drift_status is DriftStatus.DRIFTING:
            raise PromotionDeniedError(
                "a DRIFTING model cannot be promoted (drift governance)"
            )
        if record.status is ModelStatus.RETIRED:
            raise PromotionDeniedError("a retired model version cannot be promoted")
        promoted = replace(
            record,
            status=ModelStatus.ACTIVE,
            approved_by=principal.principal_id,
            approved_at=now,
        )
        self._records[record.key] = promoted
        self._audit_event(
            principal.principal_id, f"MODEL_PROMOTE:{model_id}:{version}", "active"
        )
        return promoted

    def rollback(
        self,
        principal: Principal,
        *,
        model_id: str,
        version: str,
        reason: str,
        now: datetime,
    ) -> ModelRecord:
        """ACTIVE -> SHADOW (explicit rollback; audited with a reason)."""
        if not reason.strip():
            raise ModelRegistryError("rollback requires an explicit reason")
        require(principal, AuthAction.PROMOTE_STRATEGY_POLICY, self._audit)
        record = self._require(model_id, version)
        rolled = replace(record, status=ModelStatus.SHADOW, approved_by=None, approved_at=None)
        self._records[record.key] = rolled
        self._audit_event(
            principal.principal_id, f"MODEL_ROLLBACK:{model_id}:{version}", reason
        )
        return rolled

    def set_calibration(
        self, model_id: str, version: str, status: CalibrationStatus, *, evidence_ref: str
    ) -> ModelRecord:
        """Record a calibration outcome (evidence reference mandatory)."""
        if not evidence_ref:
            raise ModelRegistryError("calibration updates require an evidence_ref")
        record = self._require(model_id, version)
        updated = replace(record, calibration_status=status)
        self._records[record.key] = updated
        return updated

    def set_drift(
        self, model_id: str, version: str, status: DriftStatus, *, evidence_ref: str
    ) -> ModelRecord:
        """Record a drift outcome; a drifting ACTIVE model stays visible so
        governance can roll it back — nothing changes silently."""
        if not evidence_ref:
            raise ModelRegistryError("drift updates require an evidence_ref")
        record = self._require(model_id, version)
        updated = replace(record, drift_status=status)
        self._records[record.key] = updated
        return updated

    # -- queries ----------------------------------------------------------------

    def get(self, model_id: str, version: str) -> ModelRecord:
        return self._require(model_id, version)

    def resolve_active(self, family: str) -> tuple[ModelRecord, ...]:
        """Only approved ACTIVE models — never a shadow/unapproved fallback."""
        return tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if record.family == family
                    and record.status is ModelStatus.ACTIVE
                    and record.approved_by is not None
                ),
                key=lambda record: (record.model_id, record.version),
            )
        )

    def shadow_models(self, family: str) -> tuple[ModelRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if record.family == family and record.status is ModelStatus.SHADOW
                ),
                key=lambda record: (record.model_id, record.version),
            )
        )

    def _require(self, model_id: str, version: str) -> ModelRecord:
        record = self._records.get((model_id, version))
        if record is None:
            raise ModelRegistryError(f"unknown model {model_id!r} version {version!r}")
        return record

    def _audit_event(self, actor: str, action: str, reason: str) -> None:
        self._audit.record(
            AuthorizationDecision(
                allowed=True, principal_id=actor, action=action, reason=reason
            )
        )


__all__ = [
    "DriftStatus",
    "ModelRecord",
    "ModelRegistry",
    "ModelRegistryError",
    "ModelStatus",
    "PromotionDeniedError",
]
