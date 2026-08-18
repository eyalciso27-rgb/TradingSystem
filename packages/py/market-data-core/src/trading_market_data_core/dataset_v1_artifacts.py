"""Dataset V1 evidence-artifact typing and dispatch.

The owner-host run of PR #84 exposed the defect this module exists to remove:
``identity-crosswalk.json`` is an auxiliary report, but the verification path
globbed ``*.json`` and parsed everything with the qualification-evidence schema,
producing ``EVIDENCE_INVALID: evidence is missing a sha256 normalized_checksum``
for a perfectly valid artifact.

The fix is classification, NOT relaxation. Every artifact type keeps its own
integrity rules and its own verifier; a tampered auxiliary artifact still fails
its own validation. What changes is that an artifact is never parsed with the
wrong schema, and only ``qualification_evidence`` may reach capability
promotion.

Classification is explicit (``artifact_type`` in the payload) with a narrow,
conservative structural fallback for artifacts written before the field
existed — a fallback that can only ever recognise, never invent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

ARTIFACTS_SCHEMA_VERSION = "dataset-v1-artifacts-1.0.0"
ARTIFACT_TYPE_KEY = "artifact_type"


class ArtifactError(Exception):
    """Fail-closed artifact failure (unknown type, bad shape, tampering)."""


class ArtifactType(StrEnum):
    """What a JSON file in the evidence directory actually is."""

    #: Capability evidence. The ONLY type that may reach capability promotion.
    QUALIFICATION_EVIDENCE = "qualification_evidence"
    #: Identity resolution across security_id / CIK / conId.
    IDENTITY_CROSSWALK = "identity_crosswalk"
    #: The outcome of a snapshot build attempt.
    SNAPSHOT_REPORT = "snapshot_report"
    #: Diagnostics that inform review but never qualify anything.
    AUXILIARY_DATA_QUALITY_REPORT = "auxiliary_data_quality_report"
    #: A declaration of sources/documents supplied by the owner.
    SOURCE_MANIFEST = "source_manifest"
    #: The authenticated record of ONE owner run: stages, outcomes, artifacts.
    #: Readiness is computed from this, never from a directory glob.
    RUN_MANIFEST = "run_manifest"


#: Only this type may be handed to the capability promotion path.
PROMOTABLE_ARTIFACT_TYPES: frozenset[ArtifactType] = frozenset(
    {ArtifactType.QUALIFICATION_EVIDENCE}
)


def canonical_bytes(body: Mapping[str, Any]) -> bytes:
    """The canonical form every artifact checksum authenticates."""
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")


def compute_checksum(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


def classify_payload(payload: object, *, source: str = "artifact") -> ArtifactType:
    """Determine an artifact's type. Explicit first, conservative fallback second.

    The fallback exists only for artifacts written before ``artifact_type`` was
    introduced. It recognises the shapes this repository actually produced; it
    never guesses a type from a filename.
    """
    if not isinstance(payload, dict):
        raise ArtifactError(f"{source}: artifact must be a JSON object")
    declared = payload.get(ARTIFACT_TYPE_KEY)
    if isinstance(declared, str) and declared.strip():
        try:
            return ArtifactType(declared.strip())
        except ValueError as error:
            raise ArtifactError(
                f"{source}: unknown artifact_type {declared!r};"
                f" known types: {sorted(t.value for t in ArtifactType)}"
            ) from error

    # -- legacy structural fallback (recognise only; never invent) ------------
    if "capability" in payload and "normalized_checksum" in payload:
        return ArtifactType.QUALIFICATION_EVIDENCE
    if {"resolved", "ambiguous", "unresolved", "conflicting"} <= payload.keys():
        return ArtifactType.IDENTITY_CROSSWALK
    if "snapshot_readiness" in payload or "capability_blockers" in payload:
        return ArtifactType.SNAPSHOT_REPORT
    if "run_id" in payload and "stages" in payload:
        return ArtifactType.RUN_MANIFEST
    raise ArtifactError(
        f"{source}: artifact declares no artifact_type and its shape matches no"
        " known type; refusing to guess (fail closed)"
    )


def load_artifact(path: Path) -> tuple[ArtifactType, dict[str, Any]]:
    """Read and classify one artifact without applying any schema to it yet."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ArtifactError(f"unreadable artifact {path}: {error}") from error
    artifact_type = classify_payload(payload, source=str(path))
    assert isinstance(payload, dict)  # classify_payload guarantees this
    return artifact_type, payload


