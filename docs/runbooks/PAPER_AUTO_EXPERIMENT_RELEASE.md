# Restricted Paper Auto — evidence and release checklist

**Status:** NOT RELEASED. This is a preparation document under proposed ADR-0020. Its existence is NOT permission to place orders.

## Before any Paper PoI which can place an order

- [ ] Owner explicitly approves that specific isolated Paper PoI scope and run window.
- [ ] Confirm isolated DU account *exactly* matches allowlist, IBKR Paper endpoint (4002) and no Live credentials.
- [ ] Record starting broker open orders, positions, account equity and existing stops in a redacted evidence file.
- [ ] Verify time synchronization, current broker session and data entitlements.
- [ ] Confirm a single authorized operator/single gateway client; rollback and cleanup commands available.
- [ ] Preserve broker-resident protective exits on every cancel/halt.
- [ ] After each drill confirm no residual unexpected order/position; capture broker evidence.
- [ ] Never assume 'cancel requested' equals 'cancelled' until broker confirms.

## Activation gates — no shortcuts

| Gate | Status | Required evidence file(s) | Reviewer |
|---|---|---|---|
| G0 — cancel and recovery (3 consecutive) | NOT VERIFIED | pending | pending |
| G1 — native bracket and stop at broker | NOT VERIFIED | pending | pending |
| G2 — end-to-end lifecycle and reconcile (10 cycles) | NOT VERIFIED | pending | pending |
| G3 — duplicate/unknown/restart chaos | NOT VERIFIED | pending | pending |
| G4 — kill switch, risk, account/quote failure injection | NOT VERIFIED | pending | pending |
| G5 — 5+ completed US trading sessions shadow | NOT VERIFIED | pending | pending |

A code-level test PASS is not evidence of broker integration. Failure in any gate means no auto orders.

## Pilot authorization — owner must complete AFTER gates PASS

- Owner approval record: **NOT PROVIDED**
- Exact DU account allowlist (store privately, not in this repository): **UNSET**
- Strategy: **baseline-trend-breakout-v1 (frozen fingerprint to be recorded)**
- Code SHA and approved deployment: **UNSET**
- Start/end UTC; expiry: **UNSET**
- Allowed universe/symbols: **UNSET**
- First-pilot entry cap: **one whole share, ≤$50 notional, ≤one new entry per session, ≤one active position**
- Risk profile / loss/halt thresholds: **UNSET; stricter of frozen strategy, adapter and owner pilot policy**
- Pager/operator and emergency procedure: **UNSET**
- Approved activation signature and date: **UNSET**
- Reversal procedure proved: **NO**

**Required proof at activation:** fresh broker account identity, clean startup reconciliation, parent/child protection readiness, durable audit availability, active emergency-stop path, no owner stop/expiry, PAPER_AUTO_ENABLED changed only by separate approved implementation/release, and agent authorization limited to the approved scope. Otherwise DENY.

## STOP / rollback (operator runbook contract; actual command pending implementation)

1. Persist halt and prohibit **new entries**. Log who/when/why.
2. Cancel unfilled **entry** orders only; never indiscriminately cancel broker-resident protective stops.
3. Read and reconcile all active broker orders, filled positions, and protective coverage. Unknown state remains halted.
4. Risk-reducing exits only via previously qualified implementation; no blind flatten/cancel.
5. Revoke pilot authorization. Require a new separate owner release before restart.

## Evidence template for each gate

- UTC timestamp, operator, ticket, code SHA, fixed strategy fingerprint
- Redacted Paper environment/account proof; no credentials
- Step-by-step reproduction, expected and observed broker states
- Parent/child ids and orderRef/permId *redacted as needed*
- Result: PASS / FAIL / BLOCKED (never infer PASS from absence of failure)
- Defect IDs, cleanup and independent reviewer acknowledgment
