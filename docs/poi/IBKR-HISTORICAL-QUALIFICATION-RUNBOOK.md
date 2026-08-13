# POI-IBKR-HIST — Read-only IBKR historical qualification runbook

> Smallest owner-authorized **READ-ONLY** run that empirically qualifies the
> owner's actual IBKR access for the frozen `baseline-trend-breakout-v1` dataset
> contract, against the existing **PAPER** Gateway. Companion code:
> `trading_broker_ibkr.poi_historical_data_client` +
> `trading_broker_ibkr.poi_historical_qualification`.

## Hard safety boundary (all mandatory)
- **NO** order / cancel / modify / fill / global-cancel — the client exposes no
  such surface (asserted in tests).
- **NO** account mutation, **NO** Gateway restart, **NO** systemd/IBC change.
- **PAPER only**: host `127.0.0.1`, port **4002** (live 4001 is rejected). The
  account id must start with `DU` or the run fails closed and disconnects.
- **Read-Only API = ON** in the Gateway; the owner logs in via the Gateway UI.
  No secret is read; the account id is only ever recorded **masked**.
- **Small sample only** — enough to prove schema/coverage, never a bulk S&P
  download.

## A. Exact command (owner's controlled host)
```bash
uv run python -m trading_broker_ibkr.poi_historical_qualification \
  --host 127.0.0.1 --port 4002 \
  --symbols AAPL,MSFT --patterns META \
  --account-alias paper-primary \
  --duration "10 D" \
  --out docs/poi/evidence/POI-IBKR-HIST.json
```

## B. What it queries (all read-only)
1. `reqCurrentTime` — connectivity.
2. `reqAccountSummary` (NetLiquidation) — **DU-paper gate only** (masked; no
   values surfaced).
3. Per symbol: `reqContractDetails` — **conId** + longName + primaryExchange
   (identity/provenance).
4. Per symbol: `reqHeadTimeStamp` (TRADES, useRTH=1) — **earliest available date**.
5. Per symbol × {**TRADES, BID, ASK, MIDPOINT, ADJUSTED_LAST**}:
   `reqHistoricalData` `barSize="1 day"`, `useRTH=1`, small `duration` — OHLCV
   schema, row counts, earliest/latest, and adjustment semantics (TRADES vs
   ADJUSTED_LAST).
6. Per pattern: `reqMatchingSymbols` — symbol-change / ticker-history discovery.

## C. Expected non-mutating effects
None on the account: no order, position, or balance changes. Only read requests
and their read-only cancels (`cancelHistoricalData`, `cancelHeadTimeStamp`,
`cancelAccountSummary`). Historical requests are pacing-limited by IBKR; the run
issues each request once and records any pacing code (no auto-retry).

## D. Pass / fail criteria (per capability)
| Capability | PASS when |
|---|---|
| `connectivity_paper_du_gate` | connected + account id starts with `DU` |
| `daily_trades_rth_schema` | ≥1 symbol returns 1-day TRADES bars with non-empty O/H/L/C **and** volume |
| `contract_identity_conid` | conId resolved for ≥1 symbol |
| `head_timestamp_coverage` | a head timestamp returned (depth recorded) |
| `bid_ask_midpoint` | PASS if BID/ASK/MIDPOINT bars returned; `PARTIAL` if entitlement-limited |
| `symbol_change_discovery` | matches returned for the pattern (`PARTIAL` if none) |

Overall run returns non-zero if any capability is `FAIL`. `sufficient_for_frozen_strategy`
is reported **for ACTIVE names only**.

## E. Records captured (sanitized evidence JSON)
- request parameters (symbols, whatToShow, barSize, useRTH, duration);
- returned field schema + first/last bar sample (values as strings);
- earliest (head timestamp) / latest bar dates; per-series **row counts**;
- `account_alias` + **masked** account id (never the full id); **conId** as
  provenance (a contract id, not an account id);
- any IBKR **pacing/error codes**; capability verdicts;
- a deterministic **sha256 evidence checksum**;
- explicit notes that this run does **not** prove delisted history, PIT S&P 500
  membership, or as-of GICS.

## Important
A successful request for **active** symbols is **NOT** proof that delisted
history is available. Delisted-name history, PIT S&P 500 membership and as-of
GICS remain external gaps regardless of this run's result. The commission
**schedule** is not a read-only API endpoint — read it from IBKR Account
Management (Reports); this run only proves connectivity.
