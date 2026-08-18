"""Dataset V1 real source adapters: IBKR-first + free official public sources.

Owner decision (2026-08-17): **IBKR is the primary approved data source**, and
FREE, PUBLIC, LEGALLY ACCESSIBLE official sources may supplement capabilities
IBKR cannot supply. No paid provider may be integrated.

This module holds the provider-specific edges — SEC EDGAR parsing, S&P public
announcement parsing, the IBKR reference probe, the identity crosswalk and
multi-source reconciliation — so Dataset V1 core stays provider-neutral.

Access-rule compliance (non-negotiable):

* SEC automated access REQUIRES a declared User-Agent carrying a real contact.
  ``SecEdgarClient`` therefore reads ``SEC_EDGAR_USER_AGENT`` from the
  environment and **fails closed** when it is absent — this package never
  fabricates a contact identity to pull from a government service, and never
  bypasses a paywall or authentication.
* Requests are rate-limited below SEC's published ceiling.
* No network call is ever made in unit tests: transport is an injected port and
  the tests drive recorded, schema-shaped fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

SOURCES_VERSION = "dataset-v1-sources-1.0.0"

#: SEC publishes a 10 requests/second ceiling for automated access.
SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.12


class SourceAdapterError(Exception):
    """Fail-closed source-adapter failure (access rules, parsing, ambiguity)."""


class SourceAuthority(StrEnum):
    """How authoritative a source is. Never let a secondary masquerade as official."""

    #: The owner's approved primary broker/data environment (IBKR).
    OWNER_PRIMARY = "OWNER_PRIMARY"
    #: A regulator or index owner's official public material (SEC, S&P DJI).
    OFFICIAL_PUBLIC = "OFFICIAL_PUBLIC"
    #: A free third-party/community dataset — usable, but labelled honestly.
    PUBLIC_SECONDARY = "PUBLIC_SECONDARY"
    #: A test/synthetic source.
    FIXTURE = "FIXTURE"
    #: A real source exposing only today's state.
    SNAPSHOT_ONLY = "SNAPSHOT_ONLY"


# -- SEC EDGAR ----------------------------------------------------------------


@runtime_checkable
class HttpFetcher(Protocol):
    """Minimal read-only transport seam. Tests inject a recorded fixture."""

    def get_json(self, url: str, *, headers: Mapping[str, str]) -> tuple[bytes, object]: ...


class SecEdgarClient:
    """Read-only SEC EDGAR client that honours SEC's automated-access rules.

    The raw response bytes are retained so a genuine artifact checksum can be
    bound into the evidence (never a checksum of a re-serialized object).
    """

    BASE = "https://data.sec.gov"
    #: The quarterly filing catalogue lives on the www host, same access rules.
    ARCHIVES = "https://www.sec.gov/Archives"

    def __init__(
        self,
        fetcher: HttpFetcher,
        *,
        user_agent: str | None = None,
        min_interval_seconds: float = SEC_MIN_REQUEST_INTERVAL_SECONDS,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        agent = user_agent if user_agent is not None else os.environ.get("SEC_EDGAR_USER_AGENT")
        if not (agent or "").strip():
            raise SourceAdapterError(
                "SEC automated access requires a User-Agent with a real contact; set"
                " SEC_EDGAR_USER_AGENT (e.g. 'Owner Name owner@example.com')."
                " Refusing to contact SEC without one (fail closed)."
            )
        self._agent = agent or ""
        self._min_interval = min_interval_seconds
        self._fetcher = fetcher
        self._sleep: Callable[[float], None] = sleep if sleep is not None else time.sleep
        self._last_request: float | None = None

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._agent, "Accept-Encoding": "gzip, deflate"}

    def submissions(self, cik: str) -> tuple[bytes, object]:
        """Company submission history. Returns (raw_bytes, parsed)."""
        padded = str(cik).strip().lstrip("CIK").zfill(10)
        if not padded.isdigit():
            raise SourceAdapterError(f"invalid CIK {cik!r}")
        self._throttle()  # stay below SEC's published request ceiling
        return self._fetcher.get_json(
            f"{self.BASE}/submissions/CIK{padded}.json", headers=self._headers()
        )

    def pace(self) -> None:
        """Block until another request is allowed under SEC's published ceiling.

        Public so a caller fetching a document through the same transport shares
        ONE throttle state instead of opening a second, unpaced budget.
        """
        self._throttle()

    def _throttle(self) -> None:
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def company_concept(self, cik: str, taxonomy: str, concept: str) -> tuple[bytes, object]:
        """One XBRL concept's full history for a filer.

        This is SEC's own STRUCTURED financial data: each fact carries a value,
        a period, the accession that reported it and the date it was filed
        (knowledge time). It is the only free official path to split and
        dividend facts that does not require interpreting prose.

        A concept the filer never reported returns HTTP 404, which the transport
        raises. That is an honest "unavailable", never a reason to invent a fact.
        """
        padded = str(cik).strip().lstrip("CIK").zfill(10)
        if not padded.isdigit():
            raise SourceAdapterError(f"invalid CIK {cik!r}")
        if not taxonomy.strip() or not concept.strip():
            raise SourceAdapterError("company_concept requires a taxonomy and concept")
        self._throttle()
        return self._fetcher.get_json(
            f"{self.BASE}/api/xbrl/companyconcept/CIK{padded}/{taxonomy}/{concept}.json",
            headers=self._headers(),
        )

    def company_facts(self, cik: str) -> tuple[bytes, object]:
        """EVERY XBRL concept a filer reported.

        Asking for one guessed concept name returns 404 and answers nothing;
        this returns what the filer actually reported so concept selection can
        be driven by data instead of by a hypothesis.
        """
        padded = str(cik).strip().lstrip("CIK").zfill(10)
        if not padded.isdigit():
            raise SourceAdapterError(f"invalid CIK {cik!r}")
        self._throttle()
        return self._fetcher.get_json(
            f"{self.BASE}/api/xbrl/companyfacts/CIK{padded}.json", headers=self._headers()
        )

    def full_index(self, year: int, quarter: int, *, fetcher: HttpBytesFetcher) -> bytes:
        """One quarter of SEC's official filing catalogue (``master.idx``).

        Bounded by construction: a single quarter, fetched once, rate-limited.
        The bytes are returned so the caller can bind a genuine checksum without
        committing the artifact itself.
        """
        if quarter not in (1, 2, 3, 4):
            raise SourceAdapterError(f"invalid SEC index quarter {quarter!r}")
        self._throttle()
        return fetcher.get_bytes(
            f"{self.ARCHIVES}/edgar/full-index/{year}/QTR{quarter}/master.idx",
            headers=self._headers(),
        )


@runtime_checkable
class HttpBytesFetcher(Protocol):
    """Read-only transport for non-JSON SEC artifacts (the quarterly index)."""

    def get_bytes(self, url: str, *, headers: Mapping[str, str]) -> bytes: ...


#: SEC forms that evidence an exchange delisting / registration removal.
DELISTING_FORMS = frozenset({"25", "25-NSE", "15-12B", "15-12G", "15-15D"})


@dataclass(frozen=True, slots=True)
class MasterIndexRow:
    """One row of SEC's quarterly master index (the official filing catalogue)."""

    cik: str
    company: str
    form: str
    date_filed: date
    filename: str


