# deploy/

Docker Compose stacks (IMP-0401, ADR-009: Docker Compose on Oracle Linux
ARM64; local development stays portable, including Windows x64).

| File | Stack | Ports | Volumes |
|---|---|---|---|
| `compose.yaml` | base + ephemeral test/CI | none published | none (ephemeral) |
| `compose.dev.yaml` | development override | 127.0.0.1 only (5432, 4222, 8222) | `pg-data-dev`, `nats-data-dev`, `universe-data-dev` |
| `compose.paper.yaml` | PAPER override | none published | `pg-data-paper`, `nats-data-paper`, `universe-data-paper` |
| `compose.observability.yaml` | metrics/logs stack (ADR-010) | Grafana on 127.0.0.1:3000 only | `prometheus-data`, `alertmanager-data`, `loki-data`, `alloy-data`, `grafana-data` |

Services: `postgres` (postgres:18, ADR-006) and `nats` (nats:2.10-alpine with
JetStream, ADR-007) — both official multi-arch images (amd64 + arm64), both
healthchecked. The remaining application services (api/worker/connectors/
execution-agent/dashboard) join once their canonical Dockerfiles exist.

### `scheduler` — the automatic monthly universe refresh

`scheduler` (image: [`Dockerfile.scheduler`](Dockerfile.scheduler)) runs
`python -m trading_scheduler`: startup maintenance on boot, then a tick every
`TRADING_SCHEDULER_TICK_HOURS` (default 6) for the life of the container. The
tick is cheap — it compares timestamps and reaches the free public source only
when the ~30-day interval has genuinely elapsed. **This process is what makes
the refresh automatic.** The `sp500-universe` CLI remains a diagnostic and
manual override; it is not how the monthly refresh normally happens.

It sits behind the `apps` Compose profile, so an ephemeral test/CI `up` still
starts infrastructure only. Paper and Live must start it explicitly:

```sh
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
               --profile apps up -d
```

**The free source has not yet been exercised against the real endpoint from
this repository's CI, which is offline by design.** To verify it on the owner
host — the only place it can honestly be verified — run the one-shot CLI, which
reaches the real public source and reports what it found:

```sh
uv run python -m trading_market_data_core.sp500_universe_cli status
```

Add `refresh` (optionally `--force`) to actually fetch and store a snapshot. A
failed or implausible fetch never replaces the last-known-good list.

Operational notes: it is stopped with `SIGTERM`, which it handles by finishing
the current tick and exiting cleanly within `stop_grace_period`; a failing
refresh is logged and retried on a backoff, never fatal (a source that is down
for a week must not stop the refresh forever); a process that was offline when
the refresh came due catches up on its next boot, because overdue is simply
due; and the accepted snapshot history lives on a per-environment volume
(`universe-data-dev` / `universe-data-paper`) so it survives restarts and is
never shared between environments. Running the scheduler does not authorize
Paper Auto or Live trading.

The observability stack (IMP-0402) adds `prometheus`, `alertmanager`, `loki`
(single binary), `alloy` (log collection — not Promtail), `grafana`, plus
`postgres-exporter` and `nats-exporter` for health monitoring of the
existing services; config lives under [observability/](observability/).
All images are multi-arch (amd64 + arm64), all services healthchecked, and
nothing is internet-exposed: Grafana binds to 127.0.0.1:3000, everything
else stays on the internal `trading-observability` network. Grafana admin
credentials are required at runtime (no defaults). Operational logs
(Loki), metrics (Prometheus) and traces (OpenTelemetry) stay separate from
the tamper-evident audit trail in PostgreSQL, which they never replace.

```sh
# development
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up -d
# paper (credentials from the PAPER secret store, never shared with dev)
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml up -d
# with observability (mandatory from Paper Manual onward)
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
               -f deploy/compose.observability.yaml up -d
```

Rules baked in: no credentials or defaults in the files (`:?` fails closed);
PAPER uses its own `PAPER_*` variables, network (`trading-paper`) and volumes,
sharing nothing with dev/test; PostgreSQL is never exposed beyond localhost
(dev) or the internal network (paper/test); LIVE remains disabled — no live
stack exists by design.
