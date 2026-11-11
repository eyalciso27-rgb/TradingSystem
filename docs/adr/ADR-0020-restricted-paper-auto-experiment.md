# ADR-0020 — Restricted IBKR Paper Auto experimental lane

- **Status:** PROPOSED — owner-directed policy change requested 2026-11-11; unmerged draft.
- **Approval boundary:** The owner requested removal of the blanket Paper Auto prohibition and preparation of a conditional policy. This is **not** an instruction to send broker orders or activate a service. Final adoption is recorded by the owner when this policy is reviewed and merged; operational activation requires a second, separate written owner release.
- **Amends if adopted:** The Paper Auto freeze in ADR-0015 `3 and its full research-first sequence in `7; the historical prohibition in ADR-0018 and ADR-0019; the corresponding blanket ban in CLAUDE.md. All other safety invariants survive.
- **Does not amend:** Live trading prohibition; RiskEngine veto; execution-agent-only submission; durable audit and broker-side protective stop; isolation of credentials/accounts/data; no automatic strategy promotion; no unverified profitability claims.
- **Related:** ADR-0014 through ADR-0019; docs/roadmaps/MVP_DELIVERY_PLAN.md; docs/strategies/baseline-trend-breakout-v1.yaml; docs/baseline-v0.5/BASELINE_MANIFEST.json.

## Context

The project has an owner-frozen daily Breakout strategy and a canonical IBKR Paper execution path, but missing real-broker proofs for post-fix cancel, restart recovery, parent+protective-stop operation, end-to-end reconciliation, idempotency under uncertainty, and emergency halt. Running complete historical point-in-time/OOS research before *any* automated Paper experiment is slowing engineering feedback. Profitability is NOT established.

The owner wants the system to progress to unattended, audited Paper trading as soon as the safety gates are proven, without waiting for a complete PIT history. This is a **controlled experiment**, not promotion of the strategy as profitable or authorization for Live.

## Decision

### D1 — Replace blanket prohibition with conditional eligibility

Development and offline testing of a restricted autonomous **IBKR Paper-only** lane are permitted. Sending automatic orders remains **DISABLED and FORBIDDEN** until all safety evidence gates G0–G5 below are recorded PASS against the actual IBKR Paper environment, and a distinct owner-signed activation record identifies account, strategy/version, risk envelope, operators, start/end or expiry, and rollback command.

Approval of this *policy* does not mint submit permissions. Existing PAPER_AUTO_ENABLED=false and LIVE_ENABLED=false remain unchanged. Production must continue to use DenyAllExecutionAuthorization until a separately reviewed, owner-enabled authorization mechanism is added. No runtime environment-variable override of these defaults is created by this ADR.

An isolated, single-use, expressly owner-approved Paper PoI may submit solely under the existing PoI runbook; this is not permission for continuous automation.

### D2 — Evidence gates for the experimental lane

| Gate | Required broker/system evidence | Minimum acceptance |
|---|---|---|
| G0 Cancel and recovery | Broker-acknowledged cancel after the fix; disconnect/restart while an entry is pending; durable state recovery | 3 consecutive clean cancel/recovery drills, no blind resubmission; masked evidence |
| G1 Broker-resident protection | Exact instrument, single-share native bracket parent + SELL STOP GTC child; correct parent/child ids, quantities, sides, stop price and status | Child actually visible at IBKR Paper, protection confirmed after fill; any ambiguous state halts entries |
| G2 Full lifecycle/reconciliation | Submit→ACK→fill→stop protection→exit/cancel→DB/broker reconciliation, including commissions and remaining orders | 10 controlled PoI cycles; no orphaned/duplicate orders and no unprotected exposure; any anomaly blocks release |
| G3 Idempotency and restart chaos | Same intent twice; disconnect before ACK; restart after durable prepare; broker truth from orders/executions | At most one broker entry per intended decision; uncertain outcomes never retried blindly |
| G4 Safety and emergency halt | Loss/size/position checks, stale market data, account mismatch, halt persistence, operator kill switch and recovery | All failure injections refuse new entries; pending *entry* orders cancelled; protective exit orders preserved; safe state after restart |
| G5 Read-only shadow | Exact frozen strategy, completed RTH daily bars, market data, scanner, reason codes, risk decisions, no broker submissions | At least 5 complete US trading sessions; decision lineage and 20 operator-reviewed candidate/NO_TRADE examples (if fewer, review all and extend window); no critical discrepancies |

No gate may PASS solely by unit tests or broker capability flags. Record an evidence path, timestamp, code SHA, masked account/endpoint identity, test runner, actual result, and independently reviewed defect disposition. Fail/unknown is BLOCKED. The owner may make a *more conservative* envelope, never waive G0–G4 by chat instruction or time pressure.

### D3 — Narrow initial broker authorization

After all gates PASS and a separate activation approval:

