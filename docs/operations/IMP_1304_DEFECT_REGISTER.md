# IMP-1304 — Operational Defect Register

> Bounded operational-defect audit for **IMP-1304 (Close operational defects)**.
> This register contains **only real, evidenced defects or gaps**. It does not
> invent work to make the task look complete, and it does **not** mark IMP-1304
> complete: `done_criteria` ("no open critical/high safety defect") is assessed
> below, but Phase 13 is **not** marked complete and no PoI status changes.
>
> Audited against `main` @ the PR #44 merge (`680606a`). POI-IBKR-001 remains
> **BLOCKED / NOT RUN**; IMP-1302 Paper Manual is not complete; Paper Auto and
> Live remain prohibited; external-provider selection and the NATS adapter
> remain out of scope.

## 1. Audit scope and method (what was actually checked)

| Source | Method | Result |
|---|---|---|
| IMP-1304 definition | read `CLAUDE_CODE_TASKS.yaml` | scope = resolve P0/P1 defects + rerun regression |
| Open PoI blockers | read `docs/poi/STATUS.yaml` | POI-IBKR-001 BLOCKED; POI-MD-001 aggregate BLOCKED; P0 social/news/AI/TV BLOCKED on owner accounts |
| Evidence roadmap | read `docs/roadmaps/EVIDENCE_ROADMAP.md` | A1 done, A2 deferred, A3 built, A4 blocked |
| Runbooks | `POI_IBKR_001_OWNER_RUN.md`, `CHAOS_RECOVERY_DRILLS.md`, `PAPER_MANUAL_ONE_TRADE.md` | cross-references resolve; every `--flag` in the owner-run runbook is a real CLI option |
| TODO/FIXME/HACK/XXX/BUG markers | grep across `apps/ packages/ scripts/ tests/` | **0** in source; 0 actionable in tests |
| Open GitHub issues | GitHub API | **0** open issues |
| Recent PR review findings | PRs #39–#44 | all merged; the safety findings they raised were fixed in-flight (see §5) |
| Skipped / CI-gated tests | grep `pytest.skip`/`skipif` | all are `DATABASE_URL`-gated (run in the CI `db-migration` job) or environment guards (symlink/git-binary); **none silently disabled** — verified PR #44's `db-migration` check was green |
| Lint / type suppressions | grep `type: ignore`/`noqa`/`pragma` | all legitimate: fail-closed `BLE001`, host-only `ibapi` imports gated by `pragma: no cover` |
| Static gates | `ruff`, `mypy`, `check_architecture`, `verify_baseline` | all clean |
| Test warnings | `pytest -W error::DeprecationWarning` | none |

## 2. Defect register

