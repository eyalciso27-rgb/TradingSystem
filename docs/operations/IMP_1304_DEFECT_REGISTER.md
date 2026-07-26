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

## 6. Effect on IMP-1304 `done_criteria`

`done_criteria` = *"No open critical/high safety defect."* After closing D-01,
the audit finds **no open P0/P1 safety defect in committed code**. This does
**not** complete IMP-1304 or Phase 13: the register's blocked items (B-01…B-04)
are owner-run/host dependencies that must still be discharged, and IMP-1302 /
the POI-IBKR-001 owner-run remain outstanding. IMP-1304 stays open pending
those.
