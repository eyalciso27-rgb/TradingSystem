"""Form 25 primary documents that are HTML/text rather than XML.

The real owner run answered the open question with data:

    documents fetched: 5 · parsed as XML: 0 · non-XML: 5
    effective-date matches: 0/5 · unmatched XML date-like elements: []

So the gap was never a missing XML tag — the documents are HTML. Adding
speculative XML element names would have been guesswork; this module parses what
SEC actually serves.

Safety rules, all enforced:

* only a date that sits next to an EXPLICIT lifecycle label is extracted. A date
  in unrelated prose is never used;
* if a label yields two or more DIFFERENT dates, that is ambiguity and the field
  fails closed rather than picking one;
* statutory dates are never computed — the Rule 12d2-2 period is not a published
  per-filing fact;
* every extracted value keeps the matched label and the exact source snippet, so
  a parse can be audited against the document;
* active content (``<script>``/``<style>``/event handlers) is stripped before
  matching and reported, so markup can never contribute text.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

FORM25_HTML_PARSER_VERSION = "dataset-v1-form25-html-1.0.0"

#: Labels that explicitly introduce the date a delisting takes effect.
EFFECTIVE_DATE_LABELS: tuple[str, ...] = (
    "effective date of delisting",
    "effective date of the delisting",
    "date of delisting",
    "delisting effective",
    "effective date of removal",
    "date the delisting became effective",
    "removal from listing effective",
)
#: Labels that explicitly introduce the last day the security traded.
LAST_TRADING_DATE_LABELS: tuple[str, ...] = (
    "last trading date",
    "last day of trading",
    "date trading ceased",
    "trading ceased on",
    "last date traded",
)
ISSUER_LABELS: tuple[str, ...] = ("name of issuer", "issuer name", "issuer:")
EXCHANGE_LABELS: tuple[str, ...] = ("name of exchange", "exchange:", "national securities exchange")
RULE_LABELS: tuple[str, ...] = ("rule", "pursuant to")

_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HANDLER_RE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v ]+")

#: Date shapes SEC documents actually use. Anything else is not a date.
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

#: How far after a label a value may sit. Beyond this the text is unrelated.
_LABEL_WINDOW = 120


@dataclass(frozen=True, slots=True)
class HtmlFieldMatch:
    """One extracted field with the label and snippet that produced it."""

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
class HtmlParseResult:
    """What an HTML/text Form 25 document actually published."""

    content_kind: str
    text_length: int
    effective_on: date | None = None
    last_trading_on: date | None = None
    issuer_name: str | None = None
    exchange: str | None = None
    rule_provision: str | None = None
    matches: tuple[HtmlFieldMatch, ...] = ()
    labels_seen: tuple[str, ...] = ()
    ambiguous_fields: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    active_content_stripped: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "parser_version": FORM25_HTML_PARSER_VERSION,
            "content_kind": self.content_kind,
            "text_length": self.text_length,
            "effective_on": self.effective_on.isoformat() if self.effective_on else None,
            "last_trading_on": (
                self.last_trading_on.isoformat() if self.last_trading_on else None
            ),
            "issuer_name": self.issuer_name,
            "exchange": self.exchange,
            "rule_provision": self.rule_provision,
            "matches": [match.to_dict() for match in self.matches],
            "labels_seen": list(self.labels_seen),
            "ambiguous_fields": list(self.ambiguous_fields),
            "active_content_stripped": self.active_content_stripped,
            "findings": list(self.findings),
        }


def _decode(raw: bytes) -> tuple[str, str]:
    """Decode with a declared charset when present; never raise on bad bytes."""
    head = raw[:4096].decode("latin-1", errors="replace").lower()
    encoding = "utf-8"
    match = re.search(r'charset=["\']?([a-z0-9_\-]+)', head)
    if match:
        encoding = match.group(1)
    try:
        return raw.decode(encoding, errors="replace"), encoding
    except LookupError:
        return raw.decode("utf-8", errors="replace"), "utf-8"


def html_to_text(raw: bytes) -> tuple[str, str, bool]:
    """Flatten HTML to text. Returns (text, content_kind, active_stripped).

    Table cells and block boundaries become separators so a label in one cell
    cannot silently merge with an unrelated neighbouring value.
    """
    decoded, _ = _decode(raw)
    lowered = decoded.lower()
    is_html = "<html" in lowered or "<table" in lowered or "<p>" in lowered or "<div" in lowered
    active = bool(_SCRIPT_RE.search(decoded)) or bool(_HANDLER_RE.search(decoded))
    text = _SCRIPT_RE.sub(" ", decoded)
    text = _COMMENT_RE.sub(" ", text)
    if is_html:
        # cell/row/block boundaries become explicit separators
        text = re.sub(r"</\s*(td|th)\s*>", " | ", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*(tr|p|div|br|li)\s*>|<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&#160;", " ")
        .replace("&quot;", '"')
    )
    text = unicodedata.normalize("NFKC", text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text, ("html" if is_html else "text"), active


def _parse_date_token(token: str) -> date | None:
    for pattern, fmt in _DATE_PATTERNS:
        match = pattern.search(token)
        if match:
            try:
                return datetime.strptime(match.group(1), fmt).date()  # noqa: DTZ007
            except ValueError:
                continue
    return None


def _dates_after_label(text: str, label: str) -> list[tuple[date, str]]:
    """Every date appearing within the window AFTER an explicit label."""
    found: list[tuple[date, str]] = []
    lowered = text.lower()
    start = 0
    while True:
        index = lowered.find(label, start)
        if index == -1:
            return found
        start = index + len(label)
        window = text[start : start + _LABEL_WINDOW]
        # stop at the next label-ish separator so a far-away date is not claimed
        window = re.split(r"\n\s*\n", window)[0]
        parsed = _parse_date_token(window)
        if parsed is not None:
            snippet = " ".join((text[index:start] + window).split())[:200]
            found.append((parsed, snippet))


def _value_after_label(text: str, labels: Sequence[str]) -> tuple[str | None, str, str]:
    lowered = text.lower()
    for label in labels:
        index = lowered.find(label)
        if index == -1:
            continue
        window = text[index + len(label) : index + len(label) + _LABEL_WINDOW]
        value = window.split("\n")[0].strip(" :|\t")
        if value:
            snippet = " ".join((text[index : index + len(label)] + " " + value).split())[:200]
            return value[:120], label, snippet
    return None, "", ""


def parse_form25_html(raw: bytes) -> HtmlParseResult:
    """Extract explicitly published lifecycle fields from an HTML/text Form 25."""
    if not raw:
        return HtmlParseResult(content_kind="empty", text_length=0, findings=("empty document",))

    text, content_kind, active = html_to_text(raw)
    findings: list[str] = []
    if active:
        findings.append("active content (script/style/handler) stripped before matching")

    matches: list[HtmlFieldMatch] = []
    labels_seen: list[str] = []
    ambiguous: list[str] = []
    lowered = text.lower()
    for label in (
        *EFFECTIVE_DATE_LABELS, *LAST_TRADING_DATE_LABELS, *ISSUER_LABELS, *EXCHANGE_LABELS
    ):
        if label in lowered:
            labels_seen.append(label)

    def _date_field(field: str, labels: Sequence[str]) -> date | None:
        candidates: list[tuple[date, str, str]] = []
        for label in labels:
            for parsed, snippet in _dates_after_label(text, label):
                candidates.append((parsed, label, snippet))
        if not candidates:
            return None
        distinct = {candidate[0] for candidate in candidates}
        if len(distinct) > 1:
            ambiguous.append(field)
            findings.append(
                f"{field}: {len(distinct)} different dates follow an explicit label"
                f" ({sorted(d.isoformat() for d in distinct)}); failing closed"
            )
            return None
        parsed, label, snippet = candidates[0]
        matches.append(
            HtmlFieldMatch(
                field_name=field, label=label, raw_value=parsed.isoformat(), snippet=snippet
            )
        )
        return parsed

    effective_on = _date_field("effective_on", EFFECTIVE_DATE_LABELS)
    last_trading_on = _date_field("last_trading_on", LAST_TRADING_DATE_LABELS)

    issuer, issuer_label, issuer_snippet = _value_after_label(text, ISSUER_LABELS)
    if issuer:
        matches.append(HtmlFieldMatch("issuer_name", issuer_label, issuer, issuer_snippet))
    exchange, exchange_label, exchange_snippet = _value_after_label(text, EXCHANGE_LABELS)
    if exchange:
        matches.append(HtmlFieldMatch("exchange", exchange_label, exchange, exchange_snippet))
    rule, rule_label, rule_snippet = _value_after_label(text, RULE_LABELS)
    if rule:
        matches.append(HtmlFieldMatch("rule_provision", rule_label, rule, rule_snippet))

    if effective_on is None and "effective_on" not in ambiguous:
        findings.append(
            "no explicit delisting effective-date label found; the statutory Rule"
            " 12d2-2 period is NEVER computed"
        )

    return HtmlParseResult(
        content_kind=content_kind,
        text_length=len(text),
        effective_on=effective_on,
        last_trading_on=last_trading_on,
        issuer_name=issuer,
        exchange=exchange,
        rule_provision=rule,
        matches=tuple(matches),
        labels_seen=tuple(sorted(set(labels_seen))),
        ambiguous_fields=tuple(sorted(set(ambiguous))),
        findings=tuple(findings),
        active_content_stripped=active,
    )


#: Phrases that indicate a lifecycle statement exists but no label matched.
RELEVANT_PHRASES: tuple[str, ...] = (
    "delist", "removal from listing", "ceased trading", "no longer listed",
    "certification", "withdraw",
)


def unmatched_relevant_phrases(text: str, labels_seen: Sequence[str]) -> tuple[str, ...]:
    """Phrases worth a human look when no explicit label matched.

    Reported so the label list can be widened from REAL documents rather than
    guessed at.
    """
    if labels_seen:
        return ()
    lowered = text.lower()
    return tuple(sorted({phrase for phrase in RELEVANT_PHRASES if phrase in lowered}))


def html_summary(results: Sequence[HtmlParseResult]) -> Mapping[str, object]:
    """Aggregate diagnostics across a population of HTML documents."""
    reasons: dict[str, int] = {}
    for result in results:
        for finding in result.findings:
            key = (
                "ambiguous_dates"
                if "failing closed" in finding
                else "no_explicit_label"
                if "no explicit delisting" in finding
                else "active_content"
                if "active content" in finding
                else "other"
            )
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "documents": len(results),
        "html_documents": sum(1 for r in results if r.content_kind == "html"),
        "text_documents": sum(1 for r in results if r.content_kind == "text"),
        "effective_date_matches": sum(1 for r in results if r.effective_on is not None),
        "last_trading_date_matches": sum(1 for r in results if r.last_trading_on is not None),
        "ambiguous_documents": sum(1 for r in results if r.ambiguous_fields),
        "labels_seen": sorted({label for r in results for label in r.labels_seen}),
        "failures_by_reason": dict(sorted(reasons.items())),
    }


__all__ = [
    "EFFECTIVE_DATE_LABELS",
    "FORM25_HTML_PARSER_VERSION",
    "LAST_TRADING_DATE_LABELS",
    "RELEVANT_PHRASES",
    "HtmlFieldMatch",
    "HtmlParseResult",
    "html_summary",
    "html_to_text",
    "parse_form25_html",
    "unmatched_relevant_phrases",
]
