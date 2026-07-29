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
| D-TIF-03 | Deterministic broker validation rejects (10052/321) time out into `UNKNOWN` (not a definite `REJECTED`), forcing unnecessary reconciliation | P2 | `broker-ibkr` host transport `_await_status`/`error()` | 📝 **documented** — determination + DEF-17 owner-host follow-up; not implemented (host-only, unverifiable offline; conservative-safe today) |
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
