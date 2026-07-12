/**
 * Research / calibration model + fixtures (IMP-1107).
 *
 * Scope (task): funnel, starvation, challengers, backtests, models.
 * DoD: NO UI action auto-promotes a policy or a model — this module
 * deliberately contains no function that changes any model/policy status.
 * Everything is a read-only, deterministic fixture view; promoting a
 * challenger (SHADOW → ACTIVE) or a policy version is an explicit OWNER
 * decision through the model-registry governance flow, never from the UI.
 * The only "promotion" surface here is PROMOTION_ACTIONS: a registry of
 * permanently disabled controls with human-readable reasons.
 */

export interface FunnelStage {
  readonly stage: string;
  readonly label: string;
  readonly count: number;
  readonly windowLabel: string;
}

export interface StarvationIndicator {
  readonly subject: string;
  readonly kind: "SOURCE" | "STAGE";
  readonly minutesSinceLastItem: number;
  readonly thresholdMinutes: number;
  readonly starving: boolean;
  /** Short human explanation an operator can act on. */
  readonly explanation: string;
}

export type ModelStatus = "ACTIVE" | "SHADOW" | "RETIRED";

export interface ChallengerRow {
  readonly family: string;
  readonly championModel: string;
  readonly championVersion: string;
  readonly championCalibration: string;
  readonly challengerModel: string;
  readonly challengerVersion: string;
  readonly challengerCalibration: string;
  readonly challengerStatus: ModelStatus; // always SHADOW here
  readonly calibrationDelta: string; // challenger minus champion
  readonly note: string;
}

export interface BacktestRun {
  readonly runId: string;
  readonly strategy: string;
  readonly policyVersion: string;
  readonly periodStart: string;
  readonly periodEnd: string;
  readonly trades: number;
  readonly hitRate: string;
  readonly profitFactor: string;
  readonly maxDrawdownPct: string;
  readonly deterministicSeed: string;
  readonly status: "COMPLETED" | "RUNNING" | "FAILED";
}

export interface ModelCardSummary {
  readonly model: string;
  readonly family: string;
  readonly version: string;
  readonly status: ModelStatus;
  readonly calibration: string;
  readonly lastEvaluatedAt: string;
  readonly note: string;
}

/**
 * DoD guard: promotion is NOT an operation this screen can perform. Every
 * entry is permanently disabled with the governance reason; there is no
 * enabled variant and no function anywhere in this module that flips a
 * ModelStatus.
 */
export const PROMOTION_ACTIONS: readonly {
  readonly action: string;
  readonly enabled: false;
  readonly reason: string;
}[] = [
  {
    action: "PROMOTE_CHALLENGER",
    enabled: false,
    reason:
      "promotion (SHADOW → ACTIVE) is an explicit owner decision through the" +
      " model registry governance flow — never from the UI",
  },
  {
    action: "PROMOTE_POLICY_VERSION",
    enabled: false,
    reason:
      "policy versions are promoted only via the governed config pipeline" +
      " with owner approval — never from the UI",
  },
  {
    action: "RETIRE_MODEL",
    enabled: false,
    reason: "retiring a model is an owner governance action outside this console",
  },
];

const AS_OF = "2026-07-10T17:00:00.000Z";

export const FUNNEL_STAGES: readonly FunnelStage[] = [
  { stage: "observations", label: "Observations", count: 142, windowLabel: "last 24h" },
  { stage: "assessments", label: "Assessments", count: 57, windowLabel: "last 24h" },
  { stage: "fusion", label: "Fusion results", count: 21, windowLabel: "last 24h" },
  { stage: "decisions", label: "Decisions", count: 9, windowLabel: "last 24h" },
  { stage: "approved", label: "Approved plans", count: 2, windowLabel: "last 24h" },
];

