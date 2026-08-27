# POI-IBKR-001 — execution-runtime owner-host probe — Evidence

**Result: the canonical execution runtime reached the fail-closed protection
gate on a real IBKR Paper Gateway and refused the entry.**
**POI-IBKR-001 aggregate status: BLOCKED / non-PASS — unchanged.**

## Scope and non-scope (read this first)

This record proves that the **read-only execution probe**
(`scripts/run_execution_probe_paper.sh`) ran end to end on the owner host
against the IBKR **Paper** Gateway, exercised the persistent-P&L / positions /
risk / audit path, and **stopped, fail-closed, at the protection gate**.

It does **NOT**:

- make POI-IBKR-001 PASS — no order lifecycle was proven;
- prove any submit, cancel, modify, flatten or fill path;
- prove a protected-entry lifecycle (`bracket`/`oco` remain `UNSUPPORTED`);
- reach authorization (the protection gate refused before it);
- enable PAPER_AUTO or LIVE (both remain prohibited).

**No order was submitted, cancelled, modified or flattened.** The probe has no
submission path; the only production authorization implementation denies.

## Provenance

| Field | Value |
|---|---|
| head SHA | **`b44448a1bccf40239d36bfc1892ce04b00fd4ae8`** |
| PR | #93 (`feat/canonical-execution-runtime`) |
| endpoint | `127.0.0.1:4002` (localhost + paper API port only) |
| account alias | `paper-poi` |
| symbol | `AAPL` |
| probe exit status | **1 — expected** (the fail-closed protection gate refused the entry) |

## Empirically verified on the real Paper Gateway

| Step | Observation |
|---|---|
| Paper DB preflight | `PAPER_DB_PREFLIGHT_OK` |
| Schema | at Alembic head `0009_corporate_actions` |
| Contract resolution | AAPL → `conid:265598` (exact broker identity) |
| Market data | **REALTIME** quote for that exact conId |
| Session P&L | **`0.0` / `PNL_OK`** — the persistent `reqPnL` subscription served a finite, fresh value |
| Portfolio | classified exposure available, **no open positions** |
| Risk outcome | **APPROVE**, approved quantity **1** |
| Durable audit | persisted successfully |
| Final stage | **`PROTECTION_UNAVAILABLE`** — execution stopped, fail-closed |
| Authorization | **not reached** (protection gate refused first) |
| Order | **none submitted** |

## What this run establishes

The canonical execution runtime, on a real IBKR Paper connection, now:

- passes the database preflight and runs against a migrated schema;
- resolves the exact broker security and takes a **real-time** quote on it;
- observes session P&L through the **persistent** `reqPnL` subscription
  (`PNL_OK`, a finite fresh value) — the round-12/13/14 P&L lifecycle work,
  exercised on real IBKR;
- reads the portfolio (no open positions) through `reqPositionsMulti` — the
  round-11 positions-lifecycle fix, exercised on real IBKR;
- runs the risk engine to an **APPROVE** with an approved quantity; and
- **persists a durable audit record** for the decision.

It then **refuses the entry** at the protection gate, because no
protected-entry lifecycle is wired — the intended fail-closed behaviour. The
non-zero probe exit status is that deliberate refusal, not a failure.

## Still not proven / out of scope

- No protected-entry (`bracket`/`oco`) lifecycle — `protection_required`
  refuses, which is why this run stops here.
- No authorization, no submit, no cancel, no fill, no flatten.
- POI-IBKR-001 remains **BLOCKED / non-PASS**; PAPER_AUTO and LIVE remain
  prohibited.

## Note on `POI-IBKR-HIST.json`

This execution-runtime evidence is recorded here, in the `POI-IBKR-001`
execution-path evidence family, and **not** in
`docs/poi/evidence/POI-IBKR-HIST.json`. That file is the typed,
checksum-verified **historical-data** qualification artifact consumed by the
dataset-v1 IBKR binding chain (`load_and_verify_ibkr_evidence`) and validated by
the market-data test suite; adding execution-probe fields to it would invalidate
its checksum, fail those tests and conflate two distinct PoIs.
