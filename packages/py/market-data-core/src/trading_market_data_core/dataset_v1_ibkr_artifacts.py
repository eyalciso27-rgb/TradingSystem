"""Typed IBKR evidence artifacts and the migration path for legacy files.

The real owner run reported ``stages: 1, artifacts bound: 0`` with:

    POI-IBKR-HIST.json: artifact declares no artifact_type and its shape
    matches no known type

``POI-IBKR-HIST.json`` and ``POI-IBKR-CORP.json`` predate the typed artifact
system. They are produced by ``broker-ibkr`` and were never given machine
readable typing, so the run manifest could not bind them and the whole IBKR
stage contributed nothing to the run ledger.

The fix is explicit typing plus an explicit migration, NOT another shape-based
guess:

* new runs emit ``artifact_type``, ``schema_version``, ``producer``,
  ``capability_scope`` and ``observed_at`` (the writers in ``broker-ibkr`` emit
  the literal type strings; ``test_ibkr_artifact_types_match`` keeps the two
  sides in agreement without creating a package dependency);
* a legacy file is recognised ONLY by ``migrate_legacy_ibkr_evidence``, which
  requires the exact historical shape and produces a typed artifact. Nothing is
  migrated implicitly, and an unmigrated legacy file can never be bound into a
  current run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_market_data_core.dataset_v1_artifacts import (
    ARTIFACT_TYPE_KEY,
    ArtifactError,
    ArtifactType,
    compute_checksum,
)

IBKR_EVIDENCE_SCHEMA_VERSION = "dataset-v1-ibkr-evidence-1.0.0"

#: The literal type strings the broker-ibkr writers emit. Kept here so the
#: registry and the producer can be asserted equal by a test instead of one
#: package importing the other.
IBKR_HISTORICAL_ARTIFACT_TYPE = "ibkr_historical_evidence"
IBKR_CORPORATE_ARTIFACT_TYPE = "ibkr_corporate_evidence"

#: The six capabilities the historical artifact is allowed to speak for.
IBKR_HISTORICAL_CAPABILITY_SCOPE: tuple[str, ...] = (
    "bid_ask_midpoint",
    "connectivity_paper_du_gate",
    "contract_identity_conid",
    "daily_trades_rth_schema",
    "head_timestamp_coverage",
    "symbol_change_discovery",
)
#: The corporate artifact qualifies NOTHING on its own: it contributes
#: adjustment behaviour and probe outcomes to the corporate-actions evidence.
IBKR_CORPORATE_CAPABILITY_SCOPE: tuple[str, ...] = ()

#: Keys that identify the exact pre-typing historical artifact. A file must
#: carry ALL of them to be eligible for migration - this is a deliberate exact
#: match, not a heuristic.
_LEGACY_HISTORICAL_KEYS = frozenset(
    {"qualification_version", "poi_id", "capabilities", "evidence_checksum", "symbols"}
)
_LEGACY_CORPORATE_KEYS = frozenset(
    {"version", "probes", "adjustment_observations", "evidence_checksum"}
)


class IbkrArtifactError(ArtifactError):
    """Fail-closed IBKR artifact failure (untyped, unmigratable, tampered)."""


@dataclass(frozen=True, slots=True)
class IbkrEvidenceArtifact:
    """A typed IBKR evidence artifact bound to one producer and scope."""

    artifact_type: ArtifactType
    schema_version: str
    producer: str
    capability_scope: tuple[str, ...]
    observed_at: datetime
    payload: Mapping[str, Any]
    #: Digest of the artifact BODY (the payload minus its own checksum field).
    normalized_checksum: str = ""
    run_id: str | None = None

    def body(self) -> dict[str, Any]:
        body = {
            key: value
            for key, value in self.payload.items()
            if key not in ("normalized_checksum", ARTIFACT_TYPE_KEY)
        }
        body[ARTIFACT_TYPE_KEY] = self.artifact_type.value
        body["schema_version"] = self.schema_version
        body["producer"] = self.producer
        body["capability_scope"] = list(self.capability_scope)
        body["observed_at"] = self.observed_at.isoformat()
        if self.run_id:
            body["run_id"] = self.run_id
        return body

    def sealed(self) -> IbkrEvidenceArtifact:
        """A copy carrying its authenticated body checksum."""
        from dataclasses import replace

        return replace(self, normalized_checksum=compute_checksum(self.body()))

    def to_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["normalized_checksum"] = self.normalized_checksum or compute_checksum(
            self.body()
        )
        return payload

    def write(self, path: Path) -> str:
        payload = self.to_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(payload["normalized_checksum"])

    def capability_states(self) -> dict[str, str]:
        """Verdicts this artifact genuinely speaks for, within its scope."""
        capabilities = self.payload.get("capabilities")
        if not isinstance(capabilities, dict):
            return {}
        return {
            name: str(verdict)
            for name, verdict in sorted(capabilities.items())
            if name in self.capability_scope
        }


def load_and_verify_ibkr_evidence(path: Path) -> IbkrEvidenceArtifact:
    """Load a TYPED IBKR artifact, recomputing its checksum.

    A legacy untyped file is refused here on purpose: migration is explicit.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise IbkrArtifactError(f"unreadable IBKR artifact {path}: {error}") from error
    if not isinstance(payload, dict):
        raise IbkrArtifactError(f"{path}: IBKR artifact must be a JSON object")

    declared_type = payload.get(ARTIFACT_TYPE_KEY)
    if declared_type is None:
        raise IbkrArtifactError(
            f"{path} is untyped legacy IBKR evidence. It is NOT trusted for"
            " current-run binding; migrate it explicitly"
            " (dataset_v1_ibkr_cli migrate) or regenerate it in an owner run."
        )
    try:
        artifact_type = ArtifactType(str(declared_type))
    except ValueError as error:
        raise IbkrArtifactError(f"{path}: unknown artifact_type {declared_type!r}") from error
    if artifact_type not in (
        ArtifactType.IBKR_HISTORICAL_EVIDENCE,
        ArtifactType.IBKR_CORPORATE_EVIDENCE,
    ):
        raise IbkrArtifactError(f"{path} is {artifact_type.value}, not IBKR evidence")

    declared = payload.get("normalized_checksum")
    if not isinstance(declared, str) or not declared.startswith("sha256:"):
        raise IbkrArtifactError(f"{path}: IBKR artifact is missing a sha256 checksum")
    body = {key: value for key, value in payload.items() if key != "normalized_checksum"}
    recomputed = compute_checksum(body)
    if recomputed != declared:
        raise IbkrArtifactError(
            f"{path}: IBKR artifact checksum mismatch (tampered or stale):"
            f" declared {declared}, recomputed {recomputed}"
        )
    return IbkrEvidenceArtifact(
        artifact_type=artifact_type,
        schema_version=str(payload.get("schema_version", "")),
        producer=str(payload.get("producer", "")),
        capability_scope=tuple(str(c) for c in payload.get("capability_scope", ())),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
        payload=body,
        normalized_checksum=declared,
        run_id=str(payload["run_id"]) if payload.get("run_id") else None,
    )


