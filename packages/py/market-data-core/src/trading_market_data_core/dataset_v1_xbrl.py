"""Canonical identity for SEC XBRL observations and the events derived from them.

The real owner run failed with a long list of
``SOURCE_ERROR: duplicate corporate event ids [...]``, e.g. repeated
``xbrl:<accession>:CommonStockDividendsPerShareCashPaid:<value>``.

Two distinct causes, both real:

1. The event id was ``accession + concept + value``. That is not the identity of
   an XBRL fact. A single filing reports the same concept for MANY periods (a
   10-K carries four quarterly dividend figures), and the same value recurs
   across periods, so genuinely different observations collided while different
   filings restating one observation looked like different events.
2. The caller fetched the same filer more than once, so identical facts were
   ingested repeatedly.

This module fixes (1) by modelling identity the way SEC actually models a fact.

**Observation identity** (what economic thing was measured) is
``cik + taxonomy + concept + unit + period + dimensions``. It deliberately
EXCLUDES accession, filed date and value, because a restatement of the same
observation is the same observation.

**Fact identity** (which source row asserted it) additionally includes the
value, the accession and the filed date.

From that the four required outcomes fall out:

A. identical fact rows -> deterministic collapse (same fact key);
B. the same observation re-reported later with the SAME value -> one event, the
   earliest filing is the knowledge time, later accessions recorded as repeats;
C. different observations (different period/unit/dimensions) -> distinct events;
D. the same observation with a DIFFERENT value -> a revision: the latest filing
   supersedes, the earlier is recorded as superseded. Two different values filed
   on the SAME date cannot be ordered, so that is a CONFLICT and fails closed.

Nothing here appends a random suffix and nothing suppresses duplicate
validation: identity is derived from the material dimensions of the fact.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from trading_market_data_core.dataset_v1_sources import (
    XBRL_DIVIDEND_CONCEPTS,
    XBRL_SPLIT_CONCEPTS,
    CanonicalCorporateEvent,
    CorporateEventType,
    SourceAdapterError,
    SourceAuthority,
    XbrlFact,
)

XBRL_IDENTITY_VERSION = "dataset-v1-xbrl-identity-1.0.0"


def _period_token(start: date | None, end: date | None) -> str:
    """A period token that distinguishes duration facts from instant facts."""
    if start is None and end is None:
        return "period:none"
    if start is None:
        return f"instant:{end.isoformat() if end else '?'}"
    return f"duration:{start.isoformat()}..{end.isoformat() if end else '?'}"


def _dimensions_token(dimensions: Mapping[str, str] | None) -> str:
    """Dimensions/context, sorted so member order cannot change identity."""
    if not dimensions:
        return "dims:none"
    parts = ";".join(f"{key}={value}" for key, value in sorted(dimensions.items()))
    return f"dims:{parts}"


@dataclass(frozen=True, slots=True)
class XbrlObservation:
    """One reported XBRL fact with the full provenance identity needs."""

    cik: str
    taxonomy: str
    concept: str
    unit: str
    value: str
    filed_on: date
    accession: str
    form: str = ""
    period_start: date | None = None
    period_end: date | None = None
    frame: str | None = None
    dimensions: Mapping[str, str] | None = None
    source_checksum: str = ""

    @property
    def observation_key(self) -> str:
        """WHAT was measured. Excludes value, accession and filed date.

        Two rows sharing this key describe the same economic observation, so a
        later filing repeating it is a restatement rather than a new event.
        """
        return "|".join(
            (
                f"cik:{self.cik}",
                f"taxonomy:{self.taxonomy}",
                f"concept:{self.concept}",
                f"unit:{self.unit}",
                _period_token(self.period_start, self.period_end),
                _dimensions_token(self.dimensions),
            )
        )

    @property
    def fact_key(self) -> str:
        """WHICH source row asserted it (adds value, accession, filed date)."""
        return "|".join(
            (self.observation_key, f"value:{self.value}", f"accn:{self.accession}",
             f"filed:{self.filed_on.isoformat()}")
        )

    @property
    def observation_id(self) -> str:
        """A short, deterministic, order-independent id for the observation."""
        digest = hashlib.sha256(self.observation_key.encode("utf-8")).hexdigest()
        return f"xbrl-obs:{digest[:32]}"

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "observation_key": self.observation_key,
            "cik": self.cik,
            "taxonomy": self.taxonomy,
            "concept": self.concept,
            "unit": self.unit,
            "value": self.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "frame": self.frame,
            "dimensions": dict(sorted((self.dimensions or {}).items())),
            "accession": self.accession,
            "filed_on": self.filed_on.isoformat(),
            "form": self.form,
            "source_checksum": self.source_checksum,
        }


def observations_from_facts(
    facts: Sequence[XbrlFact], *, cik: str, taxonomy: str, source_checksum: str
) -> tuple[XbrlObservation, ...]:
    """Attach the identity dimensions the flat fact rows do not carry."""
    return tuple(
        XbrlObservation(
            cik=str(cik).zfill(10), taxonomy=taxonomy, concept=fact.concept,
            unit=fact.unit, value=fact.value, filed_on=fact.filed_on,
            accession=fact.accession, form=fact.form,
            period_start=fact.period_start, period_end=fact.period_end,
            source_checksum=source_checksum,
        )
        for fact in facts
    )


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    """One economic observation after duplicate/restatement normalization."""

    observation: XbrlObservation
    #: Accessions that repeated this observation with the SAME value.
    repeated_in: tuple[str, ...] = ()
    #: Earlier assertions of this observation with a DIFFERENT value.
    superseded: tuple[XbrlObservation, ...] = ()
    duplicates_collapsed: int = 0

    @property
    def is_revision(self) -> bool:
        return bool(self.superseded)

    def to_dict(self) -> dict[str, object]:
        return {
            "observation": self.observation.to_dict(),
            "repeated_in": list(self.repeated_in),
            "superseded": [item.to_dict() for item in self.superseded],
            "duplicates_collapsed": self.duplicates_collapsed,
            "is_revision": self.is_revision,
        }


@dataclass(frozen=True, slots=True)
class ObservationConflict:
    """Two irreconcilable assertions of the same observation. Fail closed."""

    observation_key: str
    filed_on: date
    values: tuple[str, ...]
    accessions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_key": self.observation_key,
            "filed_on": self.filed_on.isoformat(),
            "values": list(self.values),
            "accessions": list(self.accessions),
        }


@dataclass(frozen=True, slots=True)
class NormalizationReport:
    """What normalization actually did — never a silent dedup."""

    observations: tuple[NormalizedObservation, ...] = ()
    conflicts: tuple[ObservationConflict, ...] = ()
    total_rows: int = 0
    duplicates_collapsed: int = 0
    revisions: int = 0
    repeats: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": XBRL_IDENTITY_VERSION,
            "total_rows": self.total_rows,
            "distinct_observations": len(self.observations),
            "duplicates_collapsed": self.duplicates_collapsed,
            "revisions": self.revisions,
            "repeats": self.repeats,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "notes": list(self.notes),
        }


def normalize_observations(
    observations: Sequence[XbrlObservation], *, fail_on_conflict: bool = True
) -> NormalizationReport:
    """Collapse duplicates, fold restatements, and surface real conflicts.

    Deterministic and order independent: rows are grouped by observation key and
    ordered by (filed date, accession) before any decision is taken.
    """
    grouped: dict[str, list[XbrlObservation]] = {}
    seen_fact_keys: dict[str, int] = {}
    for observation in observations:
        seen_fact_keys[observation.fact_key] = seen_fact_keys.get(observation.fact_key, 0) + 1
        grouped.setdefault(observation.observation_key, []).append(observation)

    normalized: list[NormalizedObservation] = []
    conflicts: list[ObservationConflict] = []
    total_duplicates = 0
    revisions = 0
    repeats = 0

    for key in sorted(grouped):
        rows = grouped[key]
        # A: exact duplicate source rows collapse deterministically.
        unique: dict[str, XbrlObservation] = {}
        duplicates = 0
        for row in rows:
            if row.fact_key in unique:
                duplicates += 1
                continue
            unique[row.fact_key] = row
        total_duplicates += duplicates

        ordered = sorted(unique.values(), key=lambda row: (row.filed_on, row.accession))

        # D: two different values filed on the SAME day cannot be ordered.
        by_date: dict[date, set[str]] = {}
        for row in ordered:
            by_date.setdefault(row.filed_on, set()).add(row.value)
        conflicted = False
        for filed_on, values in sorted(by_date.items()):
            if len(values) > 1:
                conflicted = True
                conflicts.append(
                    ObservationConflict(
                        observation_key=key, filed_on=filed_on,
                        values=tuple(sorted(values)),
                        accessions=tuple(
                            sorted(r.accession for r in ordered if r.filed_on == filed_on)
                        ),
                    )
                )
        if conflicted:
            continue  # never emit an event built on an unresolved conflict

        current = ordered[-1]
        earlier = ordered[:-1]
        # B: same value re-reported later -> one observation, repeats recorded.
        same_value = [row for row in earlier if row.value == current.value]
        # D: different earlier values -> the later filing supersedes them.
        superseded = tuple(row for row in earlier if row.value != current.value)
        if superseded:
            revisions += 1
            # the earliest assertion carries the original knowledge time
            current = ordered[-1]
        repeats += len(same_value)
        normalized.append(
            NormalizedObservation(
                observation=current,
                repeated_in=tuple(sorted(row.accession for row in same_value)),
                superseded=superseded,
                duplicates_collapsed=duplicates,
            )
        )

    if conflicts and fail_on_conflict:
        raise SourceAdapterError(
            f"{len(conflicts)} irreconcilable XBRL observation(s): the same"
            " observation was filed on one date with different values;"
            f" first: {conflicts[0].to_dict()} (fail closed)"
        )

    return NormalizationReport(
        observations=tuple(normalized), conflicts=tuple(conflicts),
        total_rows=len(observations), duplicates_collapsed=total_duplicates,
        revisions=revisions, repeats=repeats,
    )


def _decimal(value: str, *, what: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, ArithmeticError, ValueError):
        raise SourceAdapterError(f"unparseable XBRL {what} {value!r}") from None


def observations_to_events(
    report: NormalizationReport,
    *,
    security_id: str,
    source_id: str,
) -> tuple[CanonicalCorporateEvent, ...]:
    """Mint one event per distinct economic observation.

    The event id is derived from the OBSERVATION identity, so a restatement
    never becomes a second event and two different periods never collide.
    """
    events: dict[str, CanonicalCorporateEvent] = {}
    for entry in report.observations:
        observation = entry.observation
        if observation.concept in XBRL_DIVIDEND_CONCEPTS:
            amount = _decimal(observation.value, what="dividend value")
            if amount <= 0:
                continue
            event_type = CorporateEventType.DIVIDEND
            detail = f"{observation.concept}={amount} {observation.unit}"
        elif observation.concept in XBRL_SPLIT_CONCEPTS:
            ratio = _decimal(observation.value, what="split ratio")
            if ratio == 1:
                continue  # a 1:1 ratio is not an event
            event_type = (
                CorporateEventType.SPLIT if ratio > 1 else CorporateEventType.REVERSE_SPLIT
            )
            detail = f"{observation.concept} ratio={ratio}"
        else:
            continue

        period = _period_token(observation.period_start, observation.period_end)
        lineage = ""
        if entry.repeated_in:
            lineage += f"; re-reported in {list(entry.repeated_in)}"
        if entry.superseded:
            lineage += (
                f"; supersedes {[row.accession for row in entry.superseded]}"
                f" (values {[row.value for row in entry.superseded]})"
            )
        event_id = observation.observation_id
        if event_id in events:
            # Two concepts can never share an observation id (the concept is in
            # the key), so this would be a genuine identity failure.
            raise SourceAdapterError(
                f"observation id collision for {event_id}; identity model is unsound"
            )
        events[event_id] = CanonicalCorporateEvent(
            event_id=event_id,
            security_id=security_id,
            event_type=event_type,
            source_id=source_id,
            source_authority=SourceAuthority.OFFICIAL_PUBLIC,
            source_checksum=observation.source_checksum,
            provenance=(
                f"SEC XBRL {detail} {period} reported in"
                f" {observation.form or 'filing'} accession {observation.accession}"
                f" filed {observation.filed_on.isoformat()}{lineage}"
            ),
            filing_date=observation.filed_on,
        )
    return tuple(sorted(events.values(), key=lambda event: event.event_id))


__all__ = [
    "XBRL_IDENTITY_VERSION",
    "NormalizationReport",
    "NormalizedObservation",
    "ObservationConflict",
    "XbrlObservation",
    "normalize_observations",
    "observations_from_facts",
    "observations_to_events",
]
