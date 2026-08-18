"""Dataset V1 real-source ingestion CLI (owner host).

Commands::

    sec-delisting   discover + ingest REAL SEC delisting filings, write evidence
    sec-corporate   build canonical corporate events from SEC (+ IBKR probes)
    sp-membership   ingest an OFFICIAL S&P announcement manifest, write evidence
    crosswalk       write the identity crosswalk report
    gics            report the exact missing requirement for asof_gics

Every command writes an AUTHENTICATED evidence artifact whose checksum is
recomputed on load. Nothing here fabricates data: with no real response, no
evidence is written and the capability simply stays blocked.

Network access happens only here (owner host), never in tests. SEC access
requires ``SEC_EDGAR_USER_AGENT`` with a real contact and fails closed without
it.

Usage:
    uv run --no-sync python -m trading_market_data_core.dataset_v1_ingest_cli \\
        <command> [options]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

from trading_market_data_core.dataset_v1_artifacts import (
    ArtifactType,
    AuxiliaryReport,
    CrosswalkArtifact,
    CrosswalkSourceBinding,
)
from trading_market_data_core.dataset_v1_form25 import (
    assert_document_correlates,
    candidate_signals,
    effective_dates_by_accession,
    parse_form25_document,
    primary_document_url,
    summarize_form25,
)
from trading_market_data_core.dataset_v1_qualification import (
    CoverageWindow,
    QualificationEvidence,
    QualificationResult,
    SourceDescriptor,
    SourceKind,
)
from trading_market_data_core.dataset_v1_real_ingestion import (
    ASOF_GICS_MISSING_REQUIREMENT,
    SP_AUTOMATED_ACCESS_NOTE,
    build_crosswalk_report,
    load_index_manifest,
    qualify_corporate_actions,
    qualify_delisted_security_history,
    qualify_pit_membership,
    real_source_descriptor,
)
from trading_market_data_core.dataset_v1_run import (
    StageOutcome,
    file_checksum,
)
from trading_market_data_core.dataset_v1_sources import (
    XBRL_DIVIDEND_CONCEPTS,
    XBRL_SPLIT_CONCEPTS,
    AdjustmentCrossCheck,
    CanonicalCorporateEvent,
    CrosswalkEntry,
    IbkrCapabilityProbe,
    IbkrReferenceCapability,
    IdentityCrosswalk,
    ProbeOutcome,
    SecEdgarClient,
    SecEntityProfile,
    SecFilingRecord,
    SourceAdapterError,
    SourceAuthority,
    UrllibHttpFetcher,
    combined_artifact_checksum,
    discover_delisting_candidates,
    parse_master_index,
    parse_sec_entity_profile,
    parse_sec_submissions,
    parse_xbrl_concept,
    sec_filings_to_events,
)
from trading_market_data_core.dataset_v1_xbrl import (
    NormalizationReport,
    XbrlObservation,
    normalize_observations,
    observations_from_facts,
    observations_to_events,
)

DEFAULT_EVIDENCE_DIR = Path("docs/poi/evidence/dataset-v1")

#: Exit codes the owner runner maps to SOURCE EXECUTION status. These describe
#: what the run actually achieved, never what was merely configured.
EXIT_PASS = 0          #: real evidence, promotion eligible
EXIT_FAIL = 1          #: ran, but nothing usable was ingested
EXIT_SOURCE_ERROR = 2  #: the source could not be read (access/shape failure)
EXIT_PARTIAL = 3       #: real evidence written, genuinely incomplete
EXIT_NOT_AVAILABLE = 4 #: no legally/technically reproducible path on this host


def _exit_code(evidence: QualificationEvidence) -> int:
    if evidence.promotion_eligible:
        return EXIT_PASS
    if evidence.result is QualificationResult.PARTIAL:
        return EXIT_PARTIAL
    return EXIT_FAIL


def _report(evidence: QualificationEvidence) -> None:
    print(f"result: {evidence.result.value}  promotion_eligible: {evidence.promotion_eligible}")
    for sub in sorted(evidence.sub_capabilities, key=lambda s: s.name):
        print(f"  sub {sub.name}: {'PASS' if sub.passed else 'FAIL'} - {sub.detail}")
    for reason in evidence.blocked_reasons:
        print(f"  blocked: {reason}")


def _force_utf8_stdout() -> None:
    """A console codepage must never abort a qualification run mid-way.

    Without this, a single non-ASCII character in a report raises
    UnicodeEncodeError on a non-UTF-8 console and the run dies after doing the
    real work but before writing its evidence.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")



