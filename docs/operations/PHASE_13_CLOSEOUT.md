# PHASE-13 — End-to-End Shadow and Paper Manual: engineering closeout

Reconciles the four PHASE-13 tasks after PRs #66–#69. It distinguishes
**engineering complete** from **owner-run evidence pending**, **genuinely
blocked**, and **deferred/out-of-scope**. It marks **no** empirical Paper-Manual
or chaos PASS that the real owner runs have not produced.

## Task status

| Task | Title | Engineering | Owner-run evidence | Notes |
|---|---|---|---|---|
| **IMP-1301** | Run E2E shadow scenarios | ✅ **complete** (merged #67) | n/a (fully offline) | Traceability acceptance suite passes; run/split isolation + subsequent-candidate recovery added; all-source-types + combined Track-C acceptance |
| **IMP-1302** | Run Paper Manual | ✅ **offline complete** (merged #68) | ⏳ **pending** (owner) | Fakes-only DoD proof (zero duplicate orders / zero required unprotected positions) across 17 lifecycle scenarios incl. the entry-reconciliation latch; owner-run CLI + deferred-run manifest ready; **real IBKR Paper runs DEF-01/02/03/04/05/09/18/20 NOT RUN**; DEF-19 verify-clean PASS |
| **IMP-1303** | Run chaos and recovery drills | ✅ **offline complete** (#69, in flight) | ⏳ **pending** (owner) | 24 fakes drills + DB outbox/inbox chaos + persistent restart-latch drill; runbook documents safe behavior; **host drills DEF-06/07/08/10/11 NOT RUN**; NATS transport deferred (no adapter) |
| **IMP-1304** | Close operational defects | ✅ **complete for committed code** | ⏳ DEF-15/17 (owner) | Re-audit (`IMP_1304_REAUDIT.md`): **no open offline P0/P1 defect**; empirical verifications owner-gated |

## Engineering-complete vs. evidence-pending vs. deferred

- **Engineering complete (offline):** all four tasks' safe/offline work is done
  and green in CI. The DoDs that can be proven with fakes/replay are proven.
- **Owner-run evidence pending (not a code gap):** the real-IBKR Paper lifecycle
  (IMP-1302), the destructive host chaos drills (IMP-1303), and the empirical
  broker verifications (IMP-1304 DEF-15/17). These are recorded in
  `PHASE_13_DEFERRED_OWNER_RUNS.yaml`; POI-IBKR-001 stays BLOCKED / NOT RUN.
- **Genuinely blocked / deferred:** NATS JetStream transport end-to-end (no
  adapter; ADR-gated separate task); external market-data provider (owner
  decision, ADR-0016 satisfied the PoI-IBKR-001 dependency, POI-MD-001 aggregate
  stays BLOCKED); the Track-C external-source PoIs (owner accounts required —
  see `TRACK_C_EXTERNAL_SOURCES_READINESS.md`).

## What is explicitly NOT claimed

- No Paper-Manual empirical PASS (no real order was ever sent).
- No chaos host-drill PASS (no real Gateway kill / DB restart performed).
- No source marked CONNECTED (no adapter has produced runtime evidence).
- PAPER_AUTO disabled; watchdog timer disabled; Gateway/systemd/IBC untouched;
  LIVE disabled.

## Bottom line

PHASE-13 **engineering** is complete pending owner-run empirical evidence. The
project can continue to PHASE-14 planning surfaces that do not require the
deferred evidence, while the owner runs the deferred IBKR-Paper / host drills in
parallel.
