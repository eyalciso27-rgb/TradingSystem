# POI-MD-001 — realtime IBKR market-data observation for `F` — Evidence

**Observation result: PASS** (real owner-host read-only run, 2026-07-31).
**Aggregate POI-MD-001 status: BLOCKED — unchanged.**
**POI-IBKR-001 status: BLOCKED / non-PASS — unchanged.**

## Scope and non-scope (read this first)

This records **one successful realtime market-data observation** on the owner
host: `F` resolved, no entitlement error, `marketDataType=1 (REALTIME)`,
positive bid/ask/last, canonical freshness `REALTIME`, clean disconnect.

It does **NOT**:

- make POI-MD-001 PASS — the aggregate criterion additionally requires an
  **external provider benchmark**, outage/fallback testing, a field-ownership
  matrix and a provider ADR. That work is **deferred** (Track A / A2), so the
  aggregate stays **BLOCKED** (see `POI-MD-001-REALTIME.md`, DEF-14);
- make POI-IBKR-001 PASS — data freshness is not an order lifecycle;
- authorize any submission. `EXECUTION_ELIGIBLE: true` is a **data-quality**
  verdict only; the harness itself states that execution stays gated by
  POI-IBKR-001 regardless of data freshness;
- enable PAPER_AUTO or LIVE (both remain prohibited).

**No order, cancel or submit operation was invoked** — the read-only allowlist
makes no order API callable.

## ⚠ The quote below is HISTORICAL and must never be reused

The prices in this artifact were evidence for **that run, at that moment**, and
are now **stale**.

> **Never copy `bid=14.93`, `ask=14.97` or `last=14.94` — or any value derived
> from them — into a future dry run or `--execute` command.**

A cancel-path submission must be authorized by a quote observed **fresh,
immediately before the run** (`--quote-bid` / `--quote-ask` / `--quote-at` /
`--quote-session`). The harness refuses a quote older than 120 s and
re-validates it immediately before submitting, aborting on material drift —
this record exists to document what happened, not to supply an input.

Note also that `received_at` is the **local capture time**, not an exchange
timestamp; the artifact reports the exchange timestamp separately.

## Provenance

| Field | Value |
|---|---|
| artifact timestamp (UTC) | **2026-07-31T06:02:42Z** |
| quote captured at (UTC) | **2026-07-31T06:04:40.806158+00:00** (local capture time) |
| exchange timestamp (UTC) | **2026-07-31T05:32:20+00:00** |
| symbol | `F` |
| harness | `poi-ibkr-market-data-readonly-1.0.0` |
| endpoint | `127.0.0.1:4002` (localhost + paper API port only) |
| account | masked by the harness (`DU*****32`); account values withheld |
| host source | `/tmp/poi-ibkr-market-data-F-20260731T060242Z.log` |
| sha256 | `4f4879786214d41cb4d41bd2cde5105d5f0c6dfbcb03a63e4ba8494d2e49ceb0` |

## Verbatim artifact

Reproduced exactly as written by the harness. The account id is masked by the
harness itself; no credential, secret-store value or confirmation token appears
in it.

```text
POI-IBKR market-data read-only harness poi-ibkr-market-data-readonly-1.0.0
MODE: realtime
  [PASS] endpoint: 127.0.0.1:4002 (localhost + paper API port only)
  [PASS] connect: API socket connected
  [PASS] api_health: req_current_time answered
  [PASS] paper_account: account DU*****32
  [PASS] contract_resolution: F resolved (broker contract id present, not shown)
  [PASS] entitlement: no entitlement/permission error
  [PASS] market_data_type: marketDataType=1 (REALTIME)
  [PASS] quote_fields: bid=14.93 ask=14.97 last=14.94
  [PASS] quote_sizes: bid_size=196 ask_size=966 last_size=196 (must be >= 0; a size of 0 does not prove a usable quote)
  [PASS] timestamp: received_at=2026-07-31T06:04:40.806158+00:00 (local capture time, NOT an exchange timestamp); exchange_timestamp=2026-07-31T05:32:20+00:00
  [PASS] freshness_classification: canonical freshness=REALTIME (from IBKR REALTIME)
  [PASS] realtime_expectation: REALTIME as expected
  [PASS] disconnect: clean disconnect
RESULT: PASS
EXECUTION_ELIGIBLE: true
(read-only allowlist enforced; no order/cancel API was ever callable; account id masked, account values withheld; execution stays gated by POI-IBKR-001 regardless of data freshness)
```

## NOT EXECUTED

- ❌ no post-fix real cancel-path **execute** (DEF-18);
- ❌ no real **cancellation** after the latest changes;
- ❌ no **fill-path** owner-run;
- ❌ no Gateway **restart-recovery** cancellation (DEF-20);
- ❌ no **Live** connection and no Live order;
- ❌ no **PAPER_AUTO** activation.

**POI-MD-001 remains BLOCKED** (external-provider work deferred).
**POI-IBKR-001 remains BLOCKED / non-PASS.**
