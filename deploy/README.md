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

**First time only**, on a host that has never run the paper stack, create the
paper database credentials once:

```sh
export SECRETS_ROOT=<your canonical secret store>
bash scripts/bootstrap_paper_postgres.sh
```

That writes three files into the store you already use — under
`$SECRETS_ROOT/PAPER/postgres/` — beside the account id that is already there.
It generates a strong password, stores it owner-readable only, and never
displays it. Run it again and it changes nothing: it will not rotate, overwrite
or remove an existing credential, because the PostgreSQL image applies
`POSTGRES_PASSWORD` only to an *empty* data directory, so a regenerated password
would orphan an initialised volume — repairable only with the old password, or
by destroying the data.

**Every run after that**, including in a brand-new SSH session:

```sh
export SECRETS_ROOT=<your canonical secret store>
bash scripts/run_execution_probe_paper.sh AAPL
```

`SECRETS_ROOT` is the only thing you set, and it is a location, not a secret.
You export no credentials at all — not the broker account, not the database
user, not the database password. The wrapper reads them itself from the
canonical store and exports them only into its own process. The paper account
comes from `$SECRETS_ROOT/PAPER/ibkr/paper-account-id` and is never printed —
the probe reports the configured alias (`paper-poi`), and account identifiers
are redacted out of *all* logging, including the IBKR SDK's own.

On a host with no paper database yet, the first run creates it: `compose up`
brings up the container, the isolated `trading-paper` network and the persistent
`pg-data-paper` volume, and PostgreSQL initialises the role and database from
those credentials. Nothing about that path removes a volume; the wrapper runs no
`down`, and no `down -v` anywhere.

If the database already exists and was initialised with *different* credentials,
the authenticated preflight fails and says so, before any IBKR session. That is
a reconciliation the owner makes deliberately — alter the role in the running
database, or point `SECRETS_ROOT` at the store that describes it. Deleting the
volume is not the fix: it holds the durable execution history.

The wrapper exists because the probe needs two things that do not otherwise
meet. The IB Gateway is reachable only at `127.0.0.1:4002` — both the adapter
and the market-data session refuse anything else — so the probe runs on the
host; and the paper stack publishes no database port at all. The wrapper opens a
**loopback-only** publish of the paper database for the duration of the run
(`127.0.0.1:${PAPER_PROBE_DB_PORT:-55432}`, never `0.0.0.0`) via
`deploy/compose.paper-probe.yaml`, and closes it again on exit. That override is
never part of normal paper startup.

What it needs from you:

| variable | why |
|---|---|
| `SECRETS_ROOT` | locates the canonical PAPER secret store. That is all. |

`PAPER_POSTGRES_USER` / `PAPER_POSTGRES_PASSWORD` remain the interface
`compose.paper.yaml` consumes, unchanged; the wrapper is now what supplies them.
Setting them in the environment still works and takes precedence, but that is an
exceptional path — for running against a database the secret store does not
describe — and they must be set together or not at all, since a username from
one source and a password from another were never chosen as a pair.

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
