"""Run isolation and the authenticated run manifest.

The real owner run exposed the defect this module exists to remove: the SEC
corporate-action stage FAILED, yet a ``corporate_actions.json`` left behind by
the PREVIOUS run was still sitting in the evidence directory and was picked up
by directory-wide discovery. A failed stage silently inherited stale empirical
evidence.

The invariant enforced here:

    A capability report for run X may only consume evidence that run X's own
    manifest binds. Nothing is discovered by globbing a directory.

Mechanics:

* every run gets a ``run_id`` and its own **staging** directory;
* stages write into staging;
* an artifact is **published** only when its producing stage completed normally
  and the artifact authenticates;
* the run manifest records every stage outcome and every artifact actually
  produced in that run, with checksums, and is itself authenticated.

Historical artifacts may remain on disk for audit. They cannot influence
readiness, because readiness is computed from the manifest, not the directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # noqa: S404 - used only to read the local git SHA
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from trading_market_data_core.dataset_v1_artifacts import (
    ARTIFACT_TYPE_KEY,
    ArtifactError,
    ArtifactType,
    compute_checksum,
    load_artifact,
)

RUN_MANIFEST_SCHEMA_VERSION = "dataset-v1-run-manifest-1.0.0"


class StageOutcome(StrEnum):
    """What a source stage actually achieved in THIS run."""

    SUCCEEDED = "SUCCEEDED"
    #: Completed normally but the evidence is genuinely incomplete.
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    #: No legally/technically reproducible path on this host.
    NOT_AVAILABLE = "NOT_AVAILABLE"
    #: The stage's gate was not satisfied (e.g. no SEC User-Agent).
    NOT_RUN = "NOT_RUN"

    @property
    def may_publish(self) -> bool:
        """Only a stage that completed normally may publish evidence."""
        return self in (StageOutcome.SUCCEEDED, StageOutcome.PARTIAL)


#: How the report describes a capability whose CURRENT run produced nothing.
class CurrentRunStatus(StrEnum):
    """Stale-versus-current, made unmistakable in the report."""

    CURRENT_RUN_EVIDENCE = "CURRENT_RUN_EVIDENCE"
    CURRENT_RUN_FAILED = "CURRENT_RUN_FAILED"
    NO_CURRENT_EVIDENCE = "NO_CURRENT_EVIDENCE"


def new_run_id(now: datetime | None = None) -> str:
    """A sortable, deterministic-per-instant run id."""
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{os.getpid()}"


def git_sha(repo_root: Path) -> str:
    """The local commit, recorded so a run can be tied to the code that made it."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


@dataclass(frozen=True, slots=True)
class RunArtifact:
    """One artifact this run actually produced."""

    name: str
    artifact_type: ArtifactType
    path: str
    #: Digest of the FILE BYTES. Never compared against a canonical body digest.
    file_checksum: str
    #: Digest of the raw upstream source material, when the artifact declares one.
    source_artifact_checksum: str | None = None
    stage: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "artifact_type": self.artifact_type.value,
            "path": self.path,
            "file_checksum": self.file_checksum,
            "source_artifact_checksum": self.source_artifact_checksum,
            "stage": self.stage,
        }


@dataclass(frozen=True, slots=True)
class StageRecord:
    """One stage of the run and what it produced."""

    name: str
    outcome: StageOutcome
    detail: str = ""
    artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name, "outcome": self.outcome.value, "detail": self.detail,
            "artifacts": list(self.artifacts),
        }


