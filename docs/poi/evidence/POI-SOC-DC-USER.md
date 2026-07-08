# POI-SOC-DC-USER — Discord personal user account

**Status: BLOCKED — risk accepted, PoI still required** (ADR-0013,
2026-07-08). The owner explicitly approved working with the personal Discord
user account despite the documented ToS violation / account-termination
risk; the decision gate is resolved, but the mandatory experiment (the
owner's real session) has not run and nothing here substitutes for it. This
is NOT a PASS and does not claim the connection works.

Conditions of the acceptance (binding):

- No bypassing of platform security mechanisms, permissions, rate limits or
  controls; no cookie theft; no malware/spyware techniques; read-only access
  to servers/channels the account is legitimately a member of.
- No plaintext token/cookie/session anywhere — session material only via the
  IMP-0303 RuntimeSessionStore; static secrets only via IMP-0302.
- All ingestion goes through a `DiscordSourceAdapter` port so a future
  migration to compliant paths (official Bot API, channel Webhooks, manual
  forwarding) is an adapter swap, not a rewrite. No automatic fallback.

| Question (binding clarification 2026-07-07) | Answer |
|---|---|
| Real user account tested? | **No.** Requires the owner's Discord user account/session and owner-allowlisted servers/channels the user is already a member of. |
| Fixture/mock tested? | **No.** No Discord fixtures were created; nothing was simulated. |
| Bot used? | **No, and never as substitute.** A Discord Bot does not satisfy this PoI and a bot-only result may not be marked PASS (binding clarification). |
| Connector type | `discord-user` — separate connector type; no automatic fallback to a bot connector exists or may be added. |

## Terms-of-service and security risks (must be acknowledged before running)

- **Automating a Discord user account ("self-bot") is explicitly prohibited
  by Discord's Terms of Service** and risks account termination. This is a
  known, documented conflict between the product requirement and the
  platform's rules. Per the binding rules: no solution is invented, no
  silent workaround is applied. Before this PoI executes, the owner must
  decide — with this risk stated — whether to proceed, and if user-session
  integration proves unreliable or unacceptable, the PoI ends FAIL/BLOCKED
  with evidence and an ADR for an approved alternative (an ADR-approved bot
  deviation would be marked as a deviation from the original requirement,
  never a quiet substitution).
- Session/token material only inside the IMP-0303 `RuntimeSessionStore`;
  secrets only via IMP-0302; read-only ingestion; no messages sent and no
  remote mutation during the PoI.

## What the PoI must measure when unblocked

Authorization/session lifecycle, reconnect and resume (gateway session
resume vs. re-identify), expiry/revocation, reading servers/channels/threads
the user is a member of (subject to platform permissions), edits/deletes,
attachments, duplicate delivery and stable IDs, and rate-limit behavior.

## ARM64

A user-session client is JS/TS (Node) — expected to run on linux/arm64;
unverified until the PoI runs on the target host.
