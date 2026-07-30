# POI-IBKR-001 — owner-run evidence: cancellation failure (NOT a PASS)

> **POI-IBKR-001 status: BLOCKED / NOT RUN. This document does not change it.**
> It records what a real Paper owner-run observed, what was fixed in response,
> and — explicitly — what has **not** been executed. Only the owner may change
> `docs/poi/STATUS.yaml`, and only from a fresh post-merge Paper owner-run.

## 1. What the owner-run did

A real POI-IBKR-001 cancel-path owner-run was executed by the owner against the
IBKR **Paper** Gateway on the owner host. It reached the actual broker order
path — the first time this code has done so successfully.

Identity (both non-authorizing; the confirmation token appears nowhere):

| Field | Value |
|---|---|
| operation reference | `05D0E19B631C2548` |
| broker order reference | `ibkr-paper:2` |
| instrument | `F`, resolved via `SMART` / `USD` |
| order | `BUY 1 F LIMIT 12.02` |

## 2. What passed

- **PAPER environment gate** passed.
- **`DU` account gate** passed (account id masked in all evidence).
- Instrument resolution succeeded.
- The order was **submitted** and **acknowledged** by the broker.
- **Idempotent resubmit** returned the original outcome — no second order was
  created.
- The order **never filled** during the test window.

This is the first empirical confirmation that submit, acknowledgement, identity
propagation and idempotency work against a real Gateway.

## 3. What failed

| Check | Result |
|---|---|
| `cancel_sent` | **FAIL** (`status=FAILED`) |
| `cancel_confirmed` | **FAIL** |
| order state at the broker | remained `ACKNOWLEDGED` |
| reconciliation | `UNEXPECTED_BROKER_ORDER` |

**The owner had to cancel the order manually in the IB Gateway UI.**

The harness's fail-closed behaviour held: it did not retry, did not resend, did
not claim success and did not report a PASS. But the evidence was nearly
useless — the only diagnostic was `status=FAILED`, because order-scoped broker
errors were being discarded entirely.

## 4. Post-failure state

A later reconnect run was **clean**:

- `open_poi_orders = 0`
- non-zero positions = `0`
- `reconciliation = PASS`

So no exposure was left behind, and the account is in a known-good state.

## 5. Root causes and what was changed

Full analysis in `docs/operations/IMP_1304_DEFECT_REGISTER.md` §8
(**D-CANCEL-01 … D-CANCEL-08**). In brief:

1. **The cancellation was never encoded** (D-CANCEL-01). The transport called
   `cancelOrder(broker_order_id, "")`, but the installed API is
   `EClient.cancelOrder(orderId, orderCancel: ibapi.order_cancel.OrderCancel)`
   and reads `orderCancel.manualOrderCancelTime` / `.extOperator` /
   `.manualOrderIndicator`. Attribute access on a `str` raised inside `ibapi`
   before any request reached the wire. Now a real `OrderCancel()` is passed,
   and a Gateway whose API lacks that surface fails closed.

2. **Order errors were discarded** (D-CANCEL-03). `EWrapper.error` was not
   overridden at all, which is why the owner saw only `status=FAILED`. It is
   now overridden with a version-tolerant normalizer and correlated to the
   waiting operation; the numeric code, sanitized text and any
   advanced-rejection string reach the evidence, while farm notices (2104,
   2106, 2107, 2108, 2119, 2158) never fail an order.

3. **Callbacks were shared through one lossy queue** (D-CANCEL-03). A waiter
   could consume and discard the callback another operation was blocked on, so
   a genuine cancellation could surface as a false timeout. Replaced by a
   lossless per-stream `CallbackRouter`.

4. **Restart recovery could see but not cancel** (D-CANCEL-04), and the naive
   fix would have conflated global visibility with cancellation authority
   (D-CANCEL-06). Cancellation authority now comes only from the same-client
   open-order feed under strict, fail-closed conditions.

5. Cancellation classification, the durable execute-attempt guard, cleanup
   semantics and the honesty of the price/position claims were hardened at the
   same time (D-CANCEL-02, D-CANCEL-05, D-CANCEL-07, D-CANCEL-08).

## 6. NOT EXECUTED

Everything above was fixed and verified against fakes, a deterministic fake
transport and a stubbed `ibapi`. **No IBKR connection was made and no Paper or
Live order was sent while implementing the fix.** The following have **not**
been performed:

- ❌ no real IBKR Paper order after the code change;
- ❌ no real cancellation after the code change;
- ❌ no fill-path owner-run;
- ❌ no Gateway restart recovery owner-run;
- ❌ no Live connection and no Live order;
- ❌ no PAPER_AUTO activation.

## 7. What the owner must do next

The post-merge validation sequence is recorded — **not executed** — as:

| Record | Purpose |
|---|---|
| `DEF-19-verify-clean-readonly` | read-only proof the account is clear (run FIRST) |
| `DEF-18-post-fix-cancellation-verification` | the real post-fix cancel-path run |
| `DEF-20-restart-recovery-cancellation` | cancel a recovered order after a restart |

Templates and abort criteria: `docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.md`.
Step-by-step operator guidance: `docs/runbooks/POI_IBKR_001_OWNER_RUN.md`.

**POI-IBKR-001 remains BLOCKED / non-PASS until the owner records evidence from
a fresh post-merge Paper owner-run.**
