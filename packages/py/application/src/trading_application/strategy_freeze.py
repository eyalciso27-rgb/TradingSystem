"""Strategy freeze system (Track A / A1).

Authority: CLAUDE.md ("Do not begin Paper Auto or Live"; the Risk Engine has
absolute veto; no invented parameters), `docs/strategies/STRATEGY_FREEZE_TEMPLATE.md`
(every trading parameter is an OWNER decision — this repository invents none),
and ADR-0014 (deterministic evaluation over FROZEN inputs). The fusion manifest
shape is the existing `strategy_manifest.StrategyManifest` (IMP-0901); this
module does NOT reimplement it — it wraps it in a freeze envelope.

What this module is
-------------------
A strategy moves through an explicit lifecycle::

    DRAFT ──freeze()──▶ FROZEN ──supersede()──▶ SUPERSEDED

* **DRAFT** — owner parameters are being collected; anything may still change.
* **FROZEN** — every mandatory owner parameter is present, the fusion manifest
  is attached, and an owner approval reference, freeze timestamp and source
  commit are recorded. The record is an immutable (frozen) dataclass, so a
  parameter can never mutate after freeze — any material change must mint a NEW
  ``version_id`` via :meth:`supersede`.
* **SUPERSEDED** — replaced by a newer version; kept for lineage.

Hard invariants (enforced, with tests):

* **No invented parameters.** ``freeze_readiness`` reports which mandatory keys
  are missing; nothing here ever fills a missing value. Freezing with a missing
  mandatory parameter fails closed.
* **Provenance for every parameter.** Each parameter records *who/what* supplied
  it and when — a value with no source cannot be part of a freeze.
* **Evidence only after freeze.** Backtest / OOS / shadow evidence references
  cannot be attached to a DRAFT — that ordering is the whole point of a freeze.
* **Deterministic fingerprint.** The fingerprint is a stable hash of the
  identity + fusion manifest + every parameter (key, value, source). A material
  parameter change changes the fingerprint, which is why it demands a new
  version identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trading_application.strategy_manifest import ManifestRole, StrategyManifest

FREEZE_SYSTEM_VERSION = "strategy-freeze-1.0.0"


class FreezeError(Exception):
    """Fail-closed strategy-freeze failure (missing parameter, illegal transition)."""


class FreezeStatus(StrEnum):
    """Explicit freeze lifecycle (never inferred from evidence)."""

    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    SUPERSEDED = "SUPERSEDED"


#: The mandatory owner-controlled parameter KEYS a strategy must declare before
#: it can be frozen. These are KEYS ONLY — the repository supplies no VALUE for
#: any of them (STRATEGY_FREEZE_TEMPLATE.md). Grouped to mirror the template
#: sections the owner fills in. Adding a value for any of these is an owner act.
MANDATORY_OWNER_PARAMETERS: tuple[str, ...] = (
    # universe & liquidity (template §2)
    "universe.definition",
    "universe.min_liquidity",
    "universe.min_price",
    "universe.max_spread",
    # timeframe & setup (template §3)
    "timeframe.primary",
    "setup.definition",
    "trigger.definition",
    # entry / exit / invalidation (template §4)
    "entry.rule",
    "exit.stop_rule",
    "exit.target_rule",
    "exit.time_exit",
    "exit.invalidation",
    # sizing & risk limits (template §5)
    "sizing.method",
    "risk.per_trade",
    "risk.max_concurrent_positions",
    "risk.max_portfolio_exposure",
    "risk.daily_loss_limit",
    "risk.action_on_breach",
    # sessions & data (template §6)
    "sessions.allowed",
    "data.point_in_time_requirement",
    "data.staleness_behaviour",
    # cost assumptions (template §8) — the most common way a backtest lies
    "costs.commission_model",
    "costs.slippage_model",
    "costs.spread_assumption",
    "costs.partial_fill_assumption",
    # evaluation metrics & uncertainty (template §9)
    "metrics.primary",
    "metrics.min_sample_size",
    "metrics.confidence_interval_method",
    # data splits & validation (template §10)
    "splits.train",
    "splits.validation",
    "splits.test_oos",
    "splits.walk_forward",
    # go / no-go (template §12)
    "gonogo.go_requires",
    "gonogo.nogo_if",
)

#: The subset of mandatory parameters that are the owner cost/slippage/spread
#: assumptions a backtest additionally requires (Track A / A3 gate).
MANDATORY_COST_PARAMETERS: tuple[str, ...] = (
    "costs.commission_model",
    "costs.slippage_model",
    "costs.spread_assumption",
    "costs.partial_fill_assumption",
)


@dataclass(frozen=True, slots=True)
class OwnerParameter:
    """One owner-supplied trading parameter with full provenance.

    ``value`` is an opaque owner-provided string — this module never interprets,
    validates the trading meaning of, or invents it. ``source`` records *who or
    what* supplied it (e.g. ``owner:eyal`` / a decision reference); an empty
    source is rejected so no anonymous value can enter a freeze.
    """

    key: str
    value: str
    source: str
    supplied_at: datetime

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise FreezeError("parameter key is mandatory")
        if not self.value.strip():
            # An empty value is a MISSING value; it is never a silent default.
            raise FreezeError(f"parameter {self.key!r} has no value (owner must supply it)")
        if not self.source.strip():
            raise FreezeError(f"parameter {self.key!r} has no provenance source; fail closed")
        if self.supplied_at.tzinfo is None:
            raise FreezeError(f"parameter {self.key!r} supplied_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StrategyFreezeRecord:
    """A versioned strategy at a point in its freeze lifecycle (immutable).

    Being a frozen dataclass, no field can mutate after construction: a FROZEN
    record physically cannot have a parameter changed in place. A material
    change is expressed only by minting a new ``version_id`` (see
    :meth:`supersede`).
    """

    strategy_id: str
    version_id: str
    status: FreezeStatus
    parameters: tuple[OwnerParameter, ...] = ()
    fusion_manifest: StrategyManifest | None = None
    owner_approval_ref: str | None = None
    frozen_at: datetime | None = None
    source_commit: str | None = None
    superseded_by: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise FreezeError("strategy_id is mandatory")
        if not self.version_id.strip():
            raise FreezeError("version_id is mandatory")
        keys = [parameter.key for parameter in self.parameters]
        if len(keys) != len(set(keys)):
            raise FreezeError("duplicate owner parameter key; fail closed")
        if self.fusion_manifest is not None:
            if self.fusion_manifest.strategy_id != self.strategy_id:
                raise FreezeError("fusion manifest strategy_id does not match the record")
            if not any(e.role is ManifestRole.REQUIRED for e in self.fusion_manifest.entries):
                raise FreezeError("fusion manifest must declare at least one REQUIRED entry")
        # Evidence may only exist once FROZEN — the ordering is the point.
        if self.evidence_refs and self.status is not FreezeStatus.FROZEN:
            raise FreezeError(
                "evaluation/backtest/OOS/shadow evidence cannot be attached before FROZEN"
            )
        if self.status is FreezeStatus.FROZEN:
            self._assert_frozen_complete()
        if self.status is FreezeStatus.SUPERSEDED and not self.superseded_by:
            raise FreezeError("a SUPERSEDED record must name its successor version")

    def _assert_frozen_complete(self) -> None:
        if self.fusion_manifest is None:
            raise FreezeError("a FROZEN strategy must have its fusion manifest attached")
        if not (self.owner_approval_ref or "").strip():
            raise FreezeError("a FROZEN strategy requires an owner approval reference")
        if self.frozen_at is None or self.frozen_at.tzinfo is None:
            raise FreezeError("a FROZEN strategy requires a timezone-aware freeze timestamp")
        if not (self.source_commit or "").strip():
            raise FreezeError("a FROZEN strategy requires a source commit reference")
        missing = self.missing_mandatory_parameters()
        if missing:
            raise FreezeError(
                "cannot freeze: missing mandatory owner parameters "
                f"{list(missing)} (the repository invents none)"
            )

    # -- parameter access ------------------------------------------------------
    def parameter_keys(self) -> frozenset[str]:
        return frozenset(parameter.key for parameter in self.parameters)

    def missing_mandatory_parameters(self) -> tuple[str, ...]:
        """Mandatory keys with no owner value yet — never auto-filled."""
        present = self.parameter_keys()
        return tuple(key for key in MANDATORY_OWNER_PARAMETERS if key not in present)

    def missing_cost_parameters(self) -> tuple[str, ...]:
        present = self.parameter_keys()
        return tuple(key for key in MANDATORY_COST_PARAMETERS if key not in present)

    # -- fingerprint -----------------------------------------------------------
    def fingerprint(self) -> str:
        """Deterministic content hash of identity + manifest + every parameter.

        A material parameter change (value or source) changes this hash, which
        is exactly why such a change demands a new version identity.
        """
        material = {
            "strategy_id": self.strategy_id,
            "version_id": self.version_id,
            "parameters": sorted(
                (
                    {"key": p.key, "value": p.value, "source": p.source}
                    for p in self.parameters
                ),
                key=lambda entry: entry["key"],
            ),
            "fusion_manifest": _manifest_material(self.fusion_manifest),
        }
        serialized = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()

    # -- transitions -----------------------------------------------------------
    def supersede(self, successor_version_id: str) -> StrategyFreezeRecord:
        """Return this record marked SUPERSEDED by ``successor_version_id``."""
        if self.status is not FreezeStatus.FROZEN:
            raise FreezeError("only a FROZEN record can be superseded")
        if not successor_version_id.strip() or successor_version_id == self.version_id:
            raise FreezeError("supersede requires a distinct successor version_id")
        return StrategyFreezeRecord(
            strategy_id=self.strategy_id,
            version_id=self.version_id,
            status=FreezeStatus.SUPERSEDED,
            parameters=self.parameters,
            fusion_manifest=self.fusion_manifest,
            owner_approval_ref=self.owner_approval_ref,
            frozen_at=self.frozen_at,
            source_commit=self.source_commit,
            superseded_by=successor_version_id,
            evidence_refs=self.evidence_refs,
        )

    def with_evidence(self, evidence_refs: tuple[str, ...]) -> StrategyFreezeRecord:
        """Attach evidence references (only legal once FROZEN)."""
        if self.status is not FreezeStatus.FROZEN:
            raise FreezeError("evidence can only be attached to a FROZEN strategy")
        merged = tuple(dict.fromkeys((*self.evidence_refs, *evidence_refs)))
        return StrategyFreezeRecord(
            strategy_id=self.strategy_id,
            version_id=self.version_id,
            status=FreezeStatus.FROZEN,
            parameters=self.parameters,
            fusion_manifest=self.fusion_manifest,
            owner_approval_ref=self.owner_approval_ref,
            frozen_at=self.frozen_at,
            source_commit=self.source_commit,
            evidence_refs=merged,
        )


def _manifest_material(manifest: StrategyManifest | None) -> object:
    if manifest is None:
        return None
    return {
        "strategy_id": manifest.strategy_id,
        "version": manifest.version,
        "entries": sorted(
            (
                {
                    "assessment_key": e.assessment_key,
                    "role": e.role.value,
                    "min_score": str(e.min_score) if e.min_score is not None else None,
                    "required_status": (
                        e.required_status.value if e.required_status is not None else None
                    ),
                    "min_calibration": (
                        e.min_calibration.value if e.min_calibration is not None else None
                    ),
                    "weight": str(e.weight) if e.weight is not None else None,
                }
                for e in manifest.entries
            ),
            key=lambda entry: (str(entry["assessment_key"]), str(entry["role"])),
        ),
    }


@dataclass(frozen=True, slots=True)
class FreezeReadiness:
    """Report of whether a DRAFT can be frozen — never fills anything in."""

    strategy_id: str
    version_id: str
    has_fusion_manifest: bool
    missing_mandatory_parameters: tuple[str, ...]
    parameters_without_source: tuple[str, ...]
    ready: bool
    reasons: tuple[str, ...]


def freeze_readiness(record: StrategyFreezeRecord) -> FreezeReadiness:
    """Assess freeze readiness of a DRAFT. Reports gaps; fills nothing."""
    missing = record.missing_mandatory_parameters()
    no_source = tuple(
        parameter.key for parameter in record.parameters if not parameter.source.strip()
    )
    reasons: list[str] = []
    if record.fusion_manifest is None:
        reasons.append("fusion manifest not attached")
    if missing:
        reasons.append(f"{len(missing)} mandatory owner parameter(s) missing")
    if no_source:
        reasons.append(f"{len(no_source)} parameter(s) without provenance source")
    ready = not reasons
    return FreezeReadiness(
        strategy_id=record.strategy_id,
        version_id=record.version_id,
        has_fusion_manifest=record.fusion_manifest is not None,
        missing_mandatory_parameters=missing,
        parameters_without_source=no_source,
        ready=ready,
        reasons=tuple(reasons),
    )


def freeze(
    draft: StrategyFreezeRecord,
    *,
    owner_approval_ref: str,
    frozen_at: datetime,
    source_commit: str,
) -> StrategyFreezeRecord:
    """Transition a DRAFT to FROZEN. Fail-closed on any missing mandatory input.

    This never supplies a missing trading parameter; if the draft is not ready,
    it raises with the exact gaps.
    """
    if draft.status is not FreezeStatus.DRAFT:
        raise FreezeError(f"only a DRAFT can be frozen (status is {draft.status.value})")
    readiness = freeze_readiness(draft)
    if not readiness.ready:
        raise FreezeError(f"draft not ready to freeze: {'; '.join(readiness.reasons)}")
    return StrategyFreezeRecord(
        strategy_id=draft.strategy_id,
        version_id=draft.version_id,
        status=FreezeStatus.FROZEN,
        parameters=draft.parameters,
        fusion_manifest=draft.fusion_manifest,
        owner_approval_ref=owner_approval_ref,
        frozen_at=frozen_at,
        source_commit=source_commit,
    )


__all__ = [
    "FREEZE_SYSTEM_VERSION",
    "MANDATORY_COST_PARAMETERS",
    "MANDATORY_OWNER_PARAMETERS",
    "FreezeError",
    "FreezeReadiness",
    "FreezeStatus",
    "OwnerParameter",
    "StrategyFreezeRecord",
    "freeze",
    "freeze_readiness",
]
