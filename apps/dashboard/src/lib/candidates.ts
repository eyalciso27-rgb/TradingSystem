/**
 * Candidate-workspace model + fixtures (IMP-1103).
 *
 * Scope (task): evidence, model cards, fusion, vetoes, plan and freshness.
 * DoD: every decision is explainable from the UI — so every candidate
 * fixture carries the FULL chain (evidence -> per-model cards -> fusion ->
 * vetoes -> proposed plan -> freshness), and `isFullyExplained` enforces it.
 *
 * Read-only review only (ADR-0014): candidates are shown for inspection;
 * approve/submit/auto-trade are disabled and any real action must traverse
 * Decision -> Risk -> Allocation -> Pre-submit -> Authorization -> Execution
 * Agent. Fixtures only: no real network, no source connectors, no secrets,
 * no full account id/handle (provenance handles are masked). A vetoed,
 * stale or blocked candidate is never presented as tradeable.
 */

export type Direction = "LONG" | "SHORT" | "NEUTRAL";

export type CandidateStatus =
  | "WATCH"
  | "APPROVED"
  | "REJECTED"
  | "VETOED"
  | "NEEDS_REVIEW"
  | "STALE";

export interface EvidenceItem {
  readonly ref: string;
  readonly sourceKind: string;
  readonly summary: string;
  readonly observedAt: string;
  readonly availableAt: string;
}

export interface ModelCard {
  readonly model: string;
  readonly family: string;
  readonly assessment: string;
  readonly score: string;
  readonly calibration: string;
  readonly status: string;
  readonly verified: boolean;
}

export interface FusionView {
  readonly fusedScore: string;
  readonly actionTier: string;
  readonly familyWeights: readonly { readonly family: string; readonly weight: string }[];
  readonly contributions: readonly {
    readonly model: string;
    readonly weightedScore: string;
    readonly deduped: boolean;
  }[];
}

export interface VetoView {
  readonly source: string;
  readonly reasonCode: string;
  readonly blocking: boolean;
}

export interface PlanView {
  readonly entryType: string;
  readonly proposedQuantity: string;
  readonly entryPrice: string;
  readonly stopPrice: string;
  readonly targetPrice: string | null;
  readonly riskAmount: string;
  readonly allocationStatus: string;
  readonly preSubmitAction: string;
}

export interface FreshnessView {
  readonly asOf: string;
  readonly marketDataAgeSeconds: number;
  readonly stale: boolean;
  readonly lookaheadGuard: boolean;
}

export interface CandidateIdea {
  readonly candidateId: string;
  readonly symbol: string;
  readonly instrument: string;
  readonly direction: Direction;
  readonly provenance: string;
  readonly confidence: string;
  readonly strategySummary: string;
  readonly riskFlags: readonly string[];
  readonly status: CandidateStatus;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly blockedReasons: readonly string[];
  // -- explainability chain (DoD: every decision explainable from UI) ------
  readonly evidence: readonly EvidenceItem[];
  readonly modelCards: readonly ModelCard[];
  readonly fusion: FusionView;
  readonly vetoes: readonly VetoView[];
  readonly plan: PlanView;
  readonly freshness: FreshnessView;
}

/** Statuses that would mean "tradeable now" — none are, in this read-only shell. */
const POSITIVE_STATUS: ReadonlySet<CandidateStatus> = new Set<CandidateStatus>(["APPROVED"]);

export function isStatusPositive(status: CandidateStatus): boolean {
  return POSITIVE_STATUS.has(status);
}

/**
 * The DoD check: a candidate is explainable only if it carries the whole
 * chain. A vetoed/blocked candidate must STILL be fully explainable (the UI
 * must show *why* it is not tradeable).
 */
export function isFullyExplained(candidate: CandidateIdea): boolean {
  return (
    candidate.evidence.length > 0 &&
    candidate.modelCards.length > 0 &&
    candidate.fusion.contributions.length > 0 &&
    candidate.plan.preSubmitAction.length > 0 &&
    candidate.freshness.asOf.length > 0
  );
}

const AS_OF = "2026-07-10T17:00:00.000Z";

