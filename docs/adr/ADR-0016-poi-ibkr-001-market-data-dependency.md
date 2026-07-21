# ADR-0016: POI-IBKR-001's market-data dependency is satisfied by the realtime sub-gate

## Status

ACCEPTED (owner decision, 2026-07-21)

Scope: **dependency semantics only.** This ADR does not mark any PoI PASS, does
not select a market-data provider, does not authorize PAPER_AUTO or LIVE, and
does not change application behavior. It operates strictly inside ADR-0015.

## Context

The frozen backlog (`docs/baseline-v0.5/POI_BACKLOG.yaml`) declares:

```yaml
- id: POI-IBKR-001            # IBKR Paper execution
  depends: [POI-PLAT-ARM-001, POI-MD-001]
```

and defines POI-MD-001 (*Market data provider benchmark*) as PASS only when a
**field ownership matrix and provider decision** satisfy FAST/STANDARD
freshness and point-in-time needs — steps that include benchmarking an
**external** provider and testing **outage/fallback**.

Two facts now hold simultaneously:

1. **The IBKR realtime market-data path is proven.** Owner run 2026-07-21:
   `marketDataType=1` (REALTIME), no entitlement error, valid positive
   bid/ask/last with sizes, timestamp, clean disconnect
   (`docs/poi/evidence/POI-MD-001-REALTIME.md`; recorded as
   `POI-MD-001.realtime_subgate = PASS`).
2. **The external-provider work is deferred indefinitely.** There is no
   approved external provider; benchmark, outage/fallback, field-ownership
   matrix and provider ADR are deferred until an owner-approved provider
   exists (PR #38).

Read literally, the aggregate dependency would stall POI-IBKR-001 — and with
it Track A items A3–A6 — behind a provider that does not exist and may not for
a long time, even though the market data that an order-lifecycle experiment
actually consumes is already proven.

## Decision

**The proven `POI-MD-001.realtime_subgate` (PASS) satisfies the market-data
dependency required specifically for POI-IBKR-001 order-lifecycle
validation.** POI-IBKR-001 may proceed to its next approved preparation and
owner-run lifecycle stages.

**The POI-MD-001 aggregate remains BLOCKED** for the deferred
external-provider work. This ADR narrows dependency *semantics* for one PoI;
it does not close POI-MD-001.

### Why the realtime sub-gate is sufficient for order-lifecycle validation

POI-IBKR-001 proves **broker mechanics**: submit → acknowledge → fill →
protection → cancel → reconnect → reconciliation, with no duplicate orders and
no unprotected fills. What it consumes from market data is exactly what the
sub-gate proved:

- a resolvable instrument (contract resolution — proven);
- a **realtime** quote to place a sane limit price and to judge a fill
  (`marketDataType=1` with valid positive bid/ask/last — proven);
- an entitlement that does not block the data path (no entitlement error —
  proven);
- a timestamp for point-in-time recording (proven).

None of the PASS criteria of POI-IBKR-001 (*"no duplicate orders; protection
follows fills; normalized events; recovery deterministic; host decision
documented"*) depends on provider **selection**, **fallback routing** or a
**field-ownership matrix**.

### Why the external-provider work remains independently blocked

Provider selection answers a different question — which provider *owns* quotes,
bars, corporate actions and the calendar in production, and what happens on
outage. That governs steady-state operation and future automation, not a
single manually-approved Paper order lifecycle. It stays BLOCKED with its own
unfreeze condition (an owner-approved provider with access, commercial terms,
data scope, freshness class and test credentials).

## Safety boundaries (unchanged and binding)

- POI-IBKR-001 is **NOT** marked PASS by this ADR; it stays BLOCKED/NOT RUN
  until owner-run evidence exists.
- Paper only: localhost + port 4002; the live port 4001 is rejected by
  construction; **LIVE and PAPER_AUTO remain prohibited**.
- Every submit still requires a one-time `SubmitAuthorization` consumed by the
  Execution Agent — the only component that may send an order.
- Risk Engine veto, the final Pre-Submit gate and fail-closed behavior are
  untouched; flatten stays hard-disabled.
- No external provider is selected, assumed, purchased or integrated; no API
  key is added.
- Strategy profitability remains `PROFITABILITY_NOT_PROVEN`.

## Rollback / revisit condition

If the owner-run lifecycle evidence reveals a **market-data requirement that
the realtime sub-gate does not cover** — for example a fill or protection
decision that needs bars, corporate-action adjustment, a trading-calendar
lookup, or a fallback when the IBKR feed drops mid-order — then:

1. **stop** the lifecycle runs immediately;
2. record the missing requirement in the POI-IBKR-001 evidence file;
3. this ADR is **revisited and may be reversed**, restoring the full
   POI-MD-001 dependency for POI-IBKR-001;
4. the gap becomes an explicit input to the deferred provider-selection task.

Passing the lifecycle run does **not** retroactively close POI-MD-001.

## Consequences

- Track A can proceed: A3 (owner-run lifecycle CLI) and A4 (POI-IBKR-001
  owner-run) are no longer blocked by the provider deferral.
- POI-MD-001 stays BLOCKED and visible; nothing about it is assumed away.
- The dependency narrowing is documented, reversible and scoped to exactly one
  PoI.
