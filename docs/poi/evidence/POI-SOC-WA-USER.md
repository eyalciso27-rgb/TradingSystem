# POI-SOC-WA-USER — WhatsApp personal user account

**Status: BLOCKED — risk accepted, PoI still required** (ADR-0013,
2026-07-08). The owner explicitly approved working with the personal
WhatsApp user account despite the documented risks: ToS violation, account
ban/lockout, and the operational dependency on QR device pairing. The
decision gate is resolved, but the mandatory experiment (real pairing +
session lifecycle) has not run and nothing here substitutes for it. This is
NOT a PASS and does not claim the connection works.

Conditions of the acceptance (binding):

- No plaintext storage of secrets, QR material, cookies or sessions —
  session material only via the IMP-0303 RuntimeSessionStore; static secrets
  only via IMP-0302.
- No bypassing of platform security mechanisms or rate limits; read-only
  ingestion of groups the account is legitimately a member of.
- All ingestion goes through a `WhatsAppSourceAdapter` port so a future
  migration to compliant paths (WhatsApp Business API, Green API / an
  approved hosted provider, manual forwarding) is an adapter swap, not a
  rewrite. No automatic fallback.

| Question (binding clarification 2026-07-07) | Answer |
|---|---|
| Real user account tested? | **No.** Requires the owner's personal WhatsApp account, device pairing (QR), and explicit owner authorization of allowlisted groups. |
| Fixture/mock tested? | **No.** No WhatsApp fixtures were created; nothing was simulated. |
| Bot / Business API used? | **No, and never as substitute.** WhatsApp Business/Cloud API and bots are explicitly NOT acceptable replacements (baseline + binding clarification). |
| Connector type | `whatsapp-user` — separate connector type; no automatic fallback to any bot/business connector exists or may be added. |

## Terms-of-service and security risks (must be acknowledged before running)

- WhatsApp has **no official API for personal accounts**. Any user-session
  integration relies on an unofficial client (multi-device protocol
  libraries) or browser automation of WhatsApp Web. Both carry a documented
  risk of account restriction/ban and are sensitive to protocol changes —
  this is a stability, maintenance and ToS risk the owner must explicitly
  accept before the PoI executes. If the approach proves non-viable or
  ToS-breaking in practice, the PoI ends FAIL/BLOCKED with evidence and an
  ADR for an approved deviation — never a silent switch to a bot.
- Session material (pairing keys, credentials) must only ever exist inside
  the IMP-0303 `RuntimeSessionStore` (encrypted at rest; PAPER/LIVE
  separated; per-connector revocation). No QR, cookie or credential in
  plaintext, logs or git. Static secrets only via IMP-0302.
- Read-only ingestion is the default: the PoI sends no messages and performs
  no remote mutation.

## What the PoI must measure when unblocked

Session lifecycle (QR/login → persist → restart → reconnect), expiry and
revocation behavior, duplicate delivery / stable message IDs for dedup,
group allowlist enforcement, rate limits / flood behavior, attachment
capture, and gap behavior after downtime.

## ARM64

The candidate clients are Node- or Go-based; both run on linux/arm64, but
this is unverified until the PoI runs on the target host. If only an x86
host proves stable, that becomes an explicit host ADR.
