# POI-TV-001 — TradingView webhook

**Status: BLOCKED** (webhook contract + security behavior proven by a real
spike drill; firing a live alert needs a TradingView account)

| Field | Value |
|---|---|
| Date | 2026-07-07 |
| Environment | local dev (spike receiver under pytest) |
| Platform | Windows-11-10.0.26200 x64; Python 3.13.12 |
| Command | `uv run pytest tests/poi/test_poi_tv_001.py -q` |
| Result | 5 passed (drill: [test_poi_tv_001.py](../../../tests/poi/test_poi_tv_001.py); fixtures: [tests/fixtures/tradingview/](../../../tests/fixtures/tradingview)) |

## Decision (webhook contract)

- Payload is schema-versioned (`v1`) with a strict field set; **no order
  semantics exist in the contract** (`side`/`quantity`/`action`/… are
  rejected outright). An alert can only ever become an *external signal*
  that is revalidated internally against system market data.
- Authentication: shared secret header compared constant-time
  (`hmac.compare_digest`); unauthenticated/wrong-secret calls rejected.
  TLS termination is the deployment edge's responsibility (NPM).
- Idempotency: `alert_id` is the dedup key; duplicates and replays of an
  authenticated request are ignored (single signal).

## Expected vs. actual

| Check | Result |
|---|---|
| Valid alert → external signal only | PASS |
| Unauthenticated / wrong secret → rejected, no signal | PASS |
| Duplicate + replay → ignored, one signal total | PASS |
| Order-semantics payload → rejected (`side`, `quantity`, `action`) | PASS |
| Malformed JSON / missing / extra fields → rejected | PASS |

## Blocking requirements

A TradingView account to configure a real alert against a TLS endpoint, and
the API service (FastAPI) to host the production route. The secret must be
provisioned via IMP-0302 at that point.
