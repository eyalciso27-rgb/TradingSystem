"""Dataset V1 delisting / corporate-action / successor / retraction drills.

Covers the delisted-history + event-ledger + identity-graph + tombstone package:
survivorship protection, revision-safe visibility, double-adjustment protection
and fail-closed validation. Offline only; no provider is selected.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from trading_market_data_core.dataset_v1 import (
    CapabilityState,
    CorporateActionRecord,
    CorporateActionType,
    DailyPriceRecord,
    DatasetV1,
    DelistingReason,
    DelistingRecord,
    EffectiveInterval,
    MembershipRecord,
    PriceAdjustmentStatus,
    Provenance,
    RetractionRecord,
    SecurityIdentity,
    SecurityLifecycleState,
    SymbolHistoryRecord,
)
from trading_market_data_core.dataset_v1_ingestion import (
    IdentityResolver,
    IngestionError,
    RawCorporateAction,
    RawDelisting,
    RawSymbolInterval,
    SourceCapabilities,
    SourceKind,
    assess_reference_capability,
    build_retraction,
    dataset_fingerprint,
    ingest_corporate_actions,
    ingest_delistings,
    ingest_symbol_history,
)

OBSERVED = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
EARLY = datetime(2020, 1, 1, tzinfo=UTC)
ANCIENT = datetime(2005, 1, 1, tzinfo=UTC)  # backfilled facts known since inception
LATE = datetime(2021, 8, 1, tzinfo=UTC)
PROV = Provenance("fixture", OBSERVED)
FULL_CAPS = SourceCapabilities(
    provides_effective_dates=True, provides_known_at=True, provides_stable_identity=True
)
RESOLVER = IdentityResolver({"OLD": "sec-old", "NEW": "sec-new", "AAA": "sec-aaa"})


def _sec(security_id: str, symbol: str) -> SecurityIdentity:
    return SecurityIdentity(security_id=security_id, symbol=symbol, primary_exchange="NYSE")


class FixtureDelistingSource:
    def __init__(self, rows: tuple[RawDelisting, ...], *, kind: SourceKind = SourceKind.FIXTURE):
        self._rows = rows
        self._kind = kind

    @property
    def source_id(self) -> str:
        return "fixture-delisting"

    @property
    def kind(self) -> SourceKind:
        return self._kind

    @property
    def capabilities(self) -> SourceCapabilities:
        return FULL_CAPS

    def fetch_delistings(self) -> tuple[RawDelisting, ...]:
        return self._rows


class FixtureCorporateActionSource:
    def __init__(self, rows: tuple[RawCorporateAction, ...]):
        self._rows = rows

    @property
    def source_id(self) -> str:
        return "fixture-ca"

    @property
    def kind(self) -> SourceKind:
        return SourceKind.FIXTURE

    @property
    def capabilities(self) -> SourceCapabilities:
        return FULL_CAPS

    def fetch_corporate_actions(self) -> tuple[RawCorporateAction, ...]:
        return self._rows


# -- delisted history ---------------------------------------------------------


def _delisted_dataset() -> DatasetV1:
    return DatasetV1(
        securities=(_sec("sec-old", "OLD"),),
        delistings=(
            DelistingRecord(
                record_id="d1", security_id="sec-old", delisted_on=date(2021, 5, 3),
                known_at=ANCIENT, provenance=PROV, reason=DelistingReason.ACQUIRED,
                listing_date=date(2010, 3, 1), last_trading_date=date(2021, 4, 30),
                venue="NYSE",
            ),
        ),
    )


def test_delisted_security_remains_resolvable_and_queryable() -> None:
    dataset = _delisted_dataset()
    assert dataset.validate().valid
    record = dataset.delisting_as_of("sec-old", known_as_of=OBSERVED)
    assert record is not None and record.reason is DelistingReason.ACQUIRED
    # still addressable by stable id long after delisting
    assert dataset.lifecycle_state_as_of("sec-old", date(2026, 1, 1)) is (
        SecurityLifecycleState.DELISTED
    )


def test_lifecycle_boundaries_and_last_trading_date() -> None:
    dataset = _delisted_dataset()
    assert dataset.lifecycle_state_as_of("sec-old", date(2009, 1, 1)) is (
        SecurityLifecycleState.NOT_YET_EXISTED
    )
    assert dataset.lifecycle_state_as_of("sec-old", date(2021, 5, 2)) is (
        SecurityLifecycleState.ACTIVE
    )
    # delisting effective-date boundary: delisted ON delisted_on
    assert dataset.lifecycle_state_as_of("sec-old", date(2021, 5, 3)) is (
        SecurityLifecycleState.DELISTED
    )
    record = dataset.delisting_as_of("sec-old", known_as_of=OBSERVED)
    assert record is not None and record.last_trading_date == date(2021, 4, 30)


def test_missing_price_data_is_not_interpreted_as_delisting() -> None:
    # a security with NO delisting facts is UNKNOWN, never silently "delisted"
    dataset = DatasetV1(securities=(_sec("sec-aaa", "AAA"),))
    assert dataset.lifecycle_state_as_of("sec-aaa", date(2021, 1, 1)) is (
        SecurityLifecycleState.UNKNOWN
    )
    assert dataset.is_delisted_as_of("sec-aaa", date(2021, 1, 1)) is False


def test_delisting_revision_known_later_does_not_leak_backward() -> None:
    original = DelistingRecord(
        record_id="d1", security_id="sec-old", delisted_on=date(2021, 5, 3),
        known_at=EARLY, provenance=PROV,
    )
    corrected = DelistingRecord(
        record_id="d2", security_id="sec-old", delisted_on=date(2021, 3, 1),
        known_at=LATE, provenance=PROV, supersedes="d1",
    )
    dataset = DatasetV1(securities=(_sec("sec-old", "OLD"),), delistings=(original, corrected))
    assert dataset.validate().valid
    # before the correction is known, the ORIGINAL date governs
    assert dataset.is_delisted_as_of(
        "sec-old", date(2021, 4, 1), known_as_of=datetime(2021, 4, 2, tzinfo=UTC)
    ) is False
    # once known, the corrected earlier date governs
    assert dataset.is_delisted_as_of(
        "sec-old", date(2021, 4, 1), known_as_of=datetime(2021, 9, 1, tzinfo=UTC)
    ) is True


def test_delisting_date_ordering_is_validated() -> None:
    bad = DelistingRecord(
        record_id="d1", security_id="sec-old", delisted_on=date(2021, 5, 3), known_at=EARLY,
        provenance=PROV, listing_date=date(2022, 1, 1),
    )
    report = DatasetV1(securities=(_sec("sec-old", "OLD"),), delistings=(bad,)).validate()
    assert not report.valid
    assert "listing_date after delisted_on" in " ".join(report.errors)


# -- survivorship-bias protection --------------------------------------------


def test_delisted_security_stays_in_historical_membership() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-old", "OLD"), _sec("sec-new", "NEW")),
        memberships=(
            MembershipRecord(
                record_id="m-old", index_id="SP500", security_id="sec-old",
                interval=EffectiveInterval(date(2015, 1, 1), date(2021, 5, 2)),
                known_at=ANCIENT, provenance=PROV,
            ),
            MembershipRecord(
                record_id="m-new", index_id="SP500", security_id="sec-new",
                interval=EffectiveInterval(date(2021, 5, 3)), known_at=ANCIENT, provenance=PROV,
            ),
        ),
        delistings=(
            DelistingRecord(
                record_id="d-old", security_id="sec-old", delisted_on=date(2021, 5, 3),
                known_at=ANCIENT, provenance=PROV,
            ),
        ),
    )
    assert dataset.validate().valid
    # the historical universe on 2018 contains the security that later delisted
    assert dataset.members_as_of("SP500", date(2018, 6, 1)) == frozenset({"sec-old"})
    # today's surviving universe is NOT used for the historical answer
    assert dataset.members_as_of("SP500", date(2026, 1, 1)) == frozenset({"sec-new"})
    # and the delisted name is still resolvable by stable id
    assert any(s.security_id == "sec-old" for s in dataset.securities)


def test_missing_broker_contract_does_not_erase_historical_existence() -> None:
    # ibkr_con_id is absent (no current broker contract) but history stands
    dataset = DatasetV1(
        securities=(SecurityIdentity("sec-old", "OLD", "NYSE", ibkr_con_id=None),),
        memberships=(
            MembershipRecord(
                record_id="m", index_id="SP500", security_id="sec-old",
                interval=EffectiveInterval(date(2015, 1, 1), date(2021, 5, 2)),
                known_at=ANCIENT, provenance=PROV,
            ),
        ),
    )
    assert dataset.validate().valid
    assert dataset.members_as_of("SP500", date(2018, 1, 1)) == frozenset({"sec-old"})


def test_historical_symbol_lookup_is_not_todays_symbol_map() -> None:
    # "OLD" was the ticker until a rename; identity is stable across the rename
    dataset = DatasetV1(
        securities=(_sec("sec-old", "NEWTICK"),),
        symbol_history=(
            SymbolHistoryRecord(
                record_id="s1", security_id="sec-old", symbol="OLD",
                interval=EffectiveInterval(date(2010, 1, 1), date(2020, 12, 31)),
                known_at=ANCIENT, provenance=PROV,
            ),
            SymbolHistoryRecord(
                record_id="s2", security_id="sec-old", symbol="NEWTICK",
                interval=EffectiveInterval(date(2021, 1, 1)), known_at=ANCIENT, provenance=PROV,
            ),
        ),
    )
    assert dataset.validate().valid
    assert dataset.symbol_as_of("sec-old", date(2015, 1, 1)) == "OLD"
    assert dataset.symbol_as_of("sec-old", date(2022, 1, 1)) == "NEWTICK"
    # historical resolution uses the historical ticker, not today's
    assert dataset.resolve_security_by_symbol_as_of("OLD", date(2015, 1, 1)) == frozenset(
        {"sec-old"}
    )
    assert dataset.resolve_security_by_symbol_as_of("NEWTICK", date(2015, 1, 1)) == frozenset()


# -- corporate actions --------------------------------------------------------


def _ca(**kw: object) -> CorporateActionRecord:
    base: dict[str, object] = {
        "record_id": "ca1", "security_id": "sec-aaa",
        "action_type": CorporateActionType.SPLIT, "effective_on": date(2021, 6, 1),
        "known_at": EARLY, "provenance": PROV, "ratio": Decimal("2"),
    }
    base.update(kw)
    return CorporateActionRecord(**base)  # type: ignore[arg-type]


def test_split_and_reverse_split_ratio_validation() -> None:
    good = DatasetV1(securities=(_sec("sec-aaa", "AAA"),), corporate_actions=(_ca(),))
    assert good.validate().valid
    bad = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        corporate_actions=(_ca(action_type=CorporateActionType.REVERSE_SPLIT, ratio=None),),
    )
    assert "ratio must be > 0" in " ".join(bad.validate().errors)


def test_cash_dividend_requires_decimal_amount_and_currency() -> None:
    missing_currency = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        corporate_actions=(
            _ca(action_type=CorporateActionType.CASH_DIVIDEND, ratio=None,
                cash_amount=Decimal("0.235"), currency=None),
        ),
    )
    assert "requires a currency" in " ".join(missing_currency.validate().errors)
    negative = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        corporate_actions=(
            _ca(action_type=CorporateActionType.CASH_DIVIDEND, ratio=None,
                cash_amount=Decimal("-1"), currency="USD"),
        ),
    )
    assert "cash_amount must be >= 0" in " ".join(negative.validate().errors)
    ok = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        corporate_actions=(
            _ca(action_type=CorporateActionType.CASH_DIVIDEND, ratio=None,
                cash_amount=Decimal("0.235"), currency="USD"),
        ),
    )
    assert ok.validate().valid
    # Decimal precision is preserved exactly (no float rounding)
    assert ok.corporate_actions[0].cash_amount == Decimal("0.235")


def test_corporate_action_date_ordering_validated() -> None:
    bad = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        corporate_actions=(
            _ca(ex_date=date(2021, 6, 10), record_date=date(2021, 6, 5)),
        ),
    )
    assert "record_date before ex_date" in " ".join(bad.validate().errors)


def test_corporate_action_known_later_does_not_leak_backward() -> None:
    late_action = _ca(known_at=LATE)
    dataset = DatasetV1(securities=(_sec("sec-aaa", "AAA"),), corporate_actions=(late_action,))
    before = dataset.corporate_actions_for(
        "sec-aaa", known_as_of=datetime(2021, 5, 1, tzinfo=UTC)
    )
    after = dataset.corporate_actions_for(
        "sec-aaa", known_as_of=datetime(2021, 9, 1, tzinfo=UTC)
    )
    assert before == ()
    assert len(after) == 1


def test_corporate_actions_query_by_effective_range() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        corporate_actions=(
            _ca(record_id="c1", effective_on=date(2021, 1, 5)),
            _ca(record_id="c2", effective_on=date(2022, 1, 5)),
        ),
    )
    in_range = dataset.corporate_actions_for(
        "sec-aaa", known_as_of=OBSERVED, start=date(2021, 1, 1), end=date(2021, 12, 31)
    )
    assert [row.record_id for row in in_range] == ["c1"]


# -- successor / predecessor identity graph ----------------------------------


def test_merger_requires_successor_and_exposes_lineage() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-old", "OLD"), _sec("sec-new", "NEW")),
        corporate_actions=(
            _ca(record_id="m1", security_id="sec-old",
                action_type=CorporateActionType.MERGER, ratio=None,
                successor_security_id="sec-new"),
        ),
    )
    assert dataset.validate().valid
    assert dataset.successors_of("sec-old", known_as_of=OBSERVED) == frozenset({"sec-new"})
    # lineage is EXPOSED, not auto-stitched: the successor's own history is separate
    assert dataset.successors_of("sec-new", known_as_of=OBSERVED) == frozenset()


def test_merger_without_successor_is_rejected() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-old", "OLD"),),
        corporate_actions=(
            _ca(security_id="sec-old", action_type=CorporateActionType.MERGER, ratio=None),
        ),
    )
    assert "must name a successor" in " ".join(dataset.validate().errors)


def test_unknown_and_self_successor_rejected() -> None:
    unknown = DatasetV1(
        securities=(_sec("sec-old", "OLD"),),
        corporate_actions=(
            _ca(security_id="sec-old", action_type=CorporateActionType.MERGER, ratio=None,
                successor_security_id="ghost"),
        ),
    )
    assert "unknown security_id=ghost" in " ".join(unknown.validate().errors)
    self_link = DatasetV1(
        securities=(_sec("sec-old", "OLD"),),
        corporate_actions=(
            _ca(security_id="sec-old", action_type=CorporateActionType.MERGER, ratio=None,
                successor_security_id="sec-old"),
        ),
    )
    assert "successor cannot be itself" in " ".join(self_link.validate().errors)


def test_contradictory_successor_mappings_rejected() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-old", "OLD"), _sec("sec-new", "NEW"), _sec("sec-aaa", "AAA")),
        corporate_actions=(
            _ca(record_id="a", security_id="sec-old", action_type=CorporateActionType.MERGER,
                ratio=None, successor_security_id="sec-new"),
            _ca(record_id="b", security_id="sec-old", effective_on=date(2021, 7, 1),
                action_type=CorporateActionType.ACQUISITION, ratio=None,
                successor_security_id="sec-aaa"),
        ),
    )
    assert "contradictory successor mapping" in " ".join(dataset.validate().errors)


def test_identity_transition_cycle_rejected() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-old", "OLD"), _sec("sec-new", "NEW")),
        corporate_actions=(
            _ca(record_id="a", security_id="sec-old", action_type=CorporateActionType.MERGER,
                ratio=None, successor_security_id="sec-new"),
            _ca(record_id="b", security_id="sec-new", action_type=CorporateActionType.MERGER,
                ratio=None, successor_security_id="sec-old"),
        ),
    )
    assert "cycle" in " ".join(dataset.validate().errors)


def test_spinoff_one_to_many_lineage_is_allowed() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-old", "OLD"), _sec("sec-new", "NEW"), _sec("sec-aaa", "AAA")),
        corporate_actions=(
            _ca(record_id="s1", security_id="sec-old", action_type=CorporateActionType.SPINOFF,
                ratio=None, successor_security_id="sec-new"),
            _ca(record_id="s2", security_id="sec-old", effective_on=date(2021, 7, 1),
                action_type=CorporateActionType.SPINOFF, ratio=None,
                successor_security_id="sec-aaa"),
        ),
    )
    assert dataset.validate().valid  # a spin-off may produce several successors
    assert dataset.successors_of("sec-old", known_as_of=OBSERVED) == frozenset(
        {"sec-new", "sec-aaa"}
    )


# -- retraction / tombstone ---------------------------------------------------


def test_retraction_is_invisible_before_it_is_known_and_applies_after() -> None:
    membership = MembershipRecord(
        record_id="m1", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2020, 1, 1)), known_at=EARLY, provenance=PROV,
    )
    retraction = RetractionRecord(
        record_id="r1", target_record_id="m1", known_at=LATE, provenance=PROV,
        reason="never actually a member",
    )
    dataset = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),), memberships=(membership,),
        retractions=(retraction,),
    )
    assert dataset.validate().valid
    # earlier decisions still see what was believed then
    assert dataset.members_as_of(
        "SP500", date(2021, 1, 1), known_as_of=datetime(2021, 1, 2, tzinfo=UTC)
    ) == frozenset({"sec-aaa"})
    # later decisions no longer treat the retracted assertion as valid
    assert dataset.members_as_of(
        "SP500", date(2021, 1, 1), known_as_of=datetime(2022, 1, 1, tzinfo=UTC)
    ) == frozenset()


def test_retracting_a_correction_restores_the_original() -> None:
    original = MembershipRecord(
        record_id="m1", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2020, 1, 1), date(2021, 12, 31)),
        known_at=EARLY, provenance=PROV,
    )
    correction = MembershipRecord(
        record_id="m2", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2020, 1, 1), date(2020, 6, 30)),
        known_at=LATE, provenance=PROV, supersedes="m1",
    )
    # the correction itself was wrong and is retracted later still
    retraction = RetractionRecord(
        record_id="r1", target_record_id="m2",
        known_at=datetime(2022, 1, 1, tzinfo=UTC), provenance=PROV,
    )
    dataset = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),), memberships=(original, correction),
        retractions=(retraction,),
    )
    assert dataset.validate().valid
    # while only the correction is known -> shortened membership
    assert dataset.members_as_of(
        "SP500", date(2021, 6, 1), known_as_of=datetime(2021, 9, 1, tzinfo=UTC)
    ) == frozenset()
    # after retracting the correction -> the ORIGINAL assertion governs again
    assert dataset.members_as_of(
        "SP500", date(2021, 6, 1), known_as_of=datetime(2022, 6, 1, tzinfo=UTC)
    ) == frozenset({"sec-aaa"})


def test_retraction_chain_validation() -> None:
    membership = MembershipRecord(
        record_id="m1", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2020, 1, 1)), known_at=EARLY, provenance=PROV,
    )
    securities = (_sec("sec-aaa", "AAA"),)
    unknown_target = DatasetV1(
        securities=securities, memberships=(membership,),
        retractions=(RetractionRecord("r1", "ghost", LATE, PROV),),
    )
    assert "targets unknown record" in " ".join(unknown_target.validate().errors)

    self_cycle = DatasetV1(
        securities=securities, memberships=(membership,),
        retractions=(RetractionRecord("r1", "r1", LATE, PROV),),
    )
    assert "targets itself" in " ".join(self_cycle.validate().errors)

    chained = DatasetV1(
        securities=securities, memberships=(membership,),
        retractions=(
            RetractionRecord("r1", "m1", LATE, PROV),
            RetractionRecord("r2", "r1", LATE, PROV),
        ),
    )
    assert "targets another retraction" in " ".join(chained.validate().errors)

    duplicate = DatasetV1(
        securities=securities, memberships=(membership,),
        retractions=(
            RetractionRecord("r1", "m1", LATE, PROV),
            RetractionRecord("r2", "m1", LATE, PROV),
        ),
    )
    assert "duplicate retraction of record" in " ".join(duplicate.validate().errors)


def test_retracted_record_is_not_an_overlap_conflict() -> None:
    # two overlapping membership intervals would normally be a conflict, but the
    # first was retracted (never valid) -> validation must NOT report an overlap
    bad_original = MembershipRecord(
        record_id="m1", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2020, 1, 1), date(2021, 12, 31)),
        known_at=EARLY, provenance=PROV,
    )
    replacement = MembershipRecord(
        record_id="m2", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2021, 6, 1), date(2022, 12, 31)),
        known_at=LATE, provenance=PROV,
    )
    conflicting = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),), memberships=(bad_original, replacement)
    )
    assert "overlapping effective intervals" in " ".join(conflicting.validate().errors)

    resolved = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),), memberships=(bad_original, replacement),
        retractions=(RetractionRecord("r1", "m1", LATE, PROV),),
    )
    assert resolved.validate().valid


def test_retracted_event_is_not_a_contradictory_successor() -> None:
    first = _ca(record_id="a", security_id="sec-old", action_type=CorporateActionType.MERGER,
                ratio=None, successor_security_id="sec-new")
    wrong = _ca(record_id="b", security_id="sec-old", effective_on=date(2021, 7, 1),
                action_type=CorporateActionType.ACQUISITION, ratio=None,
                successor_security_id="sec-aaa")
    securities = (_sec("sec-old", "OLD"), _sec("sec-new", "NEW"), _sec("sec-aaa", "AAA"))
    assert "contradictory successor mapping" in " ".join(
        DatasetV1(securities=securities, corporate_actions=(first, wrong)).validate().errors
    )
    retracted = DatasetV1(
        securities=securities, corporate_actions=(first, wrong),
        retractions=(RetractionRecord("r1", "b", LATE, PROV),),
    )
    assert retracted.validate().valid


def test_retraction_requires_tz_aware_known_at() -> None:
    with pytest.raises(ValueError, match="known_at must be timezone-aware"):
        RetractionRecord("r", "m", datetime(2021, 1, 1), PROV)  # noqa: DTZ001


# -- price adjustment boundary (no double adjustment) ------------------------


def test_raw_series_may_not_carry_adjusted_close() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        prices=(
            DailyPriceRecord(
                "sec-aaa", date(2021, 1, 4), Decimal("10"), PROV,
                adjusted_close=Decimal("5"),
                adjustment_status=PriceAdjustmentStatus.RAW_UNADJUSTED,
            ),
        ),
    )
    assert "adjustment provenance is contradictory" in " ".join(dataset.validate().errors)


def test_internally_adjusted_series_must_name_its_ledger() -> None:
    missing = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        prices=(
            DailyPriceRecord(
                "sec-aaa", date(2021, 1, 4), Decimal("10"), PROV,
                adjustment_status=PriceAdjustmentStatus.INTERNALLY_ADJUSTED,
            ),
        ),
    )
    assert "double adjustment cannot be ruled out" in " ".join(missing.validate().errors)
    ok = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        prices=(
            DailyPriceRecord(
                "sec-aaa", date(2021, 1, 4), Decimal("10"), PROV,
                adjustment_status=PriceAdjustmentStatus.INTERNALLY_ADJUSTED,
                adjusted_with_ledger="sha256:abc",
            ),
        ),
    )
    assert ok.validate().valid


def test_ledger_reference_without_internal_adjustment_rejected() -> None:
    dataset = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        prices=(
            DailyPriceRecord(
                "sec-aaa", date(2021, 1, 4), Decimal("10"), PROV,
                adjustment_status=PriceAdjustmentStatus.PROVIDER_ADJUSTED,
                adjusted_with_ledger="sha256:abc",
            ),
        ),
    )
    assert "names an adjusted_with_ledger but is" in " ".join(dataset.validate().errors)


def test_default_price_adjustment_status_is_unknown_fail_closed() -> None:
    price = DailyPriceRecord("sec-aaa", date(2021, 1, 4), Decimal("10"), PROV)
    assert price.adjustment_status is PriceAdjustmentStatus.UNKNOWN


# -- ingestion ----------------------------------------------------------------


def test_delisting_ingestion_is_deterministic_and_idempotent() -> None:
    rows = (
        RawDelisting("OLD", date(2021, 5, 3), EARLY, "s1", reason=DelistingReason.ACQUIRED,
                     successor_symbol="NEW"),
        RawDelisting("OLD", date(2021, 5, 3), EARLY, "s1", reason=DelistingReason.ACQUIRED,
                     successor_symbol="NEW"),
    )
    first = ingest_delistings(FixtureDelistingSource(rows), RESOLVER, observed_at=OBSERVED)
    again = ingest_delistings(FixtureDelistingSource(rows), RESOLVER, observed_at=OBSERVED)
    assert len(first) == 1  # duplicate collapsed
    assert [r.record_id for r in first] == [r.record_id for r in again]
    assert first[0].successor_security_id == "sec-new"


def test_corporate_action_ingestion_unknown_symbol_fails_closed() -> None:
    rows = (
        RawCorporateAction("ZZZ", CorporateActionType.SPLIT, date(2021, 6, 1), EARLY, "s1",
                           ratio=Decimal("2")),
    )
    with pytest.raises(IngestionError, match="unknown security for symbol 'ZZZ'"):
        ingest_corporate_actions(FixtureCorporateActionSource(rows), RESOLVER,
                                 observed_at=OBSERVED)


def test_corporate_action_correction_becomes_supersedes() -> None:
    rows = (
        RawCorporateAction("AAA", CorporateActionType.SPLIT, date(2021, 6, 1), EARLY, "orig",
                           ratio=Decimal("2")),
        RawCorporateAction("AAA", CorporateActionType.SPLIT, date(2021, 6, 1), LATE, "fix",
                           ratio=Decimal("3"), corrects="orig"),
    )
    records = ingest_corporate_actions(FixtureCorporateActionSource(rows), RESOLVER,
                                       observed_at=OBSERVED)
    dataset = DatasetV1(securities=(_sec("sec-aaa", "AAA"),), corporate_actions=records)
    assert dataset.validate().valid
    early_view = dataset.corporate_actions_for(
        "sec-aaa", known_as_of=datetime(2021, 5, 1, tzinfo=UTC)
    )
    late_view = dataset.corporate_actions_for(
        "sec-aaa", known_as_of=datetime(2022, 1, 1, tzinfo=UTC)
    )
    assert [row.ratio for row in early_view] == [Decimal("2")]
    assert [row.ratio for row in late_view] == [Decimal("3")]


def test_conflicting_source_record_id_fails_closed() -> None:
    rows = (
        RawDelisting("OLD", date(2021, 5, 3), EARLY, "same"),
        RawDelisting("OLD", date(2021, 6, 3), EARLY, "same"),
    )
    with pytest.raises(IngestionError, match="two different records"):
        ingest_delistings(FixtureDelistingSource(rows), RESOLVER, observed_at=OBSERVED)


def test_symbol_history_ingestion_keeps_identity_stable() -> None:
    rows = (
        RawSymbolInterval("OLD", "OLD", date(2010, 1, 1), date(2020, 12, 31), EARLY, "s1"),
        RawSymbolInterval("OLD", "RENAMED", date(2021, 1, 1), None, EARLY, "s2"),
    )
    records = ingest_symbol_history(rows, RESOLVER, source_id="fixture", observed_at=OBSERVED)
    assert {r.security_id for r in records} == {"sec-old"}  # same identity across rename
    assert {r.symbol for r in records} == {"OLD", "RENAMED"}


def test_build_retraction_is_deterministic() -> None:
    a = build_retraction("m1", known_at=LATE, source="fixture", observed_at=OBSERVED)
    b = build_retraction("m1", known_at=LATE, source="fixture", observed_at=OBSERVED)
    assert a.record_id == b.record_id


# -- fingerprint coverage -----------------------------------------------------


def _base_dataset() -> DatasetV1:
    return DatasetV1(securities=(_sec("sec-aaa", "AAA"),))


def test_fingerprint_changes_on_corporate_action() -> None:
    base = _base_dataset()
    with_action = DatasetV1(securities=base.securities, corporate_actions=(_ca(),))
    assert dataset_fingerprint(base) != dataset_fingerprint(with_action)


def test_fingerprint_changes_on_delisting() -> None:
    base = _base_dataset()
    with_delisting = DatasetV1(
        securities=base.securities,
        delistings=(
            DelistingRecord("d1", "sec-aaa", date(2021, 5, 3), EARLY, PROV),
        ),
    )
    assert dataset_fingerprint(base) != dataset_fingerprint(with_delisting)


def test_fingerprint_changes_on_retraction_and_symbol_history() -> None:
    membership = MembershipRecord(
        record_id="m1", index_id="SP500", security_id="sec-aaa",
        interval=EffectiveInterval(date(2020, 1, 1)), known_at=EARLY, provenance=PROV,
    )
    base = DatasetV1(securities=(_sec("sec-aaa", "AAA"),), memberships=(membership,))
    retracted = DatasetV1(
        securities=base.securities, memberships=(membership,),
        retractions=(RetractionRecord("r1", "m1", LATE, PROV),),
    )
    assert dataset_fingerprint(base) != dataset_fingerprint(retracted)
    with_symbols = DatasetV1(
        securities=base.securities, memberships=(membership,),
        symbol_history=(
            SymbolHistoryRecord("s1", "sec-aaa", "AAA", EffectiveInterval(date(2020, 1, 1)),
                                EARLY, PROV),
        ),
    )
    assert dataset_fingerprint(base) != dataset_fingerprint(with_symbols)


def test_fingerprint_changes_on_price_adjustment_metadata() -> None:
    raw = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        prices=(
            DailyPriceRecord("sec-aaa", date(2021, 1, 4), Decimal("10"), PROV,
                             adjustment_status=PriceAdjustmentStatus.RAW_UNADJUSTED),
        ),
    )
    provider = DatasetV1(
        securities=(_sec("sec-aaa", "AAA"),),
        prices=(
            DailyPriceRecord("sec-aaa", date(2021, 1, 4), Decimal("10"), PROV,
                             adjustment_status=PriceAdjustmentStatus.PROVIDER_ADJUSTED),
        ),
    )
    assert dataset_fingerprint(raw) != dataset_fingerprint(provider)


def test_fingerprint_is_order_independent_for_events() -> None:
    a = _ca(record_id="c1", effective_on=date(2021, 1, 5))
    b = _ca(record_id="c2", effective_on=date(2022, 1, 5))
    secs = (_sec("sec-aaa", "AAA"),)
    assert dataset_fingerprint(
        DatasetV1(securities=secs, corporate_actions=(a, b))
    ) == dataset_fingerprint(DatasetV1(securities=secs, corporate_actions=(b, a)))


# -- capability promotion (fail-closed) --------------------------------------


def test_fixture_and_snapshot_cannot_promote_delisted_or_corporate_actions() -> None:
    snapshot = SourceCapabilities(
        provides_effective_dates=False, provides_known_at=False, provides_stable_identity=True
    )
    for capability in ("delisted_security_history", "corporate_actions"):
        fixture = assess_reference_capability(
            capability, source_kind=SourceKind.FIXTURE, capabilities=FULL_CAPS,
            validation_errors=(), historical_change_count=3,
            real_evidence_checksum="sha256:" + "a" * 64,
        )
        assert fixture.state is not CapabilityState.EMPIRICALLY_VERIFIED
        snapshot_only = assess_reference_capability(
            capability, source_kind=SourceKind.REAL, capabilities=snapshot,
            validation_errors=(), historical_change_count=3,
            real_evidence_checksum="sha256:" + "a" * 64,
        )
        assert snapshot_only.state is CapabilityState.BLOCKED_EXTERNAL
        assert any("snapshot-only" in reason for reason in snapshot_only.reasons)


def test_synthetic_events_without_real_evidence_stay_blocked() -> None:
    assessment = assess_reference_capability(
        "corporate_actions", source_kind=SourceKind.REAL, capabilities=FULL_CAPS,
        validation_errors=(), historical_change_count=10, real_evidence_checksum=None,
    )
    assert assessment.state is CapabilityState.BLOCKED_EXTERNAL
    assert any("checksum" in reason for reason in assessment.reasons)
