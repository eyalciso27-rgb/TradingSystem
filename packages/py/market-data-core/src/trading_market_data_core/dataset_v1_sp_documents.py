"""Owner-supplied official S&P announcement documents: discovery and parsing.

Automated retrieval from the publisher stays off the table — a plain automated
request to spglobal.com returns HTTP 403, including for robots.txt, and
circumventing bot protection is not permitted. What IS permitted is the owner
downloading the official announcements themselves.

This module removes the manual manifest-authoring step: drop the official
documents into one directory, run the single owner command, and the runner
discovers them, checksums them, parses the announced/effective dates and the
added/removed securities, and generates the manifest itself.

Extraction rules, same discipline as the Form 25 parser:

* only values next to an EXPLICIT label are read;
* two different dates under one label is ambiguity and fails closed;
* a document that yields no add and no remove is reported, never guessed at;
* every parsed field keeps its label and the exact source snippet;
* the raw document is never copied into the repository — only its digest, name
  and the parsed fields travel.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_form25_html import html_to_text
from trading_market_data_core.dataset_v1_run import file_checksum
from trading_market_data_core.dataset_v1_sources import SourceAdapterError

SP_DOCUMENTS_VERSION = "dataset-v1-sp-documents-1.0.0"

#: Extensions the owner may legitimately supply. PDFs are accepted only when a
#: text layer can be extracted with the stdlib-free path below; a scanned image
#: is reported as unparsed rather than guessed at.
SUPPORTED_SUFFIXES: tuple[str, ...] = (".htm", ".html", ".txt", ".csv", ".tsv", ".json", ".pdf")

ANNOUNCED_LABELS: tuple[str, ...] = (
    "announcement date", "announced", "date of announcement", "press release date",
)
EFFECTIVE_LABELS: tuple[str, ...] = (
    "effective date", "effective prior to the open", "will be effective",
    "effective before the open of trading", "effective at the open",
)
ADD_LABELS: tuple[str, ...] = ("will replace", "will be added", "addition", "joins", "added:")
REMOVE_LABELS: tuple[str, ...] = ("will be removed", "removal", "removed:", "will replace")

_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), "%Y-%m-%d"),
    (re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"), "%m/%d/%Y"),
    (
        re.compile(
            r"\b((?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},\s*\d{4})\b",
            re.IGNORECASE,
        ),
        "%B %d, %Y",
    ),
)
#: A ticker as it appears in an official announcement, e.g. "(NYSE: ABC)".
_TICKER_RE = re.compile(
    r"\((?:NYSE|NASDAQ|NYSE American|Cboe|BATS)[^)]*?:\s*([A-Z][A-Z.\-]{0,6})\)"
)
_LABEL_WINDOW = 200


@dataclass(frozen=True, slots=True)
class SpFieldMatch:
    field_name: str
    label: str
    raw_value: str
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field_name, "label": self.label,
            "raw_value": self.raw_value, "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class SpDocument:
    """One owner-supplied official announcement, parsed."""

    filename: str
    document_checksum: str
    content_kind: str
    announced_on: date | None = None
    effective_on: date | None = None
    added_tickers: tuple[str, ...] = ()
    removed_tickers: tuple[str, ...] = ()
    matches: tuple[SpFieldMatch, ...] = ()
    ambiguous_fields: tuple[str, ...] = ()
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_usable(self) -> bool:
        """Enough explicit facts to place a membership change on the timeline."""
        return (
            self.announced_on is not None
            and self.effective_on is not None
            and bool(self.added_tickers or self.removed_tickers)
            and not self.ambiguous_fields
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "document_checksum": self.document_checksum,
            "content_kind": self.content_kind,
            "announced_on": self.announced_on.isoformat() if self.announced_on else None,
            "effective_on": self.effective_on.isoformat() if self.effective_on else None,
            "added_tickers": list(self.added_tickers),
            "removed_tickers": list(self.removed_tickers),
            "matches": [m.to_dict() for m in self.matches],
            "ambiguous_fields": list(self.ambiguous_fields),
            "usable": self.is_usable,
            "findings": list(self.findings),
        }


def _pdf_text(raw: bytes) -> str:
    """Best-effort text from a PDF's uncompressed text operators.

    Deliberately minimal and dependency-free: it recovers text from documents
    that carry a plain text layer and returns nothing for compressed or scanned
    ones, which are then reported as unparsed rather than guessed at.
    """
    chunks: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^\\()])*\)", raw):
        token = match.group(0)[1:-1]
        try:
            chunks.append(token.decode("latin-1"))
        except UnicodeDecodeError:  # pragma: no cover - defensive
            continue
    return unicodedata.normalize("NFKC", " ".join(chunks))


def document_text(path: Path) -> tuple[str, str]:
    """Return (text, content_kind) for a supported document."""
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(raw), "pdf"
    if suffix in (".htm", ".html"):
        text, _, _ = html_to_text(raw)
        return text, "html"
    if suffix in (".csv", ".tsv", ".json", ".txt"):
        return raw.decode("utf-8", errors="replace"), suffix.lstrip(".")
    raise SourceAdapterError(f"unsupported S&P document type: {path.name}")


def _parse_date(token: str) -> date | None:
    for pattern, fmt in _DATE_PATTERNS:
        found = pattern.search(token)
        if found:
            try:
                return datetime.strptime(found.group(1), fmt).date()  # noqa: DTZ007
            except ValueError:
                continue
    return None


def _dates_after(text: str, labels: Sequence[str]) -> list[tuple[date, str, str]]:
    results: list[tuple[date, str, str]] = []
    lowered = text.lower()
    for label in labels:
        start = 0
        while True:
            index = lowered.find(label, start)
            if index == -1:
                break
            start = index + len(label)
            window = text[start : start + _LABEL_WINDOW]
            parsed = _parse_date(window)
            if parsed is not None:
                snippet = " ".join((text[index:start] + window).split())[:220]
                results.append((parsed, label, snippet))
    return results


def parse_sp_document(path: Path) -> SpDocument:
    """Parse one owner-supplied official announcement. Explicit labels only."""
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise SourceAdapterError(f"unsupported S&P document type: {path.name}")
    text, content_kind = document_text(path)
    digest = file_checksum(path)
    findings: list[str] = []
    matches: list[SpFieldMatch] = []
    ambiguous: list[str] = []

    if not text.strip():
        findings.append("document produced no extractable text (scanned or compressed)")
        return SpDocument(
            filename=path.name, document_checksum=digest, content_kind=content_kind,
            findings=tuple(findings),
        )

    def _one_date(field_name: str, labels: Sequence[str]) -> date | None:
        found = _dates_after(text, labels)
        if not found:
            return None
        distinct = {item[0] for item in found}
        if len(distinct) > 1:
            ambiguous.append(field_name)
            findings.append(
                f"{field_name}: {sorted(d.isoformat() for d in distinct)} both follow an"
                " explicit label; failing closed"
            )
            return None
        parsed, label, snippet = found[0]
        matches.append(SpFieldMatch(field_name, label, parsed.isoformat(), snippet))
        return parsed

    announced_on = _one_date("announced_on", ANNOUNCED_LABELS)
    effective_on = _one_date("effective_on", EFFECTIVE_LABELS)

    added: list[str] = []
    removed: list[str] = []
    lowered = text.lower()
    for label in ADD_LABELS:
        index = lowered.find(label)
        if index == -1:
            continue
        window = text[max(0, index - _LABEL_WINDOW) : index + _LABEL_WINDOW]
        tickers = _TICKER_RE.findall(window)
        # "X will replace Y" - the first ticker is the addition, the second the removal
        if label == "will replace" and len(tickers) >= 2:
            added.append(tickers[0])
            removed.append(tickers[1])
            matches.append(
                SpFieldMatch("replacement", label, f"{tickers[0]} replaces {tickers[1]}",
                             " ".join(window.split())[:220])
            )
        elif tickers:
            added.append(tickers[0])
            matches.append(
                SpFieldMatch("added_ticker", label, tickers[0], " ".join(window.split())[:220])
            )
    for label in REMOVE_LABELS:
        if label == "will replace":
            continue
        index = lowered.find(label)
        if index == -1:
            continue
        window = text[max(0, index - _LABEL_WINDOW) : index + _LABEL_WINDOW]
        tickers = _TICKER_RE.findall(window)
        if tickers:
            removed.append(tickers[0])
            matches.append(
                SpFieldMatch("removed_ticker", label, tickers[0], " ".join(window.split())[:220])
            )

    if not added and not removed:
        findings.append("no explicit addition or removal was found in this document")
    if announced_on is None and "announced_on" not in ambiguous:
        findings.append("no explicit announcement-date label was found")
    if effective_on is None and "effective_on" not in ambiguous:
        findings.append("no explicit effective-date label was found")

    return SpDocument(
        filename=path.name,
        document_checksum=digest,
        content_kind=content_kind,
        announced_on=announced_on,
        effective_on=effective_on,
        added_tickers=tuple(dict.fromkeys(added)),
        removed_tickers=tuple(dict.fromkeys(removed)),
        matches=tuple(matches),
        ambiguous_fields=tuple(sorted(set(ambiguous))),
        findings=tuple(findings),
    )


@dataclass(frozen=True, slots=True)
class SpDiscovery:
    """What the owner's document directory actually contains."""

    directory: str
    documents: tuple[SpDocument, ...] = ()
    unsupported: tuple[str, ...] = ()

    @property
    def usable(self) -> tuple[SpDocument, ...]:
        return tuple(doc for doc in self.documents if doc.is_usable)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": SP_DOCUMENTS_VERSION,
            "directory": self.directory,
            "documents": [doc.to_dict() for doc in self.documents],
            "usable": len(self.usable),
            "unsupported": list(self.unsupported),
        }

    def summary_lines(self) -> tuple[str, ...]:
        return (
            f"documents found          : {len(self.documents)}",
            f"documents usable         : {len(self.usable)}",
            f"unsupported files        : {len(self.unsupported)}",
            *(
                f"  {doc.filename}: announced={doc.announced_on} effective={doc.effective_on}"
                f" +{list(doc.added_tickers)} -{list(doc.removed_tickers)}"
                f"{' AMBIGUOUS' if doc.ambiguous_fields else ''}"
                for doc in self.documents
            ),
        )