def parse_master_index(raw: bytes) -> tuple[MasterIndexRow, ...]:
    """Parse SEC's pipe-delimited ``master.idx``. Fail closed on a bad row.

    This is the official catalogue of filings for one quarter, which is how real
    delisting filings are DISCOVERED instead of guessed. Malformed rows are a
    parsing failure, never silently skipped.
    """
    text = raw.decode("latin-1")
    rows: list[MasterIndexRow] = []
    started = False
    for line in text.splitlines():
        stripped = line.strip()
        if not started:
            # the header block ends with a dashed separator line
            if set(stripped) == {"-"} and len(stripped) > 10:
                started = True
            continue
        if not stripped:
            continue
        parts = stripped.split("|")
        if len(parts) != 5:
            raise SourceAdapterError(
                f"SEC master index row has {len(parts)} fields, expected 5: {stripped[:80]!r}"
            )
        cik, company, form, filed, filename = parts
        try:
            filed_on = date.fromisoformat(filed.strip())
        except ValueError as error:
            raise SourceAdapterError(
                f"unparseable SEC master index date {filed!r}: {error}"
            ) from error
        rows.append(
            MasterIndexRow(
                cik=cik.strip().zfill(10), company=company.strip(), form=form.strip(),
                date_filed=filed_on, filename=filename.strip(),
            )
        )
    if not started:
        raise SourceAdapterError("SEC master index has no header separator; fail closed")
    return tuple(rows)


def discover_delisting_candidates(
    rows: Sequence[MasterIndexRow], *, limit: int, forms: Sequence[str] = ("25", "25-NSE")
) -> tuple[MasterIndexRow, ...]:
    """A bounded, deterministic slice of real delisting filings.

    Deterministic ordering (date, CIK, accession path) keeps a qualification run
    reproducible, and ``limit`` keeps the follow-up requests qualification-scale
    rather than a bulk crawl.
    """
    if limit <= 0:
        raise SourceAdapterError("candidate limit must be positive")
    wanted = {form.upper() for form in forms}
    matching = [row for row in rows if row.form.upper() in wanted]
    matching.sort(key=lambda row: (row.date_filed, row.cik, row.filename))
    return tuple(matching[:limit])


@dataclass(frozen=True, slots=True)
class SecFilingRecord:
    """One SEC filing normalized to the fields Dataset V1 actually uses."""

    cik: str
    accession: str
    form: str
    filing_date: date
    #: SEC exposes the acceptance datetime for many filings; knowledge time.
    accepted_at: datetime | None = None
    primary_document: str | None = None
    #: SEC's published 8-K item codes (e.g. "2.01,9.01"). Structured metadata,
    #: but ambiguous about what occurred - treated as review leads, not events.
    items: str = ""

    @property
    def is_delisting_form(self) -> bool:
        return self.form in DELISTING_FORMS


