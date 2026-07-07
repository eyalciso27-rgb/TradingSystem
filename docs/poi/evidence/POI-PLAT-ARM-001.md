# POI-PLAT-ARM-001 — ARM64 Docker stack

**Status: PASS** — executed 2026-07-07 on the real target server (SSH alias
`oracle-server`; no IP/credentials recorded here).

## Environment

| Item | Value |
|---|---|
| OS | Oracle Linux Server 9.7, kernel 6.12.0 (el9uek) |
| Architecture | `aarch64` (`uname -m`), 4 CPU, 22 GiB RAM, 113 GB free disk |
| Docker | 29.5.2; Docker Compose v5.1.4 |
| Python | uv 0.11.27 (aarch64) + CPython 3.13.14, `uv sync --locked` |
| Repo state | commit `1a46f5c`, transferred via git bundle (no credentials on server) |

## What ran (commands sanitized; throwaway PoI credentials only, via env)

1. **Runtime**: `uv sync --locked` resolved every wheel natively (no missing
   aarch64 wheel). Imports of all workspace packages + native deps
   (cryptography 46.0.7 incl. live Fernet round-trip, SQLAlchemy 2.0.51,
   psycopg 3.3.4, pydantic 2.13.4, OpenTelemetry SDK, PyYAML) — OK.
2. **Focused tests on aarch64**: architecture guardrails OK; contract drift
   check OK; `pytest tests/{architecture,contracts,config,application,
   observability,backup,poi} tests/db/test_schema_static.py` → **643 passed**.
3. **Images**: all 9 canonical images pulled as `linux/arm64` — postgres:18,
   nats:2.10-alpine, prometheus v3.5.0, alertmanager v0.28.1, loki 3.4.3,
   alloy v1.9.2, grafana 11.6.3, postgres-exporter v0.17.1,
   nats-exporter 0.17.3.
4. **Compose**: `config -q` passed for base/dev/paper/observability combos.
   Isolated project (`-p poi-dev`): postgres+nats healthy. **Defect found and
   fixed**: postgres:18 image moved its volume mount to
   `/var/lib/postgresql` (was `/var/lib/postgresql/data`) — first start
   failed until compose.dev/paper were corrected; real-runtime finding.
5. **Migrations**: alembic upgrade → `0007_notification_events (head)`;
   `alembic check` clean; `pytest tests/db` against the real PostgreSQL 18.4
   on aarch64 → **72 passed** (incl. upgrade/downgrade/upgrade, outbox/inbox
   idempotency, audit chain, emergency replay, notification dedup).
6. **Restart**: marker row survived full `down` + `up` (volumes preserved).
7. **Observability stack**: all 9 containers healthy (postgres, nats,
   prometheus, alertmanager, loki, alloy, grafana, both exporters). Grafana
   `/api/health` OK; Prometheus active targets all `up` (7 jobs incl. the
   PostgreSQL and NATS exporters). Published ports bound to **127.0.0.1
   only** (3000/4222/5432/8222); no new public port.
8. **PoI image builds** (canonical output `deploy/poi/compose-poi.yaml`):
   `trading-poi-python` (uv + full workspace + guardrails in-image) and
   `trading-poi-node` (Node 24 + corepack pnpm frozen install + typecheck)
   built AND ran natively — "poi-python runtime ok on aarch64",
   "poi-node runtime ok on arm64". No emulation anywhere.
9. **Security**: every container `privileged=false`; secrets were throwaway
   PoI values passed via env only; nothing committed from the server; LIVE
   untouched; pre-existing services (n8n, uptime-kuma) untouched.
10. **Resources**: full stack ≈ 340 MiB RAM total, negligible CPU — well
    inside the 2–4 GB observability budget.
11. **Cleanup**: `down -v` (PoI-namespaced volumes only), PoI images removed,
    repo copy and /tmp artifacts deleted; server left as found.

## Limitations

- `age` is not installed on the host → the **encrypted backup/restore smoke
  stays BLOCKED** on installing `age` (pg_dump 18.4 verified available in
  the postgres container). This does not block this PoI's pass criteria.
- Production app Dockerfiles are still future work (their app tasks); the
  PoI images prove the toolchains, not the final images.
- Host SSH uses a non-post-quantum KEX (client warning) — operational note.
