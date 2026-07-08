# PHASE-5 Completion Requirements — Blocked P0 Matrix

Exit gate (DoD of IMP-0501): **"Every P0 item PASS/approved alternate ADR."**
Current: 2/12 P0 PASS (POI-SEC-SESS-002, POI-PLAT-ARM-001 — passed 2026-07-07
on the real Oracle ARM64 host), 10/12 BLOCKED, 0 alternate ADRs.
PHASE-5 is therefore **implementation complete** but the **exit gate is not
satisfied** (`phase5_exit_gate: BLOCKED_EXTERNAL` in `STATUS.yaml`).

Binding rules restated: BLOCKED is never relabeled PASS; an ADR is **not**
created merely because an account/credential/test environment is missing —
only when evidence proves the canonical approach impossible or unacceptable.
User connectors and bot connectors are separate types with **no silent
fallback**. PoIs are read-only: no message sending, no remote mutation, no
bypassing platform permissions/rate limits/controls, and no collecting data
the user's account is not authorized to access. Secrets only via IMP-0302;
sessions only via the IMP-0303 encrypted store — never plaintext.

---

## POI-SOC-WA-USER — WhatsApp personal account (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED — **risk accepted per ADR-0013 (2026-07-08), PoI still required** |
| exact blocking dependency | Owner's personal WhatsApp account + device pairing (QR) in a controlled environment |
| action required from owner | Provide the authorized personal account; perform pairing; approve allowlisted groups. ~~Acknowledge risk~~ **Done — ToS/ban/lockout + QR-pairing risk accepted in ADR-0013; ingestion must stay behind a `WhatsAppSourceAdapter` port (future: Business API / Green API / manual forwarding)** |
| secret/account/environment required | Personal WhatsApp account; pairing device; runtime host (dev, later ARM64); session key via IMP-0303 |
| explicit ToS/risk approval required | **Yes.** No official API exists for personal accounts. Options are (a) unofficial multi-device protocol clients, (b) WhatsApp-Web browser automation, (c) hosted third-party gateways such as **Green API** — all automate a personal account against WhatsApp's terms, all carry account-restriction/ban risk, and a hosted gateway additionally routes message content and the session through a third party's servers (security/privacy exposure). Owner must choose the variant and accept its documented risk; a hosted gateway also requires a data-exposure decision. |
| minimum real test for PASS | Pair real session → persist (IMP-0303) → restart → reconnect → expiry + revocation → read-only ingestion of allowlisted groups → stable IDs/dedup → attachment capture → gap behavior |
| ARM64 validation required | Yes — client must run on linux/arm64 on the target host (or an explicit host ADR) |
| expected evidence artifact | `evidence/POI-SOC-WA-USER.md` updated with run data (date/env/platform/versions/commands/expected/actual/logs refs/limitations) |
| ADR possibly needed | Only if the personal-account path proves non-viable/unacceptable in practice — documented deviation, never a silent switch to Business API/bot |
| downstream tasks blocked | IMP-0601 (WhatsApp user adapter), IMP-0605 (cross-adapter normalization, WA part) |

