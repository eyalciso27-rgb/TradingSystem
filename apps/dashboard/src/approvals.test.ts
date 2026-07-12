/**
 * Approval-queue drills (IMP-1104). Node-side only. DoD: expired /
 * materially changed plan requires refresh and blocks plan-affirming
 * actions fail-closed. APPROVE records an audit fixture and nothing else.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  APPROVAL_FIXTURE_NOW,
  APPROVAL_QUEUE,
  type ApprovalItem,
  applyAction,
  isExpired,
  isMateriallyChanged,
  planHash,
  refreshItem,
  requiresRefresh,
} from "./lib/approvals";

const HERE = dirname(fileURLToPath(import.meta.url));
const NOW = APPROVAL_FIXTURE_NOW;

function fresh(): ApprovalItem {
  const item = APPROVAL_QUEUE.find((entry) => entry.approvalId === "appr-001");
  expect(item).toBeDefined();
  return item!;
}

function changed(): ApprovalItem {
  return APPROVAL_QUEUE.find((entry) => entry.approvalId === "appr-002")!;
}

function expired(): ApprovalItem {
  return APPROVAL_QUEUE.find((entry) => entry.approvalId === "appr-003")!;
}

describe("expiry / material change (DoD)", () => {
  it("expired item requires refresh", () => {
    expect(isExpired(expired(), NOW)).toBe(true);
    expect(requiresRefresh(expired(), NOW)).toBe(true);
  });

  it("materially changed plan hash requires refresh", () => {
    expect(isMateriallyChanged(changed())).toBe(true);
    expect(requiresRefresh(changed(), NOW)).toBe(true);
  });

  it("fresh item does not require refresh", () => {
    expect(requiresRefresh(fresh(), NOW)).toBe(false);
  });

  it("approve/reduce/reanalyse on stale items are rejected fail-closed", () => {
    for (const item of [expired(), changed()]) {
      for (const action of ["APPROVE", "REDUCE_QUANTITY", "REANALYSE"] as const) {
        const result = applyAction(item, action, "eyal", NOW, { reduceTo: "1" });
        expect(result.ok).toBe(false);
        if (!result.ok) {
          expect(result.reason).toContain("REFRESH_REQUIRED");
        }
      }
    }
  });

  it("refresh re-issues the item and re-enables actions", () => {
    const stale = expired();
    const refreshed = refreshItem(stale, stale.plan, NOW, 1800, "eyal");
    expect(refreshed.item.status).toBe("PENDING");
    expect(requiresRefresh(refreshed.item, NOW)).toBe(false);
    const approve = applyAction(refreshed.item, "APPROVE", "eyal", NOW);
    expect(approve.ok).toBe(true);
    expect(refreshed.record.action).toBe("REFRESH");
  });
});

describe("review actions record fixtures only", () => {
  it("approve on a fresh item records an audit fixture and nothing else", () => {
    const result = applyAction(fresh(), "APPROVE", "eyal", NOW);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.item.status).toBe("APPROVED");
      expect(result.record.fixtureOnly).toBe(true);
      expect(result.record.detail).toContain("NO SubmitAuthorization");
    }
  });

  it("reject/watch/reanalyse record their statuses", () => {
    expect(applyAction(fresh(), "REJECT", "eyal", NOW)).toMatchObject({
      ok: true,
      record: { resultingStatus: "REJECTED" },
    });
    expect(applyAction(fresh(), "WATCH", "eyal", NOW)).toMatchObject({
      ok: true,
      record: { resultingStatus: "WATCH" },
    });
    expect(applyAction(fresh(), "REANALYSE", "eyal", NOW)).toMatchObject({
      ok: true,
      record: { resultingStatus: "REANALYSE_REQUESTED" },
    });
  });

  it("terminal items accept no further actions", () => {
    const approved = applyAction(fresh(), "APPROVE", "eyal", NOW);
    expect(approved.ok).toBe(true);
    if (approved.ok) {
      const again = applyAction(approved.item, "APPROVE", "eyal", NOW);
      expect(again.ok).toBe(false);
    }
  });
});

describe("reduce quantity", () => {
  it("reduce above proposed is rejected; at/below proposed is recorded", () => {
    const over = applyAction(fresh(), "REDUCE_QUANTITY", "eyal", NOW, { reduceTo: "41" });
    expect(over.ok).toBe(false);
    const under = applyAction(fresh(), "REDUCE_QUANTITY", "eyal", NOW, { reduceTo: "10" });
    expect(under.ok).toBe(true);
    if (under.ok) {
      expect(under.item.plan.quantity).toBe("10");
      // reviewer changed the plan themselves: hashes re-aligned
      expect(under.item.reviewedPlanHash).toBe(under.item.currentPlanHash);
    }
    const invalid = applyAction(fresh(), "REDUCE_QUANTITY", "eyal", NOW, { reduceTo: "0" });
    expect(invalid.ok).toBe(false);
  });
});

describe("plan hash", () => {
  it("is deterministic and sensitive to any plan field", () => {
    const base = fresh().plan;
    expect(planHash(base)).toBe(planHash({ ...base }));
    expect(planHash(base)).not.toBe(planHash({ ...base, stopPrice: "227.99" }));
    expect(planHash(base)).not.toBe(planHash({ ...base, quantity: "39" }));
  });
});

describe("source hygiene (no submit path, no secrets)", () => {
  it("approvals code never references authorization/broker/execution/network", () => {
    const files = [
      join(HERE, "lib", "approvals.ts"),
      join(HERE, "components", "approval-queue.tsx"),
      join(HERE, "app", "approvals", "page.tsx"),
    ];
    const forbidden = [
      "issueSession" + "Authorization",
      "Submit" + "Authorization(",
      "submit" + "Order",
      "Execution" + "Agent(",
      "ib" + "api",
      "fetch(",
      "EventSource(",
      "process.env",
      "getpass",
      "passw" + "ord",
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const marker of forbidden) {
        expect(source, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });
});
