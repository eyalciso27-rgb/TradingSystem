# Blocked P0 PoIs — blocking requirements (2026-07-07)

Each item below cannot be executed honestly in the current environment. Per
the binding PoI rules, none is marked PASS from documentation or mocks. All
were assessed on 2026-07-07 on Windows-11 x64 (no Docker, no external
accounts/credentials provisioned). Secrets, when provided, must flow through
IMP-0302 (secret store) / IMP-0303 (session store) only.

## POI-PLAT-ARM-001 — ARM64 Docker stack

- **Blocked on:** a Docker host (local Docker is not installed; the target is
  the Oracle Linux ARM64 server). App images (API/worker/connector/UI) also
  need canonical Dockerfiles, which do not exist yet.
- **Already in place:** infra compose stacks (`deploy/compose*.yaml`, all
  healthchecked, no privileged containers, no published ports beyond dev
  loopback); every pinned infra image verified multi-arch (amd64+arm64) via
  registry manifests (postgres:18, nats:2.10-alpine, prometheus v3.5.0,
  alertmanager v0.28.1, grafana 11.6.3, loki 3.4.3, alloy v1.9.2,
  postgres-exporter v0.17.1, nats-exporter 0.17.3); the CI `arm64-emulated`
  job runs the whole Python suite under QEMU aarch64.
- **To run:** build app images on the ARM host, `docker compose up`, health +
  restart-preserves-volumes drill, resource report.

## POI-SOC-WA-USER / POI-SOC-DC-USER / POI-SOC-TG-USER — personal user accounts

Per the binding clarification (2026-07-07): personal user accounts are the
mandatory primary path; bots/business APIs are never substitutes, user and
bot sessions are separate connector types with no automatic fallback, and a
bot-only result may never be marked PASS. Detailed per-source evidence
(user-account status, ToS limits, session lifecycle, reconnect, rate limits,
ARM64, security risks) lives in dedicated files:
[POI-SOC-WA-USER.md](POI-SOC-WA-USER.md),
[POI-SOC-DC-USER.md](POI-SOC-DC-USER.md),
[POI-SOC-TG-USER.md](POI-SOC-TG-USER.md).

## POI-SOC-TG-COEX — Telegram user/bot coexistence

- **Blocked on:** POI-SOC-TG-USER and POI-SOC-TG-BOT (both blocked on
  credentials). Dedup design groundwork exists (`dedup_key` rules, Data Model
  section 31.3; consumer inbox idempotency), but the parallel-ingest
  experiment needs both live modes.

## POI-MD-001 — Market data provider benchmark

- **Blocked on:** IBKR paper market-data entitlement and at least one
  external provider trial account; benchmarking freshness/completeness
  requires live quotes. No provider may be frozen without this evidence.

## POI-IBKR-001 — IBKR Paper execution

- **Blocked on:** an IBKR paper account + TWS/IB Gateway host. Explicitly an
  isolated PoI per CLAUDE.md (the only context where orders to the paper
  broker are permitted); requires POI-MD-001. No real-money account, ever.

## POI-INVPRO-001 — InvestingPro parser and drift canary

- **Blocked on:** an authorized InvestingPro subscription/login session
  (via IMP-0303) and a browser-automation host (Node/Playwright per the
  Technology spec — connectors package not yet built).

## POI-NEWS-001 — Official filings and news ingestion

- **Blocked on:** provider selection experiment requires live retrieval runs
  (SEC EDGAR fair-access client with declared User-Agent + a news provider
  sample account) and a network-enabled runtime host for repeatable capture;
  deferred with POI-MD-001 to the provider-benchmark window.

## POI-AI-001 — Structured extraction benchmark

- **Blocked on:** API keys for at least two model providers (via IMP-0302)
  and an approved evaluation budget. The golden set and schema-validation
  harness (contracts + validators) exist; adversarial prompts must run
  against real providers to be meaningful — self-reported confidence is
  never a gate.
