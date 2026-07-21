# POI-MD-001 — realtime IBKR market-data sub-gate — Evidence

**Sub-gate status: PASS** (owner manual run, 2026-07-21).
**Aggregate POI-MD-001 status: BLOCKED — unchanged.**

## Scope and non-scope (read this first)

This record proves **one subcomponent** of POI-MD-001: that the **IBKR**
realtime market-data path works on the owner host, read-only. The canonical
POI-MD-001 PASS criterion in the frozen backlog is broader —
*"field ownership matrix and provider decision satisfy FAST/STANDARD freshness
and point-in-time needs"*, with steps that include **benchmarking an external
provider** and **testing outage/fallback**. Those are **not** covered here, so
the aggregate PoI stays BLOCKED.

This evidence does **NOT**:

- make POI-MD-001 PASS (external provider benchmark + fallback + field
  ownership matrix + provider ADR are still missing);
- prove the POI-IBKR-001 order lifecycle (submit / ack / fill / cancel /
  protection / reconciliation — **never run**);
- verify any Paper lifecycle;
- enable PAPER_AUTO or LIVE (both remain prohibited);
- prove any strategy profitability (every strategy remains
  `PROFITABILITY_NOT_PROVEN`).

**No order, cancel or submit operation was invoked**; the harness exposes no
such method (read-only allowlist enforced).

## Run environment

| Field | Value |
|---|---|
| Date | 2026-07-21 |
| Host class | Oracle Linux ARM64 (`aarch64`) |
| IB Gateway | 10.48 (Paper) |
| IBKR Python API | official `ibapi` 10.48.1 (source distribution; see runbook §4b) |
| Endpoint | `127.0.0.1:4002` (localhost + paper API port only) |
| Account | Paper account confirmed, **masked** (`DU*****xx`) |
| Symbol probed | AAPL |
| Harness | `scripts/poi_ibkr_market_data_readonly.py` (read-only allowlist) |
| Session module | `trading_broker_ibkr.poi_market_data_session` |
| Owner log (host-local, not committed) | `/tmp/poi-md-001-realtime-2026-07-21.log` |

Prerequisite: this run was only possible after the IBKR 10.48 error-callback
compatibility fix (PR #36, commit `4a14210`), which removed the `TypeError`
that had been masking the real broker error.

## Structural results (no transient prices recorded)

Per the repository evidence convention, market prices are **not** copied into
this long-lived record; only structural facts are kept.

| Check | Result |
|---|---|
| Endpoint gate (127.0.0.1 + 4002 only) | PASS |
| Paper account gate (DU prefix, masked) | PASS |
| API health (`reqCurrentTime`) | PASS |
| Contract resolution (AAPL, single contract) | PASS |
| Entitlement / permission errors | **none** |
| `marketDataType` served | **1 (REALTIME)** |
| Canonical freshness classification | **REALTIME** |
| bid / ask / last present and valid (finite, > 0) | PASS |
| Quote sizes returned | PASS |
| Timestamp received (`received_at` = local capture time) | PASS |
| Clean disconnect | PASS |
| Harness verdict | `RESULT: PASS` |
| `EXECUTION_ELIGIBLE` (data-freshness indicator only) | `true` |

> `EXECUTION_ELIGIBLE: true` means only that the **data freshness** would
> satisfy a realtime decision gate. It does **not** authorize execution:
> execution remains gated by POI-IBKR-001 and the Evidence Roadmap.

## What remains before POI-MD-001 can be PASS

1. External market-data provider benchmark (coverage, latency P50/P95/P99,
   completeness).
2. Outage / fallback test between primary and fallback providers.
3. Field ownership matrix (which provider owns quotes, bars, corporate
   actions, calendar).
4. Provider decision recorded as an ADR.

Until all four exist and the owner marks it, POI-MD-001 remains **BLOCKED**.

## Safety attestation

- Read-only allowlist enforced; no order/cancel/submit method is callable.
- Localhost + paper port only; the live API port is rejected by construction.
- No credentials, secrets or full account ids appear in this file or in any
  harness output; the account id is masked.
- No PoI status other than this explicitly-scoped sub-gate was changed.
