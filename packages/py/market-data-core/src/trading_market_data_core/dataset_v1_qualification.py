"""Dataset V1 real-source qualification + authenticated evidence.

The four external Dataset V1 capabilities (`pit_sp500_membership`, `asof_gics`,
`delisted_security_history`, `corporate_actions`) can only leave
``BLOCKED_EXTERNAL`` on the strength of a REAL historical source. This module is
the harness that decides that — and the ONLY path by which a capability may be
promoted.

What it provides:

* a **stable evidence schema** shared by all four capabilities
  (``QualificationEvidence``), canonicalized and **authenticated**: the checksum
  is recomputed over the canonical body on write AND on load, so a tampered or
  stale artifact is rejected rather than trusted;
* explicit, per-capability **acceptance criteria** (historical change required,
  effective dates, knowledge time, stable identity, no snapshot substitution);
* deterministic **data-quality** and **identity-resolution** reporting, so gaps
  are visible rather than hidden behind a boolean;
* one authoritative ``promote_capability`` gate.

Hard rules (tested):

* a ``FIXTURE`` source can never be promoted, whatever its metrics;
* a ``SNAPSHOT_ONLY`` source can never satisfy a HISTORICAL capability;
* zero historical changes, missing temporal semantics, ticker-only identity,
  validation errors, unresolved identities, a missing source checksum or a
  failing evidence checksum all block promotion;
* nothing here fabricates evidence: with no real source the capability simply
  stays blocked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from trading_market_data_core.dataset_v1 import (
    DATASET_V1_REQUIRED_CAPABILITIES,
    CapabilityState,
)

QUALIFICATION_SCHEMA_VERSION = "dataset-v1-qualification-evidence-1.0.0"


class QualificationError(Exception):
    """Fail-closed qualification failure (bad evidence, tampered artifact)."""


class SourceKind(StrEnum):
    """What kind of source produced the data. Only REAL can ever be promoted."""

    REAL = "REAL"
    #: A test/synthetic source. Proves machinery only — never evidence.
    FIXTURE = "FIXTURE"
    #: A real source that only exposes today's state (no history).
    SNAPSHOT_ONLY = "SNAPSHOT_ONLY"


class QualificationResult(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


#: The capabilities this harness governs.
QUALIFIABLE_CAPABILITIES: tuple[str, ...] = (
    "pit_sp500_membership",
    "asof_gics",
    "delisted_security_history",
    "corporate_actions",
)


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """Identity + provenance of the source under qualification.

    ``raw_checksum`` is the digest of the raw source artifact when one exists. A
    source with no downloadable artifact records the strongest reproducible
    identity it actually has — it must never pretend to be stronger.
    """

    source_id: str
    name: str
    version: str
    kind: SourceKind
    #: How authoritative the source is (OWNER_PRIMARY / OFFICIAL_PUBLIC /
    #: PUBLIC_SECONDARY / FIXTURE / SNAPSHOT_ONLY). A secondary dataset must
    #: never be recorded as official.
    authority: str = "PUBLIC_SECONDARY"
    raw_checksum: str | None = None
    raw_artifact: str | None = None
    access_note: str = ""

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.name.strip() or not self.version.strip():
            raise QualificationError("source descriptor requires source_id, name and version")
        if self.raw_checksum is not None and not self.raw_checksum.startswith("sha256:"):
            raise QualificationError("raw_checksum must be a sha256: digest")

    @property
    def has_raw_artifact_checksum(self) -> bool:
        return bool(self.raw_checksum)


@dataclass(frozen=True, slots=True)
class CoverageWindow:
    start: date | None
    end: date | None

    @property
    def is_bounded(self) -> bool:
        return self.start is not None and self.end is not None


@dataclass(frozen=True, slots=True)
class TemporalSemantics:
    """Whether the source supports revision-safe point-in-time use."""

    provides_effective_dates: bool
    provides_known_at: bool
    revision_safe: bool

    @property
    def sufficient_for_pit(self) -> bool:
        return self.provides_effective_dates and self.provides_known_at


@dataclass(frozen=True, slots=True)
class IdentitySemantics:
    """How the source identifies securities. Ticker-only is never sufficient."""

    provides_stable_identifier: bool
    identifier_kinds: tuple[str, ...] = ()
    ticker_only: bool = False


@dataclass(frozen=True, slots=True)
class IdentityResolutionReport:
    """Explicit resolution outcome — ambiguity is never silently guessed."""

    resolved: int = 0
    ambiguous: int = 0
    unresolved: int = 0
    conflicting: int = 0

    @property
    def clean(self) -> bool:
        return self.ambiguous == 0 and self.unresolved == 0 and self.conflicting == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "resolved": self.resolved, "ambiguous": self.ambiguous,
            "unresolved": self.unresolved, "conflicting": self.conflicting,
        }


@dataclass(frozen=True, slots=True)
class DataQualityMetrics:
    """Per-source metrics. No single boolean hides a data-quality gap."""

    total_rows: int = 0
    accepted: int = 0
    duplicates_collapsed: int = 0
    conflicts_rejected: int = 0
    missing_provenance: int = 0
    missing_dates: int = 0
    historical_changes: int = 0
    revisions: int = 0
    retractions: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "total_rows": self.total_rows, "accepted": self.accepted,
            "duplicates_collapsed": self.duplicates_collapsed,
            "conflicts_rejected": self.conflicts_rejected,
            "missing_provenance": self.missing_provenance,
            "missing_dates": self.missing_dates,
            "historical_changes": self.historical_changes,
            "revisions": self.revisions, "retractions": self.retractions,
        }


@dataclass(frozen=True, slots=True)
class SubCapabilityResult:
    """One named sub-capability outcome, so PARTIAL is precise, not vague.

    Example: corporate_actions may prove ``split`` and ``dividend`` while
    ``merger`` remains unproven — the evidence lists each explicitly.
    """

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CapabilityCriteria:
    """The acceptance criteria a capability demands of a real source."""

    capability: str
    requires_historical_change: bool = True
    requires_effective_dates: bool = True
    requires_known_at: bool = True
    requires_stable_identity: bool = True
    #: Minimum historical changes/events the source must actually demonstrate.
    minimum_historical_changes: int = 1
    #: Extra, capability-specific proofs the run must assert (free-form ids).
    required_proofs: tuple[str, ...] = ()


CAPABILITY_CRITERIA: Mapping[str, CapabilityCriteria] = {
    "pit_sp500_membership": CapabilityCriteria(
        capability="pit_sp500_membership",
        required_proofs=("historical_addition", "historical_removal"),
    ),
    "asof_gics": CapabilityCriteria(
        capability="asof_gics",
        required_proofs=("historical_classification_change",),
    ),
    "delisted_security_history": CapabilityCriteria(
        capability="delisted_security_history",
        required_proofs=("inactive_security_resolvable",),
    ),
    "corporate_actions": CapabilityCriteria(
        capability="corporate_actions",
        required_proofs=("event_level_action",),
    ),
}


def _canonical(body: Mapping[str, Any]) -> bytes:
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    """The stable, authenticated evidence artifact shared by all capabilities."""

    schema_version: str
    capability: str
    source: SourceDescriptor
    coverage: CoverageWindow
    temporal: TemporalSemantics
    identity: IdentitySemantics
    metrics: DataQualityMetrics
    resolution: IdentityResolutionReport
    validation_errors: tuple[str, ...]
    proofs: tuple[str, ...]
    sub_capabilities: tuple[SubCapabilityResult, ...]
    result: QualificationResult
    promotion_eligible: bool
    blocked_reasons: tuple[str, ...]
    observed_at: datetime
    normalized_checksum: str = ""

    def body(self) -> dict[str, Any]:
        """The canonical body the checksum authenticates (checksum excluded)."""
        return {
            # Self-declared type so a verifier never has to guess what this
            # file is. Auxiliary artifacts declare their own types and are
            # routed away from the promotion path.
            "artifact_type": "qualification_evidence",
            "schema_version": self.schema_version,
            "capability": self.capability,
            "source": {
                "source_id": self.source.source_id, "name": self.source.name,
                "version": self.source.version, "kind": self.source.kind.value,
                "authority": self.source.authority,
                "raw_checksum": self.source.raw_checksum,
                "raw_artifact": self.source.raw_artifact,
                "access_note": self.source.access_note,
            },
            "coverage": {
                "start": self.coverage.start.isoformat() if self.coverage.start else None,
                "end": self.coverage.end.isoformat() if self.coverage.end else None,
            },
            "temporal": {
                "provides_effective_dates": self.temporal.provides_effective_dates,
                "provides_known_at": self.temporal.provides_known_at,
                "revision_safe": self.temporal.revision_safe,
            },
            "identity": {
                "provides_stable_identifier": self.identity.provides_stable_identifier,
                "identifier_kinds": list(self.identity.identifier_kinds),
                "ticker_only": self.identity.ticker_only,
            },
            "metrics": self.metrics.to_dict(),
            "resolution": self.resolution.to_dict(),
            "validation_errors": list(self.validation_errors),
            "proofs": sorted(self.proofs),
            "sub_capabilities": [
                sub.to_dict() for sub in sorted(self.sub_capabilities, key=lambda s: s.name)
            ],
            "result": self.result.value,
            "promotion_eligible": self.promotion_eligible,
            "blocked_reasons": list(self.blocked_reasons),
            "observed_at": self.observed_at.isoformat(),
        }

    def compute_checksum(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical(self.body())).hexdigest()

    def sealed(self) -> QualificationEvidence:
        """Return a copy carrying its authenticated checksum."""
        from dataclasses import replace

        return replace(self, normalized_checksum=self.compute_checksum())

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["normalized_checksum"] = self.normalized_checksum or self.compute_checksum()
        return payload

    def write(self, path: Path) -> str:
        """Write the sealed artifact and return its checksum."""
        sealed = self if self.normalized_checksum else self.sealed()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sealed.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return sealed.normalized_checksum


def load_and_verify_evidence(path: Path) -> QualificationEvidence:
    """Load an evidence artifact, RECOMPUTING and verifying its checksum."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QualificationError(f"unreadable evidence {path}: {error}") from error
    if not isinstance(payload, dict):
        raise QualificationError("evidence must be a JSON object")
    declared = payload.get("normalized_checksum")
    if not isinstance(declared, str) or not declared.startswith("sha256:"):
        raise QualificationError("evidence is missing a sha256 normalized_checksum")
    body = {key: value for key, value in payload.items() if key != "normalized_checksum"}
    recomputed = "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()
    if recomputed != declared:
        raise QualificationError(
            f"evidence checksum mismatch (tampered or stale): declared {declared},"
            f" recomputed {recomputed}"
        )
    return _from_body(body, declared)


