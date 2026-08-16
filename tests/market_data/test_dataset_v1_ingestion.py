"""Dataset V1 PIT reference-layer ingestion drills (offline; provider-neutral)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from trading_market_data_core.dataset_v1 import (
    CapabilityState,
    DatasetV1,
    EffectiveInterval,
    MembershipRecord,
    Provenance,
    SecurityIdentity,
)
from trading_market_data_core.dataset_v1_ingestion import (
    IdentityResolver,
    IngestionError,
    RawGicsAssignment,
    RawGicsInterval,
    RawMembershipChange,
    RawMembershipInterval,
    ReferenceReadinessReport,
    SourceCapabilities,
    SourceKind,
    assess_reference_capability,
    dataset_fingerprint,
    gics_assignments_to_intervals,
    ingest_gics,
    ingest_membership,
    membership_changes_to_intervals,
)

OBSERVED = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
KNOWN = datetime(2020, 1, 1, tzinfo=UTC)
FULL_CAPS = SourceCapabilities(
    provides_effective_dates=True, provides_known_at=True, provides_stable_identity=True
)
RESOLVER = IdentityResolver({"AAA": "sec-aaa", "BBB": "sec-bbb"})


class FixtureMembershipSource:
    def __init__(
        self, intervals: tuple[RawMembershipInterval, ...], *,
        kind: SourceKind = SourceKind.FIXTURE, caps: SourceCapabilities = FULL_CAPS,
        source_id: str = "fixture-membership",
    ) -> None:
        self._intervals = intervals
        self._kind = kind
        self._caps = caps
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def kind(self) -> SourceKind:
        return self._kind

    @property
    def capabilities(self) -> SourceCapabilities:
        return self._caps

    def fetch_membership_intervals(self) -> tuple[RawMembershipInterval, ...]:
        return self._intervals


class FixtureGicsSource:
    def __init__(
        self, intervals: tuple[RawGicsInterval, ...], *,
        kind: SourceKind = SourceKind.FIXTURE, caps: SourceCapabilities = FULL_CAPS,
    ) -> None:
        self._intervals = intervals
        self._kind = kind
        self._caps = caps

    @property
    def source_id(self) -> str:
        return "fixture-gics"

    @property
    def kind(self) -> SourceKind:
        return self._kind

    @property
    def capabilities(self) -> SourceCapabilities:
        return self._caps

    def fetch_gics_intervals(self) -> tuple[RawGicsInterval, ...]:
        return self._intervals


def _security(sec_id: str, symbol: str) -> SecurityIdentity:
    return SecurityIdentity(security_id=sec_id, symbol=symbol, primary_exchange="NASDAQ")


# -- entry/exit boundary semantics -------------------------------------------


def test_membership_changes_entry_exit_boundaries() -> None:
    intervals = membership_changes_to_intervals(
        (
            RawMembershipChange("SP500", "AAA", "ADD", date(2020, 1, 1), "s1"),
            RawMembershipChange("SP500", "AAA", "REMOVE", date(2021, 3, 10), "s2"),
        ),
        known_at=KNOWN,
    )
    assert len(intervals) == 1
    # a REMOVE effective 2021-03-10 means the last membership day is 2021-03-09
    assert intervals[0].valid_from == date(2020, 1, 1)
    assert intervals[0].valid_to == date(2021, 3, 9)

    records = ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)
    dataset = DatasetV1(securities=(_security("sec-aaa", "AAA"),), memberships=records)
    assert dataset.members_as_of("SP500", date(2021, 3, 9)) == frozenset({"sec-aaa"})
    assert dataset.members_as_of("SP500", date(2021, 3, 10)) == frozenset()  # exit boundary


def test_membership_re_addition_is_two_disjoint_intervals() -> None:
    intervals = membership_changes_to_intervals(
        (
            RawMembershipChange("SP500", "AAA", "ADD", date(2020, 1, 1), "s1"),
            RawMembershipChange("SP500", "AAA", "REMOVE", date(2020, 6, 1), "s2"),
            RawMembershipChange("SP500", "AAA", "ADD", date(2021, 1, 1), "s3"),
        ),
        known_at=KNOWN,
    )
    assert len(intervals) == 2
    records = ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)
    dataset = DatasetV1(securities=(_security("sec-aaa", "AAA"),), memberships=records)
    assert dataset.validate().valid  # disjoint re-addition is NOT an overlap conflict
    assert dataset.members_as_of("SP500", date(2020, 3, 1)) == frozenset({"sec-aaa"})
    assert dataset.members_as_of("SP500", date(2020, 8, 1)) == frozenset()  # in the gap
    assert dataset.members_as_of("SP500", date(2021, 6, 1)) == frozenset({"sec-aaa"})


def test_membership_remove_without_add_fails_closed() -> None:
    with pytest.raises(IngestionError, match="REMOVE .* while not a member"):
        membership_changes_to_intervals(
            (RawMembershipChange("SP500", "AAA", "REMOVE", date(2021, 1, 1), "s1"),),
            known_at=KNOWN,
        )


# -- GICS changes -------------------------------------------------------------


def test_gics_assignments_chain_into_intervals() -> None:
    intervals = gics_assignments_to_intervals(
        (
            RawGicsAssignment("AAA", "Information Technology", date(2020, 1, 1), "g1"),
            RawGicsAssignment("AAA", "Communication Services", date(2022, 6, 1), "g2"),
        ),
        known_at=KNOWN,
    )
    records = ingest_gics(FixtureGicsSource(intervals), RESOLVER, observed_at=OBSERVED)
    dataset = DatasetV1(securities=(_security("sec-aaa", "AAA"),), gics=records)
    assert dataset.validate().valid
    old = dataset.gics_as_of("sec-aaa", date(2021, 1, 1))
    new = dataset.gics_as_of("sec-aaa", date(2023, 1, 1))
    assert old is not None and old.sector == "Information Technology"
    assert new is not None and new.sector == "Communication Services"
    # change boundary: 2022-05-31 old, 2022-06-01 new
    boundary_old = dataset.gics_as_of("sec-aaa", date(2022, 5, 31))
    boundary_new = dataset.gics_as_of("sec-aaa", date(2022, 6, 1))
    assert boundary_old is not None and boundary_old.sector == "Information Technology"
    assert boundary_new is not None and boundary_new.sector == "Communication Services"


# -- stable identity / unknown security --------------------------------------


def test_unknown_symbol_fails_closed() -> None:
    source = FixtureMembershipSource(
        (RawMembershipInterval("SP500", "ZZZ", date(2020, 1, 1), None, KNOWN, "s1"),)
    )
    with pytest.raises(IngestionError, match="unknown security for symbol 'ZZZ'"):
        ingest_membership(source, RESOLVER, observed_at=OBSERVED)


# -- idempotent + deterministic ----------------------------------------------


def test_ingestion_is_idempotent() -> None:
    intervals = (
        RawMembershipInterval("SP500", "AAA", date(2020, 1, 1), date(2021, 1, 1), KNOWN, "s1"),
        RawMembershipInterval("SP500", "AAA", date(2020, 1, 1), date(2021, 1, 1), KNOWN, "s1"),
    )
    records = ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)
    assert len(records) == 1  # exact duplicate collapsed
    again = ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)
    assert [r.record_id for r in records] == [r.record_id for r in again]


def test_deterministic_fingerprint_is_order_independent() -> None:
    m1 = MembershipRecord("m1", "SP500", "sec-aaa",
                          EffectiveInterval(date(2020, 1, 1), None), KNOWN,
                          Provenance("src", OBSERVED))
    m2 = MembershipRecord("m2", "SP500", "sec-bbb",
                          EffectiveInterval(date(2021, 1, 1), None), KNOWN,
                          Provenance("src", OBSERVED))
    secs = (_security("sec-aaa", "AAA"), _security("sec-bbb", "BBB"))
    a = DatasetV1(securities=secs, memberships=(m1, m2))
    b = DatasetV1(securities=secs, memberships=(m2, m1))
    assert dataset_fingerprint(a) == dataset_fingerprint(b)
    # a value change changes the fingerprint
    m2b = MembershipRecord("m2", "SP500", "sec-bbb",
                           EffectiveInterval(date(2021, 2, 1), None), KNOWN,
                           Provenance("src", OBSERVED))
    c = DatasetV1(securities=secs, memberships=(m1, m2b))
    assert dataset_fingerprint(c) != dataset_fingerprint(a)


def test_fingerprint_covers_prices_and_events() -> None:
    from decimal import Decimal

    from trading_market_data_core.dataset_v1 import DailyPriceRecord

    secs = (_security("sec-aaa", "AAA"),)
    base = DatasetV1(
        securities=secs,
        prices=(DailyPriceRecord("sec-aaa", date(2024, 1, 2), Decimal("10.00"),
                                 Provenance("src", OBSERVED)),),
    )
    changed = DatasetV1(
        securities=secs,
        prices=(DailyPriceRecord("sec-aaa", date(2024, 1, 2), Decimal("10.01"),
                                 Provenance("src", OBSERVED)),),
    )
    # a price change must change the dataset fingerprint (not a partial fingerprint)
    assert dataset_fingerprint(base) != dataset_fingerprint(changed)


# -- restatement / retroactive correction via ingestion ----------------------


def test_ingested_correction_supersedes_and_does_not_leak() -> None:
    intervals = (
        RawMembershipInterval("SP500", "AAA", date(2020, 1, 1), date(2021, 12, 31),
                              datetime(2020, 1, 1, tzinfo=UTC), "orig"),
        # a retroactive correction: exit was actually 2021-06-30, known from 2021-08-01
        RawMembershipInterval("SP500", "AAA", date(2020, 1, 1), date(2021, 6, 30),
                              datetime(2021, 8, 1, tzinfo=UTC), "fix", corrects="orig"),
    )
    records = ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)
    dataset = DatasetV1(securities=(_security("sec-aaa", "AAA"),), memberships=records)
    assert dataset.validate().valid
    # the correction record carries a supersedes edge to the original
    assert any(r.supersedes is not None for r in records)
    # before the correction is known -> original view (member); after -> corrected
    assert dataset.members_as_of("SP500", date(2021, 7, 1)) == frozenset({"sec-aaa"})
    assert dataset.members_as_of("SP500", date(2021, 9, 1)) == frozenset()


def test_correction_referencing_unknown_source_record_fails_closed() -> None:
    intervals = (
        RawMembershipInterval("SP500", "AAA", date(2020, 1, 1), None, KNOWN, "s1",
                              corrects="does-not-exist"),
    )
    with pytest.raises(IngestionError, match="unknown source record"):
        ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)


# -- duplicate / conflict rejection ------------------------------------------


def test_conflicting_overlapping_intervals_rejected_by_validation() -> None:
    intervals = (
        RawMembershipInterval("SP500", "AAA", date(2020, 1, 1), date(2021, 12, 31), KNOWN, "s1"),
        RawMembershipInterval("SP500", "AAA", date(2021, 6, 1), date(2022, 12, 31), KNOWN, "s2"),
    )
    records = ingest_membership(FixtureMembershipSource(intervals), RESOLVER, observed_at=OBSERVED)
    dataset = DatasetV1(securities=(_security("sec-aaa", "AAA"),), memberships=records)
    report = dataset.validate()
    assert not report.valid
    assert "overlapping effective intervals" in " ".join(report.errors)


# -- promotion / readiness (fail-closed, no fabricated evidence) --------------


def test_snapshot_only_source_cannot_promote() -> None:
    snapshot_caps = SourceCapabilities(
        provides_effective_dates=False, provides_known_at=False, provides_stable_identity=True
    )
    assessment = assess_reference_capability(
        "pit_sp500_membership", source_kind=SourceKind.REAL, capabilities=snapshot_caps,
        validation_errors=(), historical_change_count=5,
        real_evidence_checksum="sha256:" + "a" * 64,
    )
    assert assessment.state is CapabilityState.BLOCKED_EXTERNAL
    assert any("snapshot-only" in r for r in assessment.reasons)


def test_fixture_source_can_never_be_empirically_verified() -> None:
    assessment = assess_reference_capability(
        "asof_gics", source_kind=SourceKind.FIXTURE, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=3,
        real_evidence_checksum="sha256:" + "b" * 64,
    )
    # a clean fixture proves the machinery only -> CONTRACT_READY, never EMPIRICAL
    assert assessment.state is CapabilityState.CONTRACT_READY
    assert not assessment.promotable


def test_missing_checksum_or_no_change_blocks_promotion() -> None:
    no_checksum = assess_reference_capability(
        "pit_sp500_membership", source_kind=SourceKind.REAL, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=2, real_evidence_checksum=None,
    )
    assert no_checksum.state is CapabilityState.BLOCKED_EXTERNAL
    no_change = assess_reference_capability(
        "pit_sp500_membership", source_kind=SourceKind.REAL, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=0,
        real_evidence_checksum="sha256:" + "c" * 64,
    )
    assert no_change.state is CapabilityState.BLOCKED_EXTERNAL


def test_real_source_with_valid_evidence_promotes() -> None:
    # proves the promotion PATH exists (no false-negative). This is a unit test of
    # the decision function; the repo's actual capabilities stay blocked because no
    # real reference source/evidence is wired.
    assessment = assess_reference_capability(
        "pit_sp500_membership", source_kind=SourceKind.REAL, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=1,
        real_evidence_checksum="sha256:" + "d" * 64,
    )
    assert assessment.state is CapabilityState.EMPIRICALLY_VERIFIED
    assert assessment.promotable


def test_readiness_report_stays_fail_closed_with_fixtures() -> None:
    membership = assess_reference_capability(
        "pit_sp500_membership", source_kind=SourceKind.FIXTURE, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=2, real_evidence_checksum=None,
    )
    gics = assess_reference_capability(
        "asof_gics", source_kind=SourceKind.FIXTURE, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=2, real_evidence_checksum=None,
    )
    report = ReferenceReadinessReport((membership, gics))
    assert report.full_dataset_contract_ready is False
    payload = report.to_dict()
    assert payload["full_dataset_contract_ready"] is False
