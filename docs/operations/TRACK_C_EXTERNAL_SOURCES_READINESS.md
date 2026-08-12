# Track-C external sources — readiness audit

One combined, **no-login** readiness audit for every external-source PoI. It
does **not** perform any real account login, build any provider integration, or
mark any source CONNECTED. It records exactly what is offline-complete, what is
owner-gated, and the single owner-action matrix (Part E).

## 0. Shared layer (applies to ALL sources) — what is offline-complete

The canonical, provider-agnostic layer that every adapter must use is
**implemented and comprehensively tested**:

- **Source taxonomy:** `trading_contracts.enums.SourceType`
  (WHATSAPP, DISCORD, TELEGRAM, INVESTINGPRO, NEWS, FILING, MARKET) — no parallel
  schema is permitted (harness fails closed on a non-canonical type).
- **Health states:** `trading_contracts.enums.IntegrationHealthStatus`
  (INITIALIZING, HEALTHY, DEGRADED, AUTH_REQUIRED, RATE_LIMITED, CIRCUIT_OPEN,
  PAUSED, DOWN). There is deliberately **no "CONNECTED"** — `HEALTHY` is the
  only "working" state and requires positive runtime evidence.
- **Canonical normalization + provenance + dedup:** `trading_contracts.social`
  (`CanonicalSocialMessage`, `SocialObservation`, `compute_social_message_dedup_key`,
  access-mode/provider legality). Proven by `tests/contracts/test_social.py`
  (31 tests: dedup stability, cross-mode collapse, edit→revision, delete→tombstone,
  attachment dedup-invariance, wrong-key rejection, **credential fields absent
  from every contract**, JSON-schema/TS parity).
- **Pipeline integration:** `trading_application.e2e_shadow` +
  `tests/application/test_e2e_shadow_scenarios.py` prove, per-source and in one
  **combined Track-C acceptance run**: every source normalized with provenance;
  an unavailable source is **DEGRADED, never CONNECTED, never a PoI pass**, and
  blocks nothing; cross-mode observations collapse to one candidate; conflicting
  duplicates fail closed; reconnect-replay never duplicates a candidate; no
  source can reach an execution surface; identity is deterministic.
- **Secret storage:** `trading_config.secrets.FileSecretStore`
  (`$SECRETS_ROOT/PAPER/...`, masked, no value ever logged) exists and is used by
  the read-only broker probe. It is the wiring an adapter will reuse; it is not
  yet wired to any source adapter (none exists).

**No production source adapter exists.** `apps/`/`packages/` contain the
contract, the JSON schemas and the dashboard sources UI — but no WhatsApp /
Discord / Telegram / TDLib / TradingView / News / InvestingPro runtime adapter.
Building one requires the owner's real account, credentials and platform
authorization (ToS), so it **cannot be completed offline** beyond the shared
layer above.

## 1. Source-by-source status (the 12 questions)

Legend: ✅ done · ⬜ not implemented (owner-gated) · ➖ shared layer ready, no
per-source producer yet.

| # Question | WA user | DC user | TG user (TDLib) | TG bot | TG coex | InvestingPro | News | TradingView | Ext. market-data |
|---|---|---|---|---|---|---|---|---|---|
| PoI id | POI-SOC-WA-USER (P0) | POI-SOC-DC-USER (P0) | POI-SOC-TG-USER (P0) | POI-SOC-TG-BOT (P1) | POI-SOC-TG-COEX (P0) | *(SourceType only)* | POI-NEWS-001 | POI-TV-001 | POI-MD-001 |
| 1 Production adapter | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ (no provider selected) |
| 2 Auth/session | ⬜ | ⬜ | ⬜ (phone/QR/2FA, encrypted TDLib DB) | ⬜ (bot token) | ⬜ | ⬜ | ⬜ | ⬜ (signed alert verify) | ⬜ |
| 3 Re-auth | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 4 Secret storage wired | ➖ (FileSecretStore ready) | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 5 Health/DEGRADED/AUTH_REQUIRED/RATE_LIMITED | ➖ (enum ready, no producer) | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 6 Reconnect/resume | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 7 Normalization/provenance → canonical | ✅ (contract) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 8 Dedup | ✅ (canonical key) | ✅ | ✅ | ✅ | ✅ (cross-mode collapse proven) | ✅ | ✅ | ✅ | ✅ |
| 9 Rate-limit/backoff/circuit-breaker | ➖ (enum states ready) | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ | ➖ |
| 10 Owner-run PoI procedure + evidence schema | steps in `POI_BACKLOG.yaml` | ✎ | ✎ | ✎ | ✎ | ✎ | ✎ | ✎ | evidence PASS for realtime sub-gate (`POI-MD-001-REALTIME.md`) |
| 11 Exact owner action still required | pair user session on owner's WhatsApp | authorize user session | api_id/api_hash + phone/QR/2FA + host decision | create dedicated bot + token | run both modes on one post | InvestingPro account | news provider access | TradingView signed-alert source | **owner ADR to select a provider** |
| 12 Anything else completable offline now | shared layer only | shared layer only | + `POI-PLAT-ARM-001` host decision (arm64/x86) | permission matrix draft | depends on user+bot | — | — | signed-alert verify design | none without an ADR |

