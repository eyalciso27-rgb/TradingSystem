# POI-EMERG-001 — Emergency halt path

**Status: BLOCKED** (core halt path proven by a real code-path drill; the
remaining canonical steps need components/hosts that do not exist yet)

| Field | Value |
|---|---|
| Date | 2026-07-07 |
| Environment | local dev (in-process drill against the real IMP-0404 service) |
| Platform | Windows-11-10.0.26200 x64; Python 3.13.12 |
| Command | `uv run pytest tests/poi/test_poi_emerg_001.py -q` |
| Result | 2 passed (drill file: [test_poi_emerg_001.py](../../../tests/poi/test_poi_emerg_001.py)) |

## What ran for real (expected vs. actual)

| Step | Expected | Actual |
|---|---|---|
| Halt via emergency endpoint/CLI equivalent | immediate, RBAC-checked, audited | PASS — authorized OWNER activation, audited twice (audit sink + append-only log) |
| Queue order then halt | queued entry blocked → `CANCELLED_BY_POLICY` | PASS |
| Pre-submit check | new submissions blocked after halt | PASS |
| Protective exits | never blocked by halt | PASS |
| In-process restart | fresh service over the persisted log derives same halts | PASS |
| Resume | fails closed without reconciliation; requires MFA principal; succeeds with health+reconciliation | PASS |

## Blocking requirements (why not PASS)

1. **Halt via UI** — no dashboard exists yet (arrives in PHASE-8); the
   UI-button step cannot run.
2. **State survives service restart (cross-process)** — requires PostgreSQL:
   the durable replay is implemented (`trading_persistence.emergency`) and
   covered by `tests/db/test_emergency.py`, which runs only in CI's
   db-migration job; no local database is available and CI results are
   verified by the human operator.
3. **Resume with real MFA** — MFA is currently an authenticated-principal
   flag (IMP-0301 hook); a real MFA factor is a later task.

## Limitations / next step

Re-run the full drill on a Docker host (compose stack + PostgreSQL) once the
API/UI services exist; promote to PASS only after the UI path and the
cross-process restart are demonstrated there.
