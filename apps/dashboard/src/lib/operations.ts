/** Shared, read-only contract for the MVP Operations Console. */
export type Known<T> = T | null;
export type EnvironmentState = "PAPER" | "LIVE_PROHIBITED" | "UNKNOWN";
export type AuthorizationMode = "DENY_ALL" | "PAPER_AUTO_DISABLED" | "PAPER_AUTO_ENABLED" | "UNKNOWN";
export type RuntimeState = "STARTING" | "RECONCILING" | "BLOCKED" | "READY_DENYALL" | "RUNNING" | "STOPPING" | "FAILED" | "UNKNOWN";
export type ConnectionState = "CONNECTED" | "DISCONNECTED" | "UNKNOWN";
export type MarketDataState = "USABLE" | "STALE" | "UNAVAILABLE" | "UNKNOWN";
export type ReconciliationState = "CLEAN" | "BLOCKED" | "NEEDS_RECONCILIATION" | "UNKNOWN";
export type HaltState = "ACTIVE" | "INACTIVE" | "UNKNOWN";
export type HealthState = "OK" | "DEGRADED" | "BLOCKED" | "UNKNOWN" | "FAILED";
export type PositionClassification = "SYSTEM_OWNED" | "EXTERNAL" | "UNKNOWN";
export type DecisionOutcome = "NO_TRADE" | "STRATEGY_REJECTED" | "RISK_REJECTED" | "READINESS_BLOCKED" | "HALT_BLOCKED" | "AUTHORIZATION_DENIED" | "SUBMITTED" | "NEEDS_RECONCILIATION" | "UNKNOWN";

export const EXECUTION_LIFECYCLE = [
  "STRATEGY", "DECISION", "RISK", "READINESS", "AUTHORIZATION",
  "SUBMIT_PREPARED", "BROKER_SUBMIT", "ACKNOWLEDGED", "FILLED",
  "CANCELLED", "RECONCILED", "NEEDS_RECONCILIATION",
] as const;
export type ExecutionStage = (typeof EXECUTION_LIFECYCLE)[number];

export interface RuntimeStatus {
  readonly environment: EnvironmentState;
  readonly liveProhibited: Known<boolean>;
  readonly authorizationMode: AuthorizationMode;
  readonly runtime: RuntimeState;
  readonly ibkr: ConnectionState;
  readonly marketData: MarketDataState;
  readonly marketDataObservedAt: Known<string>;
  readonly marketDataAgeSeconds: Known<number>;
  readonly reconciliation: ReconciliationState;
  readonly reconciledAt: Known<string>;
  readonly halt: HaltState;
  readonly observedAt: string;
}

export interface AccountSummary {
  readonly sessionPnl: Known<string>;
  readonly currency: Known<string>;
  readonly openPositions: Known<number>;
  readonly openOrders: Known<number>;
}

export interface TradingSummary {
  readonly lastStrategyEvaluation: Known<string>;
  readonly lastDecision: Known<string>;
  readonly lastRiskResult: Known<string>;
  readonly lastExecution: Known<string>;
  readonly blockingReason: Known<string>;
}

export interface PositionView {
  readonly positionId: string;
  readonly symbol: Known<string>;
  readonly conId: Known<number>;
  readonly side: Known<"LONG" | "SHORT">;
  readonly quantity: Known<string>;
  readonly averagePrice: Known<string>;
  readonly lastPrice: Known<string>;
  readonly unrealizedPnl: Known<string>;
  readonly strategyAttribution: Known<string>;
  readonly sourceAttribution: Known<string>;
  readonly protectiveStopStatus: Known<"PRESENT" | "MISSING" | "UNKNOWN">;
  readonly protectiveStopPrice: Known<string>;
  readonly classification: PositionClassification;
  readonly observedAt: Known<string>;
}

export interface DecisionView {
  readonly decisionId: string;
  readonly symbol: Known<string>;
  readonly strategyId: Known<string>;
  readonly strategyVersion: Known<string>;
  readonly outcome: DecisionOutcome;
  readonly reasonCodes: readonly string[];
  readonly explanation: Known<string>;
  readonly decidedAt: Known<string>;
}

export interface ExecutionView {
  readonly executionId: string;
  readonly decisionId: Known<string>;
  readonly clientOrderId: Known<string>;
  readonly correlationId: Known<string>;
  readonly strategyId: Known<string>;
  readonly strategyVersion: Known<string>;
  readonly symbol: Known<string>;
  readonly conId: Known<number>;
  readonly side: Known<"BUY" | "SELL">;
  readonly quantity: Known<string>;
  readonly orderType: Known<string>;
  readonly entryPrice: Known<string>;
  readonly protectiveStopPrice: Known<string>;
  readonly riskOutcome: Known<string>;
  readonly authorizationOutcome: Known<string>;
  readonly brokerOrderReferences: readonly string[];
  readonly lifecycleState: ExecutionStage;
  readonly reachedStages: readonly ExecutionStage[];
  readonly timestamps: Readonly<Partial<Record<ExecutionStage, string>>>;
  readonly blockingReason: Known<string>;
  readonly failureReason: Known<string>;
  readonly reconciliationState: ReconciliationState;
}

