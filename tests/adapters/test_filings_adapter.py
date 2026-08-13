"""IMP-0607 filings/news adapter — point-in-time, independent-source clustering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from trading_application.source_adapters.filings import (
    CanonicalFiling,
    FilingsAdapter,
    RawFilingRecord,
)
from trading_contracts.enums import IntegrationHealthStatus, SourceType

OCCURRED = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
OBSERVED = OCCURRED + timedelta(minutes=2)
AVAILABLE = OCCURRED + timedelta(minutes=2)
INGESTED = OCCURRED + timedelta(minutes=5)


class FakeSource:
    def __init__(
        self, name: str, source_type: SourceType, batches: list[tuple[RawFilingRecord, ...]]
    ):
        self._name = name
        self._source_type = source_type
        self._batches = batches
        self._i = 0
        self.raise_next = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def source_type(self) -> SourceType:
        return self._source_type

    def fetch(self, *, after_cursor: str) -> tuple[RawFilingRecord, ...]:
        if self.raise_next:
            raise ConnectionError("edgar down")
        if self._i >= len(self._batches):
            return ()
        batch = self._batches[self._i]
        self._i += 1
        return batch


def rec(accession: str, *, cik: str = "0000320193", form: str = "8-K",
        occurred: datetime = OCCURRED, observed: datetime = OBSERVED,
        available: datetime = AVAILABLE, amends: str | None = None) -> RawFilingRecord:
    return RawFilingRecord(
        accession=accession, cik=cik, form_type=form, title=f"{form} {accession}",
        occurred_at=occurred, observed_at=observed, available_at=available,
        url=f"https://sec.gov/{accession}", amends_accession=amends,
    )


def test_official_filing_is_normalized_with_point_in_time() -> None:
    src = FakeSource("sec-edgar", SourceType.FILING, [(rec("acc-1"),)])
    adapter = FilingsAdapter([src])
    out = adapter.poll(ingested_at=INGESTED)
    assert len(out) == 1
    f = out[0]
    assert isinstance(f, CanonicalFiling)
    assert f.source_type is SourceType.FILING
    assert f.occurred_at == OCCURRED and f.available_at == AVAILABLE
    assert f.ingested_at == INGESTED
    assert f.provenance_hash.startswith("sha256:")
    assert adapter.health(observed_at=INGESTED).status is IntegrationHealthStatus.HEALTHY


def test_independent_sources_of_same_event_share_cluster_key() -> None:
    edgar = FakeSource("sec-edgar", SourceType.FILING, [(rec("acc-1"),)])
    news = FakeSource("news-wire", SourceType.NEWS, [(rec("news-99"),)])  # same cik/form/day
    adapter = FilingsAdapter([edgar, news])
    out = adapter.poll(ingested_at=INGESTED)
    assert len(out) == 2
    assert out[0].cluster_key == out[1].cluster_key  # independent corroboration -> one cluster
    assert out[0].dedup_key != out[1].dedup_key  # but distinct provenance identities


def test_redelivered_accession_is_idempotent() -> None:
    src = FakeSource("sec-edgar", SourceType.FILING, [(rec("acc-1"),), (rec("acc-1"),)])
    adapter = FilingsAdapter([src])
    assert len(adapter.poll(ingested_at=INGESTED)) == 1
    assert adapter.poll(ingested_at=INGESTED) == ()  # no duplicate candidate


def test_amendment_is_a_correction_revision() -> None:
    src = FakeSource("sec-edgar", SourceType.FILING, [
        (rec("acc-1", form="10-K"),),
        (rec("acc-2", form="10-K/A", amends="acc-1"),),
    ])
    adapter = FilingsAdapter([src])
    first = adapter.poll(ingested_at=INGESTED)
    amend = adapter.poll(ingested_at=INGESTED + timedelta(days=1))
    assert first[0].revision == 1
    assert amend[0].revision == 2
    assert amend[0].amends_accession == "acc-1"
    # amendment clusters with the original filing
    assert amend[0].cluster_key == first[0].cluster_key


def test_lookahead_observed_before_occurred_fails_closed() -> None:
    bad = rec("acc-1", observed=OCCURRED - timedelta(minutes=1))
    adapter = FilingsAdapter([FakeSource("sec-edgar", SourceType.FILING, [(bad,)])])
    assert adapter.poll(ingested_at=INGESTED) == ()
    assert adapter.health(observed_at=INGESTED).status is IntegrationHealthStatus.DEGRADED


def test_ingest_before_available_fails_closed() -> None:
    src = FakeSource("sec-edgar", SourceType.FILING, [(rec("acc-1"),)])
    adapter = FilingsAdapter([src])
    early = AVAILABLE - timedelta(minutes=1)
    assert adapter.poll(ingested_at=early) == ()
    assert adapter.health(observed_at=early).status is IntegrationHealthStatus.DEGRADED


def test_malformed_record_degrades_never_silent_default() -> None:
    bad = RawFilingRecord(
        accession="", cik="0000320193", form_type="8-K", title="x",
        occurred_at=OCCURRED, observed_at=OBSERVED, available_at=AVAILABLE, url="u",
    )
    adapter = FilingsAdapter([FakeSource("sec-edgar", SourceType.FILING, [(bad,)])])
    assert adapter.poll(ingested_at=INGESTED) == ()
    assert adapter.health(observed_at=INGESTED).status is IntegrationHealthStatus.DEGRADED


def test_source_failure_degrades() -> None:
    src = FakeSource("sec-edgar", SourceType.FILING, [(rec("acc-1"),)])
    src.raise_next = True
    adapter = FilingsAdapter([src])
    assert adapter.poll(ingested_at=INGESTED) == ()
    assert adapter.health(observed_at=INGESTED).status is IntegrationHealthStatus.DEGRADED


def test_no_sources_is_rejected() -> None:
    with pytest.raises(Exception, match="at least one filing source"):
        FilingsAdapter([])


def test_adapter_has_no_order_surface() -> None:
    adapter = FilingsAdapter([FakeSource("sec-edgar", SourceType.FILING, [()])])
    for surface in ("submit", "cancel", "execute", "place_order", "flatten"):
        assert not hasattr(adapter, surface)
