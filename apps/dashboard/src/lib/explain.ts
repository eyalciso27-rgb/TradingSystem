/**
 * Human-readable, operator-facing explanations for the console's blocking
 * states. Every "not-good" status the UI can show gets one short plain
 * sentence — so a REFRESH_REQUIRED / RECONCILE_REQUIRED / UNKNOWN / VETOED
 * chip is never a bare code the reader has to decode. Also drives the
 * consistent chip tone (ok / warn / danger / muted / info).
 */

export type ChipTone = "ok" | "warn" | "danger" | "muted" | "info";

export interface StatusMeaning {
  readonly tone: ChipTone;
  /** One short human sentence; empty for plainly-good states. */
  readonly explain: string;
}

const MEANINGS: Record<string, StatusMeaning> = {
  // generic good
  OK: { tone: "ok", explain: "" },
  LIVE: { tone: "ok", explain: "" },
  HEALTHY: { tone: "ok", explain: "" },
  APPROVED: { tone: "ok", explain: "Reviewer approved (recorded only — no order was sent)." },
  // needs attention
  WATCH: { tone: "muted", explain: "Kept on the watch list; no action taken." },
  PENDING: { tone: "info", explain: "Waiting for a reviewer decision." },
  NEEDS_REVIEW: { tone: "warn", explain: "A person must look at this before anything proceeds." },
  REANALYSE_REQUESTED: { tone: "info", explain: "Sent back for re-analysis." },
  STALE: { tone: "warn", explain: "The market data behind this is too old — refresh before trusting it." },
  REFRESH_REQUIRED: {
    tone: "danger",
    explain: "The plan expired or changed since review — refresh it before approving or reducing.",
  },
  RECONCILE_REQUIRED: {
    tone: "danger",
    explain: "The broker state doesn't match ours — reconcile before any new action.",
  },
  UNKNOWN: {
    tone: "danger",
    explain: "State could not be confirmed — treated as blocking until reconciliation resolves it.",
  },
  // blocked / negative
  VETOED: { tone: "danger", explain: "A hard rule blocked this candidate; it cannot be traded." },
  REJECTED: { tone: "muted", explain: "Rejected; no further action." },
  BLOCKED: { tone: "danger", explain: "Blocked by policy until the underlying issue clears." },
  BLOCK_NEW_ENTRIES: { tone: "danger", explain: "New entries are blocked until this is resolved." },
  DISCONNECTED: { tone: "warn", explain: "Live updates are paused — showing the last known state." },
  UNHEALTHY: { tone: "danger", explain: "This component is not healthy." },
};

const DEFAULT: StatusMeaning = { tone: "muted", explain: "" };

export function meaningOf(status: string): StatusMeaning {
  return MEANINGS[status.toUpperCase()] ?? DEFAULT;
}

export function chipClass(status: string): string {
  return `chip chip-${meaningOf(status).tone}`;
}

export function explain(status: string): string {
  return meaningOf(status).explain;
}
