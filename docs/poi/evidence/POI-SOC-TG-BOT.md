# POI-SOC-TG-BOT — Telegram Bot API supplemental mode

**Status: BLOCKED** (needs a dedicated bot token; decision and golden
fixtures are done per the IMP-0502 DoD)

| Field | Value |
|---|---|
| Date | 2026-07-07 |
| Environment | local dev (fixtures drill under pytest) |
| Command | `uv run pytest tests/poi/test_poi_tg_bot.py -q` |
| Result | 5 passed (fixtures: [tests/fixtures/telegram_bot/](../../../tests/fixtures/telegram_bot)) |

## Decision

- Bot API is **supplemental only** (baseline invariant): it never replaces
  the mandatory TDLib user path and never satisfies POI-SOC-TG-USER.
- The notification bot token is **separate** from any ingestion bot token
  (Technology spec 32.3); both are provisioned via IMP-0302 only.
- Transport: webhook with secret header **or** long polling — decided at
  deployment; webhook requires TLS + allowed-updates restriction + rate
  limiting (Security spec 33.2).
- Dedup key: `telegram:<canonical chat id>:<message id>` (Data Model 31.3);
  an edit reuses the same canonical identity and is a revision, not a new
  event — verified against the golden fixtures.
- A bot message is source content only; BUY/SELL/HALT/FLATTEN in chat is
  never a command (baseline invariant; enforced again at the notification
  service).

## Permission matrix (from authority docs)

| Capability | Allowed |
|---|---|
| Read groups/channels where the bot is authorized | yes (privacy mode per group policy) |
| Send private alerts to the defined owner chat | yes (notification token) |
| Replace the Telegram user (TDLib) path | never |
| Execute/relay trading commands | never |

## Blocking requirements

A dedicated bot token (BotFather) provisioned via IMP-0302, an authorized
test group/channel, and a defined private owner chat for the test alert.
