/**
 * Source-connections model + fixtures (IMP-1102).
 *
 * Scope (task): WhatsApp / Discord / Telegram user, Telegram bot,
 * InvestingPro, news, market data. DoD: capability status is kept SEPARATE
 * from runtime status, and re-auth flows are available.
 *
 * Two independent dimensions (never conflated):
 *  - CAPABILITY: what the architecture PERMITS for the source (static
 *    policy) — e.g. Telegram user is the mandatory primary path (TDLib),
 *    the bot is only supplemental and never substitutes it, Discord/
 *    WhatsApp personal accounts are risk-accepted per ADR-0013.
 *  - RUNTIME: the live connection state. Real external activation is gated
 *    on a PoI PASS (ADR-0014), so a permitted source can still be
 *    BLOCKED_NO_POI at runtime. An unknown/blocked source is NEVER shown as
 *    connected.
 *
 * Fixtures only (ADR-0014): no real connection, no secrets, no full account
 * id — account labels are masked.
 */

export type SourceKind =
  | "TELEGRAM_USER"
  | "TELEGRAM_BOT"
  | "WHATSAPP_USER"
  | "DISCORD_USER"
  | "INVESTINGPRO"
  | "NEWS"
  | "MARKET_DATA";

/** What the architecture permits for the source (static). */
export type CapabilityLevel =
  | "MANDATORY_PRIMARY" // Telegram user via TDLib — mandatory, primary
  | "SUPPLEMENTAL" // Telegram bot — supplemental, never substitutes the user
  | "RISK_ACCEPTED" // Discord/WhatsApp personal accounts (ADR-0013)
  | "REQUIRED"; // InvestingPro / news / market data feeds

/** Live connection state (fixtures here). */
export type RuntimeState =
  | "CONNECTED"
  | "DISCONNECTED"
  | "NEEDS_REAUTH"
  | "BLOCKED_NO_POI" // real activation gated on a PoI PASS (ADR-0014)
  | "NOT_CONFIGURED";

/** Runtime states that mean "usable now". Everything else is non-positive. */
const POSITIVE_RUNTIME: ReadonlySet<RuntimeState> = new Set<RuntimeState>(["CONNECTED"]);

export function isRuntimePositive(state: RuntimeState): boolean {
  return POSITIVE_RUNTIME.has(state);
}

export interface SourceConnection {
  readonly kind: SourceKind;
  readonly displayName: string;
  readonly transport: string;
  readonly capability: CapabilityLevel;
  readonly capabilityNote: string;
  readonly runtime: RuntimeState;
  readonly runtimeDetail: string;
  /** A re-auth flow exists for this credentialed/session source. */
  readonly reauthAvailable: boolean;
  readonly evidenceRef: string;
  /** Masked; a full account id/handle never reaches the UI. */
  readonly accountLabelMasked: string;
}

/**
 * Current project-truth fixture. Every social source is the OWNER's personal
 * account (user-session-primary rule); the Telegram bot is supplemental and
 * is shown as a separate row that never substitutes the user path. All real
 * activations are still BLOCKED on their PoI (ADR-0014).
 */
export const SOURCE_CONNECTIONS: readonly SourceConnection[] = [
  {
    kind: "TELEGRAM_USER",
    displayName: "Telegram (personal user)",
    transport: "TDLib personal-user session",
    capability: "MANDATORY_PRIMARY",
    capabilityNote: "Mandatory primary path; the bot never substitutes it.",
    runtime: "BLOCKED_NO_POI",
    runtimeDetail: "POI-SOC-TG-USER blocked: owner login not performed.",
    reauthAvailable: true,
    evidenceRef: "evidence/POI-SOC-TG-USER.md",
    accountLabelMasked: "tg:@e***l",
  },
  {
    kind: "TELEGRAM_BOT",
    displayName: "Telegram (bot)",
    transport: "Bot API",
    capability: "SUPPLEMENTAL",
    capabilityNote: "Supplemental only; must not replace the personal-user path.",
    runtime: "NOT_CONFIGURED",
    runtimeDetail: "No bot token configured (supplemental).",
    reauthAvailable: true,
    evidenceRef: "evidence/BLOCKED-P0.md",
    accountLabelMasked: "bot:***",
  },
  {
    kind: "WHATSAPP_USER",
    displayName: "WhatsApp (personal user)",
    transport: "Personal account (Green API path, ADR-0013)",
    capability: "RISK_ACCEPTED",
    capabilityNote: "Owner personal account; ToS/ban risk accepted (ADR-0013).",
    runtime: "BLOCKED_NO_POI",
    runtimeDetail: "PoI blocked: owner session not established.",
    reauthAvailable: true,
    evidenceRef: "evidence/BLOCKED-P0.md",
    accountLabelMasked: "wa:+***67",
  },
  {
    kind: "DISCORD_USER",
    displayName: "Discord (personal user)",
    transport: "Personal account (ADR-0013)",
    capability: "RISK_ACCEPTED",
    capabilityNote: "Owner personal account; ToS/ban risk accepted (ADR-0013).",
    runtime: "BLOCKED_NO_POI",
    runtimeDetail: "PoI blocked: owner session not established.",
    reauthAvailable: true,
    evidenceRef: "evidence/BLOCKED-P0.md",
    accountLabelMasked: "dc:e***l#0000",
  },
  {
    kind: "INVESTINGPRO",
    displayName: "InvestingPro",
    transport: "Authorized login + browser automation",
    capability: "REQUIRED",
    capabilityNote: "Field catalog source; authorized login required.",
    runtime: "BLOCKED_NO_POI",
    runtimeDetail: "POI-INVPRO-001 blocked: authorized login + host needed.",
    reauthAvailable: true,
    evidenceRef: "evidence/BLOCKED-P0.md",
    accountLabelMasked: "ip:e***l",
  },
  {
    kind: "NEWS",
    displayName: "News / filings",
    transport: "Provider API keys",
    capability: "REQUIRED",
    capabilityNote: "Official filings + IR mandatory; key-configured, not re-auth.",
    runtime: "BLOCKED_NO_POI",
    runtimeDetail: "POI-NEWS-001 blocked: provider sample accounts needed.",
    reauthAvailable: false,
    evidenceRef: "evidence/BLOCKED-P0.md",
    accountLabelMasked: "news:***",
  },
  {
    kind: "MARKET_DATA",
    displayName: "Market data",
    transport: "Provider / IBKR entitlement",
    capability: "REQUIRED",
    capabilityNote: "Primary market-data provider; entitlement-configured.",
    runtime: "BLOCKED_NO_POI",
    runtimeDetail: "POI-MD-001 blocked: entitlement + provider trial needed.",
    reauthAvailable: false,
    evidenceRef: "evidence/BLOCKED-P0.md",
    accountLabelMasked: "md:***",
  },
];

export function sourceByKind(kind: string): SourceConnection | undefined {
  return SOURCE_CONNECTIONS.find((source) => source.kind === kind);
}
