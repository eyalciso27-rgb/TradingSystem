# deploy/

Docker Compose stacks (IMP-0401, ADR-009: Docker Compose on Oracle Linux
ARM64; local development stays portable, including Windows x64).

| File | Stack | Ports | Volumes |
|---|---|---|---|
| `compose.yaml` | base + ephemeral test/CI (infrastructure only) | none published | none (ephemeral) |
| `compose.dev.yaml` | development override | 127.0.0.1 only (5432, 4222, 8222) | `pg-data-dev`, `nats-data-dev`, `universe-data-dev` |
| `compose.paper.yaml` | PAPER override | none published | `pg-data-paper`, `nats-data-paper`, `universe-data-paper` |
| `compose.observability.yaml` | metrics/logs stack (ADR-010) | Grafana on 127.0.0.1:3000 only | `prometheus-data`, `alertmanager-data`, `loki-data`, `alloy-data`, `grafana-data` |

## Starting the system

There is exactly ONE normal startup command per environment. Each one starts
everything that environment needs, including the scheduler — no profile, no
extra flag, nothing to remember:

```sh
# PAPER — the canonical deployment (credentials from the PAPER secret store)
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml up -d
```

```sh
# PAPER with observability (mandatory from Paper Manual onward)
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
               -f deploy/compose.observability.yaml up -d
```

```sh
# development
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up -d
```

The base stack on its own is the ephemeral test/CI stack. It is deliberately
infrastructure-only: no application services, so a CI stack starts nothing that
would reach the network by itself. Application services live in the environment
overrides, which is precisely what makes them start automatically for anyone
running dev or paper the normal way.

Services: `postgres` (postgres:18, ADR-006) and `nats` (nats:2.10-alpine with
JetStream, ADR-007) — both official multi-arch images (amd64 + arm64), both
healthchecked — plus `scheduler` in dev and paper. The remaining application
services (api/worker/connectors/execution-agent/dashboard) join once their
canonical Dockerfiles exist.

### `scheduler` — the automatic monthly universe refresh

`scheduler` (image: [`Dockerfile.scheduler`](Dockerfile.scheduler)) runs
`python -m trading_scheduler`: startup maintenance on boot, then a tick every
`TRADING_SCHEDULER_TICK_HOURS` (default 6) for the life of the container. The
tick is cheap — it compares timestamps and reaches the free public source only
when the ~30-day interval has genuinely elapsed. **This process is what makes
the refresh automatic.** The `sp500-universe` CLI remains a diagnostic and
manual override; it is not how the monthly refresh normally happens.

**No monthly owner action is required.** The scheduler is an ordinary service
of the dev and paper stacks, so the canonical paper startup above starts it;
`restart: unless-stopped` brings it back after a crash, a daemon restart or a
host reboot; and because overdue is simply due, a process that was down when the
refresh came due refreshes on its next boot. `deploy/compose.yaml` alone does
not include it, which keeps the test/CI stack infrastructure-only.

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

### Execution is NOT automatic

The scheduler starts with the stack because a monthly refresh nobody remembers
is the whole point of having one. Execution is the opposite case, and it is
deliberately absent from every compose stack: no service starts the execution
path, nothing places an order at `up`, and the only production authorization
implementation denies. Paper Auto and Live remain prohibited.

The one supported way to exercise the real execution runtime is the owner-host
probe, which resolves a contract, takes a live quote, runs the risk engine, asks
the broker to validate the order with a what-if that places nothing, computes
readiness and records the decision — and then stops.

**Run it with the wrapper.** It is the canonical invocation:

```sh
bash scripts/run_execution_probe_paper.sh AAPL
```

You do not export a broker account id. The paper account is resolved internally
from the canonical secret store the rest of the owner-host runtime already uses,
at `$SECRETS_ROOT/PAPER/ibkr/paper-account-id`, and it is never printed — the
probe reports the configured alias (`paper-poi`), and account identifiers are
redacted out of *all* logging, including the IBKR SDK's own.

The wrapper exists because the probe needs two things that do not otherwise
meet. The IB Gateway is reachable only at `127.0.0.1:4002` — both the adapter
and the market-data session refuse anything else — so the probe runs on the
host; and the paper stack publishes no database port at all. The wrapper opens a
**loopback-only** publish of the paper database for the duration of the run
(`127.0.0.1:${PAPER_PROBE_DB_PORT:-55432}`, never `0.0.0.0`) via
`deploy/compose.paper-probe.yaml`, and closes it again on exit. That override is
never part of normal paper startup.

What it needs from you, all of which the paper stack already needs:

| variable | why |
|---|---|
| `SECRETS_ROOT` | locates the canonical PAPER secret store |
| `PAPER_POSTGRES_USER` / `PAPER_POSTGRES_PASSWORD` | the paper database, as for the stack itself |

Every purely local precondition is validated **before any connection is
opened**, so a missing one costs a single clear message and zero IBKR sessions.

Running the module directly still works and is what the wrapper calls, but it
then requires `DATABASE_URL` to already point at a reachable paper database:

```sh
uv run --no-sync python -m trading_execution_agent probe --mode paper --symbol AAPL
```

It has no submission path. It reports the real state of the owner host,
including answers nobody wants — a missing market-data subscription,
delayed-only ticks, an unavailable quote size — as themselves.

Rules baked in: no credentials or defaults in the files (`:?` fails closed);
PAPER uses its own `PAPER_*` variables, network (`trading-paper`) and volumes,
sharing nothing with dev/test; PostgreSQL is never exposed beyond localhost
(dev) or the internal network (paper/test); LIVE remains disabled — no live
stack exists by design.