@dataclass(frozen=True, slots=True)
class ArtifactScan:
    """What a mixed evidence directory contains, grouped by real type."""

    by_type: Mapping[ArtifactType, tuple[Path, ...]]
    unclassified: tuple[tuple[Path, str], ...] = ()

    def paths(self, artifact_type: ArtifactType) -> tuple[Path, ...]:
        return tuple(self.by_type.get(artifact_type, ()))

    @property
    def promotable(self) -> tuple[Path, ...]:
        """Exactly the artifacts allowed into the capability promotion path."""
        found: list[Path] = []
        for artifact_type in sorted(PROMOTABLE_ARTIFACT_TYPES):
            found.extend(self.by_type.get(artifact_type, ()))
        return tuple(sorted(found))

    def to_dict(self) -> dict[str, object]:
        return {
            "by_type": {
                artifact_type.value: [str(p) for p in paths]
                for artifact_type, paths in sorted(self.by_type.items())
            },
            "unclassified": [[str(p), reason] for p, reason in self.unclassified],
        }


def scan_evidence_dir(directory: Path) -> ArtifactScan:
    """Classify every JSON in a MIXED directory. Never assume one schema.

    An unclassifiable artifact is reported, not silently skipped and not forced
    through the qualification schema.
    """
    grouped: dict[ArtifactType, list[Path]] = {}
    unclassified: list[tuple[Path, str]] = []
    if not directory.is_dir():
        return ArtifactScan(by_type={})
    for path in sorted(directory.glob("*.json")):
        try:
            artifact_type, _ = load_artifact(path)
        except ArtifactError as error:
            unclassified.append((path, str(error)))
            continue
        grouped.setdefault(artifact_type, []).append(path)
    return ArtifactScan(
        by_type={key: tuple(value) for key, value in grouped.items()},
        unclassified=tuple(unclassified),
    )


# -- identity crosswalk as a first-class authenticated artifact ----------------


CROSSWALK_SCHEMA_VERSION = "dataset-v1-identity-crosswalk-1.0.0"


@dataclass(frozen=True, slots=True)
class CrosswalkSourceBinding:
    """A source that contributed identity rows, with its content digest."""

    name: str
    version: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ArtifactError("crosswalk source binding requires a name and version")
        digest = self.checksum.removeprefix("sha256:")
        if digest == self.checksum or len(digest) != 64:
            raise ArtifactError(
                f"crosswalk source {self.name!r} requires a real sha256 checksum"
            )

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version, "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class CrosswalkArtifact:
    """The identity crosswalk, canonically serialized and authenticated.

    It is a first-class artifact with its own integrity rules — and it can never
    enter capability promotion, because its ``artifact_type`` is not promotable.
    """

    entries: int
    resolved: tuple[str, ...]
    ambiguous: tuple[str, ...]
    unresolved: tuple[str, ...]
    conflicting: tuple[str, ...]
    observed_at: datetime
    sources: tuple[CrosswalkSourceBinding, ...] = ()
    schema_version: str = CROSSWALK_SCHEMA_VERSION
    checksum: str = ""

    @property
    def clean(self) -> bool:
        return not (self.ambiguous or self.unresolved or self.conflicting)

    @property
    def resolved_security_ids(self) -> frozenset[str]:
        """Every security_id this crosswalk actually binds (right of ``=``)."""
        return frozenset(
            entry.split("=", 1)[1] for entry in self.resolved if "=" in entry
        )

    def body(self) -> dict[str, Any]:
        """Canonical body. Sorted throughout, so input order cannot change it."""
        return {
            ARTIFACT_TYPE_KEY: ArtifactType.IDENTITY_CROSSWALK.value,
            "schema_version": self.schema_version,
            "entries": self.entries,
            "resolved": sorted(self.resolved),
            "ambiguous": sorted(self.ambiguous),
            "unresolved": sorted(self.unresolved),
            "conflicting": sorted(self.conflicting),
            "clean": self.clean,
            "metrics": {
                "resolved": len(self.resolved),
                "ambiguous": len(self.ambiguous),
                "unresolved": len(self.unresolved),
                "conflicting": len(self.conflicting),
                "distinct_security_ids": len(self.resolved_security_ids),
            },
            "sources": sorted(
                (source.to_dict() for source in self.sources),
                key=lambda item: (str(item["name"]), str(item["version"])),
            ),
            "observed_at": self.observed_at.isoformat(),
        }

    def compute_checksum(self) -> str:
        return compute_checksum(self.body())

    def sealed(self) -> CrosswalkArtifact:
        from dataclasses import replace

        return replace(self, checksum=self.compute_checksum())

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["checksum"] = self.checksum or self.compute_checksum()
        return payload

    def write(self, path: Path) -> str:
        sealed = self if self.checksum else self.sealed()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sealed.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return sealed.checksum