1. IBKR Paper only, exact owner-approved **DU account allowlist** (prefix alone is insufficient), endpoint/port 4002 as deployed, runtime account/session verification before every submit; reject mismatch, missing state or Live endpoint. Never use Live credentials.
2. One canonical Submission Agent; no second order path. Short-lived, bounded authorization scoped to **one frozen strategy fingerprint, one owner-approved account, and a named finite pilot window**.
3. Initial maximum entry size is **one whole share** with the existing isolated PoI notional ceiling (currently $50) until broker adapter and risk limits are explicitly requalified. This is **not** permission to increase quantities to the research report's suggested 5%-of-equity level. The stricter of risk, adapter and pilot limits always wins.
4. Maximum one new entry per session and one active position for the first pilot; a stricter symbol allowlist may apply. A prospective trade requiring more than one share or exceeding the notional ceiling is SKIPPED, never rounded up.
5. Hard circuit breakers on unknown account/equity/P&L, position/order mismatch, stale/missing quotes or bars, clock skew, broker disconnection, repeated rejects, loss breaches, expiring authorization and audit-store failure. Limit breaches block **new entries**; protective exits and strictly risk-reducing actions remain available under their separate safety path.
6. Broker-resident protective STOP-MARKET GTC required for every opening long position. Risk must approve quantity *before* intent creation; pre-submit and broker adapter validate again. Stops must not be silently tightened, widened, or cancelled by a generic kill operation.
7. Startup must reconcile persisted intent/orderRef/permId/positions/protection with IBKR broker truth before admitting new entries. UNKNOWN never becomes zero or 'not sent'; no blind retry.
8. No discretionary AI or news model may create submit authorization, override Risk or choose an unapproved strategy. No auto-promotion or self-modifying live strategy.

Pilot quotas are upper bounds, not targets; a day with zero trades can be correct. Owner may end/revoke authorization at any time; expiration blocks entries and must not cancel protective exits.

### D4 — Research and strategy track remain independent

The accepted frozen version `baseline-trend-breakout-v1` is evaluated only from **completed RTH daily bars**, signals at close, potential entry during the **following RTH session** within the frozen ATR-based price cap. Do not introduce the report's 15:45 ET unfinished-bar trigger, arbitrary mid+0.2% entry rule, or modified stop semantics under the same fingerprint.

The experiment is **forward-only Paper evidence**. A full PIT dataset, backtest, OOS, stress and profitability thresholds are *not prerequisites* to a restricted Paper experiment, but they remain mandatory for strategy qualification, objective profitability claims and any future owner proposal to consider Live. Live stays wholly prohibited.

Experimental Paper results are always labeled PROFITABILITY_NOT_PROVEN. Report net-of-commission returns with conservative modeled slippage, benchmark, drawdowns, exposure, and sample size; Paper fills must not be presented as executable Live performance.

### D5 — Roles, operations and release procedure

- **Owner:** adopts this ADR; later separately approves a specific release, account, strategy SHA/fingerprint, pilot quotas, rollback and duration. Approval of each routine trade is not required after activation.
- **Development agent:** may build, test, and document the lane but may not sign the release, activate trading, flip enablement flags or create an alternate submit path.
- **Risk/Pre-submit:** absolute veto. **Execution Agent:** only authorized broker submitter. **Operator:** may halt and inspect without waiting for AI.
- **Evidence:** G0–G5 must be placed in docs/poi/evidence (redacted) with PASS status and PR/commit references. A release checklist at docs/runbooks/PAPER_AUTO_EXPERIMENT_RELEASE.md is mandatory.
- **Emergency halt:** durable STOP_NEW_ENTRIES immediately. Cancel only pending entries, preserve existing broker-resident protective stops; any flatten requires a separately verified risk-reducing path and broker-state reconciliation.
- **Rollback:** revoke/expire authorization, halt new entries, reconcile broker state, preserve/monitor protective exits and log incident. Restoring the prior policy is documentary and does not close positions.

### D6 — Compatibility with older decisions

Historical statements 'Paper Auto prohibited' in ADR-0014/0015/0017/0018/0019 describe the status at the time of those decisions. Once ADR-0020 is adopted, it is the **narrow exception for restricted Paper-only experimentation**; it does not retroactively turn historical PoI evidence into PASS, and it does not unfreeze other Track C integrations or imply production/Live eligibility.

CLAUDE.md must be updated in the same review to remove only the blanket Paper Auto ban, preserve default DenyAll, preserve explicit owner authorization of individual broker PoIs, and require the separate operational release before automated trading.

## Alternatives considered

| Alternative | Decision |
|---|---|
| Keep Paper Auto frozen until complete PIT/OOS/stress | Rejected for *experimental* forward-only Paper; preserved for profitability qualification |
| Enable PAPER_AUTO_ENABLED or weaken DenyAll immediately | Rejected: no IBKR Paper lifecycle proof, unacceptable duplicate/unprotected-position risk |
| New broker execution path or LLM direct orders | Rejected: violates canonical submit boundary and Risk veto |
| Turn off broker stops during experimentation | Rejected: unbounded exposure even in Paper; hides execution defects |

## Security, data and operational impact

Policy/docs-only change. No credentials, network endpoints, deployments, database migrations, broker calls, access grants, schemas, or feature flags are changed. Paper/Live queues and identities stay isolated. Audit and redaction requirements remain. No new infrastructure required; Oracle ARM64 execution compatibility is unchanged, with broker verification pending.

## Approval and activation

| Action | Authority | Status |
|---|---|---|
| Request preparation of conditional Paper Auto governance | Owner (conversation, 2026-11-11) | Requested |
| Review/merge of ADR-0020 and related policy edits | Owner | Pending |
| G0–G5 broker/safety evidence | Engineering + operator | NOT VERIFIED |
| Autonomous Paper operational activation | Owner, **separate explicit release** | NOT APPROVED |
| Any Live trading | No one under this ADR | PROHIBITED |
