# POI-IBKR-001 — owner-host finding: fabricated adapter timestamps (NOT a PASS)

> **POI-IBKR-001 status: BLOCKED / NOT RUN. This document does not change it.**
> It records a read-only owner-host observation made after PR #52, the fix, and
> — explicitly — what has **not** been executed. Only the owner may change
> `docs/poi/STATUS.yaml`, and only from a fresh post-merge Paper owner-run.

## 1. What was observed (read-only)

No order was submitted, cancelled or modified to produce this finding.

| Observation | Value |
|---|---|
| host system time (UTC) | `2026-07-30T17:05:22Z` |
| first adapter timestamp | `2026-07-10T12:00:01Z` |
| following adapter timestamps | `...12:00:02Z`, `...12:00:03Z`, `...12:00:04Z` |
| open orders | `0` |
| non-zero positions | `1` — `DVLT` quantity `1352` (unrelated to any PoI run) |
| `verify-clean` output | `broker error BROKER_UNHEALTHY; no retry` — and nothing else |

The adapter timestamps advance exactly one second per call and begin roughly
**20 days before** the real host time. That is a synthetic tick sequence, not
clock drift.

## 2. Root cause (D-CLOCK-01)

`IbkrPaperAdapter.__init__` shipped a deterministic **test** clock as its
production default:

```python
self._now = clock or _deterministic_clock(datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
```

`_deterministic_clock(start)` returns `start + N seconds` on its Nth call. Any
adapter built without an explicit clock — i.e. every real owner-run — therefore
fabricated every timestamp it produced: account snapshots, order receipts,
append-only broker events, reconciliation results, health checks and recovered
open-order observations.

The harness's own clock (`run_lifecycle`) was already real UTC, so a single
evidence artifact mixed real and fabricated times about 20 days apart, and
event ordering across that boundary was meaningless.

**Not affected, verified explicitly:** quote-freshness validation (harness
clock + owner-supplied `--quote-at`), confirmation tokens and the operation
reference (parameter digests, no adapter timestamp), owner-run journal
timestamps (harness clock), and operation timeouts (already monotonic).

## 3. Second finding (D-CLOCK-02)

`verify-clean` collapsed to `broker error BROKER_UNHEALTHY; no retry`. The
first failing read aborted the whole scenario, so the owner could not tell a
dead Gateway from a reachable account that simply held a position — and the
`DVLT` holding never appeared in the artifact at all.

## 4. What changed

- The adapter's production default is now real timezone-aware UTC (`utc_now`).
  Injected clocks still work; a naive clock **fails closed**; a non-UTC clock is
  normalized. `_deterministic_clock` was deleted from the adapter and now lives
  in the test module. A guardrail test rejects any `datetime(20…)` literal
  reappearing in `broker-ibkr` production code.
- Evidence timestamps are UTC **wall-clock**; elapsed durations are measured on
  a **monotonic** source (the harness poll loop now takes an explicit monotonic
  clock, matching the callback router and the gateway-ready wait). The two are
  never mixed.
- Broker errors now render as
  `broker error <CODE>; <bounded sanitized message>; cause=<RootCauseType>;
  correlation_id=<id>; no retry`, with account-shaped text re-masked, path-like
  text stripped and a 200-character bound.
- Every read-only probe is recorded independently, and read-only scenarios
  continue past a failed preflight. Fail-closed behaviour is unchanged: a
  failed probe still records a FAILED check, so nothing is ever submitted on a
  failed preflight.

## 5. The unrelated Paper position

`DVLT` quantity `1352`, with zero open orders, is unrelated to any PoI
operation.

**Nothing in this bundle modifies, closes, protects or acts on it.** The
harness never flattens. `verify-clean` still FAILS while any non-zero position
exists — that is correct — but it now names the position explicitly
(`open: DVLT:1352`, plus a raw / non-zero / run-symbol row breakdown) and
prints the owner-action banner, instead of hiding it behind a generic broker
error. Closing it is the owner's decision, by hand, in the Gateway/TWS UI.

## 6. NOT EXECUTED

The defect was found by read-only inspection. Since discovery, no real IBKR
order and no real cancellation have been executed, and nothing here was
validated against a live Gateway:

- ❌ no cancel-path execute;
- ❌ no real cancellation after this change;
- ❌ no fill-path owner-run;
- ❌ no Gateway restart recovery owner-run;
- ❌ no Live connection and no Live order;
- ❌ no PAPER_AUTO activation.

## 7. Consequence for existing evidence

Every artifact produced by a real owner-run **before** this fix carries
fabricated adapter timestamps. Such artifacts must be **re-generated**, not
trusted, and cannot support a PASS.

Post-merge validation sequence (recorded, not executed):
`DEF-19-verify-clean-readonly` → `DEF-18-post-fix-cancellation-verification`
→ `DEF-20-restart-recovery-cancellation`, in
`docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.md`.

**POI-IBKR-001 remains BLOCKED / non-PASS.**
