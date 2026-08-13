"""Track A / A1 — strategy freeze system: lifecycle, no invented parameters."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from trading_application.strategy_freeze import (
    MANDATORY_OWNER_PARAMETERS,
    FreezeError,
    FreezeStatus,
    OwnerParameter,
    StrategyFreezeRecord,
    freeze,
    freeze_readiness,
)
from trading_application.strategy_manifest import ManifestEntry, ManifestRole, StrategyManifest
from trading_contracts.enums import AssessmentStatus

T0 = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
SID = "baseline-trend-breakout-v1"


def manifest(version: str = "v1") -> StrategyManifest:
    return StrategyManifest(
        strategy_id=SID,
        version=version,
        entries=(
            ManifestEntry(
                assessment_key="technical.breakout",
                role=ManifestRole.REQUIRED,
                required_status=AssessmentStatus.TRIGGERED,
            ),
        ),
    )


def all_params() -> tuple[OwnerParameter, ...]:
    # Placeholder OWNER values (test fixtures only). The repository itself
    # supplies none of these; here they stand in for owner decisions.
    return tuple(
        OwnerParameter(key=key, value="owner-decided", source="owner:test", supplied_at=T0)
        for key in MANDATORY_OWNER_PARAMETERS
    )


def draft(
    *, params: tuple[OwnerParameter, ...], with_manifest: bool = True
) -> StrategyFreezeRecord:
    return StrategyFreezeRecord(
        strategy_id=SID,
        version_id="v1",
        status=FreezeStatus.DRAFT,
        parameters=params,
        fusion_manifest=manifest() if with_manifest else None,
    )


# -- readiness never fills anything -----------------------------------------


def test_incomplete_draft_reports_missing_keys_and_is_not_ready() -> None:
    record = draft(params=all_params()[:3])
    readiness = freeze_readiness(record)
    assert readiness.ready is False
    assert len(readiness.missing_mandatory_parameters) == len(MANDATORY_OWNER_PARAMETERS) - 3
    # nothing was auto-filled
    assert record.missing_mandatory_parameters() == readiness.missing_mandatory_parameters


def test_draft_without_manifest_is_not_ready() -> None:
    record = draft(params=all_params(), with_manifest=False)
    readiness = freeze_readiness(record)
    assert readiness.ready is False
    assert "fusion manifest not attached" in readiness.reasons


def test_complete_draft_is_ready() -> None:
    assert freeze_readiness(draft(params=all_params())).ready is True


# -- freeze fails closed on gaps --------------------------------------------


def test_freeze_refuses_incomplete_draft() -> None:
    with pytest.raises(FreezeError, match="not ready"):
        freeze(
            draft(params=all_params()[:5]),
            owner_approval_ref="owner:sig",
            frozen_at=T0,
            source_commit="abc123",
        )


def test_freeze_requires_owner_approval_and_commit_and_timestamp() -> None:
    good = draft(params=all_params())
    frozen = freeze(good, owner_approval_ref="owner:sig", frozen_at=T0, source_commit="abc123")
    assert frozen.status is FreezeStatus.FROZEN
    assert frozen.owner_approval_ref == "owner:sig"
    assert frozen.source_commit == "abc123"


# -- mutation after freeze fails closed -------------------------------------


def test_frozen_record_cannot_mutate_in_place() -> None:
    frozen = freeze(draft(params=all_params()), owner_approval_ref="s", frozen_at=T0,
                    source_commit="c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.parameters = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.owner_approval_ref = "tampered"  # type: ignore[misc]


def test_owner_parameter_rejects_empty_value_or_source() -> None:
    with pytest.raises(FreezeError, match="no value"):
        OwnerParameter(key="risk.per_trade", value="  ", source="owner", supplied_at=T0)
    with pytest.raises(FreezeError, match="provenance"):
        OwnerParameter(key="risk.per_trade", value="1%", source="", supplied_at=T0)


# -- evidence ordering -------------------------------------------------------


def test_evidence_cannot_attach_before_frozen() -> None:
    with pytest.raises(FreezeError, match="before FROZEN"):
        StrategyFreezeRecord(
            strategy_id=SID, version_id="v1", status=FreezeStatus.DRAFT,
            parameters=all_params(), fusion_manifest=manifest(),
            evidence_refs=("backtest:run-1",),
        )


def test_evidence_attaches_only_to_frozen() -> None:
    frozen = freeze(draft(params=all_params()), owner_approval_ref="s", frozen_at=T0,
                    source_commit="c")
    with_ev = frozen.with_evidence(("backtest:run-1",))
    assert with_ev.evidence_refs == ("backtest:run-1",)
    assert with_ev.status is FreezeStatus.FROZEN


# -- version identity on material change ------------------------------------


def test_material_parameter_change_changes_the_fingerprint() -> None:
    base = draft(params=all_params())
    changed_params = (
        OwnerParameter(
            key="risk.per_trade", value="DIFFERENT", source="owner:test", supplied_at=T0
        ),
        *[p for p in all_params() if p.key != "risk.per_trade"],
    )
    changed = draft(params=changed_params)
    assert base.fingerprint() != changed.fingerprint()


def test_fingerprint_is_deterministic_regardless_of_parameter_order() -> None:
    params = all_params()
    reordered = tuple(reversed(params))
    a = draft(params=params)
    b = draft(params=reordered)
    assert a.fingerprint() == b.fingerprint()


def test_supersede_requires_distinct_version_and_marks_superseded() -> None:
    frozen = freeze(draft(params=all_params()), owner_approval_ref="s", frozen_at=T0,
                    source_commit="c")
    with pytest.raises(FreezeError, match="distinct successor"):
        frozen.supersede("v1")
    superseded = frozen.supersede("v2")
    assert superseded.status is FreezeStatus.SUPERSEDED
    assert superseded.superseded_by == "v2"


def test_duplicate_parameter_key_fails_closed() -> None:
    dup = (
        OwnerParameter(key="risk.per_trade", value="a", source="owner", supplied_at=T0),
        OwnerParameter(key="risk.per_trade", value="b", source="owner", supplied_at=T0),
    )
    with pytest.raises(FreezeError, match="duplicate"):
        StrategyFreezeRecord(
            strategy_id=SID, version_id="v1", status=FreezeStatus.DRAFT, parameters=dup,
        )
