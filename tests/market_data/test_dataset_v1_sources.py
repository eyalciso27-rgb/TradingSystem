"""Dataset V1 real-source adapter drills: IBKR-first + free official sources.

All offline and deterministic: the SEC transport is an injected port driven by
recorded, schema-shaped fixtures. No network call is made here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest
from trading_market_data_core.dataset_v1_qualification import (
    CoverageWindow,
    DataQualityMetrics,
    IdentityResolutionReport,
    IdentitySemantics,
    QualificationEvidence,
    SourceDescriptor,
    SourceKind,
    SubCapabilityResult,
    TemporalSemantics,
    promote_capability,
    qualify_source,
)
from trading_market_data_core.dataset_v1_sources import (
    DELISTING_FORMS,
    AdjustmentCrossCheck,
    CrosswalkEntry,
    IbkrCapabilityProbe,
    IbkrReferenceCapability,
    IdentityCrosswalk,
    IndexChangeAnnouncement,
    ProbeOutcome,
    SecEdgarClient,
    SourceAdapterError,
    SourceAuthority,
    SourcedFact,
    artifact_checksum,
    parse_sec_submissions,
    reconcile_facts,
    reconciliation_report,
)

OBSERVED = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
UA = "TradingSystem Research owner@example.com"


class RecordedFetcher:
    """Replays a recorded SEC payload; never touches the network."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self._payload = payload
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get_json(self, url: str, *, headers: Mapping[str, str]) -> tuple[bytes, object]:
        self.calls.append((url, dict(headers)))
        return self._raw, self._payload


def _submissions(forms: list[str] | None = None) -> dict[str, object]:
    forms = forms or ["25-NSE", "10-K", "8-K"]
    n = len(forms)
    return {
        "cik": "320193",
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": [f"0000320193-21-{i:06d}" for i in range(n)],
                "filingDate": ["2021-03-01", "2021-02-01", "2021-01-15"][:n],
                "acceptanceDateTime": ["2021-03-01T16:30:00.000Z", "", ""][:n],
                "primaryDocument": ["form25.htm", "a10k.htm", "a8k.htm"][:n],
            }
        },
    }


# -- SEC access-rule compliance ----------------------------------------------


def test_sec_client_fails_closed_without_a_contact_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with pytest.raises(SourceAdapterError, match="requires a User-Agent"):
        SecEdgarClient(RecordedFetcher(_submissions()))


def test_sec_client_sends_the_declared_user_agent() -> None:
    fetcher = RecordedFetcher(_submissions())
    client = SecEdgarClient(fetcher, user_agent=UA)
    client.submissions("320193")
    url, headers = fetcher.calls[0]
    assert url.endswith("/submissions/CIK0000320193.json")
    assert headers["User-Agent"] == UA


def test_sec_client_rate_limits_between_requests() -> None:
    slept: list[float] = []
    fetcher = RecordedFetcher(_submissions())
    client = SecEdgarClient(fetcher, user_agent=UA, sleep=slept.append)
    client.submissions("320193")
    client.submissions("320193")
    assert slept, "second request must respect the SEC rate ceiling"


def test_invalid_cik_fails_closed() -> None:
    client = SecEdgarClient(RecordedFetcher(_submissions()), user_agent=UA)
    with pytest.raises(SourceAdapterError, match="invalid CIK"):
        client.submissions("not-a-cik")


# -- SEC normalization --------------------------------------------------------


def test_sec_submissions_normalize_with_knowledge_time() -> None:
    raw, parsed = SecEdgarClient(
        RecordedFetcher(_submissions()), user_agent=UA
    ).submissions("320193")
    records = parse_sec_submissions(raw, parsed)
    assert len(records) == 3
    delisting = [r for r in records if r.is_delisting_form]
    assert len(delisting) == 1
    assert delisting[0].form == "25-NSE"
    assert delisting[0].filing_date == date(2021, 3, 1)
    assert delisting[0].accepted_at is not None  # knowledge time captured
    assert delisting[0].accepted_at.tzinfo is not None
    # a filing without an acceptance time records UNKNOWN, never a guess
    assert records[1].accepted_at is None


def test_delisting_forms_are_explicit() -> None:
    assert "25" in DELISTING_FORMS and "25-NSE" in DELISTING_FORMS
    assert "10-K" not in DELISTING_FORMS  # an annual report is not a delisting


