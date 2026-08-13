# PHASE-6 — External Source Adapters (Implementation Package)

> Branch `feat/phase6-source-adapters`, 2026-08-13. Companion to
> [`COMPONENT_EVIDENCE_MATRIX.yaml`](COMPONENT_EVIDENCE_MATRIX.yaml) and
> [`CLAUDE_CODE_TASKS.yaml`](../baseline-v0.5/CLAUDE_CODE_TASKS.yaml).
>
> **Rule that governs this whole package:** an adapter is marked `IMPLEMENTED`
> only when **real production adapter code** exists and is tested. A green fake
> test is **never** integration evidence — every real-account Proof-of-Integration
> (PoI) below stays **BLOCKED** until the owner runs it with live credentials on
> a controlled host. No PoI is marked PASS/HEALTHY on the strength of fakes.

## 1. What was built

A shared, provider-agnostic framework plus four adapters that each have an
**official or ToS-permitted** access path. Every adapter is a thin mapping from
an **injected provider-client port** to the canonical contracts; no adapter
imports a broker, execution agent, or network client, and none has an
order/submit/cancel/execute surface (read-only for trading input, per CLAUDE.md).

| Module | Purpose |
|---|---|
| `source_adapters/framework.py` | `AdapterError` (fail-closed), `content_hash`, `redact`, `BackoffPolicy` (bounded exponential), `CircuitBreaker`, `RateLimiter` (fixed window, injected clock), `DedupRegistry` (idempotent; conflicting content fails closed), `AdapterHealth` (canonical `IntegrationHealthStatus`) |
| `source_adapters/social_base.py` | Reuses **IMP-0605** (`trading_contracts.social`): `normalize_social_message(...)` → one `CanonicalSocialMessage` + one `SocialMessageObservation`, so a user-mode and a bot-mode capture of the same post share a dedup key |

### Adapters (code IMPLEMENTED, real PoI BLOCKED)

| IMP | Component | Module | Tests | Access path |
|---|---|---|---|---|
| IMP-0603 | telegram-user | `source_adapters/telegram_user.py` | `tests/adapters/test_telegram_user_adapter.py` | **TDLib** personal-user client (official) |
| IMP-0604 | telegram-bot | `source_adapters/telegram_bot.py` | `tests/adapters/test_telegram_bot_adapter.py` | **Bot API** `getUpdates` (official) |
| IMP-0607 | news | `source_adapters/filings.py` | `tests/adapters/test_filings_adapter.py` | **SEC EDGAR** + news provider port (official) |
| IMP-0608 | tradingview | `source_adapters/tradingview.py` | `tests/adapters/test_tradingview_adapter.py` | **Authenticated webhook** (official) |
| IMP-0603/0604/0605 | telegram-coexistence | (shared) | `tests/adapters/test_telegram_coexistence.py` | proves user+bot → one candidate; bot never satisfies the user gate |

Key properties proven with deterministic fakes (see the test files):

- **IMP-0603 (Telegram user / TDLib) — required-primary.** Every pre-`READY`
  authorization state (`WAIT_PHONE` / `WAIT_CODE` / `WAIT_PASSWORD` = 2FA) is
  `AUTH_REQUIRED` and yields nothing; only a `READY` session satisfies the
  mandatory user gate (`satisfies_user_requirement()`), `CLOSED` is `DOWN`.
  Edit → revision bump, delete → tombstone (`is_deleted_at_source=True`),
  allowlist enforced, restart/reconnect replay produces no duplicate (cursor
  only advances), provider failure → `DEGRADED`.
- **IMP-0604 (Telegram bot) — supplemental only.** `SUPPLEMENTAL_ONLY = True`
  and `satisfies_user_requirement()` is hard-wired **False**: the bot can never
  substitute for the mandatory user path (CLAUDE.md). Offset/cursor resume,
  allowlist, edit → revision, dedup, `AUTH_REQUIRED` when unauthorized.
- **IMP-0607 (news / filings).** Canonical **four-timestamp point-in-time**
  model (occurred / observed / available / ingested); look-ahead is refused
  (observed/available before occurred, or ingested before available → fail
  closed). Idempotent per accession; **independent sources of the same event
  share a `cluster_key`** (corroboration → one candidate); an amendment
  (`10-K/A`) is a **correction** revision. Malformed/schema-drift input opens
  the circuit and reports `DEGRADED` — **never a silent default**.
