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
   simulation-equity cap, the P&L observation and the authoritative **Start-of-Day
   equity** into `MvpAccountSnapshot`. The unknown-state gate fails closed on
   anything but a fresh `OK` P&L and a present SOD equity.
2b. **Universe facts + calendar.** Populate `UniverseFacts` from the canonical
   sources — S&P membership snapshots (`trading_market_data_core` universe;
   `sector_of` gives the authoritative GICS sector) and the feature engine's
   `adv_dollar` / `spread` — and pass the instrument's `TradingCalendar`. Every
   fact is nullable and UNKNOWN blocks, so the policy boundary is safe even if
   the runtime does not pre-filter.
3. **`ExecutablePlan` → execution runtime.** The sealed `ExecutablePlan` (and the
   single-use `SubmitAuthorization` the pre-submit check mints) are the hand-off
   point. The execution agent / protected-lifecycle work consumes them; this
   branch never consumes the authorization and never submits.
4. **MVP single-share clamp.** PR #94 constrains the protected Paper entry to ONE
   whole share to eliminate the partial-fill protection window. The strategy/risk
   layer here already supports an execution authority reducing an approved
   quantity `N` down to `1` (reducing risk is always permitted; increasing it is
   never permitted), so no change is required on this side to remain compatible.

## Base-length selection — owner decision (2026-08-31)

The frozen record says the contracting base is "10-20 trading days" but does not
state which valid length to use when several qualify — a strategy semantic the
frozen record left open. The owner resolved it on 2026-08-31
(`OWNER-EYAL-2026-08-31-BASELINE-BASE-SELECTION-LARGEST`): **use the LARGEST
valid window** (search 20 → 10). This is recorded as an explicit
`base_selection_rule` policy field and in the execution-params file; it does NOT
change the frozen record or its fingerprint.

## Frozen-rule coverage (every rule that gates a new entry)

| Frozen rule | Owner | Enforced by | Test |
|---|---|---|---|
| S&P 500 membership (PIT for history) | universe.definition | `universe_eligibility` (UNIVERSE) | `test_mvp_trading_policy::test_non_sp500_member_blocks`, `test_membership_not_point_in_time_blocks` |
| ADV20 ≥ $50M | universe.min_liquidity | `universe_eligibility` | `test_universe_eligibility`, `test_adv20_below_min_blocks` |
| Price ≥ $10 | universe.min_price | `universe_eligibility` | `test_price_below_min_blocks` |
| Spread ≤ 10 bps | universe.max_spread | `universe_eligibility` | `test_spread_too_wide_blocks` |
| Completed RTH daily bars / signal in RTH | sessions.allowed | `baseline_strategy` session gate (BLOCKED on unknown) | `test_baseline_strategy::test_unknown_session_blocks`, `test_non_rth_session_blocks` |
| New entry in the NEXT RTH session | entry.rule / sessions.allowed | `mvp_safety_gates.entry_timing_eligible` (canonical calendar) | `test_mvp_safety_gates` (same/next/weekend/holiday), `test_two_sessions_later_is_blocked` |
| Trend (Close>SMA150 & rising) | setup.definition | `baseline_strategy` | `test_declining_trend_blocks`, `test_close_below_sma_blocks` |
| Contracting base + largest-window selection | setup.definition + owner 2026-08-31 | `baseline_strategy` | `test_no_contracting_base_blocks`, `test_base_selection_is_owner_ratified_and_explicit` |
| Breakout trigger | trigger.definition | `baseline_strategy` | `test_no_breakout_blocks` |
| Entry cap (BreakoutClose+0.25·ATR20), no chase | entry.rule | `baseline_strategy` cap + pre-submit slippage (CANCEL) | `test_do_not_chase_price_slippage_cancels` |
| Structural STOP (BaseLow−0.25·ATR20) | exit.stop_rule | `baseline_strategy` + risk protective-stop gate | `test_valid_complete_setup_triggers` |
| Skip if risk > 2.5·ATR20 | exit.stop_rule | `baseline_strategy` | `test_risk_per_share_over_cap_blocks` |
| 0.25% risk per trade (fixed-fractional) | sizing.method / risk.per_trade | orchestrator sizing + RiskEngine (reduce-only) | `test_risk_reduces_but_never_increases_quantity`, `test_insufficient_risk_budget_blocks` |
| Max 8 concurrent positions | risk.max_concurrent_positions | RiskEngine | `test_max_concurrent_positions_vetoes` |
| Max 3 positions per GICS sector | risk.max_concurrent_positions | `mvp_safety_gates.sector_within_caps` (SECTOR) | `test_three_same_sector_positions_block` |
| Max 0.75% planned risk per GICS sector | risk.max_concurrent_positions | `mvp_safety_gates.sector_within_caps` | `test_sector_planned_risk_cap_blocks` |
| 75% gross exposure ceiling | risk.max_portfolio_exposure | RiskEngine notional caps + allocator | RiskEngine/allocator suites |
| 1.5% daily loss vs Start-of-Day equity | risk.daily_loss_limit | `mvp_safety_gates.daily_loss_within_limit` (DAILY_LOSS) | `test_daily_loss_uses_sod_equity_not_current_nlv`, `test_changing_current_nlv_does_not_move_sod_threshold` |
| One position per symbol / no averaging down | risk.action_on_breach / 3.1 | RiskEngine | `test_same_symbol_existing_position_vetoes` |
| conId position ownership; unknown blocks | (mission §6) | `position_attribution` | `test_position_attribution`, `test_unattributable_position_blocks_entry` |
| Usable account/P&L only when authoritative | data.staleness / broker P&L status | orchestrator account gate (PNL_OK required, SOD present) | `test_finite_pnl_without_status_blocks`, `test_unknown_*_blocks` |
| Point-in-time inputs (no look-ahead) | data.point_in_time_requirement | every engine's `as_of` guard | lookahead tests across suites |
| Strategy exits (failed breakout / time / trailing) | exit.* | `baseline_exit` → PositionCoordinator | `test_baseline_exit` |

Everything above is enforced **in this policy path** or by a canonical gate the
runtime cannot bypass; there is no silent "assumed upstream". An authoritative
fact that is not yet available resolves to NO NEW ENTRY (fail closed), never to a
pass.

## Residual blockers to an autonomous Paper MVP (out of scope here)

- The autonomous scheduler / execution worker is intentionally NOT built.
- Paper Auto and Live remain disabled.
- Real broker positions/account wiring (seams 1–2) land with PR #94.
- Strategy statistical acceptance (DSR / OOS go-no-go) is separate evidence work.