def _staged_path(args: argparse.Namespace, filename: str) -> Path:
    """Where a stage writes before its artifact is accepted.

    With no run id the command behaves as before (direct write), so the module
    stays usable outside an owner run.
    """
    evidence_dir: Path = args.evidence_dir
    if not getattr(args, "run_id", ""):
        return evidence_dir / filename
    run_root: Path = args.run_root
    staging = run_root / str(args.run_id) / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    return staging / filename


def _publish(args: argparse.Namespace, filename: str, outcome: StageOutcome) -> Path | None:
    """Publish a staged artifact ONLY when its stage completed normally.

    A FAILED stage publishes nothing, which is what stops a previous run's
    artifact from standing in for this run's missing output.
    """
    staged = _staged_path(args, filename)
    if not staged.is_file():
        return None
    if not outcome.may_publish:
        staged.unlink(missing_ok=True)
        return None
    evidence_dir: Path = args.evidence_dir
    if staged.parent == evidence_dir:
        return staged
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / filename
    shutil.move(str(staged), str(target))
    print(f"published: {target} ({file_checksum(target)})")
    return target


def _now() -> datetime:
    return datetime.now(tz=UTC)


@lru_cache(maxsize=1)
def _sec_transport() -> tuple[UrllibHttpFetcher, SecEdgarClient]:
    """ONE fetcher + client for the whole process.

    Every stage shares this so SEC's published request ceiling is respected
    across the run. Separate clients would each open their own unpaced budget
    and the effective rate would be a multiple of the limit.
    """
    fetcher = UrllibHttpFetcher()
    return fetcher, SecEdgarClient(fetcher)


def _discover_and_fetch(
    *,
    year: int,
    quarter: int,
    limit: int,
) -> tuple[list[SecFilingRecord], list[SecEntityProfile], str, str]:
    """Discover real delisting filings, then fetch each filer's full history.

    Returns (filings, profiles, index_checksum, index_url). Bounded: one
    quarterly index plus at most ``limit`` submission requests.
    """
    fetcher, client = _sec_transport()
    index_raw = client.full_index(year, quarter, fetcher=fetcher)
    consumed: list[bytes] = [index_raw]
    index_url = f"{SecEdgarClient.ARCHIVES}/edgar/full-index/{year}/QTR{quarter}/master.idx"
    rows = parse_master_index(index_raw)
    candidates = discover_delisting_candidates(rows, limit=limit)

    filings: list[SecFilingRecord] = []
    profiles: list[SecEntityProfile] = []
    # ROOT CAUSE of the real run's duplicate ids: a filer that filed more than
    # one Form 25 in the quarter appeared as several candidate ROWS, so its
    # submissions and XBRL facts were ingested once per row. Identity is the
    # FILER, not the filing, so each CIK is fetched exactly once.
    seen_ciks: set[str] = set()
    for row in candidates:
        cik = row.cik.zfill(10)
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)
        raw, parsed = client.submissions(cik)
        consumed.append(raw)
        profiles.append(parse_sec_entity_profile(raw, parsed))
        filings.extend(parse_sec_submissions(raw, parsed))
    # Authenticate EVERY response the run consumed, not just the index.
    return filings, profiles, combined_artifact_checksum(consumed), index_url



def _fetch_form25_documents(
    filings: Sequence[SecFilingRecord], *, limit: int
) -> tuple[list[object], list[bytes], list[str]]:
    """Retrieve and parse the primary document of each real delisting filing.

    The submissions metadata never carries a delisting effective date; when that
    date exists at all it is inside the document. Anything unreachable is
    recorded as a note, never filled in.
    """
    from trading_market_data_core.dataset_v1_form25 import Form25Document

    fetcher, client = _sec_transport()
    documents: list[Form25Document] = []
    consumed: list[bytes] = []
    notes: list[str] = []
    delistings = [f for f in filings if f.form in ("25", "25-NSE")][:limit]
    for filing in delistings:
        try:
            url = primary_document_url(filing)
        except SourceAdapterError as error:
            notes.append(f"{filing.accession}: {error}")
            continue
        try:
            # a document from another filing must never be attached to this one
            assert_document_correlates(url, filing)
            client.pace()  # shared throttle: one budget for the whole run
            raw = fetcher.get_bytes(url, headers={"User-Agent": _sec_user_agent()})
        except SourceAdapterError as error:
            notes.append(f"{filing.accession}: document unreachable ({error})")
            continue
        consumed.append(raw)
        try:
            documents.append(parse_form25_document(raw, filing=filing))
        except SourceAdapterError as error:
            notes.append(f"{filing.accession}: document not parsed ({error})")
    return list(documents), consumed, notes