def parse_sec_submissions(raw: bytes, parsed: object) -> tuple[SecFilingRecord, ...]:
    """Normalize a data.sec.gov submissions payload. Fail closed on bad shape.

    Only fields SEC genuinely provides are read; nothing is inferred. A filing
    whose date cannot be parsed is rejected rather than guessed.
    """
    if not isinstance(parsed, dict):
        raise SourceAdapterError("SEC submissions payload must be a JSON object")
    cik = str(parsed.get("cik", "")).strip()
    if not cik:
        raise SourceAdapterError("SEC submissions payload has no cik")
    recent = parsed.get("filings", {})
    if not isinstance(recent, dict):
        raise SourceAdapterError("SEC submissions payload has no filings object")
    block = recent.get("recent")
    if not isinstance(block, dict):
        raise SourceAdapterError("SEC submissions payload has no filings.recent block")
    columns: dict[str, list[object]] = {}
    for name in (
        "form", "accessionNumber", "filingDate", "acceptanceDateTime",
        "primaryDocument", "items",
    ):
        column = block.get(name)
        if column is None:
            columns[name] = []
            continue
        if not isinstance(column, list):
            raise SourceAdapterError(
                f"SEC filings.recent.{name} must be an array, got {type(column).__name__}"
            )
        columns[name] = column
    forms = columns["form"]
    accessions = columns["accessionNumber"]
    dates = columns["filingDate"]
    accepted = columns["acceptanceDateTime"]
    documents = columns["primaryDocument"]
    items = columns["items"]
    if not (len(forms) == len(accessions) == len(dates)):
        raise SourceAdapterError("SEC submissions arrays are not aligned; fail closed")
    records: list[SecFilingRecord] = []
    for index, form in enumerate(forms):
        try:
            filing_date = date.fromisoformat(str(dates[index]))
        except ValueError as error:
            raise SourceAdapterError(
                f"unparseable SEC filingDate {dates[index]!r}: {error}"
            ) from error
        accepted_at: datetime | None = None
        if index < len(accepted) and accepted[index]:
            try:
                stamp = datetime.fromisoformat(str(accepted[index]))
                accepted_at = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            except ValueError:
                accepted_at = None  # absent/odd acceptance time is recorded as unknown
        records.append(
            SecFilingRecord(
                cik=cik, accession=str(accessions[index]), form=str(form),
                filing_date=filing_date, accepted_at=accepted_at,
                primary_document=(
                    str(documents[index]) if index < len(documents) and documents[index] else None
                ),
                items=str(items[index]) if index < len(items) and items[index] else "",
            )
        )
    del raw  # the caller binds the raw checksum; parsing must not depend on it
    return tuple(records)


def combined_artifact_checksum(parts: Sequence[bytes]) -> str:
    """One digest over EVERY raw response a run actually consumed.

    Binding only the discovery index would leave the normalized records
    unauthenticated: the filings come from separate submissions responses. Each
    part is hashed, the digests sorted, then hashed again, so the result is
    stable regardless of fetch order but changes if any response changes.
    """
    if not parts:
        raise SourceAdapterError("cannot checksum an empty set of source artifacts")
    digests = sorted(hashlib.sha256(part).hexdigest() for part in parts)
    return "sha256:" + hashlib.sha256("|".join(digests).encode("ascii")).hexdigest()


def artifact_checksum(raw: bytes) -> str:
    """Checksum of the RAW source bytes (never of a re-serialized object)."""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# -- S&P public announcements (official index-owner material) -----------------


@dataclass(frozen=True, slots=True)
class IndexChangeAnnouncement:
    """One official index-change announcement, as published.

    ``announced_on`` is the knowledge time (what was publicly known, and when);
    ``effective_on`` is the event time. Both are required: an announcement
    without an effective date cannot support point-in-time membership.
    """

    index_id: str
    announced_on: date
    effective_on: date
    added_ticker: str | None
    removed_ticker: str | None
    source_url: str
    source_document_checksum: str

    def __post_init__(self) -> None:
        if self.added_ticker is None and self.removed_ticker is None:
            raise SourceAdapterError("an index-change announcement must add or remove a name")
        if self.effective_on < self.announced_on:
            raise SourceAdapterError(
                "effective_on precedes announced_on; a change cannot take effect before"
                " it was announced (fail closed)"
            )
        digest = self.source_document_checksum.removeprefix("sha256:")
        if digest == self.source_document_checksum or len(digest) != 64:
            raise SourceAdapterError("announcement requires a sha256 source checksum")
        if any(character not in "0123456789abcdef" for character in digest):
            raise SourceAdapterError("announcement source checksum is not a hex sha256 digest")


# -- IBKR reference probe -----------------------------------------------------


class IbkrReferenceCapability(StrEnum):
    """Reference capabilities an IBKR probe can empirically confirm or deny."""

    CONTRACT_IDENTITY_CONID = "CONTRACT_IDENTITY_CONID"
    HISTORICAL_BARS_TRADES = "HISTORICAL_BARS_TRADES"
    ADJUSTED_LAST = "ADJUSTED_LAST"
    INACTIVE_CONTRACT_DISCOVERY = "INACTIVE_CONTRACT_DISCOVERY"
    CORPORATE_EVENT_FEED = "CORPORATE_EVENT_FEED"
    INDEX_MEMBERSHIP_HISTORY = "INDEX_MEMBERSHIP_HISTORY"
    CLASSIFICATION_HISTORY = "CLASSIFICATION_HISTORY"