export interface EmergencyStatus {
  readonly halt: HaltState;
  readonly scope: Known<string>;
  readonly reason: Known<string>;
  readonly issuedAt: Known<string>;
  readonly issuerReference: Known<string>;
  readonly newEntriesBlocked: Known<boolean>;
  readonly exposureReducingActionsAllowed: Known<boolean>;
  readonly reconciliation: ReconciliationState;
  readonly authorizationMode: AuthorizationMode;
}

export interface ComponentHealthView {
  readonly component: string;
  readonly state: HealthState;
  readonly lastCheck: Known<string>;
  readonly lastSuccess: Known<string>;
  readonly details: Known<string>;
}

export interface OperationsSnapshot {
  readonly sourceKind: "MOCK" | "API";
  readonly sourceLabel: string;
  readonly runtime: RuntimeStatus;
  readonly account: AccountSummary;
  readonly trading: TradingSummary;
  readonly positions: readonly PositionView[];
  readonly decisions: readonly DecisionView[];
  readonly executions: readonly ExecutionView[];
  readonly emergency: EmergencyStatus;
  readonly health: readonly ComponentHealthView[];
}

export interface OperationsDataSource {
  readonly kind: "MOCK" | "API";
  getSnapshot(): Promise<OperationsSnapshot>;
}

const AT = "2026-08-31T09:42:00.000Z";