Notes:
- **Telegram user (TDLib) is the mandatory, primary path;** the Telegram **bot**
  is supplemental and must never substitute for it (POI-SOC-TG-BOT `fail:`
  "coupling to user requirement"). The coexistence PoI proves one canonical
  candidate from both modes — already proven at the contract + pipeline level;
  the real evidence needs both live modes.
- **External market-data provider:** no canonical provider is selected; per
  ADR-0016 the realtime dependency for POI-IBKR-001 is satisfied by the IBKR
  realtime sub-gate. Introducing a new provider requires an owner decision/ADR —
  out of scope here.
- **InvestingPro** is a canonical `SourceType` but has no dedicated PoI record in
  the frozen backlog; treated as a source-type the shared layer supports, pending
  an owner PoI if pursued.

## 2. Offline gaps implemented in this package

The shared invariants the external-source PoIs will map their evidence onto are
now proven **simultaneously** for all source types by
`tests/application/test_e2e_shadow_scenarios.py`
(`test_trackc_all_sources_combined_shadow_acceptance`,
`test_trackc_reconnect_replay_never_duplicates_a_candidate`). No production
adapter was created (it would be speculative and owner-credential/ToS-gated).

## 3. Part E — owner-action matrix

| Source / system | Current status | Offline ready? | Exact owner action required | Expected evidence | Parallel? |
|---|---|---|---|---|---|
| WhatsApp user | shared layer ✅; adapter ⬜ | contract/dedup ready | pair the owner's WhatsApp **user** session; allowlist groups | stable ids/dedup, reconnect, AUTH_REQUIRED state, no broker access | ✅ |
| Discord user | shared layer ✅; adapter ⬜ | ready | authorize the owner's Discord **user** session; allowlist servers/channels | normalized edits/deletes/threads, resume/dedup | ✅ |
| Telegram user (TDLib) | shared layer ✅; adapter ⬜ | ready | api_id/api_hash + phone/QR/2FA login; **host decision** (arm64/x86, POI-PLAT-ARM-001); encrypted TDLib DB | all events, restart with encrypted session, re-auth/revoke | ✅ (after host decision) |
| Telegram bot | shared layer ✅; adapter ⬜ | ready | create a dedicated bot; isolate token | supplemental capture + private test alert; **never** replaces user mode | ✅ |
| Telegram coexistence | depends on user+bot | contract proven | run the **same** post through both live modes | one canonical candidate, both observations retained | after user+bot |
| InvestingPro | shared layer ✅; no PoI record | ready | owner decision to pursue + account | (define PoI if pursued) | ✅ |
| News | shared layer ✅; adapter ⬜ | ready | news provider access | official negative-catalyst normalized | ✅ |
| TradingView | shared layer ✅; adapter ⬜ | ready | signed-alert source + verification key | signed alert verified and re-checked | ✅ |
| External market-data | provider undecided | blocked on ADR | **owner ADR** to select a provider | provider benchmark, outage/fallback plan | ❌ (needs ADR first) |
| IBKR Paper (context) | POI-IBKR-001 BLOCKED | offline done | run the deferred IBKR-Paper lifecycle | see `PHASE_13_DEFERRED_OWNER_RUNS.yaml` | ✅ |

No real external login or broker-changing action was performed. Gateway/systemd/
IBC untouched; PAPER_AUTO disabled; watchdog timer disabled; LIVE disabled.