export const STARVATION_INDICATORS: readonly StarvationIndicator[] = [
  {
    subject: "telegram-user",
    kind: "SOURCE",
    minutesSinceLastItem: 12,
    thresholdMinutes: 120,
    starving: false,
    explanation: "Flowing normally — last message 12 minutes ago.",
  },
  {
    subject: "news-feed",
    kind: "SOURCE",
    minutesSinceLastItem: 240,
    thresholdMinutes: 60,
    starving: true,
    explanation:
      "No news items for 4 hours (threshold: 1h). Decisions relying on news" +
      " confirmation are running blind — check the news connector status.",
  },
  {
    subject: "fusion",
    kind: "STAGE",
    minutesSinceLastItem: 30,
    thresholdMinutes: 180,
    starving: false,
    explanation: "Fusion produced a result 30 minutes ago — within the normal window.",
  },
];

export const CHALLENGER_ROWS: readonly ChallengerRow[] = [
  {
    family: "TECHNICAL",
    championModel: "technical-v1",
    championVersion: "1.3.0",
    championCalibration: "0.80",
    challengerModel: "technical-v2",
    challengerVersion: "2.0.0-rc1",
    challengerCalibration: "0.84",
    challengerStatus: "SHADOW",
    calibrationDelta: "+0.04",
    note: "Challenger ahead on calibration; needs a full shadow quarter before review.",
  },
  {
    family: "NEWS",
    championModel: "news-nlp-v1",
    championVersion: "1.1.0",
    championCalibration: "0.74",
    challengerModel: "news-nlp-v2",
    challengerVersion: "2.0.0-beta",
    challengerCalibration: "0.69",
    challengerStatus: "SHADOW",
    calibrationDelta: "-0.05",
    note: "Challenger behind the champion — not a promotion candidate yet.",
  },
];

export const BACKTEST_RUNS: readonly BacktestRun[] = [
  {
    runId: "bt-2026-07-08-01",
    strategy: "breakout-continuation",
    policyVersion: "risk-policy 1.4.0",
    periodStart: "2025-01-02",
    periodEnd: "2026-06-30",
    trades: 184,
    hitRate: "0.52",
    profitFactor: "1.61",
    maxDrawdownPct: "7.8",
    deterministicSeed: "seed:20260708",
    status: "COMPLETED",
  },
  {
    runId: "bt-2026-07-09-02",
    strategy: "news-event-momentum",
    policyVersion: "risk-policy 1.4.0",
    periodStart: "2025-06-01",
    periodEnd: "2026-06-30",
    trades: 96,
    hitRate: "0.47",
    profitFactor: "1.22",
    maxDrawdownPct: "11.3",
    deterministicSeed: "seed:20260709",
    status: "COMPLETED",
  },
  {
    runId: "bt-2026-07-10-03",
    strategy: "breakout-continuation (challenger model)",
    policyVersion: "risk-policy 1.5.0-rc",
    periodStart: "2025-01-02",
    periodEnd: "2026-06-30",
    trades: 0,
    hitRate: "—",
    profitFactor: "—",
    maxDrawdownPct: "—",
    deterministicSeed: "seed:20260710",
    status: "RUNNING",
  },
];

export const MODEL_CARDS: readonly ModelCardSummary[] = [
  {
    model: "technical-v1",
    family: "TECHNICAL",
    version: "1.3.0",
    status: "ACTIVE",
    calibration: "0.80",
    lastEvaluatedAt: AS_OF,
    note: "Champion for breakout family.",
  },
  {
    model: "technical-v2",
    family: "TECHNICAL",
    version: "2.0.0-rc1",
    status: "SHADOW",
    calibration: "0.84",
    lastEvaluatedAt: AS_OF,
    note: "Shadow challenger — output never reaches decisions.",
  },
  {
    model: "news-nlp-v1",
    family: "NEWS",
    version: "1.1.0",
    status: "ACTIVE",
    calibration: "0.74",
    lastEvaluatedAt: AS_OF,
    note: "Champion for news tone.",
  },
  {
    model: "regime-v0",
    family: "REGIME",
    version: "0.9.0",
    status: "RETIRED",
    calibration: "0.55",
    lastEvaluatedAt: "2026-05-01T00:00:00.000Z",
    note: "Superseded by heuristic regime engine.",
  },
];

export const RESEARCH_AS_OF = AS_OF;
