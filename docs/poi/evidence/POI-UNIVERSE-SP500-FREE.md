# POI-UNIVERSE-SP500-FREE — free CURRENT S&P 500 universe — Evidence

**Status: EMPIRICALLY VERIFIED** (owner-host run, 2026-08-19, after merged
PR #92).

## Scope and non-scope (read this first)

This records one narrow fact: the free public source produces a **current,
plausible, checksummed S&P 500 constituent list** on the owner host, and that
list is fit to become the last-known-good universe.

It does **NOT**:

- provide point-in-time membership history — this is CURRENT membership only,
  and substituting it for history is prohibited (ADR-0017);
- provide authoritative or licensed GICS — sector context here is
  `FREE_RECONSTRUCTED` and is optional context that never vetoes a trade;
- make any strategy tradable, or any readiness gate satisfied;
- authorize Paper Auto or Live, both of which remain prohibited;
- prove any market-data, order-lifecycle or execution capability.

## Run environment

| Field | Value |
|---|---|
| Date | 2026-08-19 |
| Host class | Oracle Linux ARM64 owner host |
| Command | `uv run --no-sync python -m trading_market_data_core.sp500_universe_cli refresh --force` then `status` |
| Code | merged PR #92 (`fix(universe): read the ticker cells the live page actually writes`) |
| Network | owner host only; CI is offline by design and never fetches this |

## Result

| Field | Value |
|---|---|
| mode | PAPER |
| status | **CURRENT_LIST_VALID** |
| source | `wikipedia:List_of_S&P_500_companies` |
| source authority | **PUBLIC_SECONDARY** |
| constituent count | **503** |
| as_of | 2026-08-19 |
| fetched_at | 2026-08-19T06:13:42.838289+00:00 |
| checksum | `sha256:479b2fa7f5658ebfe7bd20579c189845ed6bf28a6e52f647a284fdce096088f1` |
| point_in_time_history | **False** (always) |
| gics_mode | **FREE_RECONSTRUCTED** (optional context) |

503 rather than 500 is expected and correct: the index holds more *securities*
than *companies*, because several constituents list multiple share classes.
The plausible-count safety range (400–600) is unchanged and was not widened to
accommodate this.

## What this closes, and what it does not

**Closes:** the empirical gap left open by PR #91 and PR #92 — the free source
had never been exercised against the real endpoint from this repository, so it
could not honestly be called verified. It now has been, on the owner host, by
the owner.

**Does not close:** anything about *trading*. The universe is an operational
filter for strategies that declare `SP500_CURRENT`. A strategy that trades
`ALL_ELIGIBLE_US_EQUITIES` is unaffected by this record entirely (ADR-0017).

## Prior failure, for the record

The first owner-host attempt returned an empty snapshot from a healthy endpoint.
The validator correctly refused it. The cause was a parser assumption — ticker
cells were expected as `| AAPL` while the page writes exchange templates such as
`{{NasdaqSymbol|AAPL}}` — fixed in PR #92 along with bounded parse diagnostics
so the next upstream format change is diagnosable from a single run.

## Refresh and durability

The monthly refresh is automatic: the scheduler service (`python -m
trading_scheduler`, started by the canonical Paper compose stack) runs startup
maintenance and then ticks, reaching the source only when the ~30-day interval
has elapsed. A failed or implausible refresh never overwrites the
last-known-good list.

## Safety attestation

- Read-only public source over HTTPS; no authentication, no paywall, no
  anti-bot control was involved or bypassed.
- No licensed or redistribution-restricted bulk data is committed.
- No credentials, secrets or account identifiers appear in this record.
- The snapshot itself is owner-host state and is not committed to the
  repository; this file records the structural result only.
