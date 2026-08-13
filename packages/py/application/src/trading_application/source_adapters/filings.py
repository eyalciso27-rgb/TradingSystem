"""News / filings adapter (IMP-0607) — official source + independent clustering.

Canonical DoD: "Official source, news provider, corrections, clustering;
Point-in-time and independent-source tests pass." This adapter ingests
regulatory filings (official source: SEC EDGAR) and news items about the same
events through one or more INJECTED ``FilingSource`` ports. It:

  * preserves the canonical four-timestamp point-in-time model (occurred /
    observed / available / ingested) and refuses look-ahead — a record that
    claims it was observable or available before it occurred fails closed, so
    replay/backtest can never see a filing before it existed;
  * is idempotent per (source, accession): the same source redelivering an
    accession produces no second candidate;
  * CLUSTERS independent sources: an EDGAR filing and a news item describing
    the SAME underlying event (issuer + form + filing day) share a
    ``cluster_key`` so downstream fusion yields one candidate from independent
    corroboration;
  * treats an amendment (e.g. ``10-K/A``) as a correction — a new revision that
    references the amended accession;
  * on malformed / schema-drift input records a circuit-breaker failure and
    reports DEGRADED — it NEVER silently substitutes a default value.

No network client lives here; the real EDGAR/news fetch is the injected port.
Read-only for trading: there is no order/execution surface.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_contracts.enums import IntegrationHealthStatus, SourceType

from trading_application.source_adapters.framework import (
    AdapterError,
    AdapterHealth,
    CircuitBreaker,
    DedupRegistry,
    content_hash,
)

FILINGS_ADAPTER_VERSION = "filings-adapter-1.0.0"


@dataclass(frozen=True, slots=True)
class RawFilingRecord:
    """One raw record from a filing/news source (before normalization)."""

    accession: str
    cik: str
    form_type: str
    title: str
    occurred_at: datetime  # when the filing was made / event happened
    observed_at: datetime  # when this source noticed it
    available_at: datetime  # first moment it was allowed to be used
    url: str
    amends_accession: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalFiling:
    """A normalized filing/news event with full point-in-time provenance."""

    source_type: SourceType
    source_name: str
    accession: str
    cik: str
    form_type: str
    title: str
    occurred_at: datetime
    observed_at: datetime
    available_at: datetime
    ingested_at: datetime
    provenance_hash: str
    cluster_key: str
    dedup_key: str
    revision: int = 1
    amends_accession: str | None = None
    read_only_for_trading: bool = True


@runtime_checkable
class FilingSource(Protocol):
    """An injected filing/news source (official EDGAR or a news provider)."""

    @property
    def name(self) -> str: ...

    @property
    def source_type(self) -> SourceType: ...

    def fetch(self, *, after_cursor: str) -> tuple[RawFilingRecord, ...]: ...


def _cluster_key(cik: str, form_type: str, occurred_at: datetime) -> str:
    """Underlying-event identity shared across independent sources.

    Deliberately independent of accession/source so that an EDGAR filing and a
    news item about the same issuer + form on the same UTC day cluster together.
    """
    base_form = form_type.split("/", 1)[0]  # 10-K/A clusters with 10-K
    day = occurred_at.astimezone(occurred_at.tzinfo).date().isoformat()
    raw = f"{cik.strip().upper()}|{base_form.strip().upper()}|{day}"
    return "cluster:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class FilingsAdapter:
    """Fail-closed filings/news ingestion over one or more official sources."""

    def __init__(
        self,
        sources: Iterable[FilingSource],
        *,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._sources = tuple(sources)
        if not self._sources:
            raise AdapterError("at least one filing source is required")
        self._breaker = breaker or CircuitBreaker(threshold=5)
        self._dedup = DedupRegistry()
        self._cursors: dict[str, str] = {}
        self._status = IntegrationHealthStatus.INITIALIZING
        self._detail = "initialized"

    def cursor(self, source_name: str) -> str:
        return self._cursors.get(source_name, "")

    def poll(self, *, ingested_at: datetime) -> tuple[CanonicalFiling, ...]:
        """One ingestion cycle across every source. Fail-closed; idempotent.

        Point-in-time is enforced (no look-ahead); an amendment is a new
        revision; identical redelivery is deduplicated; malformed input opens
        the circuit and reports DEGRADED rather than defaulting.
        """
        if self._breaker.is_open:
            self._set(IntegrationHealthStatus.CIRCUIT_OPEN, "circuit open")
            return ()
        out: list[CanonicalFiling] = []
        for source in self._sources:
            try:
                records = source.fetch(after_cursor=self._cursors.get(source.name, ""))
                for record in sorted(records, key=lambda r: r.accession):
                    normalized = self._normalize(source, record, ingested_at=ingested_at)
                    self._cursors[source.name] = record.accession
                    if normalized is None:
                        continue  # idempotent duplicate
                    out.append(normalized)
            except AdapterError:
                self._breaker.record_failure()
                self._set(IntegrationHealthStatus.DEGRADED, f"schema drift from {source.name}")
                return ()
            except Exception as error:  # noqa: BLE001 - provider failure -> DEGRADED
                self._breaker.record_failure()
                self._set(
                    IntegrationHealthStatus.DEGRADED,
                    f"fetch failed from {source.name}: {type(error).__name__}",
                )
                return ()
        self._breaker.record_success()
        self._set(IntegrationHealthStatus.HEALTHY, f"ingested {len(out)} filing(s)")
        return tuple(out)

    def _normalize(
        self, source: FilingSource, record: RawFilingRecord, *, ingested_at: datetime
    ) -> CanonicalFiling | None:
        for name, value in (
            ("accession", record.accession),
            ("cik", record.cik),
            ("form_type", record.form_type),
        ):
            if not value or not value.strip():
                raise AdapterError(f"malformed filing: missing {name}")
        for ts_name, ts in (
            ("occurred_at", record.occurred_at),
            ("observed_at", record.observed_at),
            ("available_at", record.available_at),
        ):
            if ts.tzinfo is None:
                raise AdapterError(f"malformed filing: {ts_name} must be tz-aware")
        # No look-ahead: a source cannot observe or publish a filing before it
        # occurred, and nothing may be ingested before it was available.
        if record.observed_at < record.occurred_at or record.available_at < record.occurred_at:
            raise AdapterError("point-in-time violation: look-ahead in filing timestamps")
        if ingested_at < record.available_at:
            raise AdapterError("point-in-time violation: ingested before available")

        provenance = content_hash(f"{source.name}|{record.accession}|{record.title}|{record.url}")
        dedup_key = f"{source.name}:{record.accession}"
        if not self._dedup.register(dedup_key, provenance):
            return None
        revision = 2 if record.amends_accession else 1
        return CanonicalFiling(
            source_type=source.source_type,
            source_name=source.name,
            accession=record.accession,
            cik=record.cik,
            form_type=record.form_type,
            title=record.title,
            occurred_at=record.occurred_at,
            observed_at=record.observed_at,
            available_at=record.available_at,
            ingested_at=ingested_at,
            provenance_hash=provenance,
            cluster_key=_cluster_key(record.cik, record.form_type, record.occurred_at),
            dedup_key=dedup_key,
            revision=revision,
            amends_accession=record.amends_accession,
        )

    def health(self, *, observed_at: datetime) -> AdapterHealth:
        return AdapterHealth(status=self._status, detail=self._detail, observed_at=observed_at)

    def _set(self, status: IntegrationHealthStatus, detail: str) -> None:
        self._status = status
        self._detail = detail


__all__ = [
    "FILINGS_ADAPTER_VERSION",
    "CanonicalFiling",
    "FilingSource",
    "FilingsAdapter",
    "RawFilingRecord",
]