def load_and_verify_crosswalk(path: Path) -> CrosswalkArtifact:
    """Load a crosswalk, RECOMPUTING its checksum. Tampering fails closed."""
    artifact_type, payload = load_artifact(path)
    if artifact_type is not ArtifactType.IDENTITY_CROSSWALK:
        raise ArtifactError(
            f"{path} is {artifact_type.value}, not an identity crosswalk;"
            " refusing to parse it with the wrong schema"
        )
    declared = payload.get("checksum")
    if not isinstance(declared, str) or not declared.startswith("sha256:"):
        raise ArtifactError(f"{path}: crosswalk is missing a sha256 checksum")
    body = {key: value for key, value in payload.items() if key != "checksum"}
    recomputed = compute_checksum(body)
    if recomputed != declared:
        raise ArtifactError(
            f"{path}: crosswalk checksum mismatch (tampered or stale):"
            f" declared {declared}, recomputed {recomputed}"
        )
    return CrosswalkArtifact(
        entries=int(payload.get("entries", 0)),
        resolved=tuple(payload.get("resolved", ())),
        ambiguous=tuple(payload.get("ambiguous", ())),
        unresolved=tuple(payload.get("unresolved", ())),
        conflicting=tuple(payload.get("conflicting", ())),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
        sources=tuple(
            CrosswalkSourceBinding(
                name=str(source["name"]), version=str(source["version"]),
                checksum=str(source["checksum"]),
            )
            for source in payload.get("sources", ())
        ),
        schema_version=str(payload.get("schema_version", CROSSWALK_SCHEMA_VERSION)),
        checksum=declared,
    )


def verify_identity_binding(
    security_ids: Sequence[str], crosswalk: CrosswalkArtifact
) -> tuple[str, ...]:
    """Security ids a snapshot claims that the crosswalk does not bind.

    A snapshot must not assert identities that its identity artifact never
    resolved; the caller fails the build on a non-empty result.
    """
    bound = crosswalk.resolved_security_ids
    return tuple(sorted({sid for sid in security_ids if sid not in bound}))


# -- auxiliary data-quality report --------------------------------------------


@dataclass(frozen=True, slots=True)
class AuxiliaryReport:
    """Diagnostics that inform review and qualify NOTHING.

    Candidate signals live here rather than in the event ledger: a filing that
    merely hints at an event is a lead to review, not evidence.
    """

    name: str
    observed_at: datetime
    findings: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    schema_version: str = ARTIFACTS_SCHEMA_VERSION
    checksum: str = ""

    def body(self) -> dict[str, Any]:
        return {
            ARTIFACT_TYPE_KEY: ArtifactType.AUXILIARY_DATA_QUALITY_REPORT.value,
            "schema_version": self.schema_version,
            "name": self.name,
            "findings": sorted(self.findings),
            "counts": dict(sorted(self.counts.items())),
            "notes": list(self.notes),
            "qualifies_nothing": True,
            "observed_at": self.observed_at.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["checksum"] = self.checksum or compute_checksum(self.body())
        return payload

    def write(self, path: Path) -> str:
        payload = self.to_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(payload["checksum"])


__all__ = [
    "ARTIFACTS_SCHEMA_VERSION",
    "ARTIFACT_TYPE_KEY",
    "CROSSWALK_SCHEMA_VERSION",
    "PROMOTABLE_ARTIFACT_TYPES",
    "ArtifactError",
    "ArtifactScan",
    "ArtifactType",
    "AuxiliaryReport",
    "CrosswalkArtifact",
    "CrosswalkSourceBinding",
    "canonical_bytes",
    "classify_payload",
    "compute_checksum",
    "load_and_verify_crosswalk",
    "load_artifact",
    "scan_evidence_dir",
    "verify_identity_binding",
]
