# OWNER DECISION — A-WP2 PIT data requirements

Recorded at: 2026-09-01

This is an explicit owner decision for A-WP2 and must be treated as project and AutoDev decision evidence. Do not reinterpret or weaken it.

## Historical S&P 500 membership

Authoritative historical source: S&P Global / S&P Dow Jones Indices official announcements from spglobal.com.

Required point-in-time facts include additions, removals, effective dates, knowledge/publication dates where available, delisted constituents preserved in history, and immutable/reproducible historical snapshots.

Existing ingestion location: `config/data-sources/sp500-documents/`

Accepted artifacts: HTML, text, CSV, and text-layer PDF.

For every official artifact, preserve the original document, compute SHA-256, preserve provenance, preserve source URL/reference, preserve observed/downloaded timestamp, parse the announcement, derive membership events, and update/build `config/data-sources/sp500-announcements.json`.

Existing qualification command: `bash scripts/run_dataset_v1_real_qualification.sh`

Plain automated access to spglobal.com has previously returned HTTP 403 / robots protection. Do not bypass robots rules, bot protection, authentication, or access controls. If an official document cannot be retrieved automatically through a permitted path, mark the artifact acquisition requirement clearly and allow an owner-accessible permitted download to be ingested later.

Do not substitute secondary websites as authoritative historical truth without separate owner approval.

## Monthly current S&P 500 snapshots

Requirement: approximately monthly.

Implement or verify an automated scheduled archival mechanism for current S&P 500 membership. Every successful snapshot must be immutable and archived; never overwrite the previous snapshot.

Persist at minimum: source, source reference/URL, downloaded_at / observed_at, effective/as-of date when known, constituent identities, SHA-256, and parser/version metadata.

On refresh failure, preserve last-known-good, record failure explicitly, and never convert UNKNOWN to an empty universe.

These monthly snapshots build trustworthy forward PIT history from now on. They supplement but do not replace official historical add/drop announcements.

## Historical GICS

True requirement: security -> GICS classification -> effective date -> knowledge date.

Known findings:

- IBKR does not provide company-level historical GICS assignment history.
- SEC provides SIC, not GICS. SIC is not an acceptable substitute.
- S&P DJI taxonomy/code structure exists, but no approved/free company-level historical effective-dated assignment dataset has been established.
- InvestingPro / Investing.com does not currently provide an approved compliant historical GICS-as-of programmatic source for this requirement.

Therefore historical GICS backfill remains an `EXTERNAL_DATA_GAP` until a legitimate effective-dated company-level source is obtained.

Do not reconstruct historical GICS from current GICS, current sector, SIC, company descriptions, or present-day classifications. Do not fabricate PIT history.

## Forward GICS snapshots

If a permitted source provides current company-level GICS, begin prospective immutable monthly snapshots. This may create reliable forward GICS history starting from the first recorded snapshot. It does not backfill periods before collection began.

## Historical price / market data

IBKR remains the primary historical price source where available. Use the existing `HistoricalBarSource`.

Include as appropriate daily OHLCV, volume, exchange/calendar observations, and actual account commission evidence. Raw historical data must remain immutable and be bound into `DatasetManifest` evidence using hashes and no-look-ahead timestamps.

## Delisted / corporate-action evidence

SEC EDGAR remains an approved official evidence source for Form 25, delisting filings, filing knowledge timestamps, and corporate-action evidence where available.

SEC evidence is partial unless the required effective-date/event coverage is actually demonstrated. Do not overclaim coverage.

## Compliance boundary

Do not implement robots/403 bypass, unofficial APIs used to evade source restrictions, credential/access-control bypass, anti-bot evasion, or fabricated historical values.

Every external PIT artifact must retain provenance and SHA-256 evidence.

## Required distinction

Preserve the distinction between:

1. Historical reconstruction: official S&P add/drop announcements plus legitimate effective-dated sources.
2. Ongoing forward archive: monthly current-universe/current-classification snapshots.

Do not treat present state as historical state.

## Current V1 PIT reference requirement

The current V1 PIT reference requirement remains:

- historical S&P 500 membership with effective dates, including delisted names;
- historical/as-of GICS with effective dates;
- survivorship-free delisted-name price history;
- effective-dated corporate actions.

Historical NBBO remains secondary/soft where the frozen strategy's approved adverse-spread fallback applies.

Do not weaken these requirements merely to make qualification green.
