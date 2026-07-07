# deploy/

Docker Compose stacks (IMP-0401, ADR-009: Docker Compose on Oracle Linux
ARM64; local development stays portable, including Windows x64).

| File | Stack | Ports | Volumes |
|---|---|---|---|
| `compose.yaml` | base + ephemeral test/CI | none published | none (ephemeral) |
| `compose.dev.yaml` | development override | 127.0.0.1 only (5432, 4222, 8222) | `pg-data-dev`, `nats-data-dev` |
| `compose.paper.yaml` | PAPER override | none published | `pg-data-paper`, `nats-data-paper` |

Services: `postgres` (postgres:18, ADR-006) and `nats` (nats:2.10-alpine with
JetStream, ADR-007) — both official multi-arch images (amd64 + arm64), both
healthchecked. Application services (api/worker/scheduler/connectors/
execution-agent/dashboard) join once their canonical Dockerfiles exist;
`compose.observability.yaml` arrives with IMP-0402.

```sh
# development
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml up -d
# paper (credentials from the PAPER secret store, never shared with dev)
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml up -d
```

Rules baked in: no credentials or defaults in the files (`:?` fails closed);
PAPER uses its own `PAPER_*` variables, network (`trading-paper`) and volumes,
sharing nothing with dev/test; PostgreSQL is never exposed beyond localhost
(dev) or the internal network (paper/test); LIVE remains disabled — no live
stack exists by design.