def _from_body(body: Mapping[str, Any], checksum: str) -> QualificationEvidence:
    source = body["source"]
    coverage = body["coverage"]
    temporal = body["temporal"]
    identity = body["identity"]
    metrics = body["metrics"]
    resolution = body["resolution"]
    return QualificationEvidence(
        schema_version=str(body["schema_version"]),
        capability=str(body["capability"]),
        source=SourceDescriptor(
            source_id=str(source["source_id"]), name=str(source["name"]),
            version=str(source["version"]), kind=SourceKind(str(source["kind"])),
            authority=str(source.get("authority", "PUBLIC_SECONDARY")),
            raw_checksum=source.get("raw_checksum"), raw_artifact=source.get("raw_artifact"),
            access_note=str(source.get("access_note", "")),
        ),
        coverage=CoverageWindow(
            start=date.fromisoformat(coverage["start"]) if coverage.get("start") else None,
            end=date.fromisoformat(coverage["end"]) if coverage.get("end") else None,
        ),
        temporal=TemporalSemantics(
            provides_effective_dates=bool(temporal["provides_effective_dates"]),
            provides_known_at=bool(temporal["provides_known_at"]),
            revision_safe=bool(temporal["revision_safe"]),
        ),
        identity=IdentitySemantics(
            provides_stable_identifier=bool(identity["provides_stable_identifier"]),
            identifier_kinds=tuple(identity.get("identifier_kinds", ())),
            ticker_only=bool(identity.get("ticker_only", False)),
        ),
        metrics=DataQualityMetrics(**{key: int(value) for key, value in metrics.items()}),
        resolution=IdentityResolutionReport(
            **{key: int(value) for key, value in resolution.items()}
        ),
        validation_errors=tuple(body.get("validation_errors", ())),
        proofs=tuple(body.get("proofs", ())),
        sub_capabilities=tuple(
            SubCapabilityResult(
                name=str(sub["name"]), passed=bool(sub["passed"]),
                detail=str(sub.get("detail", "")),
            )
            for sub in body.get("sub_capabilities", ())
        ),
        result=QualificationResult(str(body["result"])),
        promotion_eligible=bool(body["promotion_eligible"]),
        blocked_reasons=tuple(body.get("blocked_reasons", ())),
        observed_at=datetime.fromisoformat(str(body["observed_at"])),
        normalized_checksum=checksum,
    )