def _sec_user_agent() -> str:
    """The owner's declared contact. Fails closed exactly like the client does."""
    import os

    agent = os.environ.get("SEC_EDGAR_USER_AGENT", "")
    if not agent.strip():
        raise SourceAdapterError(
            "SEC automated access requires SEC_EDGAR_USER_AGENT with a real contact"
        )
    return agent


def _crosswalk_from_profiles(
    profiles: Sequence[SecEntityProfile], con_id_by_ticker: dict[str, int] | None = None
) -> IdentityCrosswalk:
    """Build the crosswalk from SEC's OWN identity data (never a local guess)."""
    con_ids = con_id_by_ticker or {}
    entries = [
        CrosswalkEntry(
            security_id=profile.security_id,
            sec_cik=profile.cik,
            ibkr_con_id=next(
                (con_ids[t] for t in profile.tickers if t in con_ids), None
            ),
            current_ticker=profile.tickers[0] if profile.tickers else None,
            historical_tickers=tuple(profile.tickers[1:]),
            primary_exchange=profile.exchanges[0] if profile.exchanges else None,
        )
        for profile in profiles
    ]
    return IdentityCrosswalk(entries)


def _write(evidence_dir: Path, capability: str, payload_checksum: str) -> None:
    print(f"evidence written: {evidence_dir / f'{capability}.json'}")
    print(f"evidence checksum: {payload_checksum}")


def cmd_sec_delisting(args: argparse.Namespace) -> int:
    filings, profiles, index_checksum, index_url = _discover_and_fetch(
        year=args.year, quarter=args.quarter, limit=args.limit
    )
    if not filings:
        print("NO_REAL_FILINGS: nothing ingested; capability stays blocked")
        return EXIT_FAIL
    crosswalk = _crosswalk_from_profiles(profiles)
    security_id_by_cik = {p.cik: p.security_id for p in profiles}
    dates = sorted(f.filing_date for f in filings)

    # Primary documents: the ONLY place a published delisting effective date
    # can come from. Nothing is derived from the statutory period.
    documents, doc_bytes, doc_notes = _fetch_form25_documents(filings, limit=args.limit)
    coverage_report = summarize_form25(documents)  # type: ignore[arg-type]
    effective = effective_dates_by_accession(documents)  # type: ignore[arg-type]
    if doc_bytes:
        # Chain the prior digest with the document bytes so the evidence
        # authenticates the documents as well as the discovery responses.
        index_checksum = combined_artifact_checksum(
            [index_checksum.encode("ascii"), *doc_bytes]
        )
    AuxiliaryReport(
        name="form25-primary-document-coverage",
        observed_at=_now(),
        findings=tuple(coverage_report.findings) + tuple(doc_notes),
        counts={
            "documents": coverage_report.documents,
            "with_effective_date": coverage_report.with_effective_date,
            "with_last_trading_date": coverage_report.with_last_trading_date,
            "unparsed_documents": coverage_report.unparsed_documents,
        },
        notes=(
            "Element names observed in the real documents, for widening the"
            " allow-list from data rather than assumptions:",
            ", ".join(coverage_report.observed_elements) or "(none)",
        ),
    ).write(_staged_path(args, "form25-coverage.json"))
    print("== Form 25 primary-document coverage ==")
    for line in coverage_report.summary_lines():
        print(f"  {line}")
    _publish(args, "form25-coverage.json", StageOutcome.PARTIAL)

    evidence = qualify_delisted_security_history(
        filings,
        crosswalk=crosswalk,
        security_id_by_cik=security_id_by_cik,
        effective_dates=effective,
        source=real_source_descriptor(
            source_id="sec-edgar",
            name="SEC EDGAR (data.sec.gov + full-index)",
            version=f"{args.year}Q{args.quarter}",
            authority=SourceAuthority.OFFICIAL_PUBLIC,
            raw_checksum=index_checksum,
            raw_artifact=index_url,
            access_note=(
                "Declared User-Agent with a real contact; rate-limited below SEC's"
                " published ceiling; bounded to one quarterly index plus"
                f" {args.limit} submission request(s)."
            ),
        ),
        coverage=CoverageWindow(start=dates[0], end=dates[-1]),
        observed_at=_now(),
    )
    path = _staged_path(args, "delisted_security_history.json")
    checksum = evidence.write(path)
    _report(evidence)
    code = _exit_code(evidence)
    outcome = StageOutcome.SUCCEEDED if code == EXIT_PASS else (
        StageOutcome.PARTIAL if code == EXIT_PARTIAL else StageOutcome.FAILED
    )
    published = _publish(args, "delisted_security_history.json", outcome)
    if published is not None:
        _write(published.parent, "delisted_security_history", checksum)
    return code


