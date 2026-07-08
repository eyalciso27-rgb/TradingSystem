"""Quant/ML registry: shadow-only birth, approval gate, no silent active."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from trading_application.auth import AuthorizationError, InMemoryAuditSink, Principal, Role
from trading_application.model_registry import (
    DriftStatus,
    ModelRegistry,
    ModelRegistryError,
    ModelStatus,
    PromotionDeniedError,
)
from trading_contracts.enums import CalibrationStatus

NOW = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)


def owner() -> Principal:
    return Principal(principal_id="owner-1", roles=frozenset({Role.OWNER}), mfa_verified=True)


def reviewer() -> Principal:
    return Principal(principal_id="rev-1", roles=frozenset({Role.REVIEWER}), mfa_verified=True)


def make_registry() -> tuple[ModelRegistry, InMemoryAuditSink]:
    audit = InMemoryAuditSink()
    return ModelRegistry(audit), audit


def register(registry: ModelRegistry, *, version: str = "1.0.0") -> None:
    registry.register(
        model_id="entity-extractor",
        family="nlp",
        version=version,
        artifact_ref="object://models/entity-extractor/1.0.0",
        artifact_hash="a" * 64,
        registered_by="researcher-1",
        now=NOW,
    )


def test_new_model_is_shadow_unverified_not_evaluated() -> None:
    registry, audit = make_registry()
    register(registry)
    record = registry.get("entity-extractor", "1.0.0")
    assert record.status is ModelStatus.SHADOW
    assert record.calibration_status is CalibrationStatus.UNVERIFIED
    assert record.drift_status is DriftStatus.NOT_EVALUATED
    assert record.approved_by is None
    assert any("MODEL_REGISTER" in str(d.action) for d in audit.decisions)


def test_unapproved_model_is_never_active() -> None:
    registry, _audit = make_registry()
    register(registry)
    assert registry.resolve_active("nlp") == ()  # DoD: no unapproved active
    assert len(registry.shadow_models("nlp")) == 1


def test_promotion_without_rbac_is_denied_and_audited() -> None:
    registry, audit = make_registry()
    register(registry)
    with pytest.raises(AuthorizationError):
        registry.promote(reviewer(), model_id="entity-extractor", version="1.0.0", now=NOW)
    assert registry.get("entity-extractor", "1.0.0").status is ModelStatus.SHADOW
    assert any(not d.allowed for d in audit.decisions)


def test_explicit_approved_promotion_activates() -> None:
    registry, audit = make_registry()
    register(registry)
    promoted = registry.promote(
        owner(), model_id="entity-extractor", version="1.0.0", now=NOW
    )
    assert promoted.status is ModelStatus.ACTIVE
    assert promoted.approved_by == "owner-1" and promoted.approved_at == NOW
    active = registry.resolve_active("nlp")
    assert [record.version for record in active] == ["1.0.0"]
    assert any("MODEL_PROMOTE" in str(d.action) for d in audit.decisions)


def test_drifting_model_cannot_be_promoted() -> None:
    registry, _audit = make_registry()
    register(registry)
    registry.set_drift(
        "entity-extractor", "1.0.0", DriftStatus.DRIFTING, evidence_ref="drift-report-1"
    )
    with pytest.raises(PromotionDeniedError, match="DRIFTING"):
        registry.promote(owner(), model_id="entity-extractor", version="1.0.0", now=NOW)


def test_rollback_is_explicit_and_removes_active() -> None:
    registry, audit = make_registry()
    register(registry)
    registry.promote(owner(), model_id="entity-extractor", version="1.0.0", now=NOW)
    with pytest.raises(ModelRegistryError, match="reason"):
        registry.rollback(
            owner(), model_id="entity-extractor", version="1.0.0", reason=" ", now=NOW
        )
    rolled = registry.rollback(
        owner(), model_id="entity-extractor", version="1.0.0", reason="drift", now=NOW
    )
    assert rolled.status is ModelStatus.SHADOW and rolled.approved_by is None
    assert registry.resolve_active("nlp") == ()
    assert any("MODEL_ROLLBACK" in str(d.action) for d in audit.decisions)


def test_calibration_and_drift_updates_require_evidence() -> None:
    registry, _audit = make_registry()
    register(registry)
    updated = registry.set_calibration(
        "entity-extractor", "1.0.0", CalibrationStatus.HEURISTIC, evidence_ref="calib-1"
    )
    assert updated.calibration_status is CalibrationStatus.HEURISTIC
    with pytest.raises(ModelRegistryError, match="evidence_ref"):
        registry.set_calibration(
            "entity-extractor", "1.0.0", CalibrationStatus.CALIBRATED, evidence_ref=""
        )
    with pytest.raises(ModelRegistryError, match="evidence_ref"):
        registry.set_drift(
            "entity-extractor", "1.0.0", DriftStatus.STABLE, evidence_ref=""
        )


def test_duplicate_version_idempotent_or_rejected() -> None:
    registry, _audit = make_registry()
    register(registry)
    register(registry)  # identical metadata: idempotent no-op
    assert len(registry.shadow_models("nlp")) == 1
    with pytest.raises(ModelRegistryError, match="immutable"):
        registry.register(
            model_id="entity-extractor",
            family="nlp",
            version="1.0.0",
            artifact_ref="object://models/entity-extractor/OTHER",
            artifact_hash="b" * 64,
            registered_by="researcher-1",
            now=NOW,
        )


def test_artifact_ref_is_opaque_reference_not_binary() -> None:
    registry, _audit = make_registry()
    with pytest.raises(ModelRegistryError, match="inline artifact content"):
        registry.register(
            model_id="m",
            family="nlp",
            version="1",
            artifact_ref="x" * 1000,  # inline blob-sized content
            artifact_hash="a" * 64,
            registered_by="r",
            now=NOW,
        )
    with pytest.raises(ModelRegistryError, match="printable"):
        registry.register(
            model_id="m",
            family="nlp",
            version="1",
            artifact_ref="bin\x00ary",
            artifact_hash="a" * 64,
            registered_by="r",
            now=NOW,
        )


def test_no_secret_material_in_registry() -> None:
    registry, _audit = make_registry()
    with pytest.raises(ModelRegistryError, match="secret material"):
        registry.register(
            model_id="m",
            family="nlp",
            version="1",
            artifact_ref="object://models/api_key=abc123",
            artifact_hash="a" * 64,
            registered_by="r",
            now=NOW,
        )


def test_naive_timestamps_rejected() -> None:
    registry, _audit = make_registry()
    with pytest.raises(ValueError, match="timezone-aware"):
        registry.register(
            model_id="m",
            family="nlp",
            version="1",
            artifact_ref="object://m/1",
            artifact_hash="a" * 64,
            registered_by="r",
            now=datetime(2026, 7, 8, 21, 0),  # noqa: DTZ001
        )
    register(registry)
    with pytest.raises(ValueError, match="timezone-aware"):
        registry.promote(
            owner(),
            model_id="entity-extractor",
            version="1.0.0",
            now=datetime(2026, 7, 8, 21, 0),  # noqa: DTZ001
        )


def test_deterministic_registry_behavior() -> None:
    first_registry, _a1 = make_registry()
    second_registry, _a2 = make_registry()
    for registry in (first_registry, second_registry):
        register(registry)
        register(registry, version="1.1.0")
    assert [r.version for r in first_registry.shadow_models("nlp")] == [
        r.version for r in second_registry.shadow_models("nlp")
    ]


def test_registry_creates_no_candidate_decision_or_order() -> None:
    import trading_application.model_registry as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        forbidden_imports = (
            "trading_persistence", "httpx", "requests", "subprocess",
            "TradeCandidate", "DecisionResult", "OrderIntent", "broker",
        )
        for name in forbidden_imports:
            assert name not in line, line