# -- the single authoritative qualification / promotion path ------------------


def qualify_source(
    capability: str,
    *,
    source: SourceDescriptor,
    coverage: CoverageWindow,
    temporal: TemporalSemantics,
    identity: IdentitySemantics,
    metrics: DataQualityMetrics,
    resolution: IdentityResolutionReport,
    validation_errors: Sequence[str] = (),
    proofs: Sequence[str] = (),
    sub_capabilities: Sequence[SubCapabilityResult] = (),
    observed_at: datetime,
) -> QualificationEvidence:
    """Evaluate one source against a capability's criteria. Fail-closed.

    Returns a SEALED evidence artifact whose ``promotion_eligible`` flag is the
    only thing ``promote_capability`` will honour.
    """
    if capability not in CAPABILITY_CRITERIA:
        raise QualificationError(f"unknown qualifiable capability {capability!r}")
    if observed_at.tzinfo is None:
        raise QualificationError("observed_at must be timezone-aware")
    criteria = CAPABILITY_CRITERIA[capability]
    blocked: list[str] = []

    if source.kind is SourceKind.FIXTURE:
        blocked.append("source kind is FIXTURE; a fixture is never empirical evidence")
    if source.kind is SourceKind.SNAPSHOT_ONLY:
        blocked.append(
            "source is SNAPSHOT_ONLY; a current snapshot cannot satisfy a historical"
            " capability"
        )
    if criteria.requires_effective_dates and not temporal.provides_effective_dates:
        blocked.append("source does not provide historical effective dates")
    if criteria.requires_known_at and not temporal.provides_known_at:
        blocked.append("source does not provide knowledge/observation time")
    if not temporal.revision_safe:
        blocked.append("source is not revision-safe (restatements could leak backwards)")
    if criteria.requires_stable_identity and (
        identity.ticker_only or not identity.provides_stable_identifier
    ):
        blocked.append("source has no stable identity mapping (ticker-only is insufficient)")
    if (
        criteria.requires_historical_change
        and metrics.historical_changes < criteria.minimum_historical_changes
    ):
        blocked.append(
            f"only {metrics.historical_changes} historical change(s);"
            f" {criteria.minimum_historical_changes} required"
        )
    if not coverage.is_bounded:
        blocked.append("source coverage window is unbounded/unknown")
    if validation_errors:
        blocked.append(f"{len(validation_errors)} validation error(s)")
    if not resolution.clean:
        blocked.append(
            f"identity resolution is not clean (ambiguous={resolution.ambiguous},"
            f" unresolved={resolution.unresolved}, conflicting={resolution.conflicting})"
        )
    if not source.has_raw_artifact_checksum:
        blocked.append("no raw source checksum bound to the evidence")
    missing_proofs = sorted(set(criteria.required_proofs) - set(proofs))
    if missing_proofs:
        blocked.append(f"missing required proofs: {missing_proofs}")
    failed_subs = sorted(sub.name for sub in sub_capabilities if not sub.passed)
    if failed_subs:
        blocked.append(f"sub-capabilities not proven: {failed_subs}")

    if not blocked:
        result = QualificationResult.PASS
    elif source.kind is SourceKind.REAL and metrics.accepted > 0:
        result = QualificationResult.PARTIAL
    else:
        result = QualificationResult.FAIL

    evidence = QualificationEvidence(
        schema_version=QUALIFICATION_SCHEMA_VERSION, capability=capability, source=source,
        coverage=coverage, temporal=temporal, identity=identity, metrics=metrics,
        resolution=resolution, validation_errors=tuple(validation_errors),
        proofs=tuple(proofs), sub_capabilities=tuple(sub_capabilities),
        result=result, promotion_eligible=not blocked,
        blocked_reasons=tuple(blocked), observed_at=observed_at,
    )
    return evidence.sealed()


