# MVP Trading Policy — Strategy half (`feat/mvp-strategy-trading-policy`)

This milestone builds the **trading policy / strategy half** of the MVP: it
proves that ONE real, owner-frozen strategy produces a trade **only when all of
its rules are satisfied**, and that every candidate is then constrained by the
mandatory risk/safety envelope before it becomes a canonical execution intent.

Execution itself is out of scope here — it belongs to the canonical execution
runtime and the protected-lifecycle work in PR #94.

## Canonical path

```
MARKET DATA
  -> BaselineTrendBreakoutStrategy         (baseline_strategy.py)
  -> SIGNAL / NO TRADE
  -> StrategyManifestEvaluator -> SignalFusion -> DecisionEngine   (ENGINE DECISION)
  -> position attribution + account unknown-state gate             (RISK + SAFETY)
  -> RiskEngine -> PortfolioAllocator -> FinalPreSubmitCheck
  -> APPROVED / REDUCED / DENIED
  -> ExecutablePlan                        (CANONICAL EXECUTION INTENT)
```

The orchestrator (`mvp_trading_policy.py`) re-implements none of the canonical
engines — it composes the ones already on `main`.

## Selected strategy

`baseline-trend-breakout-v1`, FROZEN 2026-08-13
(`docs/strategies/baseline-trend-breakout-v1.yaml`, fingerprint
`sha256:75f299b8…e84dfed0`). It is the only strategy substantially specified and
owner-approved in the repository, is deterministic, is a pure technical breakout
(no ML gate), and its data needs (daily bars) are satisfied by the existing
runtime.

The numeric parameters are transcribed with provenance in
`config/strategies/baseline-trend-breakout-v1.execution-params.yaml` and
`baseline_strategy_config.py`; both are bound to the immutable freeze record by
`tests/strategy/test_baseline_strategy_config.py`. No parameter is invented.

## Ownership boundaries

- **Strategy** decides only *"is there a valid opportunity?"*. A NO-TRADE result
  is routed through the canonical decision path as a non-APPROVE decision and can
  never become an execution intent.
- **Risk / portfolio / safety** decide *"are we allowed, and at what size?"*.
  They only reduce, never invent a trade, and never enlarge risk. Any unknown
  critical state (P&L, equity, an unattributable position) fails closed to
  `NO_NEW_ENTRY` before the Risk Engine is asked.
- **Execution** is not performed here at all.

## Position ownership (conId)

`position_attribution.py` classifies every broker-reported position by its exact
`con_id`. Exact `con_id` identity outranks ticker equality; an unattributable or
symbol-mismatched position, or a system record with no matching broker position,
blocks a new entry (unknown exposure is never treated as zero).

## Integration seams expected AFTER PR #94 merges

This branch depends only on canonical interfaces already on `main`. When PR #94
merges, the following small adapters connect this policy to live execution — none
of them change the strategy or risk semantics:

1. **Broker positions → attribution input.** Map the broker adapter's
   position/reconciliation output (which carries the IBKR `conId`) into
   `position_attribution.BrokerHeldPosition`, and the durable system position
   store into `SystemPositionRecord`. The classifier itself is unchanged.
2. **Account snapshot → `MvpAccountSnapshot`.** The broker-core `AccountSnapshot`
   already carries `session_pnl` + `SessionPnlStatus`; map NLV, the configured
   simulation-equity cap and the P&L observation into `MvpAccountSnapshot`. The
   unknown-state gate already fails closed on anything but a fresh `OK` P&L.
3. **`ExecutablePlan` → execution runtime.** The sealed `ExecutablePlan` (and the
   single-use `SubmitAuthorization` the pre-submit check mints) are the hand-off
   point. The execution agent / protected-lifecycle work consumes them; this
   branch never consumes the authorization and never submits.
4. **MVP single-share clamp.** PR #94 constrains the protected Paper entry to ONE
   whole share to eliminate the partial-fill protection window. The strategy/risk
   layer here already supports an execution authority reducing an approved
   quantity `N` down to `1` (reducing risk is always permitted; increasing it is
   never permitted), so no change is required on this side to remain compatible.

## Residual blockers to an autonomous Paper MVP (out of scope here)

- The autonomous scheduler / execution worker is intentionally NOT built.
- Paper Auto and Live remain disabled.
- Real broker positions/account wiring (seams 1–2) land with PR #94.
- Strategy statistical acceptance (DSR / OOS go-no-go) is separate evidence work.