## POI-SOC-DC-USER — Discord personal account (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED — **risk accepted per ADR-0013 (2026-07-08), PoI still required** |
| exact blocking dependency | Owner's user session on a controlled host (decision gate resolved) |
| action required from owner | ~~Decide~~ **Decided — ADR-0013 accepts the risk**: automating a Discord user account (self-bot — e.g. discord.py-self, discord.js-selfbot-v13, or any user-token automation) is **explicitly prohibited by Discord's ToS** and risks account termination; there is no approved way to automate a user account. Conditions: no security-mechanism bypass, no cookie theft, no malware/spyware, read-only, sessions only via IMP-0303; ingestion must stay behind a `DiscordSourceAdapter` port (future: official Bot / Webhook / manual forwarding) |
| secret/account/environment required | If owner proceeds despite ToS: user session via IMP-0303 (never a plaintext token/cookie); allowlisted servers/channels the user is already a member of |
| explicit ToS/risk approval required | **Yes — mandatory.** Unlike WhatsApp (where third-party gateways like Green API exist, equally unofficial), **Discord has no equivalent**: no official or third-party sanctioned user-account API. The only officially supported programmatic path is the Bot API/OAuth2 — which, per the binding requirement, is NOT a substitute for user mode. |
| minimum real test for PASS | Authorize session → reconnect/resume vs re-identify → expiry/revocation → read-only reading of member servers/channels/threads (within the account's own permissions only) → edits/deletes/attachments → dedup/stable IDs → rate-limit behavior |
| ARM64 validation required | Yes — Node-based client on linux/arm64 (unverified) |
| expected evidence artifact | `evidence/POI-SOC-DC-USER.md` updated with run data, or FAIL/BLOCKED evidence |
| ADR possibly needed | **Likely.** If the owner rules the ToS risk unacceptable, the canonical outcome is an ADR proposing alternatives, each marked as a deviation: (a) official Bot adapter for servers where the owner can add a bot; (b) manual/forward-based ingestion (e.g. owner forwards content to an owned channel); (c) drop Discord as a source. No automatic fallback to (a). |
| downstream tasks blocked | IMP-0602 (Discord user adapter), IMP-0605 (Discord part) |

## POI-SOC-TG-USER — Telegram personal account via TDLib (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | `api_id`/`api_hash` (my.telegram.org) via IMP-0302 + owner login (phone/QR/2FA) + TDLib arm64 host proof |
| action required from owner | Create api_id/api_hash under the owner's account and provision via the secret store; perform the interactive login once on the controlled host |
| secret/account/environment required | api_id/api_hash (IMP-0302); TDLib encrypted DB key + session (IMP-0303); runtime host |
| explicit ToS/risk approval required | Low risk: TDLib/MTProto with user credentials is Telegram's **officially supported** user-client path (subject to flood/rate limits). No ToS conflict; risk sign-off is only operational (flood-wait handling). |
| minimum real test for PASS | Login → list/allowlist chats/channels/topics → read-only capture of messages/posts/edits/deletes/replies/forwards → restart with encrypted TDLib DB → re-auth + revoke → FLOOD_WAIT handling → gap recovery |
| ARM64 validation required | Yes — TDLib (C++) build + run on linux/arm64 target host; x86-host fallback only via explicit host ADR (the user path itself is never downgraded to Bot) |
| expected evidence artifact | `evidence/POI-SOC-TG-USER.md` updated with run data |
| ADR possibly needed | Only for host placement (x86 vs arm64), never for replacing the user path with the bot |
| downstream tasks blocked | IMP-0603 (Telegram user adapter), IMP-0605, POI-SOC-TG-COEX |

## POI-SOC-TG-COEX — Telegram user/bot coexistence (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | POI-SOC-TG-USER + POI-SOC-TG-BOT both runnable |
| action required from owner | Everything required by the two parent PoIs |
| secret/account/environment required | Union of both parents (separate bot token, separate user session) |
| explicit ToS/risk approval required | Inherited from parents |
| minimum real test for PASS | Same post ingested via both modes → one canonical evidence/candidate; edit/delete via one/both; deterministic revisions; provenance retained |
| ARM64 validation required | Inherited |
| expected evidence artifact | New `evidence/POI-SOC-TG-COEX.md` |
| ADR possibly needed | No (dedup algorithm decision is an output, not a deviation) |
| downstream tasks blocked | IMP-0605 (cross-adapter normalization DoD: "Same Telegram post via user/bot creates one candidate") |

## POI-PLAT-ARM-001 — ARM64 Docker stack (P0) — ✅ PASS 2026-07-07

Executed on the real Oracle Linux 9.7 aarch64 host: all 9 canonical images
arm64, full stack healthy, migrations + 72 real-DB tests + 643 focused tests
on aarch64, PoI Python/Node images built natively, restart preserved volumes,
no privileged containers. Evidence: `evidence/POI-PLAT-ARM-001.md`.
Remaining side-note: `age` not installed on the host (encrypted-backup smoke
only); production app Dockerfiles arrive with their app tasks.

## POI-MD-001 — Market data provider benchmark (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | IBKR paper market-data entitlement + an external provider trial account |
| action required from owner | Open/authorize IBKR paper market data; pick and register an external provider trial (decision input: cost/limits) |
| secret/account/environment required | IBKR paper credentials; provider API key via IMP-0302; network runtime host |
| explicit ToS/risk approval required | Provider trial terms only |
| minimum real test for PASS | Live benchmark of quotes/bars/corporate actions/calendar: freshness, completeness, symbol/timestamp validation, outage/fallback drill, cost/limits review. A fixture cannot prove freshness/entitlement/outage behavior. |
| ARM64 validation required | Client libraries on arm64 (usually pure HTTP — low risk, still recorded) |
| expected evidence artifact | Benchmark report + field-ownership matrix + provider ADR (this ADR is a canonical *output* of the PoI, not a deviation) |
| ADR possibly needed | Yes — the provider decision itself is delivered as an ADR |
| downstream tasks blocked | IMP-0702 (market data provider port), IMP-0704 (calendar), POI-IBKR-001, PHASE-8 engines needing real data |

## POI-IBKR-001 — IBKR Paper execution (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | IBKR **paper** account + TWS/IB Gateway host; depends on POI-MD-001 |
| action required from owner | Provide paper account; approve the gateway host (and its physical placement per Tech spec §16) |
| secret/account/environment required | Paper credentials via IMP-0302 (never in code/evidence); gateway host |
| explicit ToS/risk approval required | No (paper only); LIVE stays disabled — this PoI is the sole allowed broker-touching experiment and sends paper orders only |
| minimum real test for PASS | Connect/auth/reconnect → account snapshot → instrument mapping → limit/stop/bracket → partial fill/cancel → disconnect mid-order → unknown-state reconciliation → no duplicate orders; protection follows fills |
| ARM64 validation required | Gateway is x86-oriented; host decision (x86 vs arm64) is an expected host ADR |
| expected evidence artifact | Capability matrix + host ADR + reconciliation fixtures + `evidence/POI-IBKR-001.md` |
| ADR possibly needed | Yes — host ADR is an expected output |
| downstream tasks blocked | IMP-1001..1006 (broker core, IBKR adapter, execution agent, coordinator, protection, reconciliation) |

## POI-INVPRO-001 — InvestingPro parser + drift canary (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | Authorized InvestingPro login + browser-automation host |
| action required from owner | Provide the authorized subscription login (via IMP-0302/0303); approve browser-automation runtime |
| secret/account/environment required | InvestingPro credentials/session; Node+Playwright host |
| explicit ToS/risk approval required | Yes — scraping an authenticated paid service: owner must confirm the account is authorized for this use; drift/blocking risk documented |
| minimum real test for PASS | Real login/session persist → parse 3–5 page types → field catalog/units → golden fixtures captured from real pages → break a selector to prove the canary opens the circuit → restart session. A fixture alone cannot prove login, drift detection or anti-automation behavior. |
| ARM64 validation required | Playwright/Chromium on linux/arm64 (supported; must be proven on host) |
| expected evidence artifact | Field catalog + drift report + fixtures + `evidence/POI-INVPRO-001.md` |
| ADR possibly needed | Only if parsing proves unverifiable/fragile beyond the canary design |
| downstream tasks blocked | IMP-0606 (InvestingPro adapter), PHASE-8 fundamental engine inputs |

## POI-NEWS-001 — Official filings and news (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | News-provider sample account + network runtime host (SEC/EDGAR needs no account, only a compliant User-Agent) |
| action required from owner | Choose/register the news provider trial; approve outbound network access from the runtime host |
| secret/account/environment required | Provider API key via IMP-0302 |
| explicit ToS/risk approval required | Provider trial terms; SEC fair-access rate limits respected |
| minimum real test for PASS | Real SEC/IR retrieval + provider sample → published/observed/available timestamps → duplicate clustering → correction/revision handling. Fixtures cannot prove real-world timestamp semantics. |
| ARM64 validation required | HTTP clients — low risk, recorded |
| expected evidence artifact | Provider decision + timestamp rules + fixtures + `evidence/POI-NEWS-001.md` |
| ADR possibly needed | Yes — provider decision delivered as ADR |
| downstream tasks blocked | IMP-0607 (news/filings adapters), PHASE-8 news assessor |

## POI-AI-001 — Structured extraction benchmark (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED |
| exact blocking dependency | Model-provider API keys via IMP-0302 + an approved evaluation budget |
| action required from owner | Provide provider key(s) (at least two candidates to preserve the provider abstraction) and a budget cap |
| secret/account/environment required | API keys via IMP-0302; network host |
| explicit ToS/risk approval required | Budget approval; no PII/secret content in the golden set |
| minimum real test for PASS | Real calls per task (entity/instruction/catalyst/critic): structured output + schema adherence, malformed-response handling, timeout, rate-limit behavior, retry safety, token/cost reporting, secret redaction in logs, adversarial prompts (must remain subject to IMP-0305 trust/quarantine; AI never gains broker/tool authority). No self-reported-confidence gate. |
| ARM64 validation required | HTTP SDKs — low risk, recorded |
| expected evidence artifact | Benchmark report + provider matrix + prompt/model versions + golden set |
| ADR possibly needed | Yes — per-task provider selection lands in the catalog (currently `per-task-providers: PENDING_POI`) |
| downstream tasks blocked | PHASE-8 AI tasks (IMP-0803/0804 area), Decision Engine gates that require CALIBRATED status |
| provider abstraction | Preserved: `ModelProvider` port frozen in the catalog; no single-provider coupling |

## POI-EMERG-001 — Emergency halt path (P0)

| Field | Value |
|---|---|
| current_status | BLOCKED (in-process drill PASSED locally; full DoD needs UI + cross-process restart) |
| exact blocking dependency | Dashboard/UI halt path (PHASE-11) + PostgreSQL cross-process restart drill (Docker host) |
| action required from owner | None beyond providing the Docker host; UI part arrives with IMP-1106 |
| secret/account/environment required | Docker host with PostgreSQL |
| explicit ToS/risk approval required | No |
| minimum real test for PASS | Halt via UI **and** out-of-band CLI/endpoint → queued order blocked → pre-submit check → service restart preserves halt state (from `emergency_commands` replay) → resume with MFA+reconcile → full audit |
| ARM64 validation required | Covered by POI-PLAT-ARM-001 |
| expected evidence artifact | Runbook + drill evidence updating `evidence/POI-EMERG-001.md` |
| ADR possibly needed | No |
| downstream tasks blocked | IMP-1106 final acceptance; PAPER_AUTO gate (PHASE-13/14) |

---

## Owner action summary (what unblocks the gate)

1. **Infrastructure**: Oracle ARM64 (or interim) Docker host → unblocks PLAT-ARM, OBS, EMERG (cross-process), TG-USER host proof.
2. **Telegram**: api_id/api_hash + one interactive owner login (lowest risk, officially supported) → TG-USER, then TG-COEX with the bot token.
3. **WhatsApp**: written owner acknowledgment of unofficial-client/Green-API risk + pairing session.
4. **Discord**: owner decision on the documented ToS prohibition; if declined → ADR with explicit alternatives (bot-as-deviation / manual forwarding / drop source).
5. **Accounts/keys**: IBKR paper (+ market-data entitlement), external market-data trial, news provider trial, InvestingPro login, AI provider keys + budget.

No ADR is created for any of the above while the blocker is merely a missing
account/credential/host. The Discord ToS conflict is the one item where an
ADR is the *expected* outcome if the owner declines the risk.
