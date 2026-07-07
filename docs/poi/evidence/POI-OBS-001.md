# POI-OBS-001 — End-to-end telemetry and alerting

**Status: BLOCKED** (the telemetry chain is proven locally end-to-end across
component boundaries; the live Grafana/Loki/Alloy stack and a real Telegram
private alert remain to run on a Docker host)

| Field | Value |
|---|---|
| Date | 2026-07-07 |
| Environment | local dev (real components: structured logging, timeline, notification service) |
| Platform | Windows-11-10.0.26200 x64; Python 3.13.12 |
| Command | `uv run pytest tests/poi/test_poi_obs_001.py -q` |
| Result | 2 passed (drill: [test_poi_obs_001.py](../../../tests/poi/test_poi_obs_001.py)) |

## Expected vs. actual

| Check | Result |
|---|---|
| One correlation_id flows source→candidate→…→decision across log records | PASS |
| Timeline reconstructs the chain from event/causation ids | PASS |
| Secrets redacted across the whole stream (`[REDACTED]`) | PASS |
| Critical alert on the same correlation: dedup (1 delivery for 2 raises) | PASS |
| Unacknowledged critical alert escalates after deadline (re-delivery) | PASS |
| Simulated adapter-down (AUTH_REQUIRED) produces a correlated critical alert | PASS |

## Decision / fixtures

Dashboards remain Grafana Explore over the provisioned Prometheus/Loki
datasources (IMP-0402); alert rules live in
`deploy/observability/alert-rules.yml`. The drill file doubles as the fixture
set for the telemetry contract (log shape, timeline linkage, alert dedup).

## Blocking requirements

Docker host to run the observability stack live (Alloy→Loki ingestion,
Prometheus scrapes, Grafana health) and a Telegram notification token
(separate from ingestion) for the private test alert.
