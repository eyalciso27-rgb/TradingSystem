"""SEC Form 25 / 25-NSE primary-document ingestion.

The owner-host run proved delisting FILINGS but left
``delisting_effective_date_published`` at 0/36: the submissions metadata carries
a filing date and an acceptance time, never a delisting effective date. That
date, when it exists at all, lives in the primary document.

This module reads the primary document and extracts ONLY fields the document
explicitly publishes. Three rules make that trustworthy:

1. **Nothing is computed.** SEC Rule 12d2-2 makes a delisting effective ten days
   after filing, but that is a statutory rule, not a published per-filing fact.
   Deriving it here would manufacture a date the source never stated, so a
   filing without an explicit date yields ``None`` and the sub-capability
   reports PARTIAL.
2. **Every extracted value carries provenance** — the exact element it came
   from and its raw text — so any parse can be audited against the document.
3. **Unrecognised documents are diagnosed, not guessed.** The observed element
   names are recorded, so the allow-list can be widened from REAL documents
   instead of from assumptions about the schema.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree  # noqa: S405 - hardened below; see _parse_xml

from trading_market_data_core.dataset_v1_sources import (
    SecFilingRecord,
    SourceAdapterError,
    artifact_checksum,
)

FORM25_PARSER_VERSION = "dataset-v1-form25-1.0.0"

#: Element names (namespace-stripped, lowercased) that genuinely publish the
#: date a delisting takes effect. Extend ONLY from a real observed document.
EFFECTIVE_DATE_TAGS: tuple[str, ...] = (
    "dateofdelisting",
    "delistingdate",
    "effectivedate",
    "dateeffective",
    "effectivedateofdelisting",
)
#: Element names that publish the last day the security traded.
LAST_TRADING_DATE_TAGS: tuple[str, ...] = (
    "lasttradingdate",
    "lasttradedate",
    "datelasttraded",
    "lastdayoftrading",
)
ISSUER_TAGS: tuple[str, ...] = ("issuernm", "issuername", "issuer")
EXCHANGE_TAGS: tuple[str, ...] = ("exchangenm", "exchangename", "exchange")
RULE_TAGS: tuple[str, ...] = ("rule", "ruleprovision", "ruleprovisions")

_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%Y%m%d",
)


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    """Where one parsed field actually came from, so a parse can be audited."""

    field_name: str
    source_element: str
    raw_value: str

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field_name,
            "source_element": self.source_element,
            "raw_value": self.raw_value,
        }


@dataclass(frozen=True, slots=True)
class Form25Document:
    """One Form 25 primary document, normalized with full provenance."""

    cik: str
    accession: str
    form: str
    primary_document: str
    document_checksum: str
    effective_on: date | None = None
    last_trading_on: date | None = None
    issuer_name: str | None = None
    exchange: str | None = None
    rule_provision: str | None = None
    provenance: tuple[FieldProvenance, ...] = ()
    #: Element names seen in the document — the input for widening the
    #: allow-list from real filings rather than from assumptions.
    observed_elements: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()

    @property
    def publishes_effective_date(self) -> bool:
        return self.effective_on is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "cik": self.cik,
            "accession": self.accession,
            "form": self.form,
            "primary_document": self.primary_document,
            "document_checksum": self.document_checksum,
            "effective_on": self.effective_on.isoformat() if self.effective_on else None,
            "last_trading_on": (
                self.last_trading_on.isoformat() if self.last_trading_on else None
            ),
            "issuer_name": self.issuer_name,
            "exchange": self.exchange,
            "rule_provision": self.rule_provision,
            "provenance": [item.to_dict() for item in self.provenance],
            "observed_elements": list(self.observed_elements),
            "findings": list(self.findings),
            "parser_version": FORM25_PARSER_VERSION,
        }


def primary_document_url(filing: SecFilingRecord) -> str:
    """The canonical archive URL of a filing's primary document.

    SEC's archive path uses the accession number without dashes as the folder.
    """
    if not filing.primary_document:
        raise SourceAdapterError(
            f"filing {filing.accession} has no primaryDocument; cannot locate a document"
        )
    folder = filing.accession.replace("-", "")
    cik_plain = filing.cik.lstrip("0") or "0"
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{folder}/"
        f"{filing.primary_document}"
    )



def assert_document_correlates(url: str, filing: SecFilingRecord) -> None:
    """A document must belong to the filing it is being attached to.

    Attaching another filing's document would bind a lifecycle assertion to the
    wrong security, so the archive path is checked against the filing's own CIK,
    accession folder and primaryDocument name.
    """
    folder = filing.accession.replace("-", "")
    cik_plain = filing.cik.lstrip("0") or "0"
    expected = f"/data/{cik_plain}/{folder}/{filing.primary_document}"
    if not url.endswith(expected):
        raise SourceAdapterError(
            f"document {url} does not correlate with filing {filing.accession}"
            f" (CIK {filing.cik}); expected path ending {expected}"
        )


def _parse_date(raw: str) -> date | None:
    """Parse only unambiguous published formats. An odd value is NOT guessed."""
    from datetime import datetime as _dt

    text = raw.strip()
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        try:
            return _dt.strptime(text, pattern).date()  # noqa: DTZ007 - a date, not a time
        except ValueError:
            continue
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].strip().lower()


def _parse_xml(raw: bytes) -> ElementTree.Element | None:
    """Parse XML defensively, or return None when the payload is not XML.

    Entity declarations are refused outright: expanding them on a remote
    document is an attack surface with no legitimate use in a Form 25.
    """
    head = raw[:4096].lstrip()
    if not head.startswith(b"<"):
        return None
    lowered = raw[:8192].lower()
    if b"<!entity" in lowered or b"<!doctype" in lowered:
        raise SourceAdapterError(
            "Form 25 document declares a DTD/ENTITY; refusing to parse it (fail closed)"
        )
    try:
        return ElementTree.fromstring(raw)  # noqa: S314 - guarded above
    except ElementTree.ParseError:
        return None


def _text_of(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).strip()


def parse_form25_document(
    raw: bytes,
    *,
    filing: SecFilingRecord,
    effective_date_tags: Sequence[str] = EFFECTIVE_DATE_TAGS,
    last_trading_date_tags: Sequence[str] = LAST_TRADING_DATE_TAGS,
) -> Form25Document:
    """Normalize a Form 25 primary document. Extracts only published facts.

    A document whose schema this parser does not recognise yields no dates and
    records the element names it saw, so the gap is actionable rather than
    silently filled in.
    """
    if not raw:
        raise SourceAdapterError(f"empty Form 25 document for {filing.accession}")
    if not filing.primary_document:
        raise SourceAdapterError(
            f"filing {filing.accession} declares no primaryDocument; a document"
            " cannot be correlated to it (fail closed)"
        )
    if not filing.cik.strip():
        raise SourceAdapterError(
            f"filing {filing.accession} has no CIK; refusing to attach a document"
        )

    checksum = artifact_checksum(raw)
    root = _parse_xml(raw)
    findings: list[str] = []
    provenance: list[FieldProvenance] = []
    values: dict[str, str] = {}
    observed: list[str] = []

    if root is None:
        findings.append(
            "primary document is not XML; no structured field was extracted"
            " (no date is inferred from an unparsed document)"
        )
        return Form25Document(
            cik=filing.cik, accession=filing.accession, form=filing.form,
            primary_document=filing.primary_document or "", document_checksum=checksum,
            findings=tuple(findings),
        )

    for element in root.iter():
        name = _local_name(element.tag)
        observed.append(name)
        text = _text_of(element)
        if text and name not in values:
            values[name] = text

    def _first(tags: Sequence[str]) -> tuple[str | None, str | None]:
        for tag in tags:
            if tag in values:
                return values[tag], tag
        return None, None

    effective_on: date | None = None
    raw_effective, effective_tag = _first(effective_date_tags)
    if raw_effective is not None and effective_tag is not None:
        effective_on = _parse_date(raw_effective)
        if effective_on is None:
            findings.append(
                f"element {effective_tag!r} carried an unparseable date"
                f" {raw_effective!r}; recorded as UNKNOWN rather than guessed"
            )
        else:
            provenance.append(FieldProvenance("effective_on", effective_tag, raw_effective))

    last_trading_on: date | None = None
    raw_last, last_tag = _first(last_trading_date_tags)
    if raw_last is not None and last_tag is not None:
        last_trading_on = _parse_date(raw_last)
        if last_trading_on is None:
            findings.append(
                f"element {last_tag!r} carried an unparseable date {raw_last!r};"
                " recorded as UNKNOWN rather than guessed"
            )
        else:
            provenance.append(FieldProvenance("last_trading_on", last_tag, raw_last))

    issuer, issuer_tag = _first(ISSUER_TAGS)
    if issuer and issuer_tag:
        provenance.append(FieldProvenance("issuer_name", issuer_tag, issuer))
    exchange, exchange_tag = _first(EXCHANGE_TAGS)
    if exchange and exchange_tag:
        provenance.append(FieldProvenance("exchange", exchange_tag, exchange))
    rule, rule_tag = _first(RULE_TAGS)
    if rule and rule_tag:
        provenance.append(FieldProvenance("rule_provision", rule_tag, rule))

    if effective_on is None:
        findings.append(
            "document publishes no delisting effective date; the statutory Rule"
            " 12d2-2 period is NEVER computed here"
        )

    return Form25Document(
        cik=filing.cik,
        accession=filing.accession,
        form=filing.form,
        primary_document=filing.primary_document or "",
        document_checksum=checksum,
        effective_on=effective_on,
        last_trading_on=last_trading_on,
        issuer_name=issuer,
        exchange=exchange,
        rule_provision=rule,
        provenance=tuple(provenance),
        observed_elements=tuple(sorted(set(observed))),
        findings=tuple(findings),
    )


def effective_dates_by_accession(
    documents: Sequence[Form25Document],
) -> dict[str, date]:
    """Published effective dates only — absent ones are simply not present."""
    return {
        document.accession: document.effective_on
        for document in documents
        if document.effective_on is not None
    }


@dataclass(frozen=True, slots=True)
class Form25Coverage:
    """How much of the real Form 25 population published a lifecycle date."""

    documents: int
    with_effective_date: int
    with_last_trading_date: int
    unparsed_documents: int
    observed_elements: tuple[str, ...] = ()
    findings: tuple[str, ...] = field(default_factory=tuple)
    xml_documents: int = 0
    non_xml_documents: int = 0
    failures_by_reason: Mapping[str, int] = field(default_factory=dict)
    #: Element names seen that look like a date field but are NOT in the
    #: allow-list. This is the actionable signal for widening it from REAL data.
    unmatched_date_candidates: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.documents > 0 and self.with_effective_date == self.documents

    def summary_lines(self) -> tuple[str, ...]:
        """A concise human summary the owner run prints."""
        return (
            f"documents fetched            : {self.documents}",
            f"documents parsed as XML      : {self.xml_documents}",
            f"documents not XML            : {self.non_xml_documents}",
            f"effective-date matches       : {self.with_effective_date}/{self.documents}",
            f"last-trading-date matches    : {self.with_last_trading_date}/{self.documents}",
            f"parse failures               : {sum(self.failures_by_reason.values())}",
            f"unmatched date-like elements : {list(self.unmatched_date_candidates)}",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "documents": self.documents,
            "with_effective_date": self.with_effective_date,
            "with_last_trading_date": self.with_last_trading_date,
            "unparsed_documents": self.unparsed_documents,
            "xml_documents": self.xml_documents,
            "non_xml_documents": self.non_xml_documents,
            "complete": self.complete,
            "failures_by_reason": dict(sorted(self.failures_by_reason.items())),
            "unmatched_date_candidates": list(self.unmatched_date_candidates),
            "observed_elements": list(self.observed_elements),
            "findings": list(self.findings),
        }


def summarize_form25(documents: Sequence[Form25Document]) -> Form25Coverage:
    """Coverage of the parsed population — the input to PARTIAL semantics."""
    observed: set[str] = set()
    findings: list[str] = []
    failures: dict[str, int] = {}
    for document in documents:
        observed.update(document.observed_elements)
        findings.extend(document.findings)
        for finding in document.findings:
            reason = _failure_reason(finding)
            failures[reason] = failures.get(reason, 0) + 1
    known = {tag.lower() for tag in (*EFFECTIVE_DATE_TAGS, *LAST_TRADING_DATE_TAGS)}
    unmatched = sorted(
        name for name in observed if name not in known and _looks_like_a_date_field(name)
    )
    non_xml = sum(1 for d in documents if not d.observed_elements)
    return Form25Coverage(
        documents=len(documents),
        with_effective_date=sum(1 for d in documents if d.effective_on is not None),
        with_last_trading_date=sum(1 for d in documents if d.last_trading_on is not None),
        unparsed_documents=non_xml,
        xml_documents=len(documents) - non_xml,
        non_xml_documents=non_xml,
        failures_by_reason=failures,
        unmatched_date_candidates=tuple(unmatched),
        observed_elements=tuple(sorted(observed)),
        findings=tuple(sorted(set(findings))),
    )


#: Element names that look like they carry a date. Used ONLY to report
#: candidates for review - never to extract a value. Widening the allow-list
#: stays a deliberate decision made against a real document.
_DATE_HINTS = ("date", "dt", "effective", "delist", "trading")


def _looks_like_a_date_field(name: str) -> bool:
    return any(hint in name for hint in _DATE_HINTS)


def _failure_reason(finding: str) -> str:
    if "not XML" in finding:
        return "not_xml"
    if "unparseable" in finding:
        return "unparseable_date_value"
    if "publishes no delisting effective date" in finding:
        return "no_effective_date_element"
    return "other"


#: 8-K item codes are published metadata, but several are ambiguous about what
#: actually happened. They are recorded as CANDIDATE SIGNALS for review and are
#: never minted into events. Only parsed document evidence creates an event.
EIGHT_K_CANDIDATE_ITEMS: Mapping[str, str] = {
    "1.01": "entry into a material definitive agreement (possible merger terms)",
    "2.01": "completion of acquisition OR disposition of assets (ambiguous direction)",
    "3.01": "notice of delisting or failure to satisfy a continued listing rule",
    "3.03": "material modification to rights of security holders",
    "5.01": "changes in control of registrant",
    "5.03": "amendments to articles/bylaws (may accompany a name change)",
}

_ITEM_RE = re.compile(r"\d+\.\d+")


def candidate_signals(items: str) -> tuple[str, ...]:
    """Map an 8-K ``items`` string to review leads. These qualify NOTHING."""
    found = []
    for code in _ITEM_RE.findall(items or ""):
        description = EIGHT_K_CANDIDATE_ITEMS.get(code)
        if description:
            found.append(f"8-K item {code}: {description}")
    return tuple(sorted(set(found)))


__all__ = [
    "EFFECTIVE_DATE_TAGS",
    "EIGHT_K_CANDIDATE_ITEMS",
    "FORM25_PARSER_VERSION",
    "LAST_TRADING_DATE_TAGS",
    "FieldProvenance",
    "Form25Coverage",
    "Form25Document",
    "candidate_signals",
    "effective_dates_by_accession",
    "parse_form25_document",
    "assert_document_correlates",
    "primary_document_url",
    "summarize_form25",
]