def test_malformed_sec_payloads_fail_closed() -> None:
    with pytest.raises(SourceAdapterError, match="must be a JSON object"):
        parse_sec_submissions(b"", ["not", "an", "object"])
    with pytest.raises(SourceAdapterError, match="no cik"):
        parse_sec_submissions(b"", {"filings": {"recent": {}}})
    misaligned = {
        "cik": "1", "filings": {"recent": {"form": ["25"], "accessionNumber": [],
                                           "filingDate": ["2021-01-01"]}}
    }
    with pytest.raises(SourceAdapterError, match="not aligned"):
        parse_sec_submissions(b"", misaligned)
    bad_date = {
        "cik": "1", "filings": {"recent": {"form": ["25"], "accessionNumber": ["a"],
                                           "filingDate": ["not-a-date"]}}
    }
    with pytest.raises(SourceAdapterError, match="unparseable SEC filingDate"):
        parse_sec_submissions(b"", bad_date)


def test_sec_column_that_is_not_an_array_fails_closed() -> None:
    # a bare string would otherwise index per character and fabricate filings
    payload = {"cik": "1", "filings": {"recent": {"form": "25", "accessionNumber": ["a"],
                                                  "filingDate": ["2021-01-01"]}}}
    with pytest.raises(SourceAdapterError, match="must be an array"):
        parse_sec_submissions(b"", payload)


def test_raw_artifact_checksum_is_of_the_raw_bytes() -> None:
    raw, parsed = SecEdgarClient(
        RecordedFetcher(_submissions()), user_agent=UA
    ).submissions("320193")
    digest = artifact_checksum(raw)
    assert digest.startswith("sha256:")
    # a different response yields a different digest (tamper-evident)
    other_raw, _ = SecEdgarClient(
        RecordedFetcher(_submissions(["25"])), user_agent=UA
    ).submissions("320193")
    assert artifact_checksum(other_raw) != digest
    del parsed


def test_sec_normalization_is_deterministic() -> None:
    raw, parsed = SecEdgarClient(
        RecordedFetcher(_submissions()), user_agent=UA
    ).submissions("320193")
    assert parse_sec_submissions(raw, parsed) == parse_sec_submissions(raw, parsed)


# -- S&P official public announcements ---------------------------------------


def _announcement(**kw: object) -> IndexChangeAnnouncement:
    base: dict[str, object] = {
        "index_id": "SP500", "announced_on": date(2021, 3, 1),
        "effective_on": date(2021, 3, 22), "added_ticker": "NEWCO",
        "removed_ticker": "OLDCO", "source_url": "https://example.invalid/announcement",
        "source_document_checksum": "sha256:" + "a" * 64,
    }
    base.update(kw)
    return IndexChangeAnnouncement(**base)  # type: ignore[arg-type]


def test_announcement_carries_both_time_axes() -> None:
    announcement = _announcement()
    assert announcement.announced_on < announcement.effective_on  # knowledge before effect


def test_announcement_effective_before_announced_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="cannot take effect before"):
        _announcement(effective_on=date(2021, 1, 1))


def test_announcement_checksum_must_be_a_real_sha256_digest() -> None:
    # a prefix alone is not a checksum; a cosmetic digest must never be accepted
    with pytest.raises(SourceAdapterError, match="sha256 source checksum"):
        _announcement(source_document_checksum="sha256:abc123")
    with pytest.raises(SourceAdapterError, match="not a hex sha256"):
        _announcement(source_document_checksum="sha256:" + "z" * 64)


def test_announcement_without_a_change_or_checksum_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="must add or remove"):
        _announcement(added_ticker=None, removed_ticker=None)
    with pytest.raises(SourceAdapterError, match="sha256 source checksum"):
        _announcement(source_document_checksum="not-a-digest")


# -- IBKR probe / adjustment cross-check -------------------------------------


