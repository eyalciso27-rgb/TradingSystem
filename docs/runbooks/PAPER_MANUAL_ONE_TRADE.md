# IMP-1302 — Paper Manual: One-Trade Runbook (PREPARED, NOT ACTIVE)

Prepares the first owner-approved Paper Manual trade. **Nothing here is to
be executed until POI-IBKR-001 is PASS and the owner explicitly opens
IMP-1302.** LIVE and PAPER_AUTO remain forbidden; flatten stays
hard-disabled; every submit requires a fresh single-use authorization.

## 1. Smallest-safe-order policy

| Parameter | Value | Rationale |
|---|---|---|
| Order type | LIMIT only (DAY) | never MARKET in the first runs |
| Quantity | **1 share** | smallest possible exposure |
| Max order value | **USD 30** | one cheap liquid share |
| Max open orders | 1 at any moment | zero-duplicate invariant observable |
| Required protection | protective STOP mandatory on any fill; target optional (bracket if used) | IMP-1302 DoD |
| Stop distance | 5–10% below entry (BUY) | far from noise, small absolute risk |
| Session | regular trading hours only | `outside_rth=false` |

## 2. Symbol allowlist (liquid, cheap, USD)

`F` (Ford, ~$11) — primary. `T` (AT&T) — alternate. `AAPL` — cancel-path
only (far-from-market limit; higher price makes accidental fills costlier,
so not for the fill scenario). No other symbol without a new owner decision.

## 3. Operator checklist (before ANY submit)

- [ ] POI-IBKR-001 marked PASS by the owner (not before)
- [ ] Gateway up via IBC/systemd; `ss -tln | grep 4002` listening; VNC visible
- [ ] Account is Paper (`DU…`, masked in all output); TradingMode=paper
- [ ] Market data mode verified for the run (realtime for the fill scenario)
- [ ] Watchdog reconciliation marker path known: `/run/ibgateway/order-lifecycle.active`
- [ ] Phone at hand (2FA may re-trigger on relogin)
- [ ] Abort steps below read and understood
- [ ] Owner explicitly states in chat: which scenario, which symbol, quantity 1

## 4. One-trade flow (per approved run)

1. **Approve plan** — owner approves the complete plan (entry, stop,
   quantity, symbol) → a single-use SubmitAuthorization is minted.
2. **Drop lifecycle marker** — create `order-lifecycle.active` (inhibits
   watchdog restarts mid-lifecycle).
3. **Submit** — one LIMIT DAY order, quantity 1, via the execution agent
   path only. Record client_order_id + ack.
4. **Fill or cancel** — for the cancel-path: cancel after ack, verify
   CANCELLED. For the fill-path: wait for fill (marketable limit).
5. **Protection** — verify the protective STOP exists at the broker for the
   full filled quantity ("protection follows fills"). Zero unprotected.
6. **Reconcile** — run reconciliation; expect clean (or explicit findings).
7. **Close** — owner closes the 1-share position manually in the Gateway UI
   (code never flattens). Reconcile again until clean.
8. **Remove marker** — delete `order-lifecycle.active`.
9. **Post-run review** — fill the template below; owner files evidence.

## 5. Abort conditions (stop immediately, cancel, reconcile)

- Any duplicate order appears (same client_order_id twice at broker).
- A fill exists with no protective stop within one minute.
- Order state UNKNOWN and unresolved after one reconciliation pass.
- Gateway disconnects mid-order and does not recover on one reconnect.
- Any quote/entitlement anomaly (delayed data in a realtime-required run).
- Anything the operator does not understand — abort is always correct.

## 6. Reconciliation checklist (after every run, pass required)

- [ ] Internal open orders == broker open orders (none expected at end)
- [ ] Internal position == broker position (0 after manual close)
- [ ] Protected quantity == filled quantity at all times during the run
- [ ] All broker events processed exactly once (no missing/duplicate)
- [ ] No UNRESOLVED_UNKNOWN_ORDER findings
- [ ] `entry_block_required == False` at the end

## 7. Post-run review template

```
run_id / date:
operator (owner):
scenario (cancel-path | fill-path):
symbol / quantity / limit price:
client_order_id(s):
state transitions observed:
protection: stop order id, quantity, visible at broker (y/n):
reconciliation findings (before close / after close):
duplicates observed: (must be none)
unprotected intervals: (must be none)
data mode during run (realtime/delayed):
anomalies / deviations:
evidence file updated: docs/poi/evidence/... (y/n)
owner verdict: PASS / FAIL / RERUN
```