export const MOCK_OPERATIONS_SNAPSHOT: OperationsSnapshot = {
  sourceKind: "MOCK",
  sourceLabel: "DETERMINISTIC DEVELOPMENT FIXTURE — NOT BROKER TRUTH",
  runtime: {
    environment: "PAPER", liveProhibited: true, authorizationMode: "PAPER_AUTO_DISABLED",
    runtime: "BLOCKED", ibkr: "CONNECTED", marketData: "STALE",
    marketDataObservedAt: "2026-08-31T09:40:15.000Z", marketDataAgeSeconds: 105,
    reconciliation: "NEEDS_RECONCILIATION", reconciledAt: "2026-08-31T09:30:00.000Z",
    halt: "ACTIVE", observedAt: AT,
  },
  account: { sessionPnl: null, currency: "USD", openPositions: 2, openOrders: null },
  trading: {
    lastStrategyEvaluation: "2026-08-31T09:41:45.000Z",
    lastDecision: "NO_TRADE · dec-aapl-104",
    lastRiskResult: "RISK_REJECTED · DAILY_LOSS_STATE_UNKNOWN",
    lastExecution: "NEEDS_RECONCILIATION · exec-msft-088",
    blockingReason: "UNKNOWN_POSITION_AND_ACTIVE_HALT",
  },
  positions: [
    {
      positionId: "pos-msft-001", symbol: "MSFT", conId: 272093, side: "LONG", quantity: "10",
      averagePrice: "400.00", lastPrice: "412.55", unrealizedPnl: "125.50",
      strategyAttribution: "mvp-breakout", sourceAttribution: "execution:exec-msft-088",
      protectiveStopStatus: "PRESENT", protectiveStopPrice: "392.00",
      classification: "SYSTEM_OWNED", observedAt: AT,
    },
    {
      positionId: "pos-unknown-002", symbol: "TSLA", conId: 76792991, side: "LONG", quantity: "5",
      averagePrice: null, lastPrice: null, unrealizedPnl: null, strategyAttribution: null,
      sourceAttribution: null, protectiveStopStatus: "UNKNOWN", protectiveStopPrice: null,
      classification: "UNKNOWN", observedAt: "2026-08-31T09:41:58.000Z",
    },
  ],
  decisions: [
    {
      decisionId: "dec-aapl-104", symbol: "AAPL", strategyId: "mvp-breakout", strategyVersion: "1.0.0",
      outcome: "NO_TRADE", reasonCodes: ["ENTRY_SETUP_INCOMPLETE"],
      explanation: "The strategy found no complete entry setup; no risk or execution request was created.",
      decidedAt: "2026-08-31T09:41:45.000Z",
    },
    {
      decisionId: "dec-nvda-103", symbol: "NVDA", strategyId: "mvp-breakout", strategyVersion: "1.0.0",
      outcome: "RISK_REJECTED", reasonCodes: ["DAILY_LOSS_STATE_UNKNOWN"],
      explanation: "Risk could not establish the daily-loss state. UNKNOWN failed closed.",
      decidedAt: "2026-08-31T09:39:12.000Z",
    },
    {
      decisionId: "dec-tsla-102", symbol: "TSLA", strategyId: "mvp-breakout", strategyVersion: "1.0.0",
      outcome: "HALT_BLOCKED", reasonCodes: ["GLOBAL_ENTRY_HALT", "UNKNOWN_POSITION"],
      explanation: "A global entry halt is active and an unattributed broker position requires reconciliation.",
      decidedAt: "2026-08-31T09:37:03.000Z",
    },
  ],
  executions: [
    {
      executionId: "exec-msft-088", decisionId: "dec-msft-088", clientOrderId: "paper-entry-088",
      correlationId: "corr-088", strategyId: "mvp-breakout", strategyVersion: "1.0.0",
      symbol: "MSFT", conId: 272093, side: "BUY", quantity: "10", orderType: "LIMIT",
      entryPrice: "400.00", protectiveStopPrice: "392.00", riskOutcome: "ALLOWED",
      authorizationOutcome: "PAPER_AUTHORIZATION_GRANTED", brokerOrderReferences: ["paper-order-ref-088"],
      lifecycleState: "NEEDS_RECONCILIATION",
      reachedStages: ["STRATEGY", "DECISION", "RISK", "READINESS", "AUTHORIZATION", "SUBMIT_PREPARED", "BROKER_SUBMIT", "ACKNOWLEDGED", "FILLED", "NEEDS_RECONCILIATION"],
      timestamps: { STRATEGY: "2026-08-31T09:10:00.000Z", DECISION: "2026-08-31T09:10:01.000Z", RISK: "2026-08-31T09:10:02.000Z", READINESS: "2026-08-31T09:10:03.000Z", AUTHORIZATION: "2026-08-31T09:10:04.000Z", SUBMIT_PREPARED: "2026-08-31T09:10:05.000Z", BROKER_SUBMIT: "2026-08-31T09:10:06.000Z", ACKNOWLEDGED: "2026-08-31T09:10:07.000Z", FILLED: "2026-08-31T09:10:12.000Z", NEEDS_RECONCILIATION: "2026-08-31T09:40:00.000Z" },
      blockingReason: "BROKER_SNAPSHOT_MISMATCH", failureReason: null,
      reconciliationState: "NEEDS_RECONCILIATION",
    },
    {
      executionId: "exec-nvda-blocked", decisionId: "dec-nvda-103", clientOrderId: null,
      correlationId: "corr-103", strategyId: "mvp-breakout", strategyVersion: "1.0.0",
      symbol: "NVDA", conId: 4815747, side: "BUY", quantity: null, orderType: null,
      entryPrice: null, protectiveStopPrice: null, riskOutcome: "REJECTED",
      authorizationOutcome: "NOT_REQUESTED", brokerOrderReferences: [], lifecycleState: "RISK",
      reachedStages: ["STRATEGY", "DECISION", "RISK"], timestamps: { STRATEGY: "2026-08-31T09:39:10.000Z", DECISION: "2026-08-31T09:39:12.000Z", RISK: "2026-08-31T09:39:13.000Z" },
      blockingReason: "DAILY_LOSS_STATE_UNKNOWN", failureReason: null, reconciliationState: "CLEAN",
    },
  ],
  emergency: {
    halt: "ACTIVE", scope: "NEW_ENTRIES", reason: "UNKNOWN_POSITION_AND_RECONCILIATION_GAP",
    issuedAt: "2026-08-31T09:35:00.000Z", issuerReference: "runtime-policy:halt-031",
    newEntriesBlocked: true, exposureReducingActionsAllowed: true,
    reconciliation: "NEEDS_RECONCILIATION", authorizationMode: "PAPER_AUTO_DISABLED",
  },
  health: [
    { component: "Autonomous runtime", state: "BLOCKED", lastCheck: AT, lastSuccess: "2026-08-31T09:34:58.000Z", details: "Active halt; waiting for reconciliation" },
    { component: "Database", state: "OK", lastCheck: AT, lastSuccess: AT, details: "Read/write health check passed" },
    { component: "Durable audit", state: "OK", lastCheck: AT, lastSuccess: AT, details: "Append verification passed" },
    { component: "Object store", state: "UNKNOWN", lastCheck: null, lastSuccess: null, details: "No current health observation" },
    { component: "IBKR", state: "OK", lastCheck: AT, lastSuccess: AT, details: "Paper gateway connected" },
    { component: "Market data", state: "DEGRADED", lastCheck: AT, lastSuccess: "2026-08-31T09:40:15.000Z", details: "Latest observation is stale" },
    { component: "Reconciliation", state: "BLOCKED", lastCheck: AT, lastSuccess: "2026-08-31T09:30:00.000Z", details: "Unknown TSLA position" },
    { component: "Authorization", state: "OK", lastCheck: AT, lastSuccess: AT, details: "PAPER_AUTO_DISABLED; fail-closed" },
    { component: "Emergency / halt store", state: "OK", lastCheck: AT, lastSuccess: AT, details: "Active halt record is durable" },
  ],
};

export class MockOperationsDataSource implements OperationsDataSource {
  readonly kind = "MOCK" as const;
  async getSnapshot(): Promise<OperationsSnapshot> { return MOCK_OPERATIONS_SNAPSHOT; }
}

export const operationsDataSource: OperationsDataSource = new MockOperationsDataSource();

export function displayValue(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined || value === "") return "UNKNOWN";
  if (typeof value === "boolean") return value ? "YES" : "NO";
  return String(value);
}

export type LoadState<T> = { readonly status: "loading" } | { readonly status: "error"; readonly message: string } | { readonly status: "ready"; readonly data: T };