def promote_capability(evidence: QualificationEvidence) -> CapabilityState:
    """The ONLY path to EMPIRICALLY_VERIFIED. Re-verifies the artifact itself.

    Promotion requires a REAL source, a PASS result, an eligible verdict AND an
    evidence checksum that still authenticates the body — so a hand-edited
    artifact cannot promote anything.
    """
    if evidence.normalized_checksum != evidence.compute_checksum():
        raise QualificationError(
            "evidence checksum does not authenticate its body; refusing to promote"
        )
    if evidence.source.kind is not SourceKind.REAL:
        return CapabilityState.BLOCKED_EXTERNAL
    if evidence.result is not QualificationResult.PASS or not evidence.promotion_eligible:
        return CapabilityState.BLOCKED_EXTERNAL
    return CapabilityState.EMPIRICALLY_VERIFIED


def capability_states_from_evidence(
    evidences: Sequence[QualificationEvidence],
    *,
    base_states: Mapping[str, CapabilityState] | None = None,
) -> dict[str, CapabilityState]:
    """Capability states for the builder. Anything unproven stays blocked.

    ``base_states`` carries states already established through another
    authenticated path (e.g. the six IBKR historical capabilities proven by
    ``POI-IBKR-HIST``), so this harness reports the truth for the whole required
    set instead of under-claiming cells it does not govern. Only the four
    qualifiable capabilities can be changed here.
    """
    states: dict[str, CapabilityState] = {
        capability: CapabilityState.BLOCKED_EXTERNAL
        for capability in DATASET_V1_REQUIRED_CAPABILITIES
    }
    for name, state in (base_states or {}).items():
        if name in states and name not in QUALIFIABLE_CAPABILITIES:
            states[name] = state
    for evidence in evidences:
        if evidence.capability in states:
            states[evidence.capability] = promote_capability(evidence)
    return states


