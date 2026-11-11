# Trading System — Claude Code Project Instructions

## Source of truth

The authoritative specification baseline is `docs/baseline-v0.5/`.

Read these documents before planning or changing architecture:

1. `docs/baseline-v0.5/Master_Architecture_Baseline_v0.3_HE.md`
2. `docs/baseline-v0.5/Baseline_Consistency_Review_v0.1_HE.md`
3. `docs/baseline-v0.5/Claude_Code_Implementation_Plan_v0.1_HE.md`
4. `docs/baseline-v0.5/Proof_of_Integration_Backlog_v0.1_HE.md`
5. `docs/baseline-v0.5/Traceability_Matrix_v0.3_HE.md`

For a specific task, read the additional authoritative specification documents referenced by that task.

## Non-negotiable rules

- Do not redesign the architecture without an explicit ADR and user approval.
- Do not begin Live trading.
- Restricted Paper Auto may be implemented and tested only under the owner-gated experimental lane proposed by ADR-0020. Automatic broker submission remains forbidden until G0-G5 are integration-verified, the policy is adopted, and the owner issues a separate exact-scope operational release. Do not enable it by default.
- During the current policy draft, no agent may enable PAPER_AUTO_ENABLED, replace DenyAllExecutionAuthorization, or place automated Paper orders.
- Do not connect to real-money accounts.
- Do not send an order to a broker unless the assigned task explicitly concerns an owner-approved isolated IBKR Paper PoI. A future Paper Auto release is separate from this drafting authorization and is not currently approved.
- WhatsApp personal-user access is mandatory.
- Discord personal-user access is mandatory.
- Telegram personal-user access through TDLib is mandatory and primary.
- Telegram Bot API is supplemental and must not replace the personal-user path.
- Social adapters are read-only for trading input in the MVP.
- Telegram, WhatsApp, or Discord messages must never directly invoke BUY, SELL, HALT, or FLATTEN.
- IBKR is the first broker adapter, not a domain dependency.
- No IBKR-specific identifiers or SDK imports outside the IBKR adapter package.
- Paper and Live must remain isolated in code, configuration, credentials, queues, accounts, UI, and deployment.
- The Risk Engine has absolute veto authority.
- Kill-switch behavior must be deterministic and must not wait for an LLM.
- LLM output is untrusted structured input and must pass schema validation.
- Preserve point-in-time semantics and prevent look-ahead in replay/backtesting.
- Use Transactional Outbox and Consumer Inbox.
- Never commit secrets, sessions, tokens, cookies, API keys, account IDs, or `.env` files.
- Do not use `--dangerously-skip-permissions`.
- Do not commit or push unless the user explicitly approves.

## Required workflow for every task

1. Identify the exact task ID from `CLAUDE_CODE_TASKS.yaml`.
2. Read the task and its referenced specifications.
3. State the intended files to create or modify.
4. State the tests and acceptance criteria.
5. Implement only the assigned task.
6. Run the relevant tests, linting, type checks, and validation.
7. Report:
   - files changed;
   - commands run;
   - test results;
   - deviations or unresolved questions;
   - whether the task meets its Definition of Done.
8. Stop. Do not automatically continue to another task.

## Repository rules

- Use the repository layout defined in the Technology and Deployment specification.
- Prefer small, reviewable changes.
- Keep domain logic deterministic and independent from adapters.
- All external integrations must be behind ports/interfaces.
- All events and external payloads must be schema-versioned.
- All database changes require Alembic migrations and rollback notes.
- All new behavior requires tests.
- Use Docker Compose for the MVP; do not introduce Kubernetes, Kafka, Temporal, Redis, TimescaleDB, MinIO, or GraphQL without an approved ADR.
- Target Oracle Linux ARM64 deployment, while keeping local development portable.

## Current execution state

The current approved activity includes Phase 0, owner-approved isolated Paper Proof-of-Integration work, read-only Shadow, and documentation/implementation preparation for ADR-0020.

Under proposed ADR-0020, Paper Auto is conditionally eligible only **after** G0-G5 evidence, owner adoption, and a separate owner-scoped release. Until that release, all actual autonomous Paper submissions are forbidden; PAPER_AUTO_ENABLED stays false and production authorization remains DenyAll. Live trading is prohibited without exception.
