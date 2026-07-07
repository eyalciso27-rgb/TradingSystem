# POI-SOC-TG-USER — Telegram personal user account (TDLib)

**Status: BLOCKED** — the mandatory primary path (the owner's Telegram user
account via TDLib) has not run. The Bot API work (POI-SOC-TG-BOT) is
supplemental and does not count toward this PoI.

| Question (binding clarification 2026-07-07) | Answer |
|---|---|
| Real user account tested? | **No.** Requires `api_id`/`api_hash` (my.telegram.org) provisioned via IMP-0302 plus the owner's account for phone/QR/2FA login. |
| Fixture/mock tested? | **No fixtures for the user path.** (The Bot API fixtures under `tests/fixtures/telegram_bot/` belong to the *supplemental* POI-SOC-TG-BOT only and satisfy nothing here.) |
| Bot used? | **No.** Bot API is optional/supplemental; it is a separate connector type and may never be presented as passing this PoI. |
| Connector type | `telegram-user-tdlib` — separate from `telegram-bot`; no automatic fallback between them. |

## Terms-of-service and security notes

- TDLib/MTProto with user credentials is Telegram's **officially supported**
  path for user-account clients — no ToS conflict, subject to Telegram's
  flood/rate limits. Aggressive polling or mass joining is out of scope;
  read-only ingestion of chats/channels the user already belongs to.
- The TDLib database must live encrypted (TDLib `database_encryption_key`)
  with the key and session blob managed only through IMP-0303; `api_id`/
  `api_hash` only through IMP-0302. Per-connector revocation must not touch
  any other connector; PAPER/LIVE separated.
- No messages are sent and nothing remote is modified during the PoI.

## What the PoI must measure when unblocked

Login (phone/QR/2FA), listing + allowlisting chats/channels/topics, capture
of messages/posts/edits/deletes/replies/forwards, restart with the encrypted
TDLib DB, re-auth and revoke flows, FLOOD_WAIT/rate-limit handling, and gap
recovery after downtime.

## ARM64

TDLib builds on linux/arm64 (C++; documented builds exist) but this must be
proven on the target host as part of the PoI; if not viable, the canonical
fallback is a documented isolated x86 host decision (host ADR) — the user
path itself is never downgraded to Bot API.
