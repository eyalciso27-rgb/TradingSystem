/**
 * Approval-queue model + fixtures (IMP-1104).
 *
 * Scope (task): approve / reject / watch / reanalyse / reduce-quantity with
 * expiry. DoD: an expired or materially changed plan requires refresh —
 * `requiresRefresh` gates every plan-affirming action fail-closed.
 *
 * SAFETY: an APPROVE here records a human review decision as an audit
 * fixture and NOTHING else. It never creates a SubmitAuthorization, never
 * sends an order, never calls the Execution Agent or a broker. The only
 * path to a submission remains Decision → Risk → Allocation → Final
 * Pre-submit (which alone mints the one-time SubmitAuthorization) →
 * Execution Agent. Fixtures only: no network, no connectors, no secrets.
 */

export type ApprovalAction =
  | "APPROVE"
  | "REJECT"
  | "WATCH"
  | "REANALYSE"
  | "REDUCE_QUANTITY"
  | "REFRESH";

export type ApprovalStatus =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "WATCH"
  | "REANALYSE_REQUESTED"
  | "REFRESH_REQUIRED";

export interface PlanSnapshot {
  readonly entryType: string;
  readonly quantity: string;
  readonly entryPrice: string;
  readonly stopPrice: string;
  readonly targetPrice: string | null;
  readonly riskAmount: string;
}

/** Deterministic FNV-1a hash of the plan snapshot (material-change guard). */
export function planHash(snapshot: PlanSnapshot): string {
  const text = JSON.stringify(snapshot);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `fnv1a:${hash.toString(16).padStart(8, "0")}`;
}

export interface ApprovalItem {
  readonly approvalId: string;
  readonly candidateId: string;
  readonly symbol: string;
  readonly direction: "LONG" | "SHORT";
  readonly plan: PlanSnapshot;
  /** Hash of the plan the reviewer is looking at (frozen at issue time). */
  readonly reviewedPlanHash: string;
  /** Hash of the CURRENT plan (recomputed upstream); differs on change. */
  readonly currentPlanHash: string;
  readonly issuedAt: string;
  readonly expiresAt: string;
  readonly status: ApprovalStatus;
}

export interface ApprovalAuditRecord {
  readonly approvalId: string;
  readonly action: ApprovalAction;
  readonly operator: string;
  readonly at: string;
  readonly detail: string;
  readonly resultingStatus: ApprovalStatus;
  /** Always true in this task: the record is the ONLY output of an action. */
  readonly fixtureOnly: true;
}

export type ActionResult =
  | { readonly ok: true; readonly item: ApprovalItem; readonly record: ApprovalAuditRecord }
  | { readonly ok: false; readonly reason: string };

export function isExpired(item: ApprovalItem, nowIso: string): boolean {
  return nowIso >= item.expiresAt;
}

export function isMateriallyChanged(item: ApprovalItem): boolean {
  return item.reviewedPlanHash !== item.currentPlanHash;
}

/** DoD gate: expired or materially changed ⇒ the plan must be refreshed. */
export function requiresRefresh(item: ApprovalItem, nowIso: string): boolean {
  return isExpired(item, nowIso) || isMateriallyChanged(item);
}

/** Actions that affirm/alter the plan; blocked fail-closed while stale. */
const PLAN_AFFIRMING_ACTIONS: ReadonlySet<ApprovalAction> = new Set<ApprovalAction>([
  "APPROVE",
  "REDUCE_QUANTITY",
  "REANALYSE",
]);

function record(
  item: ApprovalItem,
  action: ApprovalAction,
  operator: string,
  nowIso: string,
  detail: string,
  resultingStatus: ApprovalStatus,
): ApprovalAuditRecord {
  return {
    approvalId: item.approvalId,
    action,
    operator,
    at: nowIso,
    detail,
    resultingStatus,
    fixtureOnly: true,
  };
}

/**
 * Apply a review action. Pure: returns the updated item + the audit record
 * (the record is the whole effect — nothing is submitted anywhere).
 */