def _load_ibkr_probes(
    path: Path | None,
) -> tuple[list[IbkrCapabilityProbe], list[AdjustmentCrossCheck]]:
    """Read the IBKR corporate artifact produced on the owner host.

    Adjustment observations are returned so they can corroborate ADJUSTMENT
    BEHAVIOUR. They are never turned into events.
    """
    if path is None or not path.is_file():
        return [], []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SourceAdapterError("IBKR corporate artifact must be a JSON object")
    probes: list[IbkrCapabilityProbe] = []
    for entry in payload.get("probes", []):
        if not isinstance(entry, dict):
            raise SourceAdapterError("IBKR probe entry must be an object")
        try:
            capability = IbkrReferenceCapability(str(entry["capability"]))
        except ValueError:
            continue  # a probe this dataset does not model is not an error
        probes.append(
            IbkrCapabilityProbe(
                capability=capability,
                outcome=ProbeOutcome(str(entry["outcome"])),
                detail=str(entry.get("detail", "")),
            )
        )
    adjustments: list[AdjustmentCrossCheck] = []
    for entry in payload.get("adjustment_observations", []):
        if not isinstance(entry, dict):
            raise SourceAdapterError("IBKR adjustment observation must be an object")
        bar_date = str(entry.get("bar_date", ""))
        try:
            observed_on = datetime.strptime(bar_date, "%Y%m%d").replace(tzinfo=UTC).date()
        except ValueError:
            try:
                observed_on = date.fromisoformat(bar_date)
            except ValueError:
                raise SourceAdapterError(
                    f"unparseable IBKR bar_date {bar_date!r}"
                ) from None
        adjustments.append(
            AdjustmentCrossCheck(
                symbol=str(entry.get("symbol", "")), observed_on=observed_on,
                trades_close=str(entry.get("trades_close", "")),
                adjusted_last_close=str(entry.get("adjusted_last_close", "")),
                diverges=bool(entry.get("diverges", False)),
            )
        )
    return probes, adjustments


