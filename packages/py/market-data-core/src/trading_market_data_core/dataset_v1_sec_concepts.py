"""SEC XBRL concept DISCOVERY for corporate actions.

The owner run asked for one hardcoded concept and got nothing:

    StockholdersEquityNoteStockSplitConversionRatio1
    requested=2  http_ok=0  rows=0

``http_ok=0`` means SEC returned 404 for that concept on those filers — it does
NOT mean the filers reported no split. Asking for a single guessed name cannot
answer the question, so this module inverts the approach: read what a filer
ACTUALLY reported (``/api/xbrl/companyfacts``), then map only concepts whose
semantics have been reviewed and written down here.

Two lists, deliberately separate:

* ``REVIEWED_CONCEPT_SEMANTICS`` — concept -> event type, each with a written
  rationale. Only these mint events.
* everything else that merely *looks* split-ish is reported as an unmapped
  CANDIDATE for human review. A name containing "split" is never sufficient:
  ``StockholdersEquityNoteStockSplitConversionRatio1`` and
  ``ReverseStockSplitConversionRatio`` mean different things, and a
  "SplitAdjusted…" measure is not an event at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trading_market_data_core.dataset_v1_sources import (
    CorporateEventType,
    SourceAdapterError,
)

SEC_CONCEPTS_VERSION = "dataset-v1-sec-concepts-1.0.0"


@dataclass(frozen=True, slots=True)
class ConceptSemantics:
    """A reviewed mapping from an XBRL concept to an event type."""

    taxonomy: str
    concept: str
    event_type: CorporateEventType | None
    #: Why this mapping is safe, or why the concept is explicitly NOT an event.
    rationale: str
    #: How the reported value is interpreted (ratio, per-share amount, ...).
    value_meaning: str = ""

    @property
    def mints_events(self) -> bool:
        return self.event_type is not None


#: Reviewed, explicit semantics. Nothing outside this table mints an event.
REVIEWED_CONCEPT_SEMANTICS: tuple[ConceptSemantics, ...] = (
    ConceptSemantics(
        taxonomy="us-gaap",
        concept="StockholdersEquityNoteStockSplitConversionRatio1",
        event_type=CorporateEventType.SPLIT,
        rationale=(
            "The standard note-level split conversion ratio. A value > 1 is a"
            " forward split and < 1 a reverse split; the direction is decided"
            " from the ratio, never from the concept name."
        ),
        value_meaning="conversion ratio (new shares per old share)",
    ),
    ConceptSemantics(
        taxonomy="us-gaap",
        concept="StockholdersEquityNoteStockSplitConversionRatio",
        event_type=CorporateEventType.SPLIT,
        rationale="Older sibling of the ...Ratio1 element with identical semantics.",
        value_meaning="conversion ratio (new shares per old share)",
    ),
    ConceptSemantics(
        taxonomy="us-gaap",
        concept="StockholdersEquityReverseStockSplit",
        event_type=None,
        rationale=(
            "A TEXT element describing a reverse split in prose. It carries no"
            " machine-readable ratio or date, so it cannot support an"
            " event-level assertion; it is a review lead only."
        ),
    ),
    ConceptSemantics(
        taxonomy="us-gaap",
        concept="CommonStockDividendsPerShareDeclared",
        event_type=CorporateEventType.DIVIDEND,
        rationale="Declared per-share dividend; the value is the amount, not a ratio.",
        value_meaning="per-share amount",
    ),
    ConceptSemantics(
        taxonomy="us-gaap",
        concept="CommonStockDividendsPerShareCashPaid",
        event_type=CorporateEventType.DIVIDEND,
        rationale="Cash dividend actually paid per share.",
        value_meaning="per-share amount",
    ),
    ConceptSemantics(
        taxonomy="us-gaap",
        concept="AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount",
        event_type=None,
        rationale=(
            "Contains no corporate action. Listed so a future reader can see it"
            " was considered and rejected rather than overlooked."
        ),
    ),
)

_BY_KEY: Mapping[tuple[str, str], ConceptSemantics] = {
    (item.taxonomy, item.concept): item for item in REVIEWED_CONCEPT_SEMANTICS
}

#: Tokens that make a concept WORTH REVIEWING. They never make it usable.
_CANDIDATE_TOKENS = ("split", "reversestock", "recapitalization", "conversionratio")


@dataclass(frozen=True, slots=True)
class DiscoveredConcept:
    """One concept a filer actually reported, with how much data it holds."""

    taxonomy: str
    concept: str
    units: tuple[str, ...]
    fact_count: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.taxonomy, self.concept)

    def to_dict(self) -> dict[str, object]:
        return {
            "taxonomy": self.taxonomy, "concept": self.concept,
            "units": list(self.units), "fact_count": self.fact_count,
        }


def parse_company_facts(raw: bytes, parsed: object) -> tuple[DiscoveredConcept, ...]:
    """Enumerate every concept a filer reported. Fail closed on a bad shape."""
    del raw
    if not isinstance(parsed, dict):
        raise SourceAdapterError("companyfacts payload must be a JSON object")
    facts = parsed.get("facts")
    if not isinstance(facts, dict):
        raise SourceAdapterError("companyfacts payload has no facts object")
    discovered: list[DiscoveredConcept] = []
    for taxonomy, concepts in sorted(facts.items()):
        if not isinstance(concepts, dict):
            raise SourceAdapterError(f"companyfacts taxonomy {taxonomy!r} must be an object")
        for concept, detail in sorted(concepts.items()):
            if not isinstance(detail, dict):
                raise SourceAdapterError(f"companyfacts concept {concept!r} must be an object")
            units = detail.get("units")
            if not isinstance(units, dict):
                continue
            count = 0
            for entries in units.values():
                if isinstance(entries, list):
                    count += len(entries)
            discovered.append(
                DiscoveredConcept(
                    taxonomy=str(taxonomy), concept=str(concept),
                    units=tuple(sorted(str(u) for u in units)), fact_count=count,
                )
            )
    return tuple(discovered)


@dataclass(frozen=True, slots=True)
class ConceptSelection:
    """What discovery decided, and why — the input to the owner's next step."""

    discovered: tuple[DiscoveredConcept, ...]
    accepted: tuple[DiscoveredConcept, ...]
    rejected: tuple[tuple[DiscoveredConcept, str], ...]
    unmapped_candidates: tuple[tuple[DiscoveredConcept, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": SEC_CONCEPTS_VERSION,
            "discovered_total": len(self.discovered),
            "accepted": [item.to_dict() for item in self.accepted],
            "rejected": [
                {**item.to_dict(), "reason": reason} for item, reason in self.rejected
            ],
            "unmapped_candidates": [
                {**item.to_dict(), "reason": reason}
                for item, reason in self.unmapped_candidates
            ],
        }

    def summary_lines(self) -> tuple[str, ...]:
        lines = [
            f"concepts discovered      : {len(self.discovered)}",
            f"concepts accepted        : {len(self.accepted)}",
            f"concepts rejected        : {len(self.rejected)}",
            f"unmapped candidates      : {len(self.unmapped_candidates)}",
        ]
        lines.extend(
            f"  accepted  {item.taxonomy}:{item.concept} ({item.fact_count} facts)"
            for item in self.accepted
        )
        lines.extend(
            f"  CANDIDATE {item.taxonomy}:{item.concept} ({item.fact_count} facts)"
            f" - {reason}"
            for item, reason in self.unmapped_candidates
        )
        return tuple(lines)


def select_corporate_concepts(
    discovered: Sequence[DiscoveredConcept],
    *,
    wanted: Sequence[CorporateEventType] = (),
) -> ConceptSelection:
    """Split discovery into accepted / rejected / candidates-for-review.

    Only concepts present in ``REVIEWED_CONCEPT_SEMANTICS`` with an event type
    are accepted. A concept that merely contains a split-ish token becomes an
    unmapped CANDIDATE: visible to the owner, never used to mint an event.
    """
    wanted_types = set(wanted)
    accepted: list[DiscoveredConcept] = []
    rejected: list[tuple[DiscoveredConcept, str]] = []
    candidates: list[tuple[DiscoveredConcept, str]] = []
    for item in discovered:
        semantics = _BY_KEY.get(item.key)
        if semantics is None:
            lowered = item.concept.lower()
            if any(token in lowered for token in _CANDIDATE_TOKENS):
                candidates.append(
                    (
                        item,
                        "name suggests a corporate action but the semantics are not"
                        " reviewed; a name match is never sufficient",
                    )
                )
            continue
        if not semantics.mints_events:
            rejected.append((item, semantics.rationale))
            continue
        if wanted_types and semantics.event_type not in wanted_types:
            rejected.append((item, f"not in the requested event set: {semantics.event_type}"))
            continue
        if item.fact_count == 0:
            rejected.append((item, "reviewed concept but the filer reported no facts"))
            continue
        accepted.append(item)
    return ConceptSelection(
        discovered=tuple(discovered),
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        unmapped_candidates=tuple(candidates),
    )


def semantics_for(taxonomy: str, concept: str) -> ConceptSemantics | None:
    return _BY_KEY.get((taxonomy, concept))


def reviewed_concepts_for(event_type: CorporateEventType) -> tuple[str, ...]:
    return tuple(
        sorted(
            item.concept
            for item in REVIEWED_CONCEPT_SEMANTICS
            if item.event_type is event_type
        )
    )


__all__ = [
    "REVIEWED_CONCEPT_SEMANTICS",
    "SEC_CONCEPTS_VERSION",
    "ConceptSelection",
    "ConceptSemantics",
    "DiscoveredConcept",
    "parse_company_facts",
    "reviewed_concepts_for",
    "select_corporate_concepts",
    "semantics_for",
]