def is_legacy_ibkr_evidence(payload: Mapping[str, Any]) -> ArtifactType | None:
    """Which legacy artifact this is, by EXACT shape. None if it is not one."""
    keys = set(payload)
    if ARTIFACT_TYPE_KEY in keys:
        return None  # already typed; migration does not apply
    if _LEGACY_HISTORICAL_KEYS <= keys:
        return ArtifactType.IBKR_HISTORICAL_EVIDENCE
    if _LEGACY_CORPORATE_KEYS <= keys:
        return ArtifactType.IBKR_CORPORATE_EVIDENCE
    return None


def migrate_legacy_ibkr_evidence(
    path: Path, *, run_id: str | None = None, now: datetime | None = None
) -> IbkrEvidenceArtifact:
    """Upgrade an untyped legacy IBKR artifact into a typed one.

    Explicit and narrow: the file must match the exact historical shape. The
    original body is preserved verbatim, so migration adds typing without
    changing a single recorded observation.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise IbkrArtifactError(f"unreadable IBKR artifact {path}: {error}") from error
    if not isinstance(payload, dict):
        raise IbkrArtifactError(f"{path}: IBKR artifact must be a JSON object")

    artifact_type = is_legacy_ibkr_evidence(payload)
    if artifact_type is None:
        if ARTIFACT_TYPE_KEY in payload:
            raise IbkrArtifactError(f"{path} is already typed; nothing to migrate")
        raise IbkrArtifactError(
            f"{path} does not match the known legacy IBKR shape; refusing to"
            " guess a type (regenerate it in an owner run instead)"
        )

    observed_raw = str(payload.get("observed_at", "")).strip()
    try:
        observed_at = (
            datetime.fromisoformat(observed_raw)
            if observed_raw
            else (now or datetime.now(tz=UTC))
        )
    except ValueError:
        observed_at = now or datetime.now(tz=UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)

    scope = (
        IBKR_HISTORICAL_CAPABILITY_SCOPE
        if artifact_type is ArtifactType.IBKR_HISTORICAL_EVIDENCE
        else IBKR_CORPORATE_CAPABILITY_SCOPE
    )
    producer = str(
        payload.get("qualification_version") or payload.get("version") or "unknown"
    )
    return IbkrEvidenceArtifact(
        artifact_type=artifact_type,
        schema_version=IBKR_EVIDENCE_SCHEMA_VERSION,
        producer=producer,
        capability_scope=scope,
        observed_at=observed_at,
        payload=payload,
        run_id=run_id,
    ).sealed()


__all__ = [
    "IBKR_CORPORATE_ARTIFACT_TYPE",
    "IBKR_CORPORATE_CAPABILITY_SCOPE",
    "IBKR_EVIDENCE_SCHEMA_VERSION",
    "IBKR_HISTORICAL_ARTIFACT_TYPE",
    "IBKR_HISTORICAL_CAPABILITY_SCOPE",
    "IbkrArtifactError",
    "IbkrEvidenceArtifact",
    "is_legacy_ibkr_evidence",
    "load_and_verify_ibkr_evidence",
    "migrate_legacy_ibkr_evidence",
]