def cmd_sec_corporate(args: argparse.Namespace) -> int:
    filings, profiles, index_checksum, index_url = _discover_and_fetch(
        year=args.year, quarter=args.quarter, limit=args.limit
    )
    security_id_by_cik = {p.cik: p.security_id for p in profiles}
    events: list[CanonicalCorporateEvent] = list(
        sec_filings_to_events(
            filings,
            security_id_by_cik=security_id_by_cik,
            source_id="sec-edgar",
            source_checksum=index_checksum,
        )
    )
    probes, adjustments = _load_ibkr_probes(args.ibkr_artifact)

    # SEC's own STRUCTURED financial data: real split and dividend facts with a
    # value, a period, an accession and a filing date. A concept the filer never
    # reported returns 404 and is recorded as unavailable, never invented.
    xbrl_notes: list[str] = []
    _, client = _sec_transport()
    normalizations: list[NormalizationReport] = []
    for profile in profiles:
        # Observations for ONE filer are normalized together, so a restatement
        # in a later filing folds into the same observation instead of becoming
        # a second event.
        observations: list[XbrlObservation] = []
        for concept in (*XBRL_DIVIDEND_CONCEPTS, *XBRL_SPLIT_CONCEPTS):
            try:
                raw, parsed = client.company_concept(profile.cik, "us-gaap", concept)
            except SourceAdapterError as error:
                xbrl_notes.append(f"{profile.cik} {concept}: unavailable ({error})")
                continue
            observations.extend(
                observations_from_facts(
                    parse_xbrl_concept(raw, parsed),
                    cik=profile.cik,
                    taxonomy="us-gaap",
                    source_checksum=combined_artifact_checksum([raw]),
                )
            )
        if not observations:
            continue
        try:
            report = normalize_observations(observations)
        except SourceAdapterError as error:
            # An irreconcilable pair of facts is a real finding, not a reason to
            # abandon the whole filer set.
            xbrl_notes.append(f"{profile.cik}: {error}")
            continue
        normalizations.append(report)
        events.extend(
            observations_to_events(
                report, security_id=profile.security_id, source_id="sec-xbrl"
            )
        )
        if report.duplicates_collapsed or report.revisions or report.repeats:
            xbrl_notes.append(
                f"{profile.cik}: {report.total_rows} fact row(s) ->"
                f" {len(report.observations)} observation(s)"
                f" (collapsed={report.duplicates_collapsed},"
                f" repeats={report.repeats}, revisions={report.revisions})"
            )

    candidates = sorted(
        {signal for filing in filings for signal in candidate_signals(filing.items)}
    )
    AuxiliaryReport(
        name="sec-corporate-action-candidates",
        observed_at=_now(),
        findings=tuple(xbrl_notes),
        counts={
            "candidate_signals": len(candidates),
            "events_minted": len(events),
            "xbrl_fact_rows": sum(r.total_rows for r in normalizations),
            "xbrl_observations": sum(len(r.observations) for r in normalizations),
            "xbrl_duplicates_collapsed": sum(r.duplicates_collapsed for r in normalizations),
            "xbrl_repeats": sum(r.repeats for r in normalizations),
            "xbrl_revisions": sum(r.revisions for r in normalizations),
        },
        notes=(
            "8-K item codes are published metadata but are AMBIGUOUS about what"
            " occurred; they are review leads and qualify NOTHING. Only parsed"
            " document/XBRL evidence mints an event.",
            *candidates,
        ),
    ).write(_staged_path(args, "corporate-action-candidates.json"))
    _publish(args, "corporate-action-candidates.json", StageOutcome.PARTIAL)

    if not events:
        print("NO_REAL_EVENTS: nothing ingested; capability stays blocked")
        return EXIT_FAIL
    dates = sorted(e.filing_date for e in events if e.filing_date)
    evidence = qualify_corporate_actions(
        events,
        adjustment_checks=adjustments,
        ibkr_probes=probes,
        source=real_source_descriptor(
            source_id="sec-edgar+ibkr",
            name="SEC EDGAR official filings (event level) + IBKR probes",
            version=f"{args.year}Q{args.quarter}",
            authority=SourceAuthority.OFFICIAL_PUBLIC,
            raw_checksum=index_checksum,
            raw_artifact=index_url,
            access_note=(
                "Event-level actions come from SEC filings. IBKR contributes"
                " adjustment BEHAVIOUR only, which is never counted as an event."
            ),
        ),
        coverage=(
            CoverageWindow(start=dates[0], end=dates[-1])
            if dates
            else CoverageWindow(None, None)
        ),
        observed_at=_now(),
    )
    path = _staged_path(args, "corporate_actions.json")
    checksum = evidence.write(path)
    _report(evidence)
    code = _exit_code(evidence)
    outcome = StageOutcome.SUCCEEDED if code == EXIT_PASS else (
        StageOutcome.PARTIAL if code == EXIT_PARTIAL else StageOutcome.FAILED
    )
    published = _publish(args, "corporate_actions.json", outcome)
    if published is not None:
        _write(published.parent, "corporate_actions", checksum)
    return code


