# PHASE-6 — Source Activation Readiness (Track C)

> 2026-08-13. Companion to
> [`PHASE6_SOURCE_ADAPTERS.md`](PHASE6_SOURCE_ADAPTERS.md) and
> [`COMPONENT_EVIDENCE_MATRIX.yaml`](COMPONENT_EVIDENCE_MATRIX.yaml).
>
> Takes the four merged adapters from **code implemented** to **the owner can
> activate without more architecture work** — with **no real login here**. All
> owner secrets are loaded at run time from the environment's `FileSecretStore`
> (IMP-0302); the machinery is proven with deterministic fakes only.

## Binding rules

- **A FAKE probe can never be a PoI PASS.** The evidence verdict is
  `NOT_EVIDENCE` for any fixture/fake client, whatever its health status; only a
  **REAL** client with a **HEALTHY** read-only preflight yields `PASS`.
- **Fail-closed secrets.** A missing required secret raises before any probe
  runs; there is no default and no PAPER↔LIVE fallback.
- **No execution surface.** Every adapter is asserted to expose none of
  `submit / cancel / execute / place_order / flatten / authorize / order_intent
  / submit_authorization / broker / execution_agent / …` — external sources are
  read-only for trading input.
- **Telegram bot never substitutes for the user path** — the probe hard-fails if
  the bot ever reports it satisfies the mandatory user gate.
- **Secrets never enter logs or evidence** — details are redacted and the
  evidence writer refuses to emit a file containing a secret value.

## Framework

`packages/py/application/src/trading_application/source_adapters/activation.py`
- `SecretSpec` — fail-closed secret-key naming (`<domain>/<connector>/<name>`).
- `read_only_preflight(...)` — loads secrets, runs the source's read-only probe,
  returns a canonical `IntegrationHealthStatus`.
- `ActivationEvidence` — sanitized PoI evidence schema + JSON writer with the
  FAKE→`NOT_EVIDENCE` rule and a fingerprint.
- `CursorStore` — durable checkpoint with corruption handling / idempotent restart.
- `assert_no_execution_surface(...)` — the read-only proof.

Per-source probes, secret specs and owner runbooks live in
`activation_sources.py`; the offline owner CLI is `activation_cli.py`
(`list` / `runbook <source>` / `secret-keys <source>` / `check-secrets <source>`).

## Owner-run activation matrix

Each source stays **BLOCKED** (real integration) until the owner runs its PoI on
a controlled host with the real client. Preflight/evidence run on that host with
the real client injected; this offline CLI provides `runbook` and
`check-secrets`.

| Source | Connector | PoI | Required secret keys | HEALTHY when | Verdict rule |
|---|---|---|---|---|---|
| Telegram user/TDLib | `telegram-user-tdlib` | POI-SOC-TG-USER | `social/telegram-user-tdlib/api_id`, `.../api_hash` | TDLib state `READY` | REAL+HEALTHY→PASS |
| Telegram bot | `telegram-bot` | POI-SOC-TG-BOT | `social/telegram-bot/bot_token` | bot authorized (supplemental) | REAL+HEALTHY→PASS; never the user gate |
| News/filings | `news-filings` | POI-NEWS-001 | *(none; `info/news-filings/provider_api_key` optional)* | source reachable | REAL+HEALTHY→PASS |
| TradingView | `tradingview-webhook` | POI-TV-001 | `info/tradingview-webhook/webhook_secret` | a real signed alert verifies (HMAC) | REAL+HEALTHY→PASS |

### Telegram user / TDLib (POI-SOC-TG-USER)
- **States:** api_id/api_hash missing → AUTH_REQUIRED · phone/QR · login code ·
  2FA password · READY · revoked · session corrupted · reconnect · logout ·
  encrypted local TDLib state.
- **Owner steps:** place `api_id`/`api_hash` in the store → `activation_cli
  check-secrets telegram-user` → complete the interactive login (phone → code →
  2FA) on a controlled host → run the host preflight with the REAL TDLib client
  (writes sanitized evidence to `docs/poi/evidence/POI-SOC-TG-USER.json`).
- **PASS:** REAL client · state READY · HEALTHY · verdict PASS · encrypted
  session persisted. **FAIL:** missing secret · stuck at WAIT_* → AUTH_REQUIRED ·
  CLOSED/revoked → DOWN · any FAKE client (NOT_EVIDENCE).
- **Re-auth:** re-run the interactive login. **Logout:** log out in Telegram +
  delete the encrypted TDLib session dir (→ CLOSED). **Rotation:** rotate
  api_id/api_hash, re-run preflight.

### Telegram bot (POI-SOC-TG-BOT) — supplemental only
- **Owner steps:** place `bot_token` → configure an owner-controlled test chat →
  `check-secrets telegram-bot` → host preflight with the REAL Bot-API client.
- **PASS:** REAL · authorized · HEALTHY · runs alongside the user adapter and
  **cannot** satisfy the user gate. **Rotation:** new BotFather token → update the
  store. **Logout:** revoke the token (→ AUTH_REQUIRED).

### News / filings (POI-NEWS-001)
- **Owner steps:** select an approved/public source (e.g. SEC EDGAR) →
  (optional) `provider_api_key` → `check-secrets filings` → host preflight with
  the real source.
- **PASS:** REAL · source reachable · HEALTHY · point-in-time + independent-source
  clustering preserved. **FAIL:** outage → DOWN · schema drift → DEGRADED/circuit.

### TradingView (POI-TV-001) — research/external-signal only
- **Owner steps:** place `webhook_secret` → configure the TradingView alert
  webhook (HMAC-SHA256) → `check-secrets tradingview` → fire a real signed test
  alert → host preflight (writes `docs/poi/evidence/POI-TV-001.json`).
- **PASS:** REAL · a real signed alert verifies (constant-time HMAC) · HEALTHY ·
  replay/idempotency holds · **no order path**. **FAIL:** missing secret ·
  invalid/tampered signature → DEGRADED · no real alert → AUTH_REQUIRED (never
  PASS). **Rotation:** rotate `webhook_secret` + reconfigure the alert.

## Status

All four adapters are **ACTIVATION_READY** (owner can activate without further
architecture). Their real-account PoIs remain **BLOCKED** and their
`real_integration_status` / `evidence_class` stay **BLOCKED** until the owner
runs each PoI. No canonical IMPLEMENTED count changes for activation-readiness
work.