def file_checksum(path: Path) -> str:
    """Digest of the FILE BYTES.

    Deliberately named apart from ``normalized_checksum`` (the canonical
    evidence-body digest) so the two can never be compared to one another.
    """
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The authenticated record of one owner run."""

    run_id: str
    started_at: datetime
    completed_at: datetime
    git_sha: str
    owner_host_mode: str
    versions: Mapping[str, str]
    stages: tuple[StageRecord, ...] = ()
    artifacts: tuple[RunArtifact, ...] = ()
    capability_results: Mapping[str, str] = field(default_factory=dict)
    snapshot_result: Mapping[str, str] = field(default_factory=dict)
    failure_reasons: tuple[str, ...] = ()
    schema_version: str = RUN_MANIFEST_SCHEMA_VERSION
    checksum: str = ""

    def body(self) -> dict[str, Any]:
        return {
            ARTIFACT_TYPE_KEY: ArtifactType.RUN_MANIFEST.value,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "git_sha": self.git_sha,
            "owner_host_mode": self.owner_host_mode,
            "versions": dict(sorted(self.versions.items())),
            "stages": [stage.to_dict() for stage in sorted(self.stages, key=lambda s: s.name)],
            "artifacts": [
                artifact.to_dict()
                for artifact in sorted(self.artifacts, key=lambda a: (a.name, a.path))
            ],
            "capability_results": dict(sorted(self.capability_results.items())),
            "snapshot_result": dict(sorted(self.snapshot_result.items())),
            "failure_reasons": sorted(self.failure_reasons),
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

    def artifacts_of_type(self, artifact_type: ArtifactType) -> tuple[RunArtifact, ...]:
        return tuple(a for a in self.artifacts if a.artifact_type is artifact_type)

    def stage(self, name: str) -> StageRecord | None:
        return next((s for s in self.stages if s.name == name), None)


def load_and_verify_run_manifest(path: Path) -> RunManifest:
    """Load a run manifest, RECOMPUTING its checksum. Tampering fails closed."""
    artifact_type, payload = load_artifact(path)
    if artifact_type is not ArtifactType.RUN_MANIFEST:
        raise ArtifactError(f"{path} is {artifact_type.value}, not a run manifest")
    declared = payload.get("checksum")
    if not isinstance(declared, str) or not declared.startswith("sha256:"):
        raise ArtifactError(f"{path}: run manifest is missing a sha256 checksum")
    body = {key: value for key, value in payload.items() if key != "checksum"}
    recomputed = compute_checksum(body)
    if recomputed != declared:
        raise ArtifactError(
            f"{path}: run manifest checksum mismatch (tampered or stale):"
            f" declared {declared}, recomputed {recomputed}"
        )
    return RunManifest(
        run_id=str(payload["run_id"]),
        started_at=datetime.fromisoformat(str(payload["started_at"])),
        completed_at=datetime.fromisoformat(str(payload["completed_at"])),
        git_sha=str(payload.get("git_sha", "unknown")),
        owner_host_mode=str(payload.get("owner_host_mode", "")),
        versions={str(k): str(v) for k, v in payload.get("versions", {}).items()},
        stages=tuple(
            StageRecord(
                name=str(stage["name"]), outcome=StageOutcome(str(stage["outcome"])),
                detail=str(stage.get("detail", "")),
                artifacts=tuple(str(a) for a in stage.get("artifacts", ())),
            )
            for stage in payload.get("stages", ())
        ),
        artifacts=tuple(
            RunArtifact(
                name=str(a["name"]), artifact_type=ArtifactType(str(a["artifact_type"])),
                path=str(a["path"]), file_checksum=str(a["file_checksum"]),
                source_artifact_checksum=(
                    str(a["source_artifact_checksum"])
                    if a.get("source_artifact_checksum")
                    else None
                ),
                stage=str(a.get("stage", "")),
            )
            for a in payload.get("artifacts", ())
        ),
        capability_results={
            str(k): str(v) for k, v in payload.get("capability_results", {}).items()
        },
        snapshot_result={str(k): str(v) for k, v in payload.get("snapshot_result", {}).items()},
        failure_reasons=tuple(str(r) for r in payload.get("failure_reasons", ())),
        schema_version=str(payload.get("schema_version", RUN_MANIFEST_SCHEMA_VERSION)),
        checksum=declared,
    )


def verify_manifest_artifacts(
    manifest: RunManifest, *, root: Path
) -> tuple[str, ...]:
    """Findings for artifacts the manifest binds. Empty tuple means clean.

    A manifest entry whose file is missing or whose bytes changed since the run
    is a finding: a stale or substituted artifact must never pass as current.
    """
    findings: list[str] = []
    for artifact in sorted(manifest.artifacts, key=lambda a: a.path):
        path = root / artifact.path if not Path(artifact.path).is_absolute() else Path(
            artifact.path
        )
        if not path.is_file():
            findings.append(f"{artifact.name}: bound artifact is missing at {path}")
            continue
        actual = file_checksum(path)
        if actual != artifact.file_checksum:
            findings.append(
                f"{artifact.name}: file bytes changed since the run"
                f" (manifest {artifact.file_checksum}, actual {actual})"
            )
    return tuple(findings)


class RunStager:
    """Stage artifacts, publish only what a successful stage produced.

    Publication is the only way an artifact becomes visible to the capability
    report, so a failed stage physically cannot leave publishable evidence.
    """

    def __init__(self, *, run_id: str, staging_root: Path, published_root: Path) -> None:
        self.run_id = run_id
        self.staging_dir = staging_root / run_id
        self.published_root = published_root
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._published: list[RunArtifact] = []
        self._stages: list[StageRecord] = []

    def stage_path(self, filename: str) -> Path:
        """Where a stage writes an artifact before it is accepted."""
        return self.staging_dir / filename

    def record_stage(
        self,
        name: str,
        outcome: StageOutcome,
        *,
        detail: str = "",
        artifacts: Sequence[str] = (),
    ) -> StageRecord:
        record = StageRecord(
            name=name, outcome=outcome, detail=detail, artifacts=tuple(artifacts)
        )
        self._stages.append(record)
        return record

    def publish(
        self,
        *,
        stage: str,
        outcome: StageOutcome,
        filenames: Sequence[str],
        source_artifact_checksum: str | None = None,
    ) -> tuple[RunArtifact, ...]:
        """Atomically publish a stage's artifacts — only if it may publish.

        A FAILED / NOT_RUN / NOT_AVAILABLE stage publishes NOTHING, so a
        previous run's artifact can never stand in for this run's output.
        """
        if not outcome.may_publish:
            return ()
        published: list[RunArtifact] = []
        self.published_root.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            staged = self.stage_path(filename)
            if not staged.is_file():
                continue
            artifact_type, _ = load_artifact(staged)
            target = self.published_root / filename
            # atomic within the same filesystem; replaces any older artifact
            shutil.move(str(staged), str(target))
            published.append(
                RunArtifact(
                    name=target.stem,
                    artifact_type=artifact_type,
                    path=str(target),
                    file_checksum=file_checksum(target),
                    source_artifact_checksum=source_artifact_checksum,
                    stage=stage,
                )
            )
        self._published.extend(published)
        return tuple(published)

    @property
    def published(self) -> tuple[RunArtifact, ...]:
        return tuple(self._published)

    @property
    def stages(self) -> tuple[StageRecord, ...]:
        return tuple(self._stages)

    def discard_staging(self) -> None:
        """Remove anything a failed stage left behind."""
        if self.staging_dir.is_dir():
            shutil.rmtree(self.staging_dir, ignore_errors=True)


def current_run_status(
    manifest: RunManifest, capability: str, *, stage_by_capability: Mapping[str, str]
) -> CurrentRunStatus:
    """Stale-versus-current for one capability, from the manifest alone."""
    stage_name = stage_by_capability.get(capability)
    if stage_name is None:
        return CurrentRunStatus.NO_CURRENT_EVIDENCE
    stage = manifest.stage(stage_name)
    if stage is None:
        return CurrentRunStatus.NO_CURRENT_EVIDENCE
    if stage.outcome is StageOutcome.FAILED:
        return CurrentRunStatus.CURRENT_RUN_FAILED
    if not stage.artifacts:
        return CurrentRunStatus.NO_CURRENT_EVIDENCE
    return CurrentRunStatus.CURRENT_RUN_EVIDENCE


__all__ = [
    "RUN_MANIFEST_SCHEMA_VERSION",
    "CurrentRunStatus",
    "RunArtifact",
    "RunManifest",
    "RunStager",
    "StageOutcome",
    "StageRecord",
    "current_run_status",
    "file_checksum",
    "git_sha",
    "load_and_verify_run_manifest",
    "new_run_id",
    "verify_manifest_artifacts",
]
