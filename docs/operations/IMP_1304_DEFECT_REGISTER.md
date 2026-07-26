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
| D-01 | Reconnect protection check is a global boolean, masking a naked position in another symbol | **P1** | `scripts/poi_ibkr_lifecycle.py` `_run_reconnect` | ✅ **fixed in this PR** |
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
- **Resolution:** protection is now per-symbol. A protective stop covers only
  the run's own symbol; positions in any other symbol are reported via a new
  `no_unexpected_symbol_position` check and, together with any naked position,
  raise `_unprotected_position_abort` and are excluded from the `protected`
  set fed to reconciliation (so they surface as `PROTECTION_MISMATCH`). The
  `":stop:"` delimiter is matched instead of a bare `"stop"` substring.
  Regression tests:
  `test_reconnect_does_not_mask_a_naked_position_in_another_symbol` and
  `test_reconnect_with_only_the_run_symbol_protected_still_passes` in
  `tests/poi/test_poi_ibkr_lifecycle_cli.py`. The two pre-existing reconnect
  tests remain green.

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
