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
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

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
from trading_market_data_core.dataset_v1_sources import (
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
    sec_filings_to_events,
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


def _now() -> datetime:
    return datetime.now(tz=UTC)


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
    fetcher = UrllibHttpFetcher()
    client = SecEdgarClient(fetcher)
    index_raw = client.full_index(year, quarter, fetcher=fetcher)
    consumed: list[bytes] = [index_raw]
    index_url = f"{SecEdgarClient.ARCHIVES}/edgar/full-index/{year}/QTR{quarter}/master.idx"
    rows = parse_master_index(index_raw)
    candidates = discover_delisting_candidates(rows, limit=limit)

    filings: list[SecFilingRecord] = []
    profiles: list[SecEntityProfile] = []
    for row in candidates:
        raw, parsed = client.submissions(row.cik)
        consumed.append(raw)
        profiles.append(parse_sec_entity_profile(raw, parsed))
        filings.extend(parse_sec_submissions(raw, parsed))
    # Authenticate EVERY response the run consumed, not just the index.
    return filings, profiles, combined_artifact_checksum(consumed), index_url


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
    evidence = qualify_delisted_security_history(
        filings,
        crosswalk=crosswalk,
        security_id_by_cik=security_id_by_cik,
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
    path = args.evidence_dir / "delisted_security_history.json"
    checksum = evidence.write(path)
    _report(evidence)
    _write(args.evidence_dir, "delisted_security_history", checksum)
    return _exit_code(evidence)


def _load_ibkr_probes(path: Path | None) -> tuple[list[IbkrCapabilityProbe], list[str]]:
    """Read the IBKR corporate artifact produced on the owner host."""
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
    notes = [str(note) for note in payload.get("notes", [])]
    return probes, notes


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
    probes, _ = _load_ibkr_probes(args.ibkr_artifact)
    if not events:
        print("NO_REAL_EVENTS: nothing ingested; capability stays blocked")
        return EXIT_FAIL
    dates = sorted(e.filing_date for e in events if e.filing_date)
    evidence = qualify_corporate_actions(
        events,
        adjustment_checks=(),
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
    path = args.evidence_dir / "corporate_actions.json"
    checksum = evidence.write(path)
    _report(evidence)
    _write(args.evidence_dir, "corporate_actions", checksum)
    return _exit_code(evidence)


def cmd_sp_membership(args: argparse.Namespace) -> int:
    if not args.manifest.is_file():
        print(f"NOT_AVAILABLE: no official announcement manifest at {args.manifest}")
        print(SP_AUTOMATED_ACCESS_NOTE)
        return EXIT_NOT_AVAILABLE
    manifest = load_index_manifest(args.manifest, document_root=args.manifest.parent)
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
    path = args.evidence_dir / "pit_sp500_membership.json"
    checksum = evidence.write(path)
    _report(evidence)
    _write(args.evidence_dir, "pit_sp500_membership", checksum)
    return _exit_code(evidence)


def cmd_crosswalk(args: argparse.Namespace) -> int:
    _, profiles, _, _ = _discover_and_fetch(
        year=args.year, quarter=args.quarter, limit=args.limit
    )
    crosswalk = _crosswalk_from_profiles(profiles)
    report = build_crosswalk_report(
        crosswalk, ciks=[p.cik for p in profiles], entry_count=len(profiles)
    )
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = args.evidence_dir / "identity-crosswalk.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    print(f"crosswalk written: {path}")
    return EXIT_PASS if report.clean else EXIT_PARTIAL


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
