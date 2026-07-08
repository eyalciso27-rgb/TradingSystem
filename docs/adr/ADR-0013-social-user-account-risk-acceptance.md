# ADR-0013: Accept Discord/WhatsApp personal user-account integration risk

## Status

ACCEPTED (owner decision, 2026-07-08)

## Context

The baseline makes personal user-account access mandatory for the social
sources (CLAUDE.md non-negotiables; binding clarification 2026-07-07): bots
and business APIs are separate connector types and never substitutes.
POI-SOC-DC-USER and POI-SOC-WA-USER were BLOCKED awaiting an explicit owner
risk decision, because:

- **Discord**: automating a user account (self-bot / user-token automation)
  is explicitly prohibited by Discord's Terms of Service and risks account
  termination. No official or sanctioned user-account API exists.
- **WhatsApp**: no official API exists for personal accounts; every option
  (unofficial multi-device clients, WhatsApp-Web automation, hosted gateways
  such as Green API) automates a personal account against WhatsApp's terms
  and carries account-restriction/ban risk.

## Decision

The owner **temporarily accepts** these risks and approves proceeding with
the personal Discord user account and the personal WhatsApp user account,
because personal-account ingestion is part of the original product
requirement.

**Binding architectural obligation attached to this acceptance**: all social
ingestion must go through per-source adapter ports (`DiscordSourceAdapter`,
`WhatsAppSourceAdapter` — the ports/interfaces rule of CLAUDE.md), so that a
future migration to compliant/approved paths requires swapping an adapter
implementation, not a rewrite. Futures that must remain reachable behind the
same port:

- Discord: official Bot API, channel Webhooks, manual forwarding.
- WhatsApp: WhatsApp Business API, Green API / an approved hosted provider,
  manual forwarding.

Hard security constraints (unchanged, restated as conditions of acceptance):
no bypassing platform security mechanisms, permissions, rate limits or
controls; no cookie theft; no malware/spyware techniques; the account reads
only servers/groups it is legitimately a member of; read-only ingestion; no
plaintext storage of tokens, QR material, cookies or sessions — session
material lives only in the IMP-0303 encrypted RuntimeSessionStore and static
secrets only in IMP-0302.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Discord official Bot only | Does not satisfy the mandatory personal-user requirement; many source servers will not admit a bot. Kept as a future migration target behind the port. |
| WhatsApp Business/Cloud API only | Cannot read the owner's personal groups; violates the product requirement. Kept as a future migration target. |
| Manual forwarding only | Loses timeliness/completeness; kept as fallback-by-decision (never automatic). |
| Drop the sources | Rejected by the owner; they are core product inputs. |

## Consequences

- POI-SOC-DC-USER / POI-SOC-WA-USER move from "awaiting owner decision" to
  **risk accepted — PoI still required**; they stay BLOCKED until the real
  experiments run and are never marked PASS by this ADR.
- This ADR is a *risk acceptance*, not an "approved alternate" that satisfies
  the PHASE-5 exit gate — the gate still requires the PoIs to PASS.
- Account termination/lockout remains a live operational risk; a ban during
  operation must surface as AUTH_REQUIRED/DOWN health, never silent data loss.

## Security impact

Attack surface grows (unofficial clients handle live account credentials).
Mitigations: IMP-0302/0303 storage, per-connector isolation and revocation,
read-only method allowlists (as in the Telegram PoI harness), redaction.

## Operational impact

Possible sudden account bans → runbook RB-07 (auth expired) applies; health
states must reflect platform enforcement actions.

## Data impact

None (no schema change; connector types already separate).

## Rollback plan

Revoke the connector session (per-connector, IMP-0303), disable the source
profile, and switch the port implementation to a compliant alternative
(bot/webhook/business API/manual) under a follow-up ADR marking the
deviation from the personal-account requirement.

## ARM64 impact

Unchanged — client choices are decided by the PoIs and must be validated on
the ARM64 host per the PoI matrix.

## Paper/Live impact

None on isolation; social ingestion remains read-only input. Live trading
remains prohibited.

## Approval

| Role  | Name  | Date       | Decision |
|-------|-------|------------|----------|
| Owner | Eyal  | 2026-07-08 | ACCEPTED (temporary, conditions above) |
