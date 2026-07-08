"""News/NLP engine v1 (IMP-0803).

Authority: Strategy_and_Decision_Framework_v0.5 section 13.2 (signal family
NEWS), Data_Model v0.6 (news_assessments: catalyst, sentiment, severity,
novelty, source cluster), Trust spec sections 2-4/7 via IMP-0305 (adversarial
content is assessed BEFORE any interpretation; prompt injection is flagged
and quarantined), and the source-class taxonomy of Trust section 3.

v1 is a DETERMINISTIC, keyword/policy-driven extractor over frozen text
fixtures — no AI provider (that port arrives with IMP-0804 and its output
stays untrusted structured input), no news provider, no network (ADR-0014).

Hard rules:

- **Trust gate first**: every item passes IMP-0305 ``assess`` before any
  extraction. A flagged item (prompt injection, integrity failure, ...) is
  BLOCKED: no entities, no catalyst, no positive output. The engine has no
  tools and text can never invoke one.
- **Conservative entity resolution**: cashtag candidates are resolved
  through the Instrument Master; an ambiguous symbol is never guessed — it
  is marked AMBIGUOUS (review) and the item becomes INCONCLUSIVE.
- **No hardcoded thresholds**: catalyst keywords, materiality bases,
  source-class multipliers, sentiment terms and injection markers are all
  ``NewsPolicy`` inputs. Missing/invalid policy fails closed.
- **Point-in-time**: an item observed/available after ``as_of`` is rejected
  (no lookahead). Naive timestamps are rejected.
- **Revisions/corrections are explicit**: a revision carries its
  ``revision_of`` linkage and a REVISION/CORRECTION status — nothing is
  silently overwritten; both versions remain addressable.
- No Candidate/Decision/Order is created; no self-reported confidence
  exists anywhere. Fixtures-validated only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_contracts.enums import SignalDirection

from trading_application.trust import SourceClass, assess

ENGINE_MODEL_ID = "news-nlp-engine"
ENGINE_MODEL_VERSION = "1.0.0"


class NewsEngineError(Exception):
    """Fail-closed engine failure (bad inputs/policy, lookahead)."""


@dataclass(frozen=True, slots=True)
class NewsItem:
    """One frozen text item with mandatory source attribution."""

    content_ref: str
    text: str
    source_id: str
    source_class: SourceClass
    provider_id: str
    observed_at: datetime
    available_at: datetime
    claimed_hash: str
    revision_of: str | None = None  # content_ref of the superseded version
    is_correction: bool = False

    def __post_init__(self) -> None:
        for stamp in (self.observed_at, self.available_at):
            if stamp.tzinfo is None:
                raise ValueError("news timestamps must be timezone-aware")
        if not self.source_id or not self.provider_id:
            raise NewsEngineError("source attribution is mandatory")


@dataclass(frozen=True, slots=True)
class CatalystRule:
    """Policy rule: keyword set + base materiality for one catalyst type."""

    name: str
    keywords: tuple[str, ...]
    base_materiality: Decimal
    direction: SignalDirection

    def __post_init__(self) -> None:
        if not self.keywords:
            raise NewsEngineError(f"catalyst {self.name!r} has no keywords")
        if self.base_materiality < 0:
            raise NewsEngineError("base_materiality must be non-negative")


@dataclass(frozen=True, slots=True)
class NewsPolicy:
    """All extraction rules — configuration input, nothing in code."""

    policy_version: str
    catalyst_rules: tuple[CatalystRule, ...]
    sentiment_positive_terms: tuple[str, ...]
    sentiment_negative_terms: tuple[str, ...]
    source_class_multipliers: tuple[tuple[SourceClass, Decimal], ...]
    injection_markers: tuple[str, ...]
    cashtag_prefix: str = "$"

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise NewsEngineError("policy_version is mandatory")
        if not self.catalyst_rules:
            raise NewsEngineError("policy must define catalyst rules")
        if not self.injection_markers:
            raise NewsEngineError("policy must define injection markers (fail closed)")
        classes = [source_class for source_class, _m in self.source_class_multipliers]
        if len(classes) != len(set(classes)):
            raise NewsEngineError("duplicate source-class multiplier")

    def multiplier_for(self, source_class: SourceClass) -> Decimal | None:
        for candidate, multiplier in self.source_class_multipliers:
            if candidate is source_class:
                return multiplier
        return None  # unmapped class: caller fails closed


class EntityStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"  # human review; never guessed
    UNRESOLVED = "UNRESOLVED"


class OutputStatus(StrEnum):
    OK = "OK"
    BLOCKED = "BLOCKED"  # trust/adversarial gate
    INCONCLUSIVE = "INCONCLUSIVE"


class RevisionStatus(StrEnum):
    ORIGINAL = "ORIGINAL"
    REVISION = "REVISION"
    CORRECTION = "CORRECTION"


@dataclass(frozen=True, slots=True)
class EntityResolution:
    """Conservative resolution of one mentioned symbol."""

    symbol: str
    status: EntityStatus
    instrument_id: str | None = None
    candidates: tuple[str, ...] = ()


# Resolver port: raise trading_domain.instruments.AmbiguousSymbolError for
# ambiguity, any other exception for unknown. Wired to InstrumentMaster.
EntityResolver = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class NewsStructuredOutput:
    """Canonical structured output of the engine (scope fields only)."""

    content_ref: str
    source_id: str
    provider_id: str
    source_class: SourceClass
    status: OutputStatus
    entities: tuple[EntityResolution, ...]
    catalyst: str | None
    catalyst_direction: SignalDirection
    materiality: Decimal | None
    sentiment: SignalDirection
    revision_status: RevisionStatus
    revision_of: str | None
    as_of: datetime
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class NewsNlpEngine:
    """Deterministic structured-output extractor over frozen news fixtures."""

    def __init__(self, policy: NewsPolicy, *, resolve_entity: EntityResolver) -> None:
        self._policy = policy
        self._resolve = resolve_entity

    def evaluate(self, item: NewsItem, *, as_of: datetime) -> NewsStructuredOutput:
        if as_of.tzinfo is None:
            raise NewsEngineError("as_of must be timezone-aware")
        if item.available_at > as_of or item.observed_at > as_of:
            raise NewsEngineError(
                "lookahead: item observed/available after as_of is rejected"
            )
        policy = self._policy
        reason_codes: list[str] = []
        evidence = [f"policy:{policy.policy_version}", f"content:{item.content_ref}"]

        # -- trust/adversarial gate FIRST (IMP-0305) --------------------------
        trust = assess(
            content_ref=item.content_ref,
            claimed_hash=item.claimed_hash,
            source_class=item.source_class,
            content_text=item.text,
            injection_markers=policy.injection_markers,
        )
        if trust.flags:
            for flag in trust.flags:
                reason_codes.append(flag.value)
            evidence.append(f"trust:{','.join(flag.value for flag in trust.flags)}")
            # Blocked content is never interpreted: no entities, no catalyst,
            # no sentiment, no positive signal, and no tool is ever invoked.
            return self._finish(
                item, as_of, OutputStatus.BLOCKED, (), None,
                SignalDirection.NEUTRAL, None, SignalDirection.NEUTRAL,
                reason_codes, evidence,
            )

        # -- conservative entity resolution ------------------------------------
        entities: list[EntityResolution] = []
        any_ambiguous = False
        for symbol in self._cashtags(item.text):
            entities.append(self._resolve_conservatively(symbol))
        for entity in entities:
            if entity.status is EntityStatus.AMBIGUOUS:
                any_ambiguous = True
                reason_codes.append(f"ENTITY_AMBIGUOUS_{entity.symbol}")
            elif entity.status is EntityStatus.UNRESOLVED:
                reason_codes.append(f"ENTITY_UNRESOLVED_{entity.symbol}")

        # -- catalyst + materiality (policy-driven) -----------------------------
        lowered = item.text.lower()
        catalyst: str | None = None
        catalyst_direction = SignalDirection.NEUTRAL
        materiality: Decimal | None = None
        for rule in policy.catalyst_rules:
            if any(keyword.lower() in lowered for keyword in rule.keywords):
                catalyst = rule.name
                catalyst_direction = rule.direction
                multiplier = policy.multiplier_for(item.source_class)
                if multiplier is None:
                    reason_codes.append("SOURCE_CLASS_UNMAPPED")
                else:
                    materiality = rule.base_materiality * multiplier
                reason_codes.append(f"CATALYST_{rule.name.upper()}")
                break
        if catalyst is None:
            reason_codes.append("NO_CATALYST")

        # -- sentiment (policy terms, no constants) ------------------------------
        positive = sum(term.lower() in lowered for term in policy.sentiment_positive_terms)
        negative = sum(term.lower() in lowered for term in policy.sentiment_negative_terms)
        if positive > negative:
            sentiment = SignalDirection.LONG
        elif negative > positive:
            sentiment = SignalDirection.NEGATIVE
        else:
            sentiment = SignalDirection.NEUTRAL

        status = OutputStatus.OK
        if any_ambiguous or (catalyst is not None and materiality is None):
            status = OutputStatus.INCONCLUSIVE  # review, never a guess

        return self._finish(
            item, as_of, status, tuple(entities), catalyst,
            catalyst_direction, materiality, sentiment, reason_codes, evidence,
        )

    def _cashtags(self, text: str) -> tuple[str, ...]:
        prefix = self._policy.cashtag_prefix
        seen: dict[str, None] = {}
        for token in text.split():
            if token.startswith(prefix) and len(token) > len(prefix):
                symbol = token[len(prefix) :].strip(".,!?:;()").upper()
                if symbol.isalpha():
                    seen.setdefault(symbol, None)
        return tuple(seen)

    def _resolve_conservatively(self, symbol: str) -> EntityResolution:
        from trading_domain.instruments import AmbiguousSymbolError

        try:
            instrument_id = self._resolve(symbol)
        except AmbiguousSymbolError as error:
            return EntityResolution(
                symbol=symbol,
                status=EntityStatus.AMBIGUOUS,
                candidates=tuple(error.candidates),
            )
        except Exception:  # unknown symbol or resolver failure: fail closed
            return EntityResolution(symbol=symbol, status=EntityStatus.UNRESOLVED)
        return EntityResolution(
            symbol=symbol, status=EntityStatus.RESOLVED, instrument_id=instrument_id
        )

    def _finish(
        self,
        item: NewsItem,
        as_of: datetime,
        status: OutputStatus,
        entities: tuple[EntityResolution, ...],
        catalyst: str | None,
        catalyst_direction: SignalDirection,
        materiality: Decimal | None,
        sentiment: SignalDirection,
        reason_codes: list[str],
        evidence: list[str],
    ) -> NewsStructuredOutput:
        if item.revision_of is not None:
            revision_status = (
                RevisionStatus.CORRECTION if item.is_correction else RevisionStatus.REVISION
            )
            reason_codes.append(f"{revision_status.value}_OF_PRIOR_VERSION")
            evidence.append(f"supersedes:{item.revision_of}")
        else:
            revision_status = RevisionStatus.ORIGINAL
        evidence.append(f"source:{item.source_id}@{item.provider_id}")
        return NewsStructuredOutput(
            content_ref=item.content_ref,
            source_id=item.source_id,
            provider_id=item.provider_id,
            source_class=item.source_class,
            status=status,
            entities=entities,
            catalyst=catalyst,
            catalyst_direction=catalyst_direction,
            materiality=materiality,
            sentiment=sentiment,
            revision_status=revision_status,
            revision_of=item.revision_of,
            as_of=as_of,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            evidence_refs=tuple(evidence),
        )


__all__ = [
    "ENGINE_MODEL_ID",
    "ENGINE_MODEL_VERSION",
    "CatalystRule",
    "EntityResolution",
    "EntityStatus",
    "NewsEngineError",
    "NewsItem",
    "NewsNlpEngine",
    "NewsPolicy",
    "NewsStructuredOutput",
    "OutputStatus",
    "RevisionStatus",
]