- **IMP-0608 (TradingView).** Authenticated webhook **receiver**: constant-time
  HMAC-SHA256 verification of the exact raw body (missing/invalid/tampered →
  `AUTH_REQUIRED`, nothing normalized); replay/idempotency (identical redelivery
  returns the first signal, conflicting content for one id fails closed); the
  signal is `EXTERNAL`, research-only, **not executable**, no order path; the
  shared secret never appears in a signal, repr, or log.

## 2. What was deliberately NOT built (ToS / no approved path)

Per the package's fail-closed rule, an adapter whose canonical design would
require an **unofficial, unsupported, ToS-problematic, or unapproved** access
path is left `NOT_IMPLEMENTED`. The blocker is documented; **no replacement API
or unofficial client was fabricated.** Building any of these requires an
approved ADR first.

| IMP | Component | Exact blocker | Unblock |
|---|---|---|---|
| IMP-0601 | whatsapp-user | WhatsApp has **no official personal-user ingestion API**. The only paths are unofficial web / reverse-engineered clients that violate WhatsApp ToS and risk a ban of the owner's number. | Owner ADR decision on an approved path (e.g. WhatsApp Business API — a different identity, not the personal user session) before any code. |
| IMP-0602 | discord-user | Automating a **personal Discord user account ("self-bot") is explicitly prohibited** by Discord ToS and risks a permanent ban. The official Bot API is a different identity, not the owner's user session. | Owner ADR decision; the official Bot API path would be a scope change, not the mandated user session. |
| IMP-0606 | investingpro | InvestingPro exposes **no official data API** for this use; ingestion would require **scraping / browser automation** behind an authenticated session, which violates the site ToS and is brittle to layout drift. | Owner ADR decision on an approved / licensed data path. |

## 3. Owner-action matrix — real-account PoI activation

Code for the four buildable adapters is complete; **activation** requires the
owner to supply credentials/hosts and run each PoI. Until then the PoIs are
BLOCKED and the components read `real_integration_status: BLOCKED`,
`evidence_class: BLOCKED`.

| PoI | Adapter | Owner action to activate |
|---|---|---|
| POI-SOC-TG-USER | IMP-0603 | Build TDLib for arm64; supply `api_id`/`api_hash` via the secret store (IMP-0302); complete the interactive login (phone → code → 2FA); capture masked evidence of the encrypted session and allowlisted events. |
| POI-SOC-TG-BOT | IMP-0604 | Provide a bot token and an owner-controlled test chat; run supplemental ingestion; confirm it runs **alongside** the user path and cannot satisfy the user gate. |
| POI-SOC-TG-COEX | coexistence | After both Telegram PoIs pass: post one message, confirm user+bot produce **one** candidate with **no silent fallback** between paths. |
| POI-NEWS-001 | IMP-0607 | Select and authorize a live source (e.g. the SEC EDGAR endpoint and/or a news feed); wire the injected fetch port; run a point-in-time and an independent-source clustering check against real data. |
| POI-TV-001 | IMP-0608 | Configure the shared webhook secret; fire a live TradingView alert to the receiver; confirm signature verification and replay idempotency end-to-end. |
| POI-SOC-WA-USER | IMP-0601 | **BLOCKED — no approved path.** Requires an owner ADR before any adapter is built. |
| POI-SOC-DC-USER | IMP-0602 | **BLOCKED — no approved path.** Requires an owner ADR before any adapter is built. |
| POI-INVPRO-001 | IMP-0606 | **BLOCKED — no approved path.** Requires an owner ADR before any adapter is built. |

## 4. Guardrails held

- No adapter imports a broker/execution/network client; none exposes an order
  surface (asserted per adapter).
- Telegram rule preserved: user/TDLib is primary and gate-satisfying; the bot is
  supplemental and can never silently substitute.
- Social adapters are read-only for trading input.
- No new external market-data provider was created (no unapproved ADR).
- PAPER_AUTO / watchdog / LIVE untouched and remain DISABLED; Gateway/systemd/IBC
  untouched.