def test_ibkr_probe_records_entitlement_gaps_without_assuming() -> None:
    probes = (
        IbkrCapabilityProbe(
            IbkrReferenceCapability.CONTRACT_IDENTITY_CONID, ProbeOutcome.AVAILABLE
        ),
        IbkrCapabilityProbe(
            IbkrReferenceCapability.CORPORATE_EVENT_FEED, ProbeOutcome.ENTITLEMENT_REQUIRED,
            "requires a subscription the account does not hold",
        ),
        IbkrCapabilityProbe(
            IbkrReferenceCapability.INDEX_MEMBERSHIP_HISTORY, ProbeOutcome.NOT_TESTED
        ),
    )
    payload = [probe.to_dict() for probe in probes]
    assert payload[1]["outcome"] == "ENTITLEMENT_REQUIRED"
    assert payload[2]["outcome"] == "NOT_TESTED"  # never silently "unavailable"


def test_adjustment_cross_check_records_the_inference_boundary() -> None:
    check = AdjustmentCrossCheck(
        symbol="AAPL", observed_on=date(2020, 8, 28), trades_close="499.23",
        adjusted_last_close="124.81", diverges=True,
    )
    payload = check.to_dict()
    assert payload["diverges"] is True
    # divergence must never be recorded as event-level corporate-action evidence
    assert "NOT event-level corporate-action evidence" in str(payload["inference_boundary"])


# -- identity crosswalk -------------------------------------------------------


def _crosswalk() -> IdentityCrosswalk:
    return IdentityCrosswalk(
        (
            CrosswalkEntry("sec-aapl", ibkr_con_id=265598, sec_cik="0000320193",
                           current_ticker="AAPL", historical_tickers=("AAPL",)),
            CrosswalkEntry("sec-old", ibkr_con_id=111, sec_cik="0000111111",
                           current_ticker=None, historical_tickers=("OLDCO",)),
        )
    )


def test_crosswalk_rejects_ticker_only_identity() -> None:
    with pytest.raises(SourceAdapterError, match="ticker alone is never"):
        IdentityCrosswalk((CrosswalkEntry("sec-x", current_ticker="X"),))


def test_crosswalk_resolves_by_cik_and_conid() -> None:
    crosswalk = _crosswalk()
    by_cik = crosswalk.resolve_by_cik(["320193", "0000999999"])
    assert by_cik.resolved == {"320193": "sec-aapl"}
    assert by_cik.unresolved == ("0000999999",)
    by_conid = crosswalk.resolve_by_conid([265598, 42])
    assert by_conid.resolved == {"265598": "sec-aapl"}
    assert by_conid.unresolved == ("42",)


def test_ambiguous_ticker_is_reported_never_guessed() -> None:
    crosswalk = IdentityCrosswalk(
        (
            CrosswalkEntry("sec-a", ibkr_con_id=1, historical_tickers=("DUP",)),
            CrosswalkEntry("sec-b", ibkr_con_id=2, historical_tickers=("DUP",)),
        )
    )
    result = crosswalk.resolve_ticker_as_of("DUP")
    assert result.ambiguous == ("DUP",)
    assert result.resolved == {}


def test_delisted_name_without_current_ticker_still_resolves() -> None:
    # survivorship protection at the identity layer
    assert _crosswalk().resolve_ticker_as_of("OLDCO").resolved == {"OLDCO": "sec-old"}


# -- multi-source reconciliation ---------------------------------------------


def _fact(source_id: str, value: str, authority: SourceAuthority) -> SourcedFact:
    return SourcedFact(
        fact_key="delisted_on:sec-old", value=value, source_id=source_id,
        authority=authority, source_checksum="sha256:" + "a" * 64, observed_at=OBSERVED,
    )


def test_agreeing_sources_are_corroborated() -> None:
    reconciled = reconcile_facts(
        (
            _fact("sec-edgar", "2021-03-01", SourceAuthority.OFFICIAL_PUBLIC),
            _fact("ibkr", "2021-03-01", SourceAuthority.OWNER_PRIMARY),
        )
    )
    assert reconciled[0].resolved is True
    assert reconciled[0].value == "2021-03-01"
    assert reconciled[0].supporting_sources == ("ibkr",)


def test_higher_authority_resolves_a_disagreement_explicitly() -> None:
    reconciled = reconcile_facts(
        (
            _fact("sec-edgar", "2021-03-01", SourceAuthority.OFFICIAL_PUBLIC),
            _fact("free-dataset", "2021-04-15", SourceAuthority.PUBLIC_SECONDARY),
        )
    )
    fact = reconciled[0]
    assert fact.resolved is True
    assert fact.value == "2021-03-01"
    assert fact.disagreements  # the disagreement is RECORDED, never hidden
    assert "authority precedence" in fact.resolution_note