class ProbeOutcome(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    #: Present in the API but gated behind an entitlement the account lacks.
    ENTITLEMENT_REQUIRED = "ENTITLEMENT_REQUIRED"
    NOT_TESTED = "NOT_TESTED"


@dataclass(frozen=True, slots=True)
class IbkrCapabilityProbe:
    """What an owner-host IBKR run actually observed. Never assumed."""

    capability: IbkrReferenceCapability
    outcome: ProbeOutcome
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value, "outcome": self.outcome.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AdjustmentCrossCheck:
    """TRADES vs ADJUSTED_LAST comparison around a known corporate action.

    This characterizes the provider's adjustment behaviour so the Builder can
    avoid double adjustment. It is explicitly NOT an event ledger: a divergence
    is evidence that an adjustment happened, not proof of the event's terms.
    """

    symbol: str
    observed_on: date
    trades_close: str
    adjusted_last_close: str
    diverges: bool
    #: Recorded so nobody later mistakes this for event-level corporate-action data.
    inference_boundary: str = (
        "adjusted-price divergence indicates provider adjustment only; it is NOT"
        " event-level corporate-action evidence"
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol, "observed_on": self.observed_on.isoformat(),
            "trades_close": self.trades_close, "adjusted_last_close": self.adjusted_last_close,
            "diverges": self.diverges, "inference_boundary": self.inference_boundary,
        }


# -- identity crosswalk -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrosswalkEntry:
    """A stable identity and the external identifiers that map to it."""

    security_id: str
    ibkr_con_id: int | None = None
    sec_cik: str | None = None
    current_ticker: str | None = None
    historical_tickers: tuple[str, ...] = ()
    primary_exchange: str | None = None

    @property
    def has_stable_identifier(self) -> bool:
        """Ticker alone is never a stable identity."""
        return self.ibkr_con_id is not None or bool((self.sec_cik or "").strip())


@dataclass(frozen=True, slots=True)
class CrosswalkResolution:
    resolved: dict[str, str] = field(default_factory=dict)
    ambiguous: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "resolved": dict(sorted(self.resolved.items())),
            "ambiguous": list(self.ambiguous), "unresolved": list(self.unresolved),
            "conflicting": list(self.conflicting),
        }


class IdentityCrosswalk:
    """Deterministic identity resolution across IBKR conId / SEC CIK / tickers."""

    def __init__(self, entries: Sequence[CrosswalkEntry]) -> None:
        self._entries = tuple(entries)
        self._by_conid: dict[int, list[str]] = {}
        self._by_cik: dict[str, list[str]] = {}
        self._by_ticker: dict[str, list[str]] = {}
        for entry in entries:
            if not entry.has_stable_identifier:
                raise SourceAdapterError(
                    f"crosswalk entry {entry.security_id!r} has no stable identifier;"
                    " ticker alone is never historical identity"
                )
            if entry.ibkr_con_id is not None:
                self._by_conid.setdefault(entry.ibkr_con_id, []).append(entry.security_id)
            if entry.sec_cik:
                self._by_cik.setdefault(entry.sec_cik.zfill(10), []).append(entry.security_id)
            tickers = set(entry.historical_tickers)
            if entry.current_ticker:
                tickers.add(entry.current_ticker)
            for ticker in tickers:
                self._by_ticker.setdefault(ticker, []).append(entry.security_id)

    @staticmethod
    def _lookup[K](index: Mapping[K, list[str]], key: K) -> tuple[str | None, str]:
        matches = index.get(key, [])
        if not matches:
            return None, "unresolved"
        if len(set(matches)) > 1:
            return None, "ambiguous"
        return matches[0], "resolved"

    def resolve_by_cik(self, ciks: Sequence[str]) -> CrosswalkResolution:
        resolved: dict[str, str] = {}
        ambiguous: list[str] = []
        unresolved: list[str] = []
        for cik in ciks:
            security_id, status = IdentityCrosswalk._lookup(self._by_cik, str(cik).zfill(10))
            if status == "resolved" and security_id is not None:
                resolved[str(cik)] = security_id
            elif status == "ambiguous":
                ambiguous.append(str(cik))
            else:
                unresolved.append(str(cik))
        return CrosswalkResolution(
            resolved=resolved, ambiguous=tuple(ambiguous), unresolved=tuple(unresolved)
        )

    def resolve_by_conid(self, con_ids: Sequence[int]) -> CrosswalkResolution:
        resolved: dict[str, str] = {}
        ambiguous: list[str] = []
        unresolved: list[str] = []
        for con_id in con_ids:
            security_id, status = IdentityCrosswalk._lookup(self._by_conid, con_id)
            if status == "resolved" and security_id is not None:
                resolved[str(con_id)] = security_id
            elif status == "ambiguous":
                ambiguous.append(str(con_id))
            else:
                unresolved.append(str(con_id))
        return CrosswalkResolution(
            resolved=resolved, ambiguous=tuple(ambiguous), unresolved=tuple(unresolved)
        )

    def resolve_ticker_as_of(self, ticker: str) -> CrosswalkResolution:
        """Ticker resolution is ADVISORY: ambiguity is reported, never guessed."""
        security_id, status = IdentityCrosswalk._lookup(self._by_ticker, ticker)
        if status == "resolved" and security_id is not None:
            return CrosswalkResolution(resolved={ticker: security_id})
        if status == "ambiguous":
            return CrosswalkResolution(ambiguous=(ticker,))
        return CrosswalkResolution(unresolved=(ticker,))