@dataclass(frozen=True, slots=True)
class QualificationRun:
    """The outcome of one owner-run qualification across the four capabilities."""

    evidences: tuple[QualificationEvidence, ...] = ()
    discovery_notes: tuple[str, ...] = field(default_factory=tuple)
    #: States already proven through another authenticated path (e.g. the six
    #: IBKR historical capabilities from POI-IBKR-HIST).
    base_states: Mapping[str, CapabilityState] = field(default_factory=dict)

    def states(self) -> dict[str, CapabilityState]:
        return capability_states_from_evidence(self.evidences, base_states=self.base_states)

    def full_dataset_contract_ready(self) -> bool:
        states = self.states()
        return all(
            states.get(name) is CapabilityState.EMPIRICALLY_VERIFIED
            for name in DATASET_V1_REQUIRED_CAPABILITIES
        )

    def to_dict(self) -> dict[str, Any]:
        states = self.states()
        return {
            "schema_version": QUALIFICATION_SCHEMA_VERSION,
            "capabilities": {
                name: {
                    "state": state.value,
                    "evidence": next(
                        (
                            e.normalized_checksum
                            for e in self.evidences
                            if e.capability == name
                        ),
                        None,
                    ),
                    "blocked_reasons": next(
                        (list(e.blocked_reasons) for e in self.evidences if e.capability == name),
                        []
                        if state is CapabilityState.EMPIRICALLY_VERIFIED
                        else ["no qualification evidence supplied"],
                    ),
                }
                for name, state in sorted(states.items())
            },
            "full_dataset_contract_ready": self.full_dataset_contract_ready(),
            "discovery_notes": list(self.discovery_notes),
        }


__all__ = [
    "CAPABILITY_CRITERIA",
    "QUALIFIABLE_CAPABILITIES",
    "QUALIFICATION_SCHEMA_VERSION",
    "CapabilityCriteria",
    "CoverageWindow",
    "DataQualityMetrics",
    "IdentityResolutionReport",
    "IdentitySemantics",
    "QualificationError",
    "QualificationEvidence",
    "QualificationResult",
    "QualificationRun",
    "SourceDescriptor",
    "SourceKind",
    "SubCapabilityResult",
    "TemporalSemantics",
    "capability_states_from_evidence",
    "load_and_verify_evidence",
    "promote_capability",
    "qualify_source",
]
