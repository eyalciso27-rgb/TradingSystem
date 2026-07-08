# ADR-0014: Defer external PoIs; allow core development on mocks/fixtures

## Status

ACCEPTED (owner decision, 2026-07-08)

## Context

The PHASE-5 exit gate (DoD of IMP-0501: "Every P0 item PASS/approved
alternate ADR.") is **not satisfied**: 2/12 P0 PASS (POI-SEC-SESS-002,
POI-PLAT-ARM-001), 10 BLOCKED on owner-supplied external accounts,
credentials, hosts and a UI (Telegram, Discord, WhatsApp, IBKR, market data,
InvestingPro, news, AI keys, emergency-UI). See
`docs/poi/PHASE5-COMPLETION-REQUIREMENTS.md`. ADR-0013 accepted the social
user-account risk but did not make any PoI PASS.

The frozen task graph has every post-PHASE-5 task depend transitively on
PHASE-6, so a strict reading blocks all further work behind external
integrations the owner cannot supply right now.

## Decision

The owner **defers** the external PoIs and authorizes continuing with **core
system development** in parallel, under strict conditions. This ADR does
**not** claim the PHASE-5 exit gate passed and does **not** mark any PoI
PASS. The external PoIs remain OPEN in `STATUS.yaml`, now labelled
`deferred_by_owner: ADR-0014`; each still requires a real PASS before any
real activation.

### Allowed now (core development, no real external activation)

- Mock/stub connectors behind the canonical source-adapter ports.
- Golden/synthetic fixtures and replay datasets; synthetic market data.
- Manual sample import of non-sensitive data.
- Paper *simulation* that never contacts a broker (in-process/simulated fills).
- Core domain, ports, engines, persistence, scheduling, UI on fixtures.

### Forbidden until the corresponding PoI PASSes

- LIVE trading (already prohibited by baseline; stays disabled).
- Real PAPER_AUTO against a broker; sending any order to IBKR.
- Any real Telegram/Discord/WhatsApp/InvestingPro/news/market-data/AI call.
- Any claim that an external source or broker is "validated".
- Using secrets that were never supplied through IMP-0302/0303.

### Binding gate rule (new)

**Before any real run against an external source or broker, that source's /
broker's PoI must be PASS.** A mock/fixture implementation may be built and
tested first, but flipping it to a live provider is gated on the PoI. This
rule is added to the open-risk register below and to
`PHASE5-COMPLETION-REQUIREMENTS.md`.

### Production external adapters (IMP-0601/0602/0603, PHASE-6)

These remain gated: their DoD ("Contract/integration tests from PoI pass in
production adapter") cannot be met until the PoIs produce fixtures and PASS.
They may be scaffolded against the PoI harnesses but are not "done" and are
never wired to a real account under this ADR.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Halt all work until every P0 PoI PASSes | Blocks months of core work on owner-external dependencies. |
| Mark PoIs PASS on fixtures to unblock | Dishonest; violates the binding rule that PASS requires a real run. |
| Redefine PHASE-6 to accept mocks | Would let a mock reach production activation; rejected. |

## Consequences

- Core phases can proceed on fixtures; integration risk is retired later when
  each PoI runs. Every mock is a debt tracked against its PoI.
- Risk of "mock drift" from real provider behavior — mitigated by keeping the
  PoI contract/fixtures authoritative and re-validating at activation.

## Security impact

None new: no secrets, no external calls. Session/secret stores (IMP-0302/0303)
stay unused until a PoI runs.

## Operational impact

Deployments remain non-live; observability/emergency work continues on the
existing host. No real alert/notification channel is wired.

## Data impact

None. Schema changes still require new Alembic revisions per policy; frozen
migrations untouched.

## Rollback plan

Revert this ADR to reinstate the strict PHASE-6 dependency; no data/config
change is entailed.

## ARM64 impact

None; core work stays portable and CI-verified on ARM64 as before.

## Paper/Live impact

LIVE stays disabled. PAPER remains simulation-only (no broker) until
POI-IBKR-001 PASS. Paper/Live isolation invariants unchanged.

## Open risks (preserved — none of the original blockers are closed)

All 10 BLOCKED P0 items remain open exactly as in
`PHASE5-COMPLETION-REQUIREMENTS.md`: POI-SOC-TG-USER, POI-SOC-TG-COEX,
POI-SOC-WA-USER, POI-SOC-DC-USER, POI-MD-001, POI-IBKR-001, POI-INVPRO-001,
POI-NEWS-001, POI-AI-001, POI-EMERG-001 (UI part). ADR-0013 risk acceptance
still stands for the two social user accounts. The binding gate rule above
governs their eventual real activation.

## Approval

| Role  | Name  | Date       | Decision |
|-------|-------|------------|----------|
| Owner | Eyal  | 2026-07-08 | ACCEPTED |