# -- multi-source reconciliation ---------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcedFact:
    """One assertion of a fact by one source."""

    fact_key: str
    value: str
    source_id: str
    authority: SourceAuthority
    source_checksum: str
    observed_at: datetime
    known_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReconciledFact:
    fact_key: str
    value: str | None
    primary_source: str | None
    supporting_sources: tuple[str, ...]
    disagreements: tuple[str, ...]
    resolved: bool
    resolution_note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "fact_key": self.fact_key, "value": self.value,
            "primary_source": self.primary_source,
            "supporting_sources": list(self.supporting_sources),
            "disagreements": list(self.disagreements), "resolved": self.resolved,
            "resolution_note": self.resolution_note,
        }


#: Documented, owner-approved precedence: the index owner / regulator outranks a
#: secondary dataset; the owner's primary broker outranks a secondary dataset.
#: Precedence NEVER silently overrides a disagreement between two sources of the
#: SAME authority — that stays unresolved (fail closed).
_AUTHORITY_RANK: Mapping[SourceAuthority, int] = {
    SourceAuthority.OFFICIAL_PUBLIC: 3,
    SourceAuthority.OWNER_PRIMARY: 2,
    SourceAuthority.PUBLIC_SECONDARY: 1,
    SourceAuthority.SNAPSHOT_ONLY: 0,
    SourceAuthority.FIXTURE: 0,
}