| ID | Title | Class | Component | Fix now w/o IBKR? |
|---|---|---|---|---|
| D-01 | Reconnect protection check is a global boolean, masking a naked position in another symbol | **P1** | `scripts/poi_ibkr_lifecycle.py` `_run_reconnect` | ✅ fixed (PR #45) |
| D-01b | Per-symbol fix still let any stale/foreign/malformed `:stop:` order mark the symbol protected | **P1** | `scripts/poi_ibkr_lifecycle.py` `_run_reconnect` | ✅ fixed (PR #46) (canonical date+scenario+symbol match) |
| D-01c | The match bound date+scenario+symbol only: two same-parameter same-day runs share `entry_id`, so a stale same-day stop from a run with **different** parameters could still match | **P1** | `scripts/poi_ibkr_lifecycle.py` identity + `_run_reconnect` | ✅ **fixed in this PR** (bind order ids to a deterministic operation reference) |
| D-01d | The operation reference initially reused the **confirmation token**, embedding a reusable execution-authorization value in `client_order_id`, broker-visible order metadata, and evidence | **P1 (security)** | `scripts/poi_ibkr_lifecycle.py` identity | ✅ **fixed in this PR** (separate, domain-separated, non-authorizing operation reference) |
| D-01e | Evidence artifact lacked broker order identity, fill identity and intended-parameters fields, so an owner could not correlate the artifact with the real order | P2 | `scripts/poi_ibkr_lifecycle.py` fill/cancel path | ✅ **fixed in this bundle** (records `intended_parameters`, `entry_broker_order`, `fill_identity`) |
| D-ADAPTER-01 | Reconnect owner-run cannot recover the harness order id / operation reference from a real Gateway after a restart: the paper adapter never set `order.orderRef` on `placeOrder`, and `get_open_orders` returned only in-memory orders (not a live `reqAllOpenOrders`) | **P1** | `broker-ibkr/paper_adapter.py` | ✅ **code fix implemented + contract-tested in this bundle**; **owner-host empirical verification BLOCKED / NOT RUN** (see below) |
| D-TIF-01 | The IBKR transport built the `Order` with **no `tif`** (and no explicit `transmit`), so a real Paper submit reached the API with an empty TIF and was rejected: `ERROR 1 … 10052 Invalid time in force:Empty`. Observed on a real cancel-path owner-run (BUY LIMIT F qty 1). The harness then failed closed correctly (`UNKNOWN_BROKER_STATE`, `RECONCILE_BEFORE_ANY_RETRY`) — no unsafe outcome | **P1** | `broker-ibkr/paper_adapter.py` transport | ✅ **fixed** — canonical `TimeInForce` (default `DAY`), validated + translated before `placeOrder`; regression-proofed |
| D-TIF-02 | The TIF fix defaulted **every** order to `DAY`, including the protective STOP — a DAY protective stop expires at session close and leaves the real position **naked overnight** | **P1** | `agent.py` / `poi_ibkr_lifecycle.py` | ✅ **fixed in pre-merge review** — protective STOP is now **GTC**; entry LIMIT stays DAY |
| D-TIF-03 | Deterministic broker validation rejects (10052/321) time out into `UNKNOWN` (not a definite `REJECTED`), forcing unnecessary reconciliation | P2 | `broker-ibkr` host transport `_await_status`/`error()` | ✅ **implemented in this bundle** (was: documented only) — `error()` is now overridden and correlated, and a small conservative allowlist `{103, 201, 321, 10052}` maps to terminal `BROKER_REJECTED`; every other code stays `UNKNOWN`. DEF-17 remains open to empirically confirm the allowlist |
| D-CANCEL-01 | The host transport called `self._app.cancelOrder(broker_order_id, "")`. The installed API is `EClient.cancelOrder(orderId, orderCancel: ibapi.order_cancel.OrderCancel)` and reads `orderCancel.manualOrderCancelTime` / `.extOperator` / `.manualOrderIndicator`, so a `str` raised **before any cancellation request was encoded**. Observed on a real Paper owner-run: `cancel_sent=FAIL`, order stayed `ACKNOWLEDGED`, owner cancelled by hand | **P1** | `broker-ibkr/paper_adapter.py` host transport | ✅ **fixed in this bundle** — real `OrderCancel()`; fail-closed if the SDK lacks the surface |
| D-CANCEL-02 | Cancellation reused the generic "first order status wins" wait. A stale `Submitted`/`PreSubmitted` could be read as the cancellation result, a `Filled` during cancellation would have counted as success, and `Inactive` was not a deterministic failure | **P1** | `broker-ibkr/paper_adapter.py` cancel wait | ✅ **fixed in this bundle** — dedicated disposition classifier + reducer (stale ignored, PendingCancel accepted-not-final, fill = exposure, reject = terminal, timeout = UNKNOWN, never retried) |
| D-CANCEL-03 | `EWrapper.error` was **not overridden at all**, so every order-scoped broker error was discarded; and all callbacks shared ONE `queue.Queue`, so a waiter could consume and discard the callback another operation was blocked on (a real cancellation could surface as a false timeout) | **P1** | `broker-ibkr` host transport | ✅ **fixed in this bundle** — `error()` normalized + correlated by issued id; lossless per-stream `CallbackRouter` replaces the shared queue |
| D-CANCEL-04 | `get_open_orders()` recovered identity from the broker snapshot and returned receipts, but never populated the cancellation maps, so cleanup after a process/Gateway restart could SEE the resting order and still fail `ORDER_NOT_FOUND` | **P1** | `broker-ibkr/paper_adapter.py` | ✅ **fixed in this bundle** — `recover_cancellable_orders()` (see D-CANCEL-06 for why it is not simply hydrated from the global feed) |
| D-CANCEL-05 | The `--execute` confirmation token is a deterministic digest of the run parameters, so it is **not single-use across processes**: the identical command could be replayed, and a crash between "order sent" and "status received" left the owner able to submit a duplicate | **P1 (safety)** | `scripts/poi_ibkr_lifecycle.py` + `trading_application` | ✅ **fixed in this bundle** — durable `OwnerRunJournal` with an atomic `O_EXCL` execute-attempt claim; `--execute` refused without `--journal-dir` |
| D-CANCEL-06 | Hydrating cancellable state from `reqAllOpenOrders` would conflate **visibility** with **authority**: that feed reports orders from every API client and from the TWS/Gateway UI, so a foreign or manual order could have become cancellable through this adapter | **P1 (safety)** | `broker-ibkr/paper_adapter.py` | ✅ **fixed in this bundle** — global feed grants nothing; authority requires the same-client feed + matching `clientId` + account + canonical `orderRef` + valid non-zero id + unambiguous identity. No `reqGlobalCancel` anywhere |
| D-CANCEL-07 | Cleanup evidence read `mode: DRY-RUN (nothing sent)` although cleanup sends cancellations; it did not wait for confirmation or re-read the broker; one failing cancel aborted the whole sweep; and `reconnect` (a fill-path protection verifier) was the only "is it clean" check available to a cancel-path run | **P1** | `scripts/poi_ibkr_lifecycle.py` | ✅ **fixed in this bundle** — `CANCEL-ONLY` header, wait + fresh re-read, one attempt per eligible order with no early abort and no retry, plus a dedicated read-only `--scenario verify-clean` |
| D-CANCEL-08 | The cancel path asserted the resting limit "cannot fill", bound that claim to a possibly-stale hand-copied `--reference-price`, and reported zero-quantity broker rows as positions (a flat account could read as `1 position(s)`) | P2 | `scripts/poi_ibkr_lifecycle.py` | ✅ **fixed in this bundle** — "non-marketable at the observed quote; fill risk reduced but not eliminated", mandatory fresh two-sided quote (age/spread/session/drift, revalidated immediately before submit), and raw / non-zero / run-symbol position rows reported separately |
| D-CLOCK-01 | `IbkrPaperAdapter.__init__` defaulted to a **deterministic TEST clock** frozen at `2026-07-10T12:00Z` (`clock or _deterministic_clock(...)`, +1s per call). On a real owner-run at host time `2026-07-30T17:05:22Z` the adapter stamped every account snapshot, receipt, broker event, reconciliation result and health check with a fabricated timestamp ~20 days stale — evidence that could not be correlated with the Gateway log, with the harness's own real-UTC timestamps, or with reality | **P1 (evidence integrity)** | `broker-ibkr/paper_adapter.py` | ✅ **fixed in this bundle** — production default is real tz-aware UTC (`utc_now`); injected clocks still honoured; naive clocks fail closed; non-UTC normalized; durations measured monotonically |
| D-CLOCK-02 | A real `verify-clean` owner-run reported only `broker error BROKER_UNHEALTHY; no retry`. The first failing read aborted the whole scenario, so the owner could not distinguish a dead Gateway from a reachable account that merely held an unrelated position (`DVLT` 1352, zero open orders), and got no code detail or root cause | **P1 (diagnosability)** | `scripts/poi_ibkr_lifecycle.py` | ✅ **fixed in this bundle** — sanitized bounded message + chained root-cause TYPE + correlation id; every read-only probe recorded independently; read-only scenarios continue past a failed preflight; open positions named explicitly. Fail-closed behaviour unchanged |
| D-CLOCK-03 | `health_check()` called `transport.is_connected()` (→ `EClient.isConnected`) with **no guard**, and `disconnect()` let an arbitrary SDK exception escape. The harness normalizes `BrokerPortError` only, so an ibapi/socket failure on the **first probe of every preflight** collapsed the whole run into "unknown error, failing closed" — re-creating the D-CLOCK-02 blindness on a path the D-CLOCK-02 fix did not cover | **P1** | `broker-ibkr/paper_adapter.py` | ✅ **fixed in the PR #53 pre-merge audit** — `health_check` answers UNHEALTHY with the exception TYPE in `detail` (keeping the agent's tested `BROKER_UNHEALTHY`/`NOT_SENT` refusal on its normal path); `disconnect` normalizes to `BrokerPortError` and still clears the transport. A parametrized test asserts **no public adapter method** raises anything but `BrokerPortError`, and a completeness test fails if a new public method is added un-audited |
| D-CLOCK-04 | `describe_broker_error` sanitized the broker message but passed `error.correlation_id` into the evidence **unsanitized** — a generic-helper evidence path that bypassed every other gate (today's ids are internally generated, so latent, not realised) | P2 | `scripts/poi_ibkr_lifecycle.py` | ✅ **fixed in the PR #53 pre-merge audit** — the correlation id goes through the same bounded sanitizer; tests cover account-shaped tokens, filesystem paths, control characters and over-long values |
| D-WATCHDOG-01 | The unattended watchdog restarted IB Gateway on `PROCESS_DOWN` / `API_DOWN` / login failure / 2FA timeout with **no knowledge of account state**. A restart while an order is working, a position is open or the broker state is unresolved can orphan the order, sever a run mid-flight and destroy the evidence reconciliation needs — and the watchdog would have reported success, because the process came back. Process liveness was being used as an account-safety signal | **P1** | `scripts/ib_gateway_watchdog.py` | ✅ **fixed in this bundle** — a read-only account-safety probe (`CLEAN`/`NOT_CLEAN`/`UNKNOWN`) gates every unattended START/RESTART/RELOGIN. Connectivity failure is UNKNOWN, never CLEAN; every restart-triggering state is definitionally UNKNOWN, so the unattended watchdog now supervises and alerts rather than restarting. **Owner-host activation NOT RUN** |
| D-WATCHDOG-02 | The deferred ledger referenced `--status` and `--deep-probe --status` commands that **did not exist**; `/run/ibgateway` permissions, IBC log rotation and 4002/5901 exposure were unaudited; and OCI network security had no stated verification boundary | P2 | watchdog CLI + systemd assets + host audit | ✅ **fixed in this bundle** — read-only `--status` / `--deep-probe --status` with JSON, `RuntimeDirectory` 0700 + tmpfiles, logrotate, a read-only host audit and an explicit "OCI is never verifiable from the host" rule. **Owner-host activation NOT RUN** |
| D-02 | Owner-run `--client-id` default (7) diverges from adapter `DEFAULT_CLIENT_ID` (1); stale-connection recovery undocumented | P2 | lifecycle CLI + runbook | ✅ (doc) — not done here (P2, out of P0/P1 scope) |
| B-01 | POI-IBKR-001 Paper lifecycle (cancel-path/fill-path/protection/reconnect/reconciliation) not run | BLOCKED | owner-run | ❌ owner + host |
| B-02 | Owner-run chaos/recovery host drills not run | BLOCKED | `CHAOS_RECOVERY_DRILLS.md` §4 | ❌ blocked on B-01 |
| B-03 | IBC/watchdog host patches not upstreamed; no host transcript in repo | BLOCKED | ops scripts | ❌ host evidence |
| B-04 | Watchdog deep-probe not enabled (needs broker package on host) | BLOCKED | `ib_gateway_watchdog.py` | ❌ host |
| N-01 | NATS JetStream adapter absent (`trading_event_bus` skeleton) | DEFERRED | event-bus | out of IMP-1304 scope |
| N-02 | External market-data provider not selected | DEFERRED | market-data | A2 owner decision (deferred) |
| N-03 | POI-MD-001 aggregate BLOCKED | DEFERRED | PoI | provider-gated |

## 3. P0/P1 defects found

**D-01 (P1) — Reconnect protection verification fails open across symbols.**

- **Evidence:** `_run_reconnect` computed
  `protected = {symbol: qty for symbol,pos in live.items() if protective}` and
  `protection_survived_reconnect = not live or bool(protective)`, where
  `protective` is a single account-wide tuple. `bool(protective)` / `if
  protective` are **global**: any one resting protective stop marks **every**
  live position — including one in a different symbol with no stop — as
  protected. `BrokerPosition` carries `symbol`, so per-symbol coverage was
  available but unused.
- **Affected component:** `scripts/poi_ibkr_lifecycle.py::_run_reconnect`
  (owner-run reconnect scenario, step 6 of the owner-run sequence).
- **Safety impact:** high. The reconnect scenario exists to prove *"no naked
  position after a Gateway restart."* The bug lets a genuinely unprotected
  position be reported protected and lets reconciliation pass clean — a
  **fail-open in a protection-verification path**.
- **Reproducibility:** deterministic against fakes — an account reporting a
  protected position in `F` and a naked position in `G` reported the run as
  passed before the fix.
- **Acceptance criteria:** (a) protection coverage is evaluated per symbol; (b)
  a live position outside the run's allowlisted symbol is flagged, never
  silently protected; (c) any naked position triggers the loud
  `>>> OWNER ACTION REQUIRED <<<` abort and a reconciliation mismatch; (d) a
  single correctly-protected position in the run's own symbol still passes.
- **Resolution (two stages):**
  - *First fix (PR #45)* made coverage per-symbol but was **incomplete**: it
    still did `protected_symbols = {envelope.symbol} if protective else set()`
    on any `:stop:` order, so a **stale, cross-run, cross-scenario, or
    malformed** harness-looking stop could still mark `envelope.symbol`
    protected. This was caught in review and re-opened as **D-01b**.
  - *Second fix (PR #46)* bound the match to `entry_id =
    poi-ibkr-001-<run_date>-fill-path-<symbol>`. Review found this binds only
    date + scenario + symbol: two same-parameter same-day runs share the same
    `entry_id`, and a stale same-day stop from a run with different parameters
    could still match. Re-opened as **D-01c**.
  - *Corrective fix (this PR, D-01c + D-01d)* binds order identity to a
    **deterministic operation reference** and — critically — makes that
    reference a **separate, non-authorizing** value:
    - **D-01d (security):** the operation reference must **not** be the
      confirmation token. The token is accepted by `--execute --confirm`, so
      embedding it in `client_order_id`, broker-visible order metadata, and
      evidence would expose a reusable execution-authorization value. The
      reference is now `operation_reference(envelope)` — a SHA-256 digest over
      the same canonical fields but under a **distinct domain prefix**
      (`OPERATION_REF_NAMESPACE = "poi-ibkr-run-ref-v1"`, length
      `OPERATION_REF_LENGTH = 16`). It cannot equal a confirmation token and is
      never accepted by `--confirm`. `confirmation_token` stays exclusively for
      `--confirm` authorization.
    - The operation reference is embedded in
      `LifecycleEnvelope.client_order_id`
      (`…-fill-path-<symbol>-<run_ref>`), so the entry order id and the derived
      protective stop id both carry it. The reconnect scenario is given the
      fill-path **operation reference** via `--run-ref` (printed in the
      fill-path evidence); it reconstructs the expected stop id and matches
      only that. No random id is invented.
- **Deterministic operation-identity rule (corrective fix):** a resting order
  protects `envelope.symbol` only when its `client_order_id` is exactly
  `<entry_id>-protection:<entry_id>:stop:<positive-int>` with
  `entry_id = poi-ibkr-001-<run_date>-fill-path-<symbol>-<run_ref>`, it is in a
  working state (`SUBMITTING`/`ACKNOWLEDGED`/`PARTIALLY_FILLED`), and `run_ref`
  equals the fill-path **operation reference** supplied to `--run-ref`. The
  same reference is used consistently by the entry order id, the protective
  stop id, the reconnect matcher, and the fill-path evidence artifact. The
  reference is a **deterministic operation identity / crash-safe resumption
  identity / scenario-bound operation reference**; it is **not** a unique
  execution-instance id and does **not** distinguish two identical-parameter
  runs (that reliance is on the gates below).
- **Fail-closed rules:**
  - **`--run-ref` is required** for the reconnect scenario; without it a
    resting stop cannot be bound to this fill-path operation from
    `BrokerOrderReceipt`, so the run **fails closed**
    (`run_reference_provided` = False) and never claims protection;
  - protection requires **exactly one** matching working stop for the operation
    — zero ⇒ naked, more than one ⇒ ambiguous; both **fail closed**;
  - a stale prior-day stop (different `run_date`), a stale **same-day stop from
    a run with different parameters** (different operation reference), a
    cross-symbol stop, a cross-scenario order, or a malformed protective-looking
    id **never** counts;
  - identical-parameter same-day runs share the operation reference by design;
    the **preflight no-stale-order gate** and **pre-existing-position gate**
    keep the account clean before each fill-path run, so this cannot leave two
    distinct same-parameter positions;
  - **every non-zero** position is considered (long **and** short); a short in
    any symbol, or any cross-symbol position, is an anomaly that **fails
    closed** — the repository contract does not prove shorts impossible, so
    they are surfaced, never ignored;
  - the resting stop's **quantity cannot be read from `BrokerOrderReceipt`**,
    so full-coverage is **not asserted from the receipt**; the run records an
    explicit owner note to confirm the stop quantity covers the full position
    in the Gateway UI;
  - only the matched working stops for the operation are fed to reconciliation
    as internally expected, so a foreign/stale resting order surfaces as
    `UNEXPECTED_BROKER_ORDER`, and only a uniquely-protected run-symbol
    position is fed as `protected` (otherwise `POSITION_MISMATCH` /
    `PROTECTION_MISMATCH`).
- **Regression tests** (`tests/poi/test_poi_ibkr_lifecycle_cli.py`) — identity
  security: `test_operation_reference_is_not_the_confirmation_token`,
  `test_operation_reference_cannot_satisfy_confirm`,
  `test_operation_reference_changes_with_parameters`,
  `test_operation_reference_is_deterministic_for_identical_parameters`,
  `test_confirmation_token_never_appears_in_order_ids_or_evidence`; reconnect
  matching: `test_reconnect_matcher_rule_is_exact`,
  `test_reconnect_without_run_reference_fails_closed`,
  `test_reconnect_protected_f_but_naked_g_fails`,
  `test_reconnect_naked_f_with_unrelated_symbol_stop_fails`,
  `test_reconnect_naked_f_with_stale_prior_day_stop_fails`,
  `test_reconnect_naked_f_with_stale_same_day_different_run_stop_fails`,
  `test_reconnect_same_date_symbol_different_run_reference_fails`,
  `test_reconnect_malformed_protective_looking_order_fails`,
  `test_reconnect_exact_same_run_stop_passes`,
  `test_reconnect_short_position_fails_closed`,
  `test_reconnect_ambiguous_multiple_canonical_stops_fails_closed`. The
  pre-existing end-to-end reconnect tests remain green (now passing the
  fill-path operation reference to reconnect).
- **Quantity limitation (documented):** a resting stop's quantity is not
  exposed by `BrokerOrderReceipt`, so full coverage is never asserted from the
  receipt — the run records an explicit owner note to confirm the stop
  quantity covers the full position in the Gateway UI. A residual identity
  limitation is also documented: the operation reference is a **deterministic
  operation identity**, so two runs with *identical* parameters on the same day
  share it (the crash-safe resumption identity) — it does **not** distinguish
  two identical-parameter runs. The **preflight no-stale-order and
  pre-existing-position gates**, **exactly one matching working stop**, and
  **fail-closed ambiguity handling** are what keep that safe.

No **P0** defect was found.

## 4. Blocked items (cannot be closed under IMP-1304 now)

- **B-01** POI-IBKR-001 lifecycle owner-run — blocked on the owner running
  `docs/runbooks/POI_IBKR_001_OWNER_RUN.md` on the host.
- **B-02** Owner-run host chaos drills (`CHAOS_RECOVERY_DRILLS.md` §4) — blocked
  on B-01.
- **B-03** IBC/watchdog host patches upstreaming + host transcript — blocked on
  host evidence (documented in `CURRENT_PROJECT_STATUS.md` §6/§10).
- **B-04** Watchdog deep-probe activation — blocked on the broker package being
  present on the host.

None of these are defects in committed code; they are external/owner-run
dependencies and are tracked, not resolved here.

## 5. Non-defects / deferred scope

- **N-01** No NATS JetStream adapter — the event-bus is a documented skeleton;
  the consumer-side idempotency contract it will use is already proven at the
  persistence layer (`process_once`). Implementing the adapter is out of
  IMP-1304 scope.
- **N-02 / N-03** External provider + POI-MD-001 aggregate — deferred by the
  Track A / A2 owner decision (`EVIDENCE_ROADMAP.md`); no provider is selected.
- **D-02** `--client-id` default divergence is a **P2** consistency/doc gap, not
  a P0/P1 safety defect; recorded for a later doc pass rather than fixed here,
  to avoid inventing work. It fails closed today (IBKR rejects a duplicate
  client id), so there is no safety exposure.
- The safety findings from the pre-merge reviews of PRs #39–#42 (rejected-stop
  handling, partial-fill remainder, cross-day token/id scoping, protective-leg
  envelope, secret-store account resolution, environment-directory casing) were
  all **already fixed** in those PRs and re-verified here; they are closed, not
  open.

## 6. Integrated audit (Phase-13 non-market bundle, Section A)

Re-verified after the PR #39–#48 sequence — not assumed correct because CI was
green.

| Audit item | Result |
|---|---|
| confirmation token is authorization-only | ✅ used only for `--confirm` (`run_lifecycle` compares `confirmation_token(envelope)`); never embedded in ids/evidence |
| operation reference non-authorizing + domain-separated | ✅ `operation_reference` under `OPERATION_REF_NAMESPACE="poi-ibkr-run-ref-v1"`; ≠ token; rejected by `--confirm` (test) |
| no authorization token leak to ids/logs/reports/evidence | ✅ test `test_confirmation_token_never_appears_in_order_ids_or_evidence` |
| operation identity deterministic, not overclaimed as unique instance | ✅ terminology corrected (PR #48); identical-parameter runs share it by design |
| client_order_id length/charset valid for the adapter | ✅ the adapter uses it only as an internal dict key; **it is never sent to IBKR** (transport uses the integer orderId; `order.orderRef` is not set — see D-ADAPTER-01) |
| stop identity parseable after adding the operation reference | ✅ `_is_current_run_stop` parses `<entry_id>-protection:<entry_id>:stop:<int>` |
| reconnect matches only one working stop | ✅ exactly-one rule; zero→naked, >1→ambiguous, both fail closed |
| stale/malformed/foreign/cross-symbol/ambiguous stops fail closed | ✅ dedicated tests |
| all non-zero positions incl. shorts fail closed | ✅ `test_reconnect_short_position_fails_closed` |
| reconciliation receives correct expected orders/positions/protected | ✅ only matched working stops fed as expected; only uniquely-protected symbol fed as protected |
| cleanup cannot flatten | ✅ `_run_cleanup` cancels orders only; never reads/closes positions |
| partial + surprise fills remain protected | ✅ remainder cancelled before protection; surprise-fill abort |
| no retry can duplicate an order | ✅ agent idempotency + broker idempotency; duplicate-delivery tests |
| authorization/reference changes preserve crash-safe resumption | ✅ deterministic id for identical parameters |
| evidence redaction keeps fields needed for reconnect | ✅ operation reference is printed (non-secret) so `--run-ref` is recoverable |
| evidence has no secrets or executable authorization values | ✅ account masked; token never in the executed-run artifact |
| all runbook commands use `uv run --no-sync` | ✅ verified across all host runbooks |

### D-TIF-01 resolution — explicit, validated time-in-force

**Root cause:** the host transport constructed `ibapi.order.Order` and set
action/orderType/quantity/prices/orderRef but **never set `order.tif`**, so
IBKR received an empty TIF and rejected the order with error **10052**. There
was no canonical TIF concept at the broker boundary (the frozen contracts
model `time_in_force` as a token string — documented gap).

**Fix (code, contract-tested — no IBKR connection):**
- Added a typed **`TimeInForce`** enum at the broker-port boundary
  (`trading_broker_core.port`) with the broker-supported values `DAY`, `GTC`;
  `OrderIntent.time_in_force` defaults to **`DAY`** (the canonical MVP value for
  the BUY LIMIT entry and protective STOP). It is a required enum, never
  implicitly empty.
- The adapter translates every outbound field through pure, fail-closed helpers
  (`ibkr_order_type`, `ibkr_time_in_force`) and a single
  **`build_outgoing_order`** projection. An absent / empty / unknown /
  unsupported order type or TIF **raises before `placeOrder`** — validated both
  in `_validate_intent` and at order-build time.
- The transport now sets `order.tif` from the validated map and sets
  `order.transmit = True` explicitly (never left implicit).
- **Outgoing IBKR field matrix (after):** `account` = DU alias · `action` =
  BUY/SELL · `orderType` ∈ {LMT, STP, STP LMT, MKT} · **`tif` = "DAY" for the
  entry LIMIT, "GTC" for the protective STOP** (see D-TIF-02 below) ·
  `totalQuantity` = whole shares · `lmtPrice`/`auxPrice` only when applicable ·
  `orderRef` = `poi1:<client_order_id>` (non-authorizing) · `transmit` = True.
  **Before:** `tif` was empty; `transmit` implicit.

### D-TIF-02 resolution — protective STOP is GTC, not DAY (pre-merge review)

**Finding (P1, merge-blocking):** the first D-TIF-01 fix gave **every** order —
including the protective STOP — the default `DAY` TIF. A **DAY protective stop
expires at the session close** while the real 1-share position remains open,
leaving it **naked overnight** until the owner closes it by hand. This defeats
the "protection follows fills / no unprotected position" invariant.

**Fix:** the protective STOP is now submitted **GTC** so it rests until the
position is closed; the marketable entry LIMIT stays **DAY** (it is cancelled or
filled within the session). `ExecutionRequest.time_in_force` (default `DAY`) is
threaded through the Execution Agent into `OrderIntent`; the lifecycle harness
sets `TimeInForce.GTC` on the protective order only. `transmit = True` is safe
because the entry and the stop are **separate** orders (bracket/OCA are declared
`UNSUPPORTED` and unused) — there is no parent-child group an early transmit
could release incomplete.

Tests: `test_fill_path_entry_is_day_and_protective_stop_is_gtc` (lifecycle),
`test_protective_stop_gtc_outgoing_fields` + `test_day_and_gtc_are_both_supported_and_distinct`
(adapter field matrix).

### D-TIF-03 — deterministic broker rejects classified UNKNOWN (P2, documented)

**Finding (P2, operability — not a safety defect):** the host transport's
`_await_status` reacts only to `orderStatus`; a deterministic pre-acceptance
**validation reject** (e.g. `10052` empty-TIF, `321` Read-Only/validation)
arrives via the `error()` callback and produces **no** `orderStatus`, so
`_await_status` times out into `UNKNOWN_BROKER_RESPONSE`. That is *conservative
and safe* (UNKNOWN → mandatory reconciliation, never an unsafe action), but it
forces an **unnecessary reconciliation** for an order that was definitely never
submitted.

**Determination:** a deterministic broker validation reject *should* map to a
definite `REJECTED` / not-submitted state (distinct from pre-submit local
validation, an acknowledged order, and genuine transport ambiguity). **Not
implemented in this PR:** the fix lives entirely in the host-only
(`# pragma: no cover`) transport error-callback wiring and **cannot be verified
without a real Gateway**; a mis-scoped reject-code allowlist could hide a truly
working order (a safety regression worse than an extra reconciliation). It is
recorded as an owner-host-verified follow-up (DEF-17). The primary observed
reject (10052) is now impossible via the TIF fix, and Read-Only (321) is
prevented by the runbook precondition.
- **UNKNOWN handling and no-blind-retry are unchanged** — the harness still
  fails closed on UNKNOWN and never resends.

Tests: 13 new in `tests/broker/test_ibkr_paper_adapter.py` (BUY-LIMIT and
protective-STOP field matrices independently; DAY default; supported-TIF map is
exhaustive; absent/non-canonical and unsupported TIF fail closed; unsupported
order type fails closed; **10052 regression — an unmappable TIF fails closed
before `placeOrder`**; the builder never emits an empty TIF; the ref carries no
token/secret) + 2 in `tests/poi/test_poi_ibkr_lifecycle_cli.py` (cancel-path
entry and fill-path entry + protective stop all carry `DAY`).

### D-ADAPTER-01 resolution — orderRef persistence + open-order recovery

The **code fix is implemented and contract-tested** in this bundle
(`packages/py/broker-ibkr/src/trading_broker_ibkr/paper_adapter.py`); only the
**owner-host empirical verification** remains BLOCKED.

- **Outbound identity persistence:** every lifecycle order now carries its
  canonical, **non-authorizing** `client_order_id` in IBKR `Order.orderRef`
  under a versioned prefix. **orderRef encoding rule:** `orderRef =
  "poi1:" + client_order_id`, fully reversible; bounded by a documented
  `ORDER_REF_MAX_LEN = 200`; if an id ever exceeds the budget the submit
  **fails closed** (`OrderRefTooLongError`) — never truncates into a colliding
  ref. The ref carries no confirmation token, account secret or authorization
  value (asserted by tests).
- **Open-order recovery:** `get_open_orders` now takes a **read-only**
  `reqAllOpenOrders` snapshot (`open_order_snapshot` on the transport seam,
  ending on the canonical `openOrderEnd`) and normalizes it into
  `BrokerOrderReceipt`s, recovering the lifecycle identity from `orderRef` via
  `decode_order_ref`. **Recovery rule:** a `poi1:`-prefixed non-empty ref →
  lifecycle-owned (id recovered verbatim); a missing/foreign ref →
  `foreign:…`; a `poi1:`-prefixed empty/blank ref → `malformed:…`. Foreign,
  malformed, cancelled/filled/rejected, unmapped-status and duplicate-callback
  orders are never claimed as this run's protection; the lifecycle reconnect
  matcher does the finer stale/cross-symbol/ambiguous rejection, fail-closed.
- **Restart-safe:** a fresh adapter with empty memory recovers the resting
  stop from the snapshot alone (no process-local dependency) — tested.
- **Read-only:** the recovery path never calls `placeOrder` / `cancelOrder` /
  any mutation (static + behavioural tests).

Tests: 17 new in `tests/broker/test_ibkr_paper_adapter.py` — codec
reversibility, too-long fail-closed, token-absence, submit-sets-ref +
restart recovery, missing/malformed/foreign refs, duplicate callback,
cancelled/filled/rejected not working, unknown status dropped, snapshot API
error fail-closed, reconcile-uses-snapshot, read-path-never-mutates, ambiguity
surfaced, no-secret-in-ref.

**Still BLOCKED (owner host + market execution):** empirically verifying that a
real IB Gateway preserves `orderRef` and round-trips the official Paper
`openOrder`/`openOrderEnd` callbacks; restarting a real Gateway with a resting
stop and recovering it from a fresh process; and confirming the broker-specific
`orderRef` size limit against the installed API. Recorded as DEF-07 / DEF-15.

## 7. Effect on IMP-1304 `done_criteria`

`done_criteria` = *"No open critical/high safety defect."* The audit finds **no
open P0/P1 safety defect in committed code**. One P1, **D-ADAPTER-01**, is
**BLOCKED** on broker connectivity (host-only transport, unverifiable without a
real Gateway) and is documented rather than shipped untested. This does **not**
complete IMP-1304 or Phase 13: the blocked items (B-01…B-04, D-ADAPTER-01) are
owner-run/host dependencies, and IMP-1302 / the POI-IBKR-001 owner-run remain
outstanding. IMP-1304 stays open.

## 8. Cancellation + restart-recovery hardening (D-CANCEL-01 … D-CANCEL-08)

### 8.1 What the real owner-run proved

A real POI-IBKR-001 Paper owner-run reached the actual broker order path. This
is the first empirical evidence for this code, and it is the source of every
defect in this section.

**Worked:**

- PAPER environment gate and `DU` account gate passed;
- `F` resolved through `SMART` / `USD`;
- `BUY 1 F LIMIT 12.02` submitted and **acknowledged** by the broker;
- `broker_order_ref = ibkr-paper:2`;
- `operation_reference = 05D0E19B631C2548` (non-authorizing, as designed —
  the confirmation token never appeared in an order id or in the evidence);
- an idempotent resubmit correctly returned the original outcome — no second
  order;
- the order never filled during the test window.

**Failed:**

- `cancel_sent = FAIL` (`status=FAILED`);
- `cancel_confirmed = FAIL`;
- the order remained `ACKNOWLEDGED` at the broker;
- reconciliation reported `UNEXPECTED_BROKER_ORDER`;
- **the owner had to cancel the order manually in the Gateway UI.**

**Afterwards:** a later reconnect was clean — `open_poi_orders=0`, non-zero
positions `0`, `reconciliation=PASS`.

The harness's fail-closed behaviour held throughout (no retry, no resend, no
false PASS), but the evidence was nearly useless: the only diagnostic was
`status=FAILED`, because order-scoped broker errors were being discarded.

### 8.2 Root cause 1 — the cancellation was never encoded (D-CANCEL-01)

The installed API is:

```
EClient.cancelOrder(self, orderId: int, orderCancel: ibapi.order_cancel.OrderCancel)
```

and it reads `orderCancel.manualOrderCancelTime`, `orderCancel.extOperator`
and `orderCancel.manualOrderIndicator`. The transport passed `""`. Attribute
access on a `str` raised inside `ibapi` **before a valid cancellation request
was encoded**, so nothing was ever sent to the broker.

Fixed by importing `OrderCancel` alongside the other host-only `ibapi` imports
and calling `cancelOrder(broker_order_id, OrderCancel())`. If the installed API
does not expose `ibapi.order_cancel.OrderCancel`, the transport **fails closed**
with `BROKER_UNHEALTHY` rather than guessing a compatibility shim that might
send an invalid request. `ibapi` remains confined to the lazily-built host
transport (enforced by `scripts/check_architecture.py`).

### 8.3 Root cause 2 — recovery returned receipts but no cancellation state

`get_open_orders()` recovered each order's canonical identity from its
`orderRef` and returned `BrokerOrderReceipt`s, but never populated the
adapter's `_orders` / `_broker_ids` maps. `cancel_order()` reads only those
maps, so after a process or Gateway restart cleanup could **see** the resting
order and still fail `ORDER_NOT_FOUND` (D-CANCEL-04).

The obvious fix — hydrate from the same `reqAllOpenOrders` snapshot — is
itself unsafe (D-CANCEL-06). That request is a **global visibility** feed: it
reports orders placed by other API clients and by the TWS/Gateway UI. Seeing an
order there proves it exists; it proves nothing about whether this API client
may cancel it, and hydrating from it would have made a foreign or manual order
cancellable through this adapter.

The two concepts are now separate:

| | `get_open_orders()` | `recover_cancellable_orders()` |
|---|---|---|
| request | `reqAllOpenOrders` (global) | `reqOpenOrders` (this client) |
| purpose | reconciliation, anomaly detection, owner evidence | cancellation authority after a restart |
| grants cancellation? | **never** | only under every condition below |

An order becomes cancellable only when **all** hold: it came from the
same-client feed; `order.clientId` equals the configured adapter client id; the
account matches the configured Paper account; it is genuinely working; its
`orderRef` is a valid canonical `poi1:` reference (**the prefix alone is not
proof of ownership**); its broker order id is valid and non-zero; and its
identity is unambiguous. Ambiguity — one identity mapping to two broker order
ids, one broker order id carrying two identities, conflicting `permId`s, or a
snapshot contradicting process-local state — raises `UNKNOWN_BROKER_RESPONSE`
and hydrates **nothing**: cancelling the wrong order is far worse than
cancelling none (D-CANCEL-15 semantics, tracked here under D-CANCEL-06).
`reqGlobalCancel` does not appear anywhere in the adapter or the harness, and a
test asserts that.

### 8.4 Stale acknowledgements, order errors and the lossy queue

Three further defects made the failure both possible and undiagnosable:

- **D-CANCEL-02** — cancellation reused the generic "first order status wins"
  wait. A leftover `Submitted`/`PreSubmitted` could have been read as the
  cancellation result, and a fill arriving during cancellation would have
  counted as success. Cancellation now has its own classifier and reducer:
  stale acks are ignored, `PendingCancel` is accepted-but-not-final,
  `Cancelled`/`ApiCancelled` (and IBKR error **202**, which is how the broker
  reports "Order Cancelled") confirm, `Filled`/`PartiallyFilled` is an
  **exposure** event mapped to `UNKNOWN_BROKER_RESPONSE`, `Inactive` is a
  terminal `BROKER_REJECTED`, and a timeout is `UNKNOWN_BROKER_RESPONSE`
  requiring reconciliation. Cancellation is never retried automatically.

- **D-CANCEL-03 (errors)** — `EWrapper.error` was not overridden, so every
  order-scoped broker error was silently dropped; that is why the owner saw
  only `status=FAILED`. `error()` is now overridden with a version-tolerant
  normalizer (the 10.48 signature inserts `errorTime`), and errors are
  correlated to the waiting submit/cancel by the ids this transport actually
  issued. Informational farm notices (`2104`, `2106`, `2107`, `2108`, `2119`,
  `2158`) never fail an order but stay observable on a diagnostics stream. The
  original numeric code, the sanitized bounded text and any advanced-rejection
  string reach the evidence; no account id, credential, secret path or
  authorization material ever does.

- **D-CANCEL-03 (routing)** — every callback shared one `queue.Queue`, so a
  waiter draining positions could consume and discard the `orderStatus` a
  cancellation was blocked on, turning a real cancellation into a false
  timeout. `CallbackRouter` replaces it with independent per-key streams
  (order id, request id, open-order snapshot, positions, diagnostics). A
  consumer only ever removes envelopes from its own stream, waiter cleanup is
  explicit, and late callbacks are archived rather than dropped.

### 8.5 Governance, evidence and honesty fixes

- **D-CANCEL-05** — the confirmation token is a deterministic digest, so it
  cannot be single-use on its own. `OwnerRunJournal` adds a durable,
  crash-safe execute-attempt claim (`O_CREAT | O_EXCL`, which is atomic across
  processes) keyed by the operation reference and a canonical parameters hash.
  After `EXECUTE_STARTED`, a second `--execute` is refused regardless of any
  freshly minted in-memory authorization; only reconciliation, cleanup and
  read-only inspection may proceed; terminal-clean reconciliation closes the
  operation without erasing the audit record or the marker. The journal stores
  no token, account id, credential or authorization material.

- **D-CANCEL-07** — cleanup is labelled
  `CANCEL-ONLY (no order submission; existing POI orders may be cancelled)`
  instead of the misleading `DRY-RUN (nothing sent)`; it waits for terminal
  confirmation, re-reads the broker, and passes only when no targeted working
  order remains. Every eligible order gets exactly one exposure-reducing
  attempt even if an earlier one fails (still no retry), and the report lists
  attempted / confirmed / unresolved / still-working. A new read-only
  `--scenario verify-clean` is the scenario-correct verifier for a cancel-path
  operation; `reconnect` stays dedicated to proving a fill-path position and
  its exact protective stop survived a restart.

- **D-CANCEL-08** — the cancel path no longer claims an order "cannot fill".
  A limit below the market is **non-marketable at the observed quote**: fill
  risk reduced, not eliminated. The approval is bound to a fresh two-sided
  quote (positive bid/ask, sane spread, open session, bounded age) which is
  revalidated immediately before submission and aborts on material drift, and
  `never_filled` is reported as an observed post-run fact. Position evidence
  now separates raw rows, non-zero positions and run-symbol positions, so a
  flat account can no longer read as `1 position(s)`.

### 8.6 What is still NOT proven

Everything in this section is verified against fakes, a deterministic fake
transport and a stubbed `ibapi` — no IBKR connection was made and no Paper or
Live order was sent while implementing it. The following remain **NOT RUN**:

- no real IBKR Paper order after the code change;
- no real cancellation after the code change;
- no fill-path owner-run;
- no Gateway restart recovery owner-run;
- no Live connection and no Live order;
- no PAPER_AUTO activation.

**POI-IBKR-001 stays BLOCKED / non-PASS.** Only the owner may change it, from a
fresh post-merge Paper owner-run, in `docs/poi/STATUS.yaml`. The post-merge
validation sequence is recorded as DEF-18 … DEF-20 in
`docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.yaml`.

## 9. Adapter clock and diagnosability (D-CLOCK-01, D-CLOCK-02)

### 9.1 What the owner host observed

A read-only owner-host inspection after PR #52 produced two findings.

| Observation | Value |
|---|---|
| host system time (UTC) | `2026-07-30T17:05:22Z` |
| first adapter timestamp | `2026-07-10T12:00:01Z` |
| subsequent adapter timestamps | `...12:00:02Z`, `...12:00:03Z`, `...12:00:04Z` |
| open orders | 0 |
| non-zero positions | 1 — `DVLT` quantity `1352` (unrelated to any PoI run) |
| `verify-clean` output | `broker error BROKER_UNHEALTHY; no retry` (nothing else) |

The timestamps advance by exactly one second per call and start ~20 days
before the real host time. That is not clock drift — it is a synthetic tick
sequence.

### 9.2 D-CLOCK-01 — a test clock shipped in the production adapter

`IbkrPaperAdapter.__init__` contained:

```python
self._now = clock or _deterministic_clock(datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
```

`_deterministic_clock` returns `start + N seconds` on the Nth call. It exists so
tests can compare receipts and events for equality. As the **default** it meant
that any adapter constructed without an explicit clock — i.e. every real
owner-run — fabricated its timestamps.

**Every** evidence timestamp the adapter produces went through `self._now()`:

| Timestamp | Where |
|---|---|
| `AccountSnapshot.observed_at` | `get_account_snapshot` |
| `BrokerOrderReceipt.received_at` | `submit_order`, `cancel_order` |
| `BrokerOrderEvent.observed_at` | `_append_event` (the append-only stream) |
| `ReconciliationResult.reconciled_at` | `reconcile` |
| `IntegrationHealth.checked_at` | `health_check` |
| recovered open-order receipts | `get_open_orders`, `recover_cancellable_orders` |
| UNKNOWN / reconciliation evidence | `reconcile` + the unresolved set |

**Safety and evidence impact.**

- *Evidence integrity (realised).* Artifacts from a real owner-run cannot be
  correlated with the IB Gateway log, with wall-clock reality, or with the
  harness's own timestamps. Since the harness clock (`run_lifecycle`) was
  already real UTC, a single artifact mixed real and fabricated times ~20 days
  apart.
- *Event ordering (realised).* Adapter-stamped `BrokerOrderEvent`s and
  agent-stamped records sat in two unrelated time bases, so "what happened
  first" was not answerable across that boundary.
- *Reconciliation age (latent).* Any age-based judgement over broker
  timestamps would have measured against a fixed past instant.
- *Not affected — checked explicitly:*
  - **quote freshness** uses the harness clock (`now_fn`, already real UTC) and
    the owner-supplied `--quote-at`, never the adapter clock;
  - **authorization / confirmation tokens** are digests of parameters plus the
    `--run-date`, with no adapter timestamp in the material;
  - **journal timestamps** come from `run_lifecycle`'s `now_fn`;
  - **operation timeouts** never used adapter time — `CallbackRouter` and
    `threading.Event.wait` are monotonic (see 9.3).

**Fix.** The default is now the real clock:

```python
self._now = _validated_clock(clock or utc_now)
```

- `utc_now()` returns `datetime.now(tz=UTC)` — timezone-aware, no import-time
  constant, no dependence on the host's local timezone;
- `_validated_clock` **fails closed** (`VALIDATION_FAILED`) if a clock returns
  a naive timestamp, and normalizes any aware reading to UTC;
- injected clocks still work, so deterministic tests keep full control of time;
- `_deterministic_clock` was **deleted** from the adapter — that helper belongs
  to tests, and it now lives in `tests/broker/test_ibkr_paper_adapter.py`;
- the **fake broker keeps its own deterministic default** (unchanged): it is a
  test double, its reproducibility is the point, and the conformance suite's
  "same inputs ⇒ same result" assertion depends on it. The IBKR conformance
  test now injects that deterministic clock explicitly instead of relying on a
  frozen production default.

A guardrail test fails the build if any `datetime(20…)` literal reappears in
`broker-ibkr` production code.

### 9.3 Wall-clock vs monotonic

Evidence timestamps are UTC **wall-clock** — that is what an owner correlates
against. Elapsed-duration measurement must not be, because the system clock can
step (NTP correction, tz database update, manual change): a backwards jump
could extend a bounded wait indefinitely, and a forwards jump could abandon a
live order early.

| Purpose | Source |
|---|---|
| evidence timestamps | `utc_now()` / `run_lifecycle`'s `now_fn` — UTC wall-clock |
| callback waits | `CallbackRouter` — `time.monotonic` |
| gateway-ready wait | `threading.Event.wait` — monotonic internally |
| harness order polling | `_await_state` — `time.monotonic` (**changed here**) |

`_await_state` previously accumulated `poll_seconds` per iteration, which
measured sleeps rather than elapsed time. It now takes an explicit `monotonic`
source and computes a real deadline. The two bases are never mixed: monotonic
values are durations only and never reach an artifact.

### 9.4 D-CLOCK-02 — a fail-closed abort that hid the account state

`verify-clean` reported only `broker error BROKER_UNHEALTHY; no retry`. Two
things were wrong with that.

1. **No detail.** The code alone cannot distinguish a dead Gateway from a
   refused preflight. Broker errors now render as
   `broker error <CODE>; <bounded sanitized message>; cause=<RootCauseType>;
   correlation_id=<id>; no retry`. The root cause's TYPE is included but never
   its text (a third-party exception's text is not ours to trust). Messages are
   collapsed, truncated to 200 characters, re-masked for anything shaped like a
   broker account, and stripped of anything path-like so a secret-store
   location can never surface.

2. **One failure erased everything else.** The first failing read raised out of
   the whole run. Now each read-only probe is recorded independently, and a
   read-only scenario continues past a failed preflight — it can send nothing,
   and the owner needs the whole picture. A failed probe still records a FAILED
   check, so the run still fails closed and still refuses to submit; only the
   surviving evidence improves.

So the `DVLT` position is now named explicitly
(`verify_no_open_position … open: DVLT:1352`, plus the raw/non-zero/run-symbol
breakdown) instead of vanishing behind a generic broker error.

### 9.5 The unrelated Paper position

The owner host holds `DVLT` quantity `1352` with zero open orders. It is
unrelated to any PoI operation.

**Nothing in this bundle modifies, closes, protects or otherwise acts on it,**
and the harness still never flattens. `verify-clean` continues to FAIL while any
non-zero position exists — that is correct — but it now names the position and
prints the owner-action banner instead of hiding it. Closing it is the owner's
decision, by hand, in the Gateway/TWS UI.

### 9.6 NOT EXECUTED

The defect was found by read-only host inspection. Since discovery, **no real
IBKR order and no real cancellation have been executed**, and nothing in this
bundle was validated against a live Gateway:

- ❌ no cancel-path execute;
- ❌ no real cancellation after this change;
- ❌ no fill-path owner-run;
- ❌ no Gateway restart recovery owner-run;
- ❌ no Live connection and no Live order;
- ❌ no PAPER_AUTO activation.

**POI-IBKR-001 remains non-PASS.** The post-merge validation sequence is
DEF-19 → DEF-18 → DEF-20 in
`docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.yaml`; every evidence artifact
produced before this fix carries fabricated adapter timestamps and must be
re-generated rather than trusted.

### 9.7 Pre-merge audit of PR #53

An independent audit of the clock fix before merge. No owner-run was performed
and no order was submitted, cancelled, modified or flattened.

#### A-01 (test efficacy, mandated finding) — the regression test was too weak

`test_the_default_clock_does_not_stay_frozen` asserted `second >= first`. That
does **not** prove the clock is dynamic: an implementation capturing
`datetime.now(tz=UTC)` **once at construction** and returning that instant
forever satisfies every assertion it made — the value is current, it is not the
old `2026-07-10` default, and it is trivially non-decreasing.

Replaced by `test_the_default_resolves_through_utc_now_on_every_reading`, which
monkeypatches the module-level `utc_now` **before** constructing the adapter to
hand out two distinct controlled timestamps, builds the adapter with **no**
`clock` argument, and asserts both readings equal those two values with a call
counter. That proves (1) the default resolves through the production source —
an inline `datetime.now(...)` would ignore the patch and fail; (2) the source is
invoked once per reading; (3) nothing is captured at import or construction
time (the counter is asserted to be 0 after construction). No sleep, no timing
race.

**Verified by mutation.** With the capture-once defect deliberately injected,
the old-style assertions still passed and only the new test failed:

```
assert calls["count"] == 0, "construction must not read the clock"
E   AssertionError: construction must not read the clock
E   assert 1 == 0
1 failed, 17 passed
```

The mutant was then reverted. Two supporting tests remain: one demonstrating
the capture-once shape is detectable, one keeping the end-to-end sanity check on
the unpatched default.

#### D-CLOCK-03 (P1) — arbitrary exceptions could bypass the error boundary

`_probe` catches `BrokerPortError` by design. The audit therefore checked every
public adapter read for arbitrary-exception leakage:

| Method | Before | Now |
|---|---|---|
| `health_check` | **LEAKED** — unguarded `transport.is_connected()` | UNHEALTHY + exception TYPE in `detail` |
| `disconnect` | **LEAKED** — unguarded `transport.disconnect()` | `BrokerPortError`; transport still cleared |
| `get_account_snapshot`, `resolve_instrument`, `get_open_orders`, `get_positions`, `recover_cancellable_orders`, `reconcile` | `_guarded` ✓ | unchanged |
| `capabilities`, `order_events` | pure, no I/O ✓ | unchanged |
| `connect`, `submit_order`, `cancel_order`, `preview_order` | normalized ✓ | unchanged |

`health_check` was the worst case: it is the **first** probe of every preflight,
so a single SDK hiccup blanked the entire artifact — exactly the blindness
D-CLOCK-02 set out to remove, on a path that fix did not cover.

It is normalized to a **value** (`healthy=False`) rather than a raise, because
that is this method's contract and it keeps the Execution Agent's tested
`BROKER_UNHEALTHY` / `NOT_SENT` pre-submit refusal on its normal path instead of
rerouting a safety gate through an exception. The failure is not swallowed: the
exception TYPE is reported (its text is not — it may come from a third party).

`_probe` was deliberately **not** broadened to catch `Exception`; that would
hide programmer errors instead of fixing the adapter boundary.

#### D-CLOCK-04 (P2) — unsanitized correlation id in evidence

`describe_broker_error` bounded and scrubbed the broker message but interpolated
`error.correlation_id` raw. Current ids are internally generated, so this was
latent rather than realised, but the helper is generic and would have been an
evidence path bypassing every other gate. Now sanitized identically.

#### Structural confirmations (no change required)

- **Read-only continuation** is `READ_ONLY_SCENARIOS = {preflight, reconnect,
  verify-clean}` — asserted exactly, asserted disjoint from
  `SUBMITTING_SCENARIOS`, and asserted to exclude `cleanup`, `cancel-path`,
  `fill-path` and `dry-run`. Behaviourally, a broker that raises `AssertionError`
  on `submit_order` / `cancel_order` / `preview_order` is driven through a failed
  preflight in every scenario: the three read-only ones continue and never call
  one, and the four mutating ones stop dead with "nothing was submitted".
  An **AST** check (not a substring match — the words appear in prose) proves the
  read-only scenario bodies contain no call to a state-changing method, and a
  guard-the-guard test proves the AST check detects a real one.
- **`verify-clean`**: `DVLT 1352` with zero PoI orders is reported explicitly,
  the result stays FAIL, the position is asserted unmodified, an unresolved
  reconciliation finding cannot produce PASS, a failed open-order probe still
  shows the position, and a failed position probe reads `UNPROVEN` — never
  "flat".
- **Clock coverage**: all seven `self._now()` call sites map to evidence fields
  (`AccountSnapshot.observed_at`; `received_at` on submit, cancel, recovered and
  same-client-recovered receipts; `reconciled_at`; `checked_at`), and
  `BrokerOrderEvent.observed_at` derives from the receipt. Each is covered by a
  bounded current-UTC test.
- **Monotonic isolation**: `monotonic` appears only as the `_await_state`
  deadline and as a threaded parameter; `_await_state` records nothing, and a
  run driven by a fake monotonic of `1234.5` is asserted not to contain that
  value anywhere in its artifact.

#### Still NOT EXECUTED

Unchanged by this audit: no cancel-path execute, no real cancellation, no
fill-path, no restart-recovery owner-run, no Live connection or order, no
PAPER_AUTO. **POI-IBKR-001 remains BLOCKED / non-PASS.**

## 10. Operational hardening (D-WATCHDOG-01, D-WATCHDOG-02)

### 10.1 The defect

The watchdog supervised a **process**. It restarted IB Gateway whenever the
process was down or the API was unresponsive, and it knew nothing about the
**account** behind that process.

Those two signals look similar and are not. Restarting a Gateway while an order
is working can orphan that order; doing it while a position is open removes the
session that was supposed to be watching it; doing it while the broker state is
unresolved destroys the evidence reconciliation needs. In every one of those
cases the old watchdog would have reported success — the process came back.

### 10.2 The fix: an account-safety gate

Every unattended `START` / `RESTART` / `RELOGIN` is now gated on ONE complete
read-only snapshot classified as `CLEAN` / `NOT_CLEAN` / `UNKNOWN`.

`CLEAN` requires all of: localhost only · Paper port 4002 only · `DU`/Paper
account validation · successful API connection · successful liveness round trip
· complete open-order snapshot · complete position snapshot · zero working
orders · zero non-zero positions · zero unresolved operation states · clean
reconciliation with zero blocking findings · clean disconnect.

| Classification | Unattended action |
|---|---|
| `CLEAN` | policy may be evaluated (budgets, locks, 2FA unchanged) |
| `NOT_CLEAN` | `WAIT` + alert; the launcher is never called |
| `UNKNOWN` | `ALERT`, fail closed; the launcher is never called |

**Connectivity failure is `UNKNOWN`; it is never `CLEAN`.** Historical, cached
or last-known-clean state is never consulted.

The probe is handed a facade that **does not define** `submit_order`,
`cancel_order` or any modify verb — the order surface is not merely unused, it
is unreachable.

### 10.3 The deliberate consequence

Every state that could trigger a restart cannot query the broker, so it is
definitionally `UNKNOWN`. **The unattended watchdog therefore no longer
restarts anything; it supervises and alerts.** Recovering a Gateway is an
owner-authorized action.

The controlled, serialized DEF-20 restart under the canonical runbook is
**unaffected** — manual owner-authorized restart and unattended watchdog
restart are separate capabilities, and only the second was removed.

The lifecycle marker still inhibits action; it is **necessary but not
sufficient**, which is precisely why the gate sits above it. Budgets and locks
are preserved and can never override the gate: a freed restart budget is not a
proof about the account.

### 10.4 Also delivered

Read-only `--status` and `--deep-probe --status` (with deterministic JSON and
an explicit **proof level**, so `TCP_OPEN` can never be mistaken for a claim
about the account) · `RuntimeDirectory=ibgateway` at `0700` plus tmpfiles ·
logrotate for the IBC log · one canonical Gateway service asserted in code and
in the host audit · a read-only host audit for B-03/B-04 with redaction and a
non-zero exit on security failure · port-exposure detection with an explicit
**"OCI network security is never verifiable from the host"** rule · an
installer that defaults to CHECK/DRY-RUN and never touches firewall, OCI, SSH
or VNC.

### 10.5 Implementation readiness vs host activation

| | State |
|---|---|
| Code, units, audit, installer, tests | ✅ **IMPLEMENTED AND TESTED WITH FAKES** |
| Owner-host installation | 🟡 **READY FOR OWNER-HOST ACTIVATION** |
| Owner-host evidence | ❌ **NOT EXECUTED** |

**B-02, B-03 and B-04 remain OPEN. DEF-12 and DEF-13 remain `NOT_RUN`.**
Nothing here marks POI-PLAT-ARM-001 newly passed, POI-IBKR-001 PASS, IMP-1303
complete or IMP-1304 complete.

Twelve safety mutations were injected to prove the guards are real (UNKNOWN
permitting a restart, NOT_CLEAN permitting a restart, the gate removed,
`PROCESS_DOWN` treated as probeable, an optimistic default probe, a connection
failure reported as CLEAN, a working order or position no longer blocking, the
facade gaining an order surface, the service auto-restarting, redaction
removed, OCI claimed as verified). **All twelve fail the suite.**

### 10.6 NOT EXECUTED

- ❌ no IBKR connection of any kind;
- ❌ no `systemctl` command run against a real host;
- ❌ no Gateway start, stop, restart or relogin;
- ❌ no order submitted, cancelled, modified or flattened;
- ❌ no firewall, nftables, firewalld or OCI change;
- ❌ no credential, account id or IBC config content added;
- ❌ no PAPER_AUTO, no LIVE.