export function applyAction(
  item: ApprovalItem,
  action: Exclude<ApprovalAction, "REFRESH">,
  operator: string,
  nowIso: string,
  options?: { readonly reduceTo?: string },
): ActionResult {
  if (item.status !== "PENDING" && item.status !== "WATCH") {
    return { ok: false, reason: `item is terminal (${item.status}); no further actions` };
  }
  if (PLAN_AFFIRMING_ACTIONS.has(action) && requiresRefresh(item, nowIso)) {
    const cause = isExpired(item, nowIso) ? "EXPIRED" : "PLAN_MATERIALLY_CHANGED";
    return {
      ok: false,
      reason: `REFRESH_REQUIRED (${cause}): ${action} is blocked until the plan is refreshed`,
    };
  }
  switch (action) {
    case "APPROVE": {
      const updated: ApprovalItem = { ...item, status: "APPROVED" };
      return {
        ok: true,
        item: updated,
        record: record(
          item,
          action,
          operator,
          nowIso,
          "human review recorded; NO SubmitAuthorization created, nothing sent",
          "APPROVED",
        ),
      };
    }
    case "REJECT": {
      const updated: ApprovalItem = { ...item, status: "REJECTED" };
      return {
        ok: true,
        item: updated,
        record: record(item, action, operator, nowIso, "rejected by reviewer", "REJECTED"),
      };
    }
    case "WATCH": {
      const updated: ApprovalItem = { ...item, status: "WATCH" };
      return {
        ok: true,
        item: updated,
        record: record(item, action, operator, nowIso, "moved to watch", "WATCH"),
      };
    }
    case "REANALYSE": {
      const updated: ApprovalItem = { ...item, status: "REANALYSE_REQUESTED" };
      return {
        ok: true,
        item: updated,
        record: record(
          item,
          action,
          operator,
          nowIso,
          "re-analysis requested (recorded only)",
          "REANALYSE_REQUESTED",
        ),
      };
    }
    case "REDUCE_QUANTITY": {
      const reduceTo = options?.reduceTo ?? "";
      const requested = Number(reduceTo);
      const proposed = Number(item.plan.quantity);
      if (!Number.isFinite(requested) || requested <= 0) {
        return { ok: false, reason: "reduce quantity must be a positive number" };
      }
      if (requested > proposed) {
        return {
          ok: false,
          reason: `reduce quantity may only go DOWN (requested ${reduceTo} > proposed ${item.plan.quantity})`,
        };
      }
      const plan: PlanSnapshot = { ...item.plan, quantity: reduceTo };
      const updated: ApprovalItem = {
        ...item,
        plan,
        // The reviewer changed the plan themselves: hashes re-align to the
        // reduced plan they are looking at.
        reviewedPlanHash: planHash(plan),
        currentPlanHash: planHash(plan),
      };
      return {
        ok: true,
        item: updated,
        record: record(
          item,
          action,
          operator,
          nowIso,
          `quantity reduced ${item.plan.quantity} -> ${reduceTo} (recorded only)`,
          updated.status,
        ),
      };
    }
  }
}

/** Refresh re-issues the item against the CURRENT plan with a new expiry. */
export function refreshItem(
  item: ApprovalItem,
  currentPlan: PlanSnapshot,
  nowIso: string,
  ttlSeconds: number,
  operator: string,
): { readonly item: ApprovalItem; readonly record: ApprovalAuditRecord } {
  const hash = planHash(currentPlan);
  const expires = new Date(Date.parse(nowIso) + ttlSeconds * 1000).toISOString();
  const updated: ApprovalItem = {
    ...item,
    plan: currentPlan,
    reviewedPlanHash: hash,
    currentPlanHash: hash,
    issuedAt: nowIso,
    expiresAt: expires,
    status: "PENDING",
  };
  return {
    item: updated,
    record: record(item, "REFRESH", operator, nowIso, "plan refreshed for review", "PENDING"),
  };
}

// -- deterministic fixtures ---------------------------------------------------

const NOW_FIXTURE = "2026-07-10T17:00:00.000Z";

const FRESH_PLAN: PlanSnapshot = {
  entryType: "BUY_STOP_TRIGGER",
  quantity: "40",
  entryPrice: "232.10",
  stopPrice: "228.00",
  targetPrice: "244.00",
  riskAmount: "164.00",
};

const CHANGED_PLAN_OLD: PlanSnapshot = {
  entryType: "BUY_LIMIT_MAX",
  quantity: "25",
  entryPrice: "410.00",
  stopPrice: "402.00",
  targetPrice: null,
  riskAmount: "200.00",
};

const CHANGED_PLAN_NEW: PlanSnapshot = { ...CHANGED_PLAN_OLD, stopPrice: "405.00" };

export const APPROVAL_FIXTURE_NOW = NOW_FIXTURE;

export const APPROVAL_QUEUE: readonly ApprovalItem[] = [
  {
    approvalId: "appr-001",
    candidateId: "cand-aapl-001",
    symbol: "AAPL",
    direction: "LONG",
    plan: FRESH_PLAN,
    reviewedPlanHash: planHash(FRESH_PLAN),
    currentPlanHash: planHash(FRESH_PLAN),
    issuedAt: "2026-07-10T16:50:00.000Z",
    expiresAt: "2026-07-10T17:20:00.000Z", // fresh at fixture-now
    status: "PENDING",
  },
  {
    approvalId: "appr-002",
    candidateId: "cand-msft-003",
    symbol: "MSFT",
    direction: "LONG",
    plan: CHANGED_PLAN_OLD,
    reviewedPlanHash: planHash(CHANGED_PLAN_OLD),
    currentPlanHash: planHash(CHANGED_PLAN_NEW), // materially changed upstream
    issuedAt: "2026-07-10T16:40:00.000Z",
    expiresAt: "2026-07-10T17:40:00.000Z",
    status: "PENDING",
  },
  {
    approvalId: "appr-003",
    candidateId: "cand-tsla-002",
    symbol: "TSLA",
    direction: "SHORT",
    plan: FRESH_PLAN,
    reviewedPlanHash: planHash(FRESH_PLAN),
    currentPlanHash: planHash(FRESH_PLAN),
    issuedAt: "2026-07-10T15:00:00.000Z",
    expiresAt: "2026-07-10T16:00:00.000Z", // expired at fixture-now
    status: "PENDING",
  },
];

export const APPROVAL_TTL_SECONDS = 1800;
