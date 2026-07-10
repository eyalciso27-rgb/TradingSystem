/**
 * Dashboard status model + fixtures (IMP-1101).
 *
 * Scope (task): mode, health, P&L, risk, positions, alerts, halt banner.
 * Everything rendered by the shell comes from this typed model; in this
 * task the provider is FIXTURES ONLY (ADR-0014) — the real feed arrives
 * with the API wiring tasks. Account identifiers are masked at the source
 * so no full account id can ever reach the screen or a log line.
 */

export type SystemMode = "RESEARCH" | "PAPER_MANUAL" | "HALTED";

export interface ComponentHealth {
  readonly component: string;
  readonly healthy: boolean;
  readonly detail: string;
}

export interface PnlSummary {
  /** Decimal strings — the UI never does float math on money. */
  readonly realizedToday: string;
  readonly unrealized: string;
  readonly currency: string;
}

export interface RiskSummary {
  readonly openRiskAmount: string;
  readonly openRiskEnvelopeUsedPct: string;
  readonly concurrentPositions: number;
  readonly maxConcurrentPositions: number;
}

export interface PositionRow {
  readonly symbol: string;
  readonly quantity: string;
  readonly averagePrice: string;
  readonly protectedQuantity: string;
  readonly accountAliasMasked: string;
}

export interface AlertRow {
  readonly alertId: string;
  readonly severity: "INFO" | "WARNING" | "CRITICAL";
  readonly message: string;
  readonly raisedAt: string;
}

export interface HaltBannerState {
  readonly active: boolean;
  readonly scope: string;
  readonly reasonCode: string;
}

export interface DashboardStatus {
  readonly asOf: string;
  readonly mode: SystemMode;
  readonly environment: "PAPER";
  readonly health: readonly ComponentHealth[];
  readonly pnl: PnlSummary;
  readonly risk: RiskSummary;
  readonly positions: readonly PositionRow[];
  readonly alerts: readonly AlertRow[];
  readonly halt: HaltBannerState;
}

/** `DU1234567` -> `DU*****67`; the full id never reaches the UI. */
export function maskAccountId(accountId: string): string {
  if (accountId.length <= 4) {
    return `${accountId.slice(0, 2)}**`;
  }
  return `${accountId.slice(0, 2)}${"*".repeat(accountId.length - 4)}${accountId.slice(-2)}`;
}

/**
 * Dangerous operations are DISABLED in the shell by policy: every one of
 * them requires the full pipeline (Risk -> Final Pre-submit -> one-time
 * SubmitAuthorization -> Execution Agent) or the emergency runbook, and
 * none of that is wired to the console in this task. The UI renders them
 * as visibly disabled with this registry as the single source of truth.
 */
export const DANGEROUS_ACTIONS: readonly {
  readonly action: string;
  readonly enabled: false;
  readonly reason: string;
}[] = [
  { action: "SUBMIT_ORDER", enabled: false, reason: "requires full pipeline; not wired" },
  { action: "CANCEL_ORDER", enabled: false, reason: "requires execution agent; not wired" },
  { action: "FLATTEN", enabled: false, reason: "emergency runbook only; manual" },
  { action: "ACTIVATE_KILL_SWITCH", enabled: false, reason: "emergency runbook only; manual" },
  { action: "RESUME_TRADING", enabled: false, reason: "MFA + reconciliation gate; manual" },
];

/** Deterministic PAPER-observation fixture the SSE stream serves. */
export function fixtureStatus(tick: number): DashboardStatus {
  const base = Date.UTC(2026, 6, 10, 17, 0, 0);
  return {
    asOf: new Date(base + tick * 1000).toISOString(),
    mode: "PAPER_MANUAL",
    environment: "PAPER",
    health: [
      { component: "event-bus", healthy: true, detail: "fixture" },
      { component: "market-data", healthy: true, detail: "fixture" },
      { component: "broker-gateway", healthy: tick % 7 !== 6, detail: "fixture" },
      { component: "execution-agent", healthy: true, detail: "fixture" },
    ],
    pnl: { realizedToday: "0.00", unrealized: "125.50", currency: "USD" },
    risk: {
      openRiskAmount: "150.00",
      openRiskEnvelopeUsedPct: "30.0",
      concurrentPositions: 1,
      maxConcurrentPositions: 4,
    },
    positions: [
      {
        symbol: "MSFT",
        quantity: "10",
        averagePrice: "400.00",
        protectedQuantity: "10",
        accountAliasMasked: maskAccountId("DU1234567"),
      },
    ],
    alerts: [
      {
        alertId: `alert-${tick}`,
        severity: "INFO",
        message: "fixture stream tick",
        raisedAt: new Date(base + tick * 1000).toISOString(),
      },
    ],
    halt: { active: false, scope: "", reasonCode: "" },
  };
}
