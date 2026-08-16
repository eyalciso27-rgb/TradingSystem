from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1 import (
    CapabilityState,
    CorporateActionRecord,
    CorporateActionType,
    DailyPriceRecord,
    DatasetCapabilityMatrix,
    DatasetV1,
    DelistingRecord,
    EffectiveInterval,
    GicsRecord,
    MembershipRecord,
    Provenance,
    SecurityIdentity,
)

UTC_NOW = datetime(2026, 8, 16, 10, 28, tzinfo=UTC)
PROVENANCE = Provenance(source="fixture", observed_at=UTC_NOW)
OWNER_EVIDENCE = Path("docs/poi/evidence/POI-IBKR-HIST.json")
OWNER_EVIDENCE_CHECKSUM = (
    "sha256:663150437934774a09c3a5d5b81614458b4da329dfb42df64f930533ab552945"
)
EMPIRICALLY_VERIFIED = {
    "connectivity_paper_du_gate",
    "daily_trades_rth_schema",
    "contract_identity_conid",
    "head_timestamp_coverage",
    "bid_ask_midpoint",
    "symbol_change_discovery",
}
BLOCKED_EXTERNAL = {
    "pit_sp500_membership",
    "asof_gics",
    "delisted_security_history",
    "corporate_actions",
}


def _security(security_id: str, symbol: str) -> SecurityIdentity:
    return SecurityIdentity(
        security_id=security_id,
        symbol=symbol,
        primary_exchange="NASDAQ",
        ibkr_con_id=1,
    )


def _write_evidence(tmp_path: Path, bid_ask_midpoint: str = "PASS") -> Path:
    evidence = {
        "poi_id": "POI-IBKR-HIST",
        "capabilities": {
            "connectivity_paper_du_gate": "PASS",
            "daily_trades_rth_schema": "PASS",
            "contract_identity_conid": "PASS",
            "head_timestamp_coverage": "PASS",
            "bid_ask_midpoint": bid_ask_midpoint,
            "symbol_change_discovery": "PASS",
        },
        "active_name_price_qualification_sufficient": True,
        "full_dataset_contract_ready": False,
        "evidence_checksum": "sha256:abc123",
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def test_membership_lookup_is_point_in_time_and_excludes_future_entrant() -> None:
    dataset = DatasetV1(
        securities=(_security("old", "OLD"), _security("new", "NEW")),
        memberships=(
            MembershipRecord(
                "SP500",
                "old",
                EffectiveInterval(date(2020, 1, 1), date(2021, 12, 31)),
                PROVENANCE,
            ),
            MembershipRecord(
                "SP500",
                "new",
                EffectiveInterval(date(2022, 1, 1)),
                PROVENANCE,
            ),
        ),
    )

    assert dataset.members_as_of("SP500", date(2021, 6, 1)) == frozenset({"old"})
    assert dataset.members_as_of("SP500", date(2022, 6, 1)) == frozenset({"new"})


def test_gics_lookup_uses_as_of_classification() -> None:
    dataset = DatasetV1(
        securities=(_security("x", "XYZ"),),
        gics=(
            GicsRecord(
                "x",
                "Information Technology",
                None,
                None,
                None,
                EffectiveInterval(date(2020, 1, 1), date(2022, 12, 31)),
                PROVENANCE,
            ),
            GicsRecord(
                "x",
                "Communication Services",
                None,
                None,
                None,
                EffectiveInterval(date(2023, 1, 1)),
                PROVENANCE,
            ),
        ),
    )

    old_gics = dataset.gics_as_of("x", date(2021, 1, 1))
    new_gics = dataset.gics_as_of("x", date(2024, 1, 1))
    assert old_gics is not None
    assert new_gics is not None
    assert old_gics.sector == "Information Technology"
    assert new_gics.sector == "Communication Services"


def test_delisted_security_is_preserved_in_identity_and_history() -> None:
    dataset = DatasetV1(
        securities=(_security("gone", "GONE"),),
        delistings=(
            DelistingRecord("gone", date(2021, 5, 3), "acquired", PROVENANCE),
        ),
        prices=(
            DailyPriceRecord("gone", date(2021, 4, 30), 12.5, PROVENANCE),
        ),
    )

    assert not dataset.is_delisted_as_of("gone", date(2021, 5, 2))
    assert dataset.is_delisted_as_of("gone", date(2021, 5, 3))
    assert dataset.validate().valid


def test_validator_rejects_overlapping_membership_intervals() -> None:
    dataset = DatasetV1(
        securities=(_security("x", "XYZ"),),
        memberships=(
            MembershipRecord(
                "SP500",
                "x",
                EffectiveInterval(date(2020, 1, 1), date(2021, 12, 31)),
                PROVENANCE,
            ),
            MembershipRecord(
                "SP500",
                "x",
                EffectiveInterval(date(2021, 6, 1), date(2022, 12, 31)),
                PROVENANCE,
            ),
        ),
    )

    report = dataset.validate()
    assert not report.valid
    assert "overlapping effective intervals" in " ".join(report.errors)


def test_validator_rejects_unknown_identity_and_missing_provenance() -> None:
    dataset = DatasetV1(
        prices=(
            DailyPriceRecord(
                "missing",
                date(2026, 8, 14),
                100.0,
                Provenance(source="", observed_at=datetime(2026, 8, 16, 10, 0)),
            ),
        )
    )

    report = dataset.validate()
    assert not report.valid
    text = " ".join(report.errors)
    assert "unknown security_id=missing" in text
    assert "provenance.source must be non-empty" in text
    assert "timezone-aware" in text


def test_corporate_action_split_requires_positive_ratio() -> None:
    dataset = DatasetV1(
        securities=(_security("x", "XYZ"),),
        corporate_actions=(
            CorporateActionRecord(
                security_id="x",
                action_type=CorporateActionType.SPLIT,
                effective_on=date(2024, 6, 1),
                provenance=PROVENANCE,
                ratio=0,
            ),
        ),
    )

    assert not dataset.validate().valid


def test_ibkr_evidence_maps_only_proven_capabilities_to_empirical_state(
    tmp_path: Path,
) -> None:
    matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(
        _write_evidence(tmp_path)
    )

    for name in EMPIRICALLY_VERIFIED:
        assert matrix.states[name] is CapabilityState.EMPIRICALLY_VERIFIED
    for name in BLOCKED_EXTERNAL:
        assert matrix.states[name] is CapabilityState.BLOCKED_EXTERNAL
    assert not matrix.full_dataset_contract_ready


def test_checked_in_owner_evidence_drives_dataset_capability_matrix() -> None:
    matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(OWNER_EVIDENCE)

    assert matrix.evidence_checksum == OWNER_EVIDENCE_CHECKSUM
    for name in EMPIRICALLY_VERIFIED:
        assert matrix.states[name] is CapabilityState.EMPIRICALLY_VERIFIED
    for name in BLOCKED_EXTERNAL:
        assert matrix.states[name] is CapabilityState.BLOCKED_EXTERNAL
    assert not matrix.full_dataset_contract_ready


def test_full_readiness_fails_closed_until_every_required_capability_is_empirical(
    tmp_path: Path,
) -> None:
    matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(
        _write_evidence(tmp_path)
    )

    with pytest.raises(ValueError, match="pit_sp500_membership"):
        matrix.assert_full_dataset_contract_ready()


def test_non_pass_ibkr_capability_cannot_be_promoted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bid_ask_midpoint"):
        DatasetCapabilityMatrix.from_ibkr_historical_evidence(
            _write_evidence(tmp_path, bid_ask_midpoint="PARTIAL")
        )
