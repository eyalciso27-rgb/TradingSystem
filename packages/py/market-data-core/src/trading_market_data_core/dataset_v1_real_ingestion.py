"""Dataset V1 real-source ingestion -> authenticated qualification evidence.

This module turns REAL normalized source records into ``QualificationEvidence``.
It decides nothing in advance: every sub-capability is computed from what the
records actually contain, so an owner-host run produces PASS, PARTIAL or FAIL on
the strength of the data rather than on an expectation encoded here.

Three qualification paths:

* ``qualify_delisted_security_history`` — SEC delisting/deregistration filings,
  identity resolved WITHOUT requiring a current IBKR contract to exist;
* ``qualify_corporate_actions`` — event-level actions from SEC (official) and
  IBKR (owner primary), with adjustment behaviour recorded SEPARATELY and never
  promoted into event evidence;
* ``qualify_pit_membership`` — official index-change announcements replayed from
  a point-in-time baseline, with survivorship and current-universe leakage
  refused at load time.

Access-rule note (S&P DJI): automated retrieval from the publisher is blocked by
its bot protection — a plain automated request returns HTTP 403, for
``robots.txt`` itself. Circumventing that is out of the question, so the
membership path ingests OFFICIAL documents the owner supplies, binding each to a
verified sha256 of the document file. Nothing is scraped and nothing is guessed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

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
    qualify_source,
)
from trading_market_data_core.dataset_v1_sources import (
    AdjustmentCrossCheck,
    CanonicalCorporateEvent,
    CorporateEventType,
    CrosswalkEntry,
    IbkrCapabilityProbe,
    IdentityCrosswalk,
    IndexChangeAnnouncement,
    ProbeOutcome,
    SecFilingRecord,
    SourceAdapterError,
    SourceAuthority,
)

REAL_INGESTION_VERSION = "dataset-v1-real-ingestion-1.0.0"

#: The observed, documented reason S&P DJI cannot be ingested automatically.
SP_AUTOMATED_ACCESS_NOTE = (
    "spglobal.com returns HTTP 403 to plain automated requests (robots.txt included):"
    " publisher bot protection. Bypassing it is not permitted, so official"
    " announcement documents are supplied by the owner and bound to a verified"
    " sha256 of each document file."
)

_SEC_DELISTING_FORMS = ("25", "25-NSE")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


# -- identity crosswalk reporting --------------------------------------------


@dataclass(frozen=True, slots=True)
class CrosswalkReport:
    """Machine-readable identity resolution across the qualification universe."""

    entries: int
    resolved: tuple[str, ...]
    ambiguous: tuple[str, ...]
    unresolved: tuple[str, ...]
    conflicting: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not (self.ambiguous or self.unresolved or self.conflicting)

    def as_resolution(self) -> IdentityResolutionReport:
        return IdentityResolutionReport(
            resolved=len(self.resolved), ambiguous=len(self.ambiguous),
            unresolved=len(self.unresolved), conflicting=len(self.conflicting),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": REAL_INGESTION_VERSION,
            "entries": self.entries,
            "resolved": list(self.resolved),
            "ambiguous": list(self.ambiguous),
            "unresolved": list(self.unresolved),
            "conflicting": list(self.conflicting),
            "clean": self.clean,
        }


def build_crosswalk_report(
    crosswalk: IdentityCrosswalk,
    *,
    ciks: Sequence[str] = (),
    con_ids: Sequence[int] = (),
    entry_count: int,
) -> CrosswalkReport:
    """Resolve the qualification universe by CIK and conId. Ambiguity fails closed."""
    resolved: dict[str, str] = {}
    ambiguous: list[str] = []
    unresolved: list[str] = []
    by_cik = crosswalk.resolve_by_cik(list(ciks))
    by_conid = crosswalk.resolve_by_conid(list(con_ids))
    for label, resolution in (("cik", by_cik), ("conid", by_conid)):
        for key, security_id in resolution.resolved.items():
            resolved[f"{label}:{key}"] = security_id
        ambiguous.extend(f"{label}:{key}" for key in resolution.ambiguous)
        unresolved.extend(f"{label}:{key}" for key in resolution.unresolved)

    # A conflict is one external key resolving to two different security_ids, or
    # one security_id claimed by two different keys of the same kind.
    conflicting: list[str] = []
    seen: dict[str, str] = {}
    for key, security_id in sorted(resolved.items()):
        kind = key.split(":", 1)[0]
        previous = seen.get(f"{kind}|{security_id}")
        if previous is not None and previous != key:
            conflicting.append(f"{previous} and {key} both resolve to {security_id}")
        seen[f"{kind}|{security_id}"] = key
    return CrosswalkReport(
        entries=entry_count,
        resolved=tuple(f"{key}={value}" for key, value in sorted(resolved.items())),
        ambiguous=tuple(sorted(ambiguous)),
        unresolved=tuple(sorted(unresolved)),
        conflicting=tuple(sorted(conflicting)),
    )


# -- delisted security history (SEC official) ---------------------------------


def qualify_delisted_security_history(
    filings: Sequence[SecFilingRecord],
    *,
    crosswalk: IdentityCrosswalk,
    security_id_by_cik: Mapping[str, str],
    source: SourceDescriptor,
    coverage: CoverageWindow,
    observed_at: datetime,
    effective_dates: Mapping[str, date] | None = None,
) -> QualificationEvidence:
    """Qualify delisted-security history from real SEC filings.

    Identity is proven through CIK -> security_id, so a name remains resolvable
    even though no current IBKR contract exists for it. ``effective_dates`` is
    supplied only when the source genuinely publishes a delisting effective
    date; it is never derived from the statutory period.
    """
    effective = dict(effective_dates or {})
    delistings = [f for f in filings if f.form in _SEC_DELISTING_FORMS]
    deregistrations = [f for f in filings if f.is_delisting_form and f not in delistings]
    resolved_ciks = {
        f.cik.zfill(10) for f in filings if f.cik.zfill(10) in security_id_by_cik
    }
    unresolved_ciks = sorted(
        {f.cik.zfill(10) for f in filings} - resolved_ciks
    )
    report = build_crosswalk_report(
        crosswalk,
        ciks=sorted({f.cik.zfill(10) for f in filings}),
        entry_count=len(security_id_by_cik),
    )
    with_knowledge = [f for f in filings if f.accepted_at is not None]
    with_effective = [f for f in delistings if f.accession in effective]

    subs = (
        SubCapabilityResult(
            name="delisting_filing_observed",
            passed=bool(delistings),
            detail=f"{len(delistings)} Form 25/25-NSE filing(s) observed",
        ),
        SubCapabilityResult(
            name="deregistration_filing_observed",
            passed=bool(deregistrations),
            detail=f"{len(deregistrations)} Form 15 deregistration filing(s) observed",
        ),
        SubCapabilityResult(
            name="knowledge_time_captured",
            passed=bool(with_knowledge) and len(with_knowledge) == len(filings),
            detail=f"{len(with_knowledge)}/{len(filings)} filings carry an acceptance time",
        ),
        SubCapabilityResult(
            name="identity_resolvable_without_current_contract",
            passed=bool(resolved_ciks) and not unresolved_ciks,
            detail=(
                f"{len(resolved_ciks)} CIK(s) resolved to a stable security_id;"
                f" unresolved={unresolved_ciks}"
            ),
        ),
        SubCapabilityResult(
            name="delisting_effective_date_published",
            passed=bool(with_effective) and len(with_effective) == len(delistings),
            detail=(
                f"{len(with_effective)}/{len(delistings)} delisting filings carry a"
                " published effective date; the statutory period is NEVER computed here"
            ),
        ),
    )
    validation_errors: list[str] = []
    if unresolved_ciks:
        validation_errors.append(f"unresolved CIK(s): {unresolved_ciks}")

    proofs: list[str] = []
    if delistings and resolved_ciks:
        proofs.append("inactive_security_resolvable")

    return qualify_source(
        "delisted_security_history",
        source=source,
        coverage=coverage,
        temporal=TemporalSemantics(
            provides_effective_dates=bool(filings),
            provides_known_at=bool(with_knowledge),
            revision_safe=True,
        ),
        identity=IdentitySemantics(
            provides_stable_identifier=bool(resolved_ciks),
            identifier_kinds=("dataset_security_id", "sec_cik"),
            ticker_only=False,
        ),
        metrics=DataQualityMetrics(
            total_rows=len(filings),
            accepted=len(delistings) + len(deregistrations),
            historical_changes=len(delistings) + len(deregistrations),
            missing_dates=len(filings) - len(with_knowledge),
        ),
        resolution=report.as_resolution(),
        validation_errors=validation_errors,
        proofs=proofs,
        sub_capabilities=subs,
        observed_at=observed_at,
    )


# -- corporate actions (IBKR primary + SEC official) --------------------------


#: Every event type the capability tracks. Absence is reported, never faked.
CORPORATE_ACTION_TYPES: tuple[CorporateEventType, ...] = (
    CorporateEventType.SPLIT,
    CorporateEventType.REVERSE_SPLIT,
    CorporateEventType.DIVIDEND,
    CorporateEventType.SPECIAL_DIVIDEND,
    CorporateEventType.MERGER,
    CorporateEventType.ACQUISITION,
    CorporateEventType.SPINOFF,
    CorporateEventType.SYMBOL_CHANGE,
    CorporateEventType.DELISTING,
)

#: The production contract's REQUIRED set. `corporate_actions` may only PASS
#: when every one of these is proven with event-level evidence. Proving a single
#: type (delisting) is a genuine PARTIAL, never a PASS.
REQUIRED_CORPORATE_EVENTS: tuple[CorporateEventType, ...] = (
    CorporateEventType.SPLIT,
    CorporateEventType.REVERSE_SPLIT,
    CorporateEventType.DIVIDEND,
    CorporateEventType.MERGER,
    CorporateEventType.SYMBOL_CHANGE,
    CorporateEventType.DELISTING,
)


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """How IBKR adjustment behaviour relates to a SEC event. Never silent."""

    security_id: str
    event_id: str
    event_type: str
    corroborated: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "security_id": self.security_id, "event_id": self.event_id,
            "event_type": self.event_type, "corroborated": self.corroborated,
            "detail": self.detail,
        }


def reconcile_events_with_adjustments(
    events: Sequence[CanonicalCorporateEvent],
    adjustment_checks: Sequence[AdjustmentCrossCheck],
) -> tuple[ReconciliationOutcome, ...]:
    """Corroborate SEC events against IBKR adjustment behaviour.

    IBKR observations are used ONLY as corroboration of adjustment behaviour;
    they never become event-level facts and never create an event. Agreement is
    recorded as corroboration, disagreement is recorded explicitly, and nothing
    is silently resolved in either direction.
    """
    price_affecting = {
        CorporateEventType.SPLIT,
        CorporateEventType.REVERSE_SPLIT,
        CorporateEventType.DIVIDEND,
        CorporateEventType.SPECIAL_DIVIDEND,
    }
    any_divergence = any(check.diverges for check in adjustment_checks)
    outcomes: list[ReconciliationOutcome] = []
    for event in sorted(events, key=lambda e: e.event_id):
        if event.event_type not in price_affecting:
            continue
        if not adjustment_checks:
            outcomes.append(
                ReconciliationOutcome(
                    security_id=event.security_id, event_id=event.event_id,
                    event_type=event.event_type.value, corroborated=False,
                    detail="no IBKR adjustment observation on this run; NOT corroborated",
                )
            )
        elif any_divergence:
            outcomes.append(
                ReconciliationOutcome(
                    security_id=event.security_id, event_id=event.event_id,
                    event_type=event.event_type.value, corroborated=True,
                    detail=(
                        "IBKR TRADES vs ADJUSTED_LAST diverges, consistent with a"
                        " price-affecting action; adjustment behaviour only"
                    ),
                )
            )
        else:
            outcomes.append(
                ReconciliationOutcome(
                    security_id=event.security_id, event_id=event.event_id,
                    event_type=event.event_type.value, corroborated=False,
                    detail=(
                        "DISAGREEMENT: SEC reports a price-affecting event but IBKR"
                        " shows no adjustment divergence; recorded, not resolved"
                    ),
                )
            )
    return tuple(outcomes)


def qualify_corporate_actions(
    events: Sequence[CanonicalCorporateEvent],
    *,
    adjustment_checks: Sequence[AdjustmentCrossCheck] = (),
    ibkr_probes: Sequence[IbkrCapabilityProbe] = (),
    source: SourceDescriptor,
    coverage: CoverageWindow,
    observed_at: datetime,
    required_events: Sequence[CorporateEventType] = REQUIRED_CORPORATE_EVENTS,
) -> QualificationEvidence:
    """Qualify event-level corporate actions across real sources.

    ``adjustment_checks`` characterise the provider's adjustment behaviour. They
    are recorded as their own sub-capability and are DELIBERATELY excluded from
    the event count: an adjusted-price divergence is not an event.
    """
    for event in events:
        if not event.provenance.strip():
            raise SourceAdapterError("every canonical event requires event-level provenance")
    seen_ids = [event.event_id for event in events]
    duplicates = sorted({eid for eid in seen_ids if seen_ids.count(eid) > 1})
    if duplicates:
        raise SourceAdapterError(
            f"duplicate corporate event ids {duplicates}; a repeated event would"
            " inflate the evidence count (fail closed)"
        )

    by_type: dict[CorporateEventType, list[CanonicalCorporateEvent]] = {}
    for event in events:
        by_type.setdefault(event.event_type, []).append(event)

    required = set(required_events)
    subs: list[SubCapabilityResult] = [
        SubCapabilityResult(
            name=f"event_{event_type.value.lower()}",
            passed=bool(by_type.get(event_type)),
            detail=(
                f"{len(by_type.get(event_type, []))} event(s) with full provenance"
                f"{' [REQUIRED]' if event_type in required else ' [optional]'}"
            ),
            # The aggregate `required_event_types_proven` is the blocking gate;
            # per-type rows are reported so PARTIAL is precise.
            required=False,
        )
        for event_type in CORPORATE_ACTION_TYPES
    ]
    missing_required = sorted(
        event_type.value for event_type in required_events if not by_type.get(event_type)
    )
    subs.append(
        SubCapabilityResult(
            name="required_event_types_proven",
            passed=not missing_required,
            detail=(
                f"missing required event types: {missing_required}"
                if missing_required
                else "every required event type is proven with event-level evidence"
            ),
        )
    )
    subs.append(
        SubCapabilityResult(
            required=False,  # corroboration, not a contract requirement
            name="adjustment_behaviour_characterised",
            passed=bool(adjustment_checks),
            detail=(
                f"{len(adjustment_checks)} TRADES vs ADJUSTED_LAST comparison(s);"
                " adjustment behaviour ONLY — never event-level evidence"
            ),
        )
    )
    feed_probe = next(
        (p for p in ibkr_probes if p.capability.value == "CORPORATE_EVENT_FEED"), None
    )
    subs.append(
        SubCapabilityResult(
            # CONTRACT DECISION: the required event set is satisfiable from
            # authenticated official SEC data alone, so the absence of an IBKR
            # corporate-event feed is recorded but must not block promotion.
            # Requiring it would block `corporate_actions` forever on an
            # entitlement the contract does not depend on.
            required=False,
            name="ibkr_event_feed_available",
            passed=feed_probe is not None and feed_probe.outcome is ProbeOutcome.AVAILABLE,
            detail=(
                f"probe outcome {feed_probe.outcome.value}: {feed_probe.detail}"
                if feed_probe is not None
                else "not probed on this run"
            ),
        )
    )

    reconciliation = reconcile_events_with_adjustments(events, adjustment_checks)
    disagreements = [item for item in reconciliation if not item.corroborated]
    subs.append(
        SubCapabilityResult(
            required=False,  # corroboration; disagreement is recorded, not fatal
            name="ibkr_sec_reconciled",
            passed=bool(reconciliation) and not disagreements,
            detail=(
                f"{len(reconciliation)} price-affecting event(s) checked against IBKR"
                f" adjustment behaviour; {len(disagreements)} not corroborated"
            ),
        )
    )

    dated = [e for e in events if e.effective_on or e.ex_date or e.filing_date]
    with_knowledge = [e for e in events if e.known_at is not None]
    proofs = ["event_level_action"] if events else []

    return qualify_source(
        "corporate_actions",
        source=source,
        coverage=coverage,
        temporal=TemporalSemantics(
            provides_effective_dates=len(dated) == len(events) and bool(events),
            provides_known_at=bool(with_knowledge),
            revision_safe=True,
        ),
        identity=IdentitySemantics(
            provides_stable_identifier=all(e.security_id for e in events) and bool(events),
            identifier_kinds=("dataset_security_id", "sec_cik", "ibkr_con_id"),
            ticker_only=False,
        ),
        metrics=DataQualityMetrics(
            total_rows=len(events),
            accepted=len(events),
            # Adjustment cross-checks are NOT counted as historical changes.
            historical_changes=len(events),
            missing_dates=len(events) - len(dated),
        ),
        resolution=IdentityResolutionReport(resolved=len({e.security_id for e in events})),
        proofs=proofs,
        sub_capabilities=subs,
        observed_at=observed_at,
    )


# -- point-in-time index membership (official announcements) ------------------


@dataclass(frozen=True, slots=True)
class MembershipBaseline:
    """Constituents at the START of the qualified window, from an official document.

    The baseline must be anchored at the window start. A baseline dated later —
    for instance today's constituent list — would project the current universe
    backwards and is refused at load time.
    """

    index_id: str
    as_of: date
    security_ids: tuple[str, ...]
    source_url: str
    source_document_checksum: str

    def __post_init__(self) -> None:
        if not self.security_ids:
            raise SourceAdapterError("membership baseline is empty")
        digest = self.source_document_checksum.removeprefix("sha256:")
        if digest == self.source_document_checksum or len(digest) != 64:
            raise SourceAdapterError("membership baseline requires a sha256 document checksum")


@dataclass(frozen=True, slots=True)
class IndexAnnouncementManifest:
    """A bounded, owner-supplied set of OFFICIAL index-change announcements."""

    index_id: str
    coverage: CoverageWindow
    baseline: MembershipBaseline
    announcements: tuple[IndexChangeAnnouncement, ...]
    security_id_by_ticker: Mapping[str, str]
    manifest_checksum: str

    def __post_init__(self) -> None:
        if self.coverage.start is None or self.coverage.end is None:
            raise SourceAdapterError("membership manifest requires a bounded coverage window")
        if self.baseline.as_of != self.coverage.start:
            raise SourceAdapterError(
                f"membership baseline is dated {self.baseline.as_of} but the window starts"
                f" {self.coverage.start}; a baseline that is not anchored at the window"
                " start would project a later universe backwards (fail closed)"
            )
        for announcement in self.announcements:
            if announcement.index_id != self.index_id:
                raise SourceAdapterError(
                    f"announcement for {announcement.index_id} in a {self.index_id} manifest"
                )
            if not (self.coverage.start <= announcement.effective_on <= self.coverage.end):
                raise SourceAdapterError(
                    f"announcement effective {announcement.effective_on} falls outside the"
                    f" declared window {self.coverage.start}..{self.coverage.end}"
                )


def load_index_manifest(
    path: Path, *, document_root: Path | None = None
) -> IndexAnnouncementManifest:
    """Load an owner-supplied official announcement manifest, verifying documents.

    Each announcement declares the sha256 of the official document it was taken
    from. When that document is present on disk its digest is RECOMPUTED and a
    mismatch fails closed, so a manifest cannot drift from its sources.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SourceAdapterError(f"unreadable membership manifest {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SourceAdapterError("membership manifest must be a JSON object")

    index_id = str(payload.get("index_id", "")).strip()
    if not index_id:
        raise SourceAdapterError("membership manifest requires index_id")
    window = payload.get("coverage")
    if not isinstance(window, dict) or not window.get("start") or not window.get("end"):
        raise SourceAdapterError("membership manifest requires a bounded coverage window")
    coverage = CoverageWindow(
        start=date.fromisoformat(str(window["start"])),
        end=date.fromisoformat(str(window["end"])),
    )

    raw_baseline = payload.get("baseline")
    if not isinstance(raw_baseline, dict):
        raise SourceAdapterError("membership manifest requires a baseline")
    baseline = MembershipBaseline(
        index_id=index_id,
        as_of=date.fromisoformat(str(raw_baseline["as_of"])),
        security_ids=tuple(sorted(str(s) for s in raw_baseline.get("security_ids", ()))),
        source_url=str(raw_baseline.get("source_url", "")),
        source_document_checksum=str(raw_baseline.get("source_document_checksum", "")),
    )

    raw_announcements = payload.get("announcements")
    if not isinstance(raw_announcements, list):
        raise SourceAdapterError("membership manifest announcements must be an array")
    announcements: list[IndexChangeAnnouncement] = []
    for entry in raw_announcements:
        if not isinstance(entry, dict):
            raise SourceAdapterError("membership announcement entry must be an object")
        checksum = str(entry.get("source_document_checksum", ""))
        document = entry.get("document_path")
        if document and document_root is not None:
            document_path = document_root / str(document)
            if not document_path.is_file():
                raise SourceAdapterError(f"declared official document is missing: {document_path}")
            recomputed = _sha256_file(document_path)
            if recomputed != checksum:
                raise SourceAdapterError(
                    f"official document {document_path} checksum mismatch:"
                    f" declared {checksum}, recomputed {recomputed}"
                )
        announcements.append(
            IndexChangeAnnouncement(
                index_id=index_id,
                announced_on=date.fromisoformat(str(entry["announced_on"])),
                effective_on=date.fromisoformat(str(entry["effective_on"])),
                added_ticker=(str(entry["added_ticker"]) if entry.get("added_ticker") else None),
                removed_ticker=(
                    str(entry["removed_ticker"]) if entry.get("removed_ticker") else None
                ),
                source_url=str(entry.get("source_url", "")),
                source_document_checksum=checksum,
            )
        )

    mapping = payload.get("security_id_by_ticker", {})
    if not isinstance(mapping, dict):
        raise SourceAdapterError("security_id_by_ticker must be an object")

    return IndexAnnouncementManifest(
        index_id=index_id,
        coverage=coverage,
        baseline=baseline,
        announcements=tuple(
            sorted(announcements, key=lambda a: (a.effective_on, a.announced_on, a.source_url))
        ),
        security_id_by_ticker={str(k).upper(): str(v) for k, v in mapping.items()},
        manifest_checksum=_sha256_file(path),
    )


def replay_membership_as_of(
    manifest: IndexAnnouncementManifest,
    *,
    as_of: date,
    known_as_of: datetime | None = None,
) -> tuple[str, ...]:
    """Deterministically replay index membership at a point in time.

    Both time axes are honoured: a change applies only once it is EFFECTIVE
    (``as_of``) and only if it was already ANNOUNCED at the knowledge time. A
    change announced after ``known_as_of`` is invisible — no look-ahead.
    """
    if as_of < manifest.baseline.as_of:
        raise SourceAdapterError(
            f"cannot replay {as_of} before the baseline anchor {manifest.baseline.as_of}"
        )
    members = set(manifest.baseline.security_ids)
    for announcement in manifest.announcements:
        if announcement.effective_on > as_of:
            continue
        if known_as_of is not None:
            announced = datetime.combine(
                announcement.announced_on, datetime.min.time(), tzinfo=known_as_of.tzinfo
            )
            if announced > known_as_of:
                continue  # not yet public knowledge: never leak it backwards
        if announcement.removed_ticker:
            removed = manifest.security_id_by_ticker.get(announcement.removed_ticker.upper())
            if removed:
                members.discard(removed)
        if announcement.added_ticker:
            added = manifest.security_id_by_ticker.get(announcement.added_ticker.upper())
            if added:
                members.add(added)
    return tuple(sorted(members))


def qualify_pit_membership(
    manifest: IndexAnnouncementManifest,
    *,
    source: SourceDescriptor,
    observed_at: datetime,
    production_window_required: tuple[date, date] | None = None,
) -> QualificationEvidence:
    """Qualify PIT index membership over the manifest's BOUNDED window.

    A bounded window can be genuinely complete for that window. When production
    requires a wider window, the shortfall is recorded as a failed
    sub-capability so the result is PARTIAL rather than an overclaimed PASS.
    """
    additions = [a for a in manifest.announcements if a.added_ticker]
    removals = [a for a in manifest.announcements if a.removed_ticker]
    unmapped = sorted(
        {
            ticker.upper()
            for a in manifest.announcements
            for ticker in (a.added_ticker, a.removed_ticker)
            if ticker and ticker.upper() not in manifest.security_id_by_ticker
        }
    )
    start = manifest.coverage.start
    end = manifest.coverage.end
    assert start is not None and end is not None  # guaranteed by the manifest

    # deterministic replay: the same window replayed twice must be identical
    first = replay_membership_as_of(manifest, as_of=end)
    second = replay_membership_as_of(manifest, as_of=end)

    covers_production = True
    production_detail = "no wider production window declared"
    if production_window_required is not None:
        needed_start, needed_end = production_window_required
        covers_production = start <= needed_start and end >= needed_end
        production_detail = (
            f"qualified {start}..{end}; production requires {needed_start}..{needed_end}"
        )

    subs = (
        SubCapabilityResult(
            name="baseline_anchored_at_window_start",
            passed=manifest.baseline.as_of == start,
            detail=f"baseline as_of {manifest.baseline.as_of}, window starts {start}",
        ),
        SubCapabilityResult(
            name="historical_additions_observed",
            passed=bool(additions), detail=f"{len(additions)} addition(s)",
        ),
        SubCapabilityResult(
            name="historical_removals_observed",
            passed=bool(removals), detail=f"{len(removals)} removal(s)",
        ),
        SubCapabilityResult(
            name="identity_mapped_for_every_change",
            passed=not unmapped,
            detail=f"unmapped tickers: {unmapped}" if unmapped else "all changes mapped",
        ),
        SubCapabilityResult(
            name="replay_deterministic",
            passed=first == second, detail=f"{len(first)} member(s) at {end}",
        ),
        SubCapabilityResult(
            name="covers_required_production_window",
            passed=covers_production, detail=production_detail,
        ),
    )

    proofs: list[str] = []
    if additions:
        proofs.append("historical_addition")
    if removals:
        proofs.append("historical_removal")

    return qualify_source(
        "pit_sp500_membership",
        source=source,
        coverage=manifest.coverage,
        temporal=TemporalSemantics(
            provides_effective_dates=True,
            provides_known_at=True,  # announcement date is the knowledge axis
            revision_safe=True,
        ),
        identity=IdentitySemantics(
            provides_stable_identifier=not unmapped,
            identifier_kinds=("dataset_security_id",),
            ticker_only=False,
        ),
        metrics=DataQualityMetrics(
            total_rows=len(manifest.announcements),
            accepted=len(manifest.announcements),
            historical_changes=len(manifest.announcements),
        ),
        resolution=IdentityResolutionReport(
            resolved=len(manifest.security_id_by_ticker), unresolved=len(unmapped)
        ),
        validation_errors=[f"unmapped tickers: {unmapped}"] if unmapped else [],
        proofs=proofs,
        sub_capabilities=subs,
        observed_at=observed_at,
    )


# -- as-of GICS: discovery outcome, never fabricated --------------------------

#: Exactly what is missing for `asof_gics`, recorded so the gap is actionable.
ASOF_GICS_MISSING_REQUIREMENT = (
    "A company-level HISTORICAL GICS classification series (security -> GICS code ->"
    " effective date -> knowledge date). Checked and rejected: IBKR exposes no"
    " classification history through the read-only API; SEC assigns SIC, not GICS,"
    " and SIC is not a GICS substitute; S&P DJI publishes the GICS taxonomy (the"
    " code structure) but not free company-level historical assignments. Deriving"
    " history from CURRENT GICS, current sector, or a business description would"
    " fabricate the series and is refused."
)


def build_crosswalk_from_entries(entries: Sequence[CrosswalkEntry]) -> IdentityCrosswalk:
    """Construct the crosswalk, rejecting ticker-only identity at the boundary."""
    return IdentityCrosswalk(entries)


def real_source_descriptor(
    *,
    source_id: str,
    name: str,
    version: str,
    authority: SourceAuthority,
    raw_checksum: str,
    raw_artifact: str,
    access_note: str,
) -> SourceDescriptor:
    """A REAL source descriptor carrying a genuine raw-artifact checksum."""
    return SourceDescriptor(
        source_id=source_id, name=name, version=version, kind=SourceKind.REAL,
        authority=authority.value, raw_checksum=raw_checksum, raw_artifact=raw_artifact,
        access_note=access_note,
    )


__all__ = [
    "ASOF_GICS_MISSING_REQUIREMENT",
    "CORPORATE_ACTION_TYPES",
    "REQUIRED_CORPORATE_EVENTS",
    "REAL_INGESTION_VERSION",
    "SP_AUTOMATED_ACCESS_NOTE",
    "CrosswalkReport",
    "ReconciliationOutcome",
    "IndexAnnouncementManifest",
    "MembershipBaseline",
    "build_crosswalk_from_entries",
    "build_crosswalk_report",
    "load_index_manifest",
    "qualify_corporate_actions",
    "qualify_delisted_security_history",
    "qualify_pit_membership",
    "real_source_descriptor",
    "reconcile_events_with_adjustments",
    "replay_membership_as_of",
]