def reconcile_facts(facts: Sequence[SourcedFact]) -> tuple[ReconciledFact, ...]:
    """Deterministically reconcile multi-source facts. Fail closed on ties.

    Agreement across sources is a corroborated fact. A disagreement is resolved
    ONLY by a strictly higher authority; equal-authority disagreement is left
    explicitly UNRESOLVED rather than silently picking a winner.
    """
    grouped: dict[str, list[SourcedFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.fact_key, []).append(fact)
    out: list[ReconciledFact] = []
    for fact_key, group in sorted(grouped.items()):
        values = {fact.value for fact in group}
        ordered = sorted(group, key=lambda f: (-_AUTHORITY_RANK[f.authority], f.source_id))
        if len(values) == 1:
            out.append(
                ReconciledFact(
                    fact_key=fact_key, value=ordered[0].value,
                    primary_source=ordered[0].source_id,
                    # deterministic: derived from the ORDERED list, never input order
                    supporting_sources=tuple(sorted(f.source_id for f in ordered[1:])),
                    disagreements=(), resolved=True,
                    resolution_note=(
                        "all sources agree" if len(group) > 1 else "single source"
                    ),
                )
            )
            continue
        top_rank = _AUTHORITY_RANK[ordered[0].authority]
        top = [f for f in ordered if _AUTHORITY_RANK[f.authority] == top_rank]
        disagreements = tuple(
            sorted(f"{f.source_id}={f.value}" for f in group)
        )
        if len({f.value for f in top}) == 1 and len(top) < len(group):
            out.append(
                ReconciledFact(
                    fact_key=fact_key, value=top[0].value, primary_source=top[0].source_id,
                    supporting_sources=tuple(sorted(f.source_id for f in top[1:])),
                    disagreements=disagreements, resolved=True,
                    resolution_note=(
                        "resolved by documented authority precedence"
                        f" ({top[0].authority.value} outranks the dissenting source)"
                    ),
                )
            )
        else:
            out.append(
                ReconciledFact(
                    fact_key=fact_key, value=None, primary_source=None,
                    supporting_sources=(), disagreements=disagreements, resolved=False,
                    resolution_note=(
                        "equal-authority disagreement; left UNRESOLVED (fail closed)"
                    ),
                )
            )
    return tuple(out)


def reconciliation_report(reconciled: Sequence[ReconciledFact]) -> dict[str, object]:
    return {
        "sources_version": SOURCES_VERSION,
        "total_facts": len(reconciled),
        "resolved": sum(1 for fact in reconciled if fact.resolved),
        "unresolved": sum(1 for fact in reconciled if not fact.resolved),
        "facts": [fact.to_dict() for fact in reconciled],
    }


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# -- real transport (owner host only; never exercised in CI) ------------------


class UrllibHttpFetcher:
    """Real read-only HTTP transport built on the stdlib.

    Used ONLY by the owner-host runner. Unit tests inject a recorded fetcher and
    never reach this class, so CI stays offline and deterministic.
    """

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = timeout_seconds

    def get_bytes(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        return self._fetch(url, headers)[0]

    def get_json(self, url: str, *, headers: Mapping[str, str]) -> tuple[bytes, object]:
        raw = self._fetch(url, headers)[0]
        try:
            parsed: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SourceAdapterError(f"{url} did not return valid JSON: {error}") from error
        return raw, parsed

    def _fetch(self, url: str, headers: Mapping[str, str]) -> tuple[bytes, int]:
        import gzip
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                status = int(response.status)
                raw = bytes(response.read())
                encoding = str(response.headers.get("Content-Encoding", "")).lower()
        except urllib.error.HTTPError as error:
            raise SourceAdapterError(f"{url} returned HTTP {error.code}") from error
        except OSError as error:
            raise SourceAdapterError(f"{url} could not be retrieved: {error}") from error
        if status != 200:
            raise SourceAdapterError(f"{url} returned HTTP {status}")
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        return raw, status


# -- SEC pagination + ticker/CIK mapping --------------------------------------


def sec_filing_shards(parsed: object) -> tuple[str, ...]:
    """Older filing shards referenced by a submissions payload (``filings.files``).

    SEC keeps only the most recent filings inline; the remainder live in
    additional documents. Ignoring them would silently truncate history, so the
    caller must fetch these too.
    """
    if not isinstance(parsed, dict):
        raise SourceAdapterError("SEC submissions payload must be a JSON object")
    filings = parsed.get("filings")
    if not isinstance(filings, dict):
        return ()
    files = filings.get("files")
    if files is None:
        return ()
    if not isinstance(files, list):
        raise SourceAdapterError("SEC filings.files must be an array")
    names: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise SourceAdapterError("SEC filings.files entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SourceAdapterError("SEC filings.files entry has no name")
        names.append(name.strip())
    return tuple(names)


def parse_sec_filing_shard(raw: bytes, parsed: object, *, cik: str) -> tuple[SecFilingRecord, ...]:
    """Normalize an older SEC filing shard (columns at the top level)."""
    if not isinstance(parsed, dict):
        raise SourceAdapterError("SEC filing shard must be a JSON object")
    wrapped = {"cik": cik, "filings": {"recent": parsed}}
    return parse_sec_submissions(raw, wrapped)


@dataclass(frozen=True, slots=True)
class SecEntityProfile:
    """Identity SEC itself publishes for a filer — never invented locally.

    Building the crosswalk from this rather than from a hand-written mapping
    means the authoritative source supplies the identity, so a wrong local guess
    cannot enter the dataset.
    """

    cik: str
    name: str
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]
    former_names: tuple[str, ...]

    @property
    def security_id(self) -> str:
        """A stable identity that is NOT a ticker and survives delisting."""
        return f"sec-cik:{self.cik}"


def parse_sec_entity_profile(raw: bytes, parsed: object) -> SecEntityProfile:
    """Extract filer identity from a submissions payload. Fail closed on shape."""
    del raw
    if not isinstance(parsed, dict):
        raise SourceAdapterError("SEC submissions payload must be a JSON object")
    cik = str(parsed.get("cik", "")).strip()
    if not cik:
        raise SourceAdapterError("SEC submissions payload has no cik")

    def _string_list(key: str) -> tuple[str, ...]:
        value = parsed.get(key)
        if value is None:
            return ()
        if not isinstance(value, list):
            raise SourceAdapterError(f"SEC {key} must be an array")
        return tuple(str(item).strip() for item in value if str(item).strip())

    former = parsed.get("formerNames")
    former_names: tuple[str, ...] = ()
    if isinstance(former, list):
        former_names = tuple(
            str(entry.get("name", "")).strip()
            for entry in former
            if isinstance(entry, dict) and str(entry.get("name", "")).strip()
        )
    return SecEntityProfile(
        cik=cik.zfill(10),
        name=str(parsed.get("name", "")).strip(),
        tickers=tuple(t.upper() for t in _string_list("tickers")),
        exchanges=_string_list("exchanges"),
        former_names=former_names,
    )


@dataclass(frozen=True, slots=True)
class SecTickerRecord:
    """One SEC ticker↔CIK mapping row (identity, not price data)."""

    cik: str
    ticker: str
    title: str


def parse_sec_company_tickers(raw: bytes, parsed: object) -> tuple[SecTickerRecord, ...]:
    """Normalize SEC's company_tickers.json. Fail closed on an unexpected shape."""
    del raw
    if not isinstance(parsed, dict):
        raise SourceAdapterError("SEC company_tickers payload must be a JSON object")
    records: list[SecTickerRecord] = []
    for key, entry in sorted(parsed.items()):
        if not isinstance(entry, dict):
            raise SourceAdapterError(f"SEC company_tickers entry {key!r} must be an object")
        cik_value = entry.get("cik_str")
        ticker = entry.get("ticker")
        if cik_value is None or not isinstance(ticker, str) or not ticker.strip():
            raise SourceAdapterError(f"SEC company_tickers entry {key!r} is missing cik/ticker")
        records.append(
            SecTickerRecord(
                cik=str(cik_value).zfill(10),
                ticker=ticker.strip().upper(),
                title=str(entry.get("title", "")).strip(),
            )
        )
    return tuple(records)


# -- canonical corporate events ----------------------------------------------


class CorporateEventType(StrEnum):
    """Event-level corporate actions Dataset V1 recognises."""

    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    DIVIDEND = "DIVIDEND"
    SPECIAL_DIVIDEND = "SPECIAL_DIVIDEND"
    MERGER = "MERGER"
    ACQUISITION = "ACQUISITION"
    SPINOFF = "SPINOFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"
    DEREGISTRATION = "DEREGISTRATION"


#: SEC forms mapped to the event they genuinely evidence. Nothing else is
#: inferred: a form that does not by itself establish an event is not listed.
SEC_FORM_EVENTS: Mapping[str, CorporateEventType] = {
    "25": CorporateEventType.DELISTING,
    "25-NSE": CorporateEventType.DELISTING,
    "15-12B": CorporateEventType.DEREGISTRATION,
    "15-12G": CorporateEventType.DEREGISTRATION,
    "15-15D": CorporateEventType.DEREGISTRATION,
}


@dataclass(frozen=True, slots=True)
class CanonicalCorporateEvent:
    """One event-level corporate action with full provenance.

    Every event must carry a stable identity, at least one real date, and a
    source checksum. An event whose only support is an adjusted-price
    divergence is NOT constructible here — see ``AdjustmentCrossCheck``.
    """

    event_id: str
    security_id: str
    event_type: CorporateEventType
    source_id: str
    source_authority: SourceAuthority
    source_checksum: str
    provenance: str
    effective_on: date | None = None
    ex_date: date | None = None
    filing_date: date | None = None
    known_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.security_id.strip():
            raise SourceAdapterError("a corporate event requires a stable security_id")
        if not (self.effective_on or self.ex_date or self.filing_date):
            raise SourceAdapterError(
                f"corporate event {self.event_id!r} has no effective/ex/filing date;"
                " an undated event cannot be placed on a timeline (fail closed)"
            )
        digest = self.source_checksum.removeprefix("sha256:")
        if digest == self.source_checksum or len(digest) != 64:
            raise SourceAdapterError(
                f"corporate event {self.event_id!r} requires a sha256 source checksum"
            )
        if not self.provenance.strip():
            raise SourceAdapterError(
                f"corporate event {self.event_id!r} requires event-level provenance"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "security_id": self.security_id,
            "event_type": self.event_type.value,
            "source_id": self.source_id,
            "source_authority": self.source_authority.value,
            "source_checksum": self.source_checksum,
            "provenance": self.provenance,
            "effective_on": self.effective_on.isoformat() if self.effective_on else None,
            "ex_date": self.ex_date.isoformat() if self.ex_date else None,
            "filing_date": self.filing_date.isoformat() if self.filing_date else None,
            "known_at": self.known_at.isoformat() if self.known_at else None,
        }


#: XBRL concepts that publish a real corporate-action fact.
XBRL_DIVIDEND_CONCEPTS: tuple[str, ...] = (
    "CommonStockDividendsPerShareDeclared",
    "CommonStockDividendsPerShareCashPaid",
)
XBRL_SPLIT_CONCEPTS: tuple[str, ...] = (
    "StockholdersEquityNoteStockSplitConversionRatio1",
)


@dataclass(frozen=True, slots=True)
class XbrlFact:
    """One reported XBRL fact, with its period and its knowledge time."""

    concept: str
    unit: str
    value: str
    period_start: date | None
    period_end: date | None
    #: The date SEC received the filing that reported it - the knowledge axis.
    filed_on: date
    accession: str
    form: str

    def to_dict(self) -> dict[str, object]:
        return {
            "concept": self.concept, "unit": self.unit, "value": self.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "filed_on": self.filed_on.isoformat(), "accession": self.accession,
            "form": self.form,
        }


def _optional_xbrl_date(row: Mapping[str, object], key: str) -> date | None:
    value = row.get(key)
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise SourceAdapterError(f"unparseable XBRL {key} {value!r}: {error}") from error


def parse_xbrl_concept(raw: bytes, parsed: object) -> tuple[XbrlFact, ...]:
    """Normalize an XBRL companyconcept payload. Fail closed on a bad shape."""
    del raw
    if not isinstance(parsed, dict):
        raise SourceAdapterError("XBRL concept payload must be a JSON object")
    concept = str(parsed.get("tag", "")).strip()
    if not concept:
        raise SourceAdapterError("XBRL concept payload has no tag")
    units = parsed.get("units")
    if not isinstance(units, dict):
        raise SourceAdapterError("XBRL concept payload has no units object")
    facts: list[XbrlFact] = []
    for unit, entries in sorted(units.items()):
        if not isinstance(entries, list):
            raise SourceAdapterError(f"XBRL unit {unit!r} must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                raise SourceAdapterError(f"XBRL fact in unit {unit!r} must be an object")
            if "val" not in entry or "filed" not in entry or "accn" not in entry:
                raise SourceAdapterError(
                    f"XBRL fact in unit {unit!r} is missing val/filed/accn; fail closed"
                )
            try:
                filed_on = date.fromisoformat(str(entry["filed"]))
            except ValueError as error:
                raise SourceAdapterError(
                    f"unparseable XBRL filed date {entry['filed']!r}: {error}"
                ) from error
            facts.append(
                XbrlFact(
                    concept=concept, unit=str(unit), value=str(entry["val"]),
                    period_start=_optional_xbrl_date(entry, "start"),
                    period_end=_optional_xbrl_date(entry, "end"),
                    filed_on=filed_on, accession=str(entry["accn"]),
                    form=str(entry.get("form", "")),
                )
            )
    return tuple(sorted(facts, key=lambda f: (f.filed_on, f.accession, f.unit, f.value)))


def xbrl_facts_to_events(
    facts: Sequence[XbrlFact],
    *,
    security_id: str,
    source_id: str,
    source_checksum: str,
) -> tuple[CanonicalCorporateEvent, ...]:
    """Turn reported XBRL facts into canonical events with real provenance.

    Deliberately conservative:

    * a dividend's EX-DATE is not published by XBRL, so ``effective_on`` stays
      unset and the reported period is recorded as provenance instead. The
      filing date is a real date and anchors the event on the timeline.
    * a split ratio of exactly 1 is not a split and produces no event.
    * duplicate facts (the same concept, value and accession restated across
      filings) collapse to one event.
    """
    events: dict[str, CanonicalCorporateEvent] = {}
    for fact in facts:
        if fact.concept in XBRL_DIVIDEND_CONCEPTS:
            try:
                amount = Decimal(fact.value)
            except (ArithmeticError, ValueError):
                raise SourceAdapterError(
                    f"unparseable XBRL dividend value {fact.value!r}"
                ) from None
            if amount <= 0:
                continue
            event_type = CorporateEventType.DIVIDEND
            detail = f"{fact.concept}={amount} {fact.unit}"
        elif fact.concept in XBRL_SPLIT_CONCEPTS:
            try:
                ratio = Decimal(fact.value)
            except (ArithmeticError, ValueError):
                raise SourceAdapterError(
                    f"unparseable XBRL split ratio {fact.value!r}"
                ) from None
            if ratio == 1:
                continue  # a 1:1 "split" is not an event
            event_type = (
                CorporateEventType.SPLIT if ratio > 1 else CorporateEventType.REVERSE_SPLIT
            )
            detail = f"{fact.concept} ratio={ratio}"
        else:
            continue
        start = fact.period_start.isoformat() if fact.period_start else "?"
        end = fact.period_end.isoformat() if fact.period_end else "?"
        period = f" period {start}..{end}" if (fact.period_start or fact.period_end) else ""
        event_id = f"xbrl:{fact.accession}:{fact.concept}:{fact.value}"
        events[event_id] = CanonicalCorporateEvent(
            event_id=event_id,
            security_id=security_id,
            event_type=event_type,
            source_id=source_id,
            source_authority=SourceAuthority.OFFICIAL_PUBLIC,
            source_checksum=source_checksum,
            provenance=(
                f"SEC XBRL {detail} reported in {fact.form or 'filing'}"
                f" accession {fact.accession}{period}"
            ),
            filing_date=fact.filed_on,
        )
    return tuple(sorted(events.values(), key=lambda event: event.event_id))


def sec_filings_to_events(
    filings: Sequence[SecFilingRecord],
    *,
    security_id_by_cik: Mapping[str, str],
    source_id: str,
    source_checksum: str,
) -> tuple[CanonicalCorporateEvent, ...]:
    """Convert SEC filings into canonical events. Unmapped identity is skipped.

    Only forms in ``SEC_FORM_EVENTS`` produce events. The effective date is
    deliberately left unset: Form 25 states a filing date, and the delisting's
    effectiveness follows a statutory period that this parser does NOT compute.
    Guessing it would manufacture a date the source never published.
    """
    events: list[CanonicalCorporateEvent] = []
    for filing in filings:
        event_type = SEC_FORM_EVENTS.get(filing.form)
        if event_type is None:
            continue
        security_id = security_id_by_cik.get(filing.cik.zfill(10))
        if not security_id:
            continue  # unresolved identity is reported by the crosswalk, never guessed
        events.append(
            CanonicalCorporateEvent(
                event_id=f"sec:{filing.accession}",
                security_id=security_id,
                event_type=event_type,
                source_id=source_id,
                source_authority=SourceAuthority.OFFICIAL_PUBLIC,
                source_checksum=source_checksum,
                provenance=f"SEC {filing.form} accession {filing.accession}",
                filing_date=filing.filing_date,
                known_at=filing.accepted_at,
            )
        )
    return tuple(sorted(events, key=lambda event: (event.event_id, event.security_id)))


__all__ = [
    "DELISTING_FORMS",
    "SEC_FORM_EVENTS",
    "SEC_MIN_REQUEST_INTERVAL_SECONDS",
    "XBRL_DIVIDEND_CONCEPTS",
    "XBRL_SPLIT_CONCEPTS",
    "SOURCES_VERSION",
    "AdjustmentCrossCheck",
    "CanonicalCorporateEvent",
    "CorporateEventType",
    "CrosswalkEntry",
    "CrosswalkResolution",
    "HttpBytesFetcher",
    "HttpFetcher",
    "IbkrCapabilityProbe",
    "IbkrReferenceCapability",
    "IdentityCrosswalk",
    "IndexChangeAnnouncement",
    "MasterIndexRow",
    "ProbeOutcome",
    "ReconciledFact",
    "SecEdgarClient",
    "SecEntityProfile",
    "SecFilingRecord",
    "SecTickerRecord",
    "XbrlFact",
    "SourceAdapterError",
    "SourceAuthority",
    "SourcedFact",
    "UrllibHttpFetcher",
    "artifact_checksum",
    "canonical_json",
    "combined_artifact_checksum",
    "discover_delisting_candidates",
    "parse_master_index",
    "parse_sec_company_tickers",
    "parse_sec_entity_profile",
    "parse_sec_filing_shard",
    "parse_sec_submissions",
    "parse_xbrl_concept",
    "reconcile_facts",
    "reconciliation_report",
    "sec_filing_shards",
    "sec_filings_to_events",
    "xbrl_facts_to_events",
]
