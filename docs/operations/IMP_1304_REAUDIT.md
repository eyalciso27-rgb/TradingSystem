# IMP-1304 — Re-audit of current main after PRs #66–#69

**Task:** IMP-1304 "Close operational defects" — *"Resolve P0/P1 defects and
rerun regression; No open critical/high safety defect."*

This is a fresh re-audit of **current `main`** (through the merged PRs #66–#69)
that supersedes the older audit in
`IMP_1304_DEFECT_REGISTER.md` (which was written against an earlier main). It
does **not** change any PoI status; POI-IBKR-001 stays BLOCKED / NOT RUN.

## 1. Method

- Read the canonical task (`CLAUDE_CODE_TASKS.yaml`), the defect register,
  `PHASE_13_DEFERRED_OWNER_RUNS.yaml`, `docs/poi/STATUS.yaml`, the Master Plan,
  the Component Evidence Matrix, the chaos/recovery runbook and the Paper-Manual
  readiness.
- Reviewed the code merged in **#66** (bounded, fail-closed Gateway recovery +
  the final-boundary reconciliation latch), **#67** (IMP-1301 E2E-shadow
  acceptance), **#68** (IMP-1302 Paper-Manual offline), **#69** (IMP-1303 chaos
  restart-latch, merged) for any newly-introduced safety/operational defect.
- Marker sweep and full regression.

## 2. Findings

| Check | Result |
|---|---|
| `TODO` / `FIXME` / `HACK` / `XXX` in `apps/ packages/ scripts/` | **0** (the 2 grep hits are the words inside log-level comments) |
| Open GitHub issues | 0 |
| Full regression (Linux/Bash) | **2319 passed, 64 skipped, 0 failed** |
| ruff / mypy strict / architecture guardrails / status-consistency | green |
| New #66 code — recovery exit-code contract + persistent reconciliation latch | reviewed: it **closes** a real safety gap (a frozen pre-crash `SubmitAuthorization` could submit into a recovered session); it is a fix, not a defect. Latch is fail-closed, entry-only, cancel/protective ungated. |
| New #67/#68/#69 code | test-only; no production surface changed |
| Prior last open P1 — **D-ADAPTER-01** (orderRef persistence + open-order recovery) | **resolved in committed code** and contract-tested; its *empirical* round-trip is owner-gated (DEF-15), not a code defect |

## 3. Open P0/P1 defects that can be fixed offline

**None.** After PRs #66–#69 there is **no open offline P0/P1 safety or
operational defect in committed code.** The `IMP_1304_DEFECT_REGISTER.md`
findings (D-01/D-01b/D-01c protection-verification fail-open, D-TIF-01/03
time-in-force, D-ADAPTER-01 orderRef, D-CANCEL family) are all **resolved in
committed code** and covered by tests; no new defect was introduced by
#66–#69 (the latch is a net safety improvement).

## 4. What remains — owner-gated evidence, not code defects

These are **evidence** dependencies, explicitly not counted as open code
defects (per the task's intent and the owner's instruction not to mark an
owner-gated evidence gap as a code defect):

- IMP-1302 real-IBKR Paper runs (DEF-01/02/03/04/05/09/18/20); DEF-19
  verify-clean is PASS.
- IMP-1303 host chaos drills (DEF-06/07/08/10/11).
- IMP-1304 empirical verifications (DEF-15 orderRef round-trip, DEF-17 broker
  reject-code classification).
- NATS JetStream transport end-to-end (no adapter exists; ADR-gated separate
  task).
- PAPER_AUTO-only items (watchdog `.venv` 203/EXEC; deterministic event-driven
  native-reconnect session invalidation) — do not affect Shadow / Paper-Manual.

## 5. Conclusion

IMP-1304's `done_criteria` ("no open critical/high safety defect") is **met for
committed code**: the offline defect surface is closed. IMP-1304 as a task stays
**open** only on the owner-run empirical verifications above, which are recorded
as deferred evidence — not as open code defects.