def test_equal_authority_disagreement_stays_unresolved() -> None:
    reconciled = reconcile_facts(
        (
            _fact("sec-edgar", "2021-03-01", SourceAuthority.OFFICIAL_PUBLIC),
            _fact("sp-dji", "2021-04-15", SourceAuthority.OFFICIAL_PUBLIC),
        )
    )
    fact = reconciled[0]
    assert fact.resolved is False  # fail closed
    assert fact.value is None
    assert "UNRESOLVED" in fact.resolution_note
    report = reconciliation_report(reconciled)
    assert report["unresolved"] == 1


def test_reconciliation_is_deterministic() -> None:
    facts = (
        _fact("b-source", "2021-03-01", SourceAuthority.OFFICIAL_PUBLIC),
        _fact("a-source", "2021-03-01", SourceAuthority.OWNER_PRIMARY),
    )
    assert reconcile_facts(facts) == reconcile_facts(tuple(reversed(facts)))


# -- authority + PARTIAL semantics in evidence -------------------------------


def _descriptor(
    authority: SourceAuthority, kind: SourceKind = SourceKind.REAL
) -> SourceDescriptor:
    return SourceDescriptor(
        source_id="sec-edgar", name="SEC EDGAR", version="2026-08", kind=kind,
        authority=authority.value, raw_checksum="sha256:" + "a" * 64,
        raw_artifact="CIK0000320193.json",
    )


def _qualify(
    sub_capabilities: tuple[SubCapabilityResult, ...] = (),
    authority: SourceAuthority = SourceAuthority.OFFICIAL_PUBLIC,
) -> QualificationEvidence:
    return qualify_source(
        "corporate_actions", source=_descriptor(authority),
        coverage=CoverageWindow(date(2018, 1, 1), date(2026, 6, 30)),
        temporal=TemporalSemantics(True, True, True),
        identity=IdentitySemantics(True, ("security_id", "cik"), False),
        metrics=DataQualityMetrics(total_rows=100, accepted=100, historical_changes=8),
        resolution=IdentityResolutionReport(resolved=100),
        proofs=("event_level_action",), sub_capabilities=sub_capabilities,
        observed_at=OBSERVED,
    )


def test_authority_is_recorded_in_the_evidence_body() -> None:
    evidence = _qualify()
    source = evidence.to_dict()["source"]
    assert isinstance(source, dict)
    assert source["authority"] == "OFFICIAL_PUBLIC"


def test_secondary_source_cannot_masquerade_as_official() -> None:
    secondary = _qualify(authority=SourceAuthority.PUBLIC_SECONDARY)
    source = secondary.to_dict()["source"]
    assert isinstance(source, dict)
    assert source["authority"] == "PUBLIC_SECONDARY"


def test_failed_sub_capability_blocks_promotion_and_is_itemised() -> None:
    evidence = _qualify(
        sub_capabilities=(
            SubCapabilityResult("split", True, "3 splits proven"),
            SubCapabilityResult("dividend", True, "12 dividends proven"),
            SubCapabilityResult("merger", False, "no merger event in the window"),
        )
    )
    assert evidence.promotion_eligible is False
    assert any("sub-capabilities not proven: ['merger']" in r for r in evidence.blocked_reasons)
    from trading_market_data_core.dataset_v1 import CapabilityState

    assert promote_capability(evidence) is CapabilityState.BLOCKED_EXTERNAL
    subs = evidence.to_dict()["sub_capabilities"]
    assert isinstance(subs, list)
    listed = {sub["name"]: sub["passed"] for sub in subs}
    assert listed == {"split": True, "dividend": True, "merger": False}


def test_all_sub_capabilities_passing_allows_promotion() -> None:
    evidence = _qualify(
        sub_capabilities=(
            SubCapabilityResult("split", True), SubCapabilityResult("dividend", True),
        )
    )
    from trading_market_data_core.dataset_v1 import CapabilityState

    assert promote_capability(evidence) is CapabilityState.EMPIRICALLY_VERIFIED


def test_sub_capabilities_are_part_of_the_authenticated_checksum() -> None:
    a = _qualify(sub_capabilities=(SubCapabilityResult("split", True),))
    b = _qualify(sub_capabilities=(SubCapabilityResult("split", False),))
    assert a.normalized_checksum != b.normalized_checksum
