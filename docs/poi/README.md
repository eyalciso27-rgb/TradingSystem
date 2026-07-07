# Proof-of-Integration — Canonical Status and Evidence (PHASE-5)

The canonical PoI backlog is frozen in
`docs/baseline-v0.5/POI_BACKLOG.yaml` (never edited). This directory is the
canonical **status and evidence registry** for executing that backlog
(IMP-0501/0502/0503).

## States

Every PoI is in exactly one canonical state:

| State | Meaning |
|---|---|
| `PASS` | The experiment ran for real and met all pass criteria. |
| `FAIL` | The experiment ran and a fail criterion triggered. |
| `BLOCKED` | The experiment cannot run yet (missing credential, host, account or Docker); the blocking requirements are documented. |

Rules (binding):

- A PoI is **never** marked PASS from reading documentation, and never from
  mocks when the DoD requires a real connection.
- Evidence records date, environment, platform, versions, command, expected
  vs. actual results and limitations.
- Evidence never contains credentials, tokens, cookies, sessions or secrets;
  secrets flow only through the IMP-0302/IMP-0303 mechanisms.
- LIVE trading stays disabled; no orders to a real broker; experiments
  touching third-party services must be minimal, reversible and documented.

## Layout

- `STATUS.yaml` — machine-readable registry (id, priority, status, evidence).
- `evidence/<POI-ID>.md` — evidence per executed PoI drill.
- `evidence/BLOCKED-P0.md` — per-item blocking requirements for PoIs that
  cannot run in the current environment.
- Executable drills live in `tests/poi/` and run in normal pytest (and under
  the CI arm64-emulated job).
