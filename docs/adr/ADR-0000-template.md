# ADR-NNNN: Title

<!--
Copy this template to docs/adr/ADR-NNNN-short-title.md.
Architecture may not be redesigned without an explicit ADR and user approval
(CLAUDE.md, non-negotiable rules). ADR-001..ADR-012 are recorded in
docs/baseline-v0.5/Technology_and_Deployment_Decisions_v0.6_Trading_System_HE.md.
-->

## Status

PROPOSED | ACCEPTED | REJECTED | SUPERSEDED by ADR-NNNN

## Context

What forces are at play? Which baseline documents and constraints apply?

## Decision

The change that is being proposed or has been agreed.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
|             |              |

## Consequences

What becomes easier or harder as a result of this decision?

## Security impact

Effect on secrets, sessions, RBAC, audit, trust/quarantine and attack surface.

## Operational impact

Effect on deployment, monitoring, alerting, backup/restore and runbooks.

## Data impact

Effect on schemas, migrations (Alembic + rollback notes), retention and
point-in-time semantics.

## Rollback plan

How the decision is reverted, including config pointer and data rollback.

## ARM64 impact

Compatibility with the Oracle Linux ARM64 deployment target. State explicitly
whether the change was verified on ARM64 or verification is pending.

## Paper/Live impact

Effect on Paper/Live isolation (accounts, secrets, queues, processes,
allowlists, UI, audit). Live trading remains prohibited by baseline policy.

## Approval

| Role  | Name | Date | Decision |
|-------|------|------|----------|
| Owner |      |      |          |