def cmd_sp_membership(args: argparse.Namespace) -> int:
    if not args.manifest.is_file():
        print(f"NOT_AVAILABLE: no official announcement manifest at {args.manifest}")
        print(SP_AUTOMATED_ACCESS_NOTE)
        return EXIT_NOT_AVAILABLE
    # Official documents may live beside the manifest or in their own directory;
    # either way each declared checksum is RECOMPUTED from the real file.
    documents_dir = args.documents_dir if args.documents_dir else args.manifest.parent
    manifest = load_index_manifest(args.manifest, document_root=documents_dir)
    production = None
    if args.production_start and args.production_end:
        production = (
            date.fromisoformat(args.production_start),
            date.fromisoformat(args.production_end),
        )
    evidence = qualify_pit_membership(
        manifest,
        source=SourceDescriptor(
            source_id="spdji-official-announcements",
            name="S&P Dow Jones Indices official public announcements",
            version=manifest.manifest_checksum[:19],
            kind=SourceKind.REAL,
            authority=SourceAuthority.OFFICIAL_PUBLIC.value,
            raw_checksum=manifest.manifest_checksum,
            raw_artifact=str(args.manifest),
            access_note=SP_AUTOMATED_ACCESS_NOTE,
        ),
        observed_at=_now(),
        production_window_required=production,
    )
    path = _staged_path(args, "pit_sp500_membership.json")
    checksum = evidence.write(path)
    _report(evidence)
    code = _exit_code(evidence)
    outcome = StageOutcome.SUCCEEDED if code == EXIT_PASS else (
        StageOutcome.PARTIAL if code == EXIT_PARTIAL else StageOutcome.FAILED
    )
    published = _publish(args, "pit_sp500_membership.json", outcome)
    if published is not None:
        _write(published.parent, "pit_sp500_membership", checksum)
    return code


def cmd_crosswalk(args: argparse.Namespace) -> int:
    _, profiles, index_checksum, _ = _discover_and_fetch(
        year=args.year, quarter=args.quarter, limit=args.limit
    )
    crosswalk = _crosswalk_from_profiles(profiles)
    report = build_crosswalk_report(
        crosswalk, ciks=[p.cik for p in profiles], entry_count=len(profiles)
    )
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = _staged_path(args, "identity-crosswalk.json")
    artifact = CrosswalkArtifact(
        entries=report.entries,
        resolved=report.resolved,
        ambiguous=report.ambiguous,
        unresolved=report.unresolved,
        conflicting=report.conflicting,
        observed_at=_now(),
        sources=(
            CrosswalkSourceBinding(
                name="sec-edgar", version=f"{args.year}Q{args.quarter}",
                checksum=index_checksum,
            ),
        ),
    ).sealed()
    checksum = artifact.write(path)
    print(json.dumps(artifact.to_dict(), indent=2, sort_keys=True))
    print(f"crosswalk written: {path}")
    print(f"crosswalk artifact_type: {ArtifactType.IDENTITY_CROSSWALK.value}")
    print(f"crosswalk checksum: {checksum}")
    code = EXIT_PASS if report.clean else EXIT_PARTIAL
    _publish(
        args, "identity-crosswalk.json",
        StageOutcome.SUCCEEDED if code == EXIT_PASS else StageOutcome.PARTIAL,
    )
    return code


def cmd_gics(args: argparse.Namespace) -> int:
    del args
    print("asof_gics: BLOCKED_EXTERNAL")
    print(ASOF_GICS_MISSING_REQUIREMENT)
    return EXIT_NOT_AVAILABLE


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("sec-delisting", "sec-corporate", "sp-membership", "crosswalk", "gics"),
    )
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--quarter", type=int, default=1)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--ibkr-artifact", type=Path, default=None)
    parser.add_argument(
        "--manifest", type=Path, default=Path("config/data-sources/sp500-announcements.json")
    )
    parser.add_argument("--documents-dir", type=Path, default=None)
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--run-root", type=Path, default=Path("docs/poi/evidence/dataset-v1-runs")
    )
    parser.add_argument("--production-start", default="")
    parser.add_argument("--production-end", default="")
    _force_utf8_stdout()
    args = parser.parse_args(argv[1:])

    handlers = {
        "sec-delisting": cmd_sec_delisting,
        "sec-corporate": cmd_sec_corporate,
        "sp-membership": cmd_sp_membership,
        "crosswalk": cmd_crosswalk,
        "gics": cmd_gics,
    }
    try:
        return handlers[args.command](args)
    except SourceAdapterError as error:
        print(f"SOURCE_ERROR: {error}")
        return EXIT_SOURCE_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv))