def discover_sp_documents(directory: Path) -> SpDiscovery:
    """Parse every supported document in the owner's directory."""
    if not directory.is_dir():
        return SpDiscovery(directory=str(directory))
    documents: list[SpDocument] = []
    unsupported: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            unsupported.append(path.name)
            continue
        documents.append(parse_sp_document(path))
    return SpDiscovery(
        directory=str(directory), documents=tuple(documents), unsupported=tuple(unsupported)
    )


def build_manifest_payload(
    discovery: SpDiscovery,
    *,
    index_id: str,
    baseline: Mapping[str, object] | None = None,
    security_id_by_ticker: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Generate the membership manifest from the discovered documents.

    The owner no longer authors JSON by hand. Only USABLE documents contribute;
    the rest are visible in the discovery report with their reasons.
    """
    usable = discovery.usable
    if not usable:
        raise SourceAdapterError(
            "no usable official S&P document was found; nothing can be qualified"
        )
    effective_dates = sorted(doc.effective_on for doc in usable if doc.effective_on)
    mapping = dict(security_id_by_ticker or {})
    for doc in usable:
        for ticker in (*doc.added_tickers, *doc.removed_tickers):
            mapping.setdefault(ticker.upper(), f"sp-ticker:{ticker.upper()}")
    announcements = [
        {
            "announced_on": doc.announced_on.isoformat() if doc.announced_on else None,
            "effective_on": doc.effective_on.isoformat() if doc.effective_on else None,
            "added_ticker": doc.added_tickers[0] if doc.added_tickers else None,
            "removed_ticker": doc.removed_tickers[0] if doc.removed_tickers else None,
            "source_url": f"owner-supplied:{doc.filename}",
            "source_document_checksum": doc.document_checksum,
            "document_path": doc.filename,
        }
        for doc in usable
    ]
    return {
        "index_id": index_id,
        "generated_by": SP_DOCUMENTS_VERSION,
        "coverage": {
            "start": effective_dates[0].isoformat(),
            "end": effective_dates[-1].isoformat(),
        },
        "baseline": dict(baseline)
        if baseline
        else {
            "as_of": effective_dates[0].isoformat(),
            "security_ids": sorted(set(mapping.values())),
            "source_url": "owner-supplied baseline",
            "source_document_checksum": discovery.documents[0].document_checksum,
            "_note": (
                "Derived from the supplied documents. Replace with an official"
                " point-in-time constituent list to qualify a real window."
            ),
        },
        "security_id_by_ticker": mapping,
        "announcements": announcements,
    }


OWNER_INPUT_MESSAGE = (
    "S&P MEMBERSHIP INPUT REQUIRED: place official S&P announcement documents in"
    " config/data-sources/sp500-documents/ (HTML, text, CSV or a text-layer PDF"
    " downloaded from the official source) and rerun"
    " `bash scripts/run_dataset_v1_real_qualification.sh`."
    " The runner will checksum, parse and build the manifest itself."
)


__all__ = [
    "ADD_LABELS",
    "ANNOUNCED_LABELS",
    "EFFECTIVE_LABELS",
    "OWNER_INPUT_MESSAGE",
    "REMOVE_LABELS",
    "SP_DOCUMENTS_VERSION",
    "SUPPORTED_SUFFIXES",
    "SpDiscovery",
    "SpDocument",
    "SpFieldMatch",
    "build_manifest_payload",
    "discover_sp_documents",
    "document_text",
    "parse_sp_document",
]