export const CANDIDATE_IDEAS: readonly CandidateIdea[] = [
  {
    candidateId: "cand-aapl-001",
    symbol: "AAPL",
    instrument: "AAPL / SMART / USD",
    direction: "LONG",
    provenance: "tg:@e***l → technical+news",
    confidence: "0.71",
    strategySummary: "Breakout continuation above prior day high with supportive news tone.",
    riskFlags: ["EARNINGS_IN_9D"],
    status: "WATCH",
    createdAt: "2026-07-10T16:45:00.000Z",
    updatedAt: AS_OF,
    blockedReasons: [],
    evidence: [
      {
        ref: "observation:a-1@tg-user",
        sourceKind: "TELEGRAM_USER",
        summary: "Owner-forwarded idea: long above 232.",
        observedAt: "2026-07-10T16:40:00.000Z",
        availableAt: "2026-07-10T16:41:00.000Z",
      },
      {
        ref: "observation:n-7@news",
        sourceKind: "NEWS",
        summary: "Positive supplier-guidance filing.",
        observedAt: "2026-07-10T16:30:00.000Z",
        availableAt: "2026-07-10T16:35:00.000Z",
      },
    ],
    modelCards: [
      {
        model: "technical-v1",
        family: "TECHNICAL",
        assessment: "Breakout with rising ADV.",
        score: "0.68",
        calibration: "0.80",
        status: "SATISFIED",
        verified: true,
      },
      {
        model: "news-nlp-v1",
        family: "NEWS",
        assessment: "Net-positive tone, official filing.",
        score: "0.62",
        calibration: "0.74",
        status: "SATISFIED",
        verified: true,
      },
    ],
    fusion: {
      fusedScore: "0.71",
      actionTier: "ADVISORY",
      familyWeights: [
        { family: "TECHNICAL", weight: "0.55" },
        { family: "NEWS", weight: "0.45" },
      ],
      contributions: [
        { model: "technical-v1", weightedScore: "0.374", deduped: false },
        { model: "news-nlp-v1", weightedScore: "0.279", deduped: false },
      ],
    },
    vetoes: [],
    plan: {
      entryType: "BUY_STOP_TRIGGER",
      proposedQuantity: "40",
      entryPrice: "232.10",
      stopPrice: "228.00",
      targetPrice: "244.00",
      riskAmount: "164.00",
      allocationStatus: "ALLOW",
      preSubmitAction: "PENDING_REVIEW",
    },
    freshness: {
      asOf: AS_OF,
      marketDataAgeSeconds: 12,
      stale: false,
      lookaheadGuard: true,
    },
  },
  {
    candidateId: "cand-tsla-002",
    symbol: "TSLA",
    instrument: "TSLA / SMART / USD",
    direction: "SHORT",
    provenance: "dc:e***l#0000 → sentiment",
    confidence: "0.40",
    strategySummary: "Short on momentum fade; contradicted by an active thesis veto.",
    riskFlags: ["HIGH_VOLATILITY"],
    status: "VETOED",
    createdAt: "2026-07-10T16:20:00.000Z",
    updatedAt: AS_OF,
    blockedReasons: ["THESIS_INVALIDATED"],
    evidence: [
      {
        ref: "observation:d-3@discord-user",
        sourceKind: "DISCORD_USER",
        summary: "Owner note: momentum fading.",
        observedAt: "2026-07-10T16:15:00.000Z",
        availableAt: "2026-07-10T16:16:00.000Z",
      },
    ],
    modelCards: [
      {
        model: "regime-v1",
        family: "REGIME",
        assessment: "HIGH_VOLATILITY regime; shorts restricted.",
        score: "0.30",
        calibration: "0.60",
        status: "VETO",
        verified: true,
      },
    ],
    fusion: {
      fusedScore: "0.40",
      actionTier: "REJECT",
      familyWeights: [{ family: "REGIME", weight: "1.00" }],
      contributions: [{ model: "regime-v1", weightedScore: "0.300", deduped: false }],
    },
    vetoes: [{ source: "regime-v1", reasonCode: "REGIME_SHORT_RESTRICTED", blocking: true }],
    plan: {
      entryType: "NONE",
      proposedQuantity: "0",
      entryPrice: "—",
      stopPrice: "—",
      targetPrice: null,
      riskAmount: "0",
      allocationStatus: "REJECT",
      preSubmitAction: "CANCEL",
    },
    freshness: {
      asOf: AS_OF,
      marketDataAgeSeconds: 20,
      stale: false,
      lookaheadGuard: true,
    },
  },
  {
    candidateId: "cand-msft-003",
    symbol: "MSFT",
    instrument: "MSFT / SMART / USD",
    direction: "LONG",
    provenance: "ip:e***l → fundamental",
    confidence: "0.55",
    strategySummary: "Value re-rating; awaiting fresh quote before any evaluation.",
    riskFlags: [],
    status: "STALE",
    createdAt: "2026-07-10T15:00:00.000Z",
    updatedAt: AS_OF,
    blockedReasons: ["MARKET_DATA_STALE"],
    evidence: [
      {
        ref: "observation:f-2@investingpro",
        sourceKind: "INVESTINGPRO",
        summary: "Fundamental screen hit.",
        observedAt: "2026-07-10T14:55:00.000Z",
        availableAt: "2026-07-10T14:57:00.000Z",
      },
    ],
    modelCards: [
      {
        model: "fundamental-v1",
        family: "FUNDAMENTAL",
        assessment: "Below fair-value band.",
        score: "0.58",
        calibration: "0.70",
        status: "SATISFIED",
        verified: true,
      },
    ],
    fusion: {
      fusedScore: "0.55",
      actionTier: "WATCH",
      familyWeights: [{ family: "FUNDAMENTAL", weight: "1.00" }],
      contributions: [{ model: "fundamental-v1", weightedScore: "0.580", deduped: false }],
    },
    vetoes: [],
    plan: {
      entryType: "BUY_LIMIT_MAX",
      proposedQuantity: "0",
      entryPrice: "—",
      stopPrice: "—",
      targetPrice: null,
      riskAmount: "0",
      allocationStatus: "MANUAL",
      preSubmitAction: "NEEDS_REVIEW",
    },
    freshness: {
      asOf: AS_OF,
      marketDataAgeSeconds: 7200,
      stale: true,
      lookaheadGuard: true,
    },
  },
];

/** Read-only workspace: these actions require the full pipeline; disabled. */
export const CANDIDATE_ACTIONS: readonly {
  readonly action: string;
  readonly enabled: false;
  readonly reason: string;
}[] = [
  { action: "APPROVE", enabled: false, reason: "requires Decision→Risk→Pre-submit; not wired" },
  { action: "SUBMIT", enabled: false, reason: "requires Authorization + Execution Agent" },
  { action: "AUTO_TRADE", enabled: false, reason: "PAPER_AUTO is disabled" },
];

export function candidateById(candidateId: string): CandidateIdea | undefined {
  return CANDIDATE_IDEAS.find((candidate) => candidate.candidateId === candidateId);
}
