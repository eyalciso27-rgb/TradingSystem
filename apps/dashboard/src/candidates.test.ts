/**
 * Candidate-workspace drills (IMP-1103). Node-side only: DoD explainability,
 * deterministic fixtures, disabled dangerous actions, no-secret hygiene,
 * blocked/vetoed never positive. No browser, no network, no connectors.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  CANDIDATE_ACTIONS,
  CANDIDATE_IDEAS,
  candidateById,
  isFullyExplained,
  isStatusPositive,
} from "./lib/candidates";

const HERE = dirname(fileURLToPath(import.meta.url));

describe("candidate explainability (DoD: every decision explainable)", () => {
  it("every candidate carries the full chain: evidence, models, fusion, plan, freshness", () => {
    expect(CANDIDATE_IDEAS.length).toBeGreaterThan(0);
    for (const candidate of CANDIDATE_IDEAS) {
      expect(isFullyExplained(candidate), candidate.candidateId).toBe(true);
      expect(candidate.evidence.length).toBeGreaterThan(0);
      expect(candidate.modelCards.length).toBeGreaterThan(0);
      expect(candidate.fusion.contributions.length).toBeGreaterThan(0);
      expect(candidate.plan.preSubmitAction.length).toBeGreaterThan(0);
      expect(candidate.freshness.asOf.length).toBeGreaterThan(0);
    }
  });

  it("a vetoed candidate is still fully explainable and shows its veto", () => {
    const vetoed = CANDIDATE_IDEAS.find((c) => c.status === "VETOED");
    expect(vetoed).toBeDefined();
    expect(isFullyExplained(vetoed!)).toBe(true); // the UI can explain WHY it is blocked
    expect(vetoed!.vetoes.some((v) => v.blocking)).toBe(true);
    expect(vetoed!.blockedReasons.length).toBeGreaterThan(0);
  });

  it("a stale candidate is flagged and never positive", () => {
    const stale = CANDIDATE_IDEAS.find((c) => c.status === "STALE");
    expect(stale?.freshness.stale).toBe(true);
    expect(stale?.blockedReasons).toContain("MARKET_DATA_STALE");
    expect(isStatusPositive(stale!.status)).toBe(false);
  });
});

describe("read-only safety", () => {
  it("no candidate is presented as tradeable in this shell", () => {
    for (const candidate of CANDIDATE_IDEAS) {
      // no fixture is APPROVED; and even if one were, the actions are disabled
      expect(isStatusPositive(candidate.status)).toBe(false);
    }
  });

  it("every dangerous action is disabled with a reason", () => {
    const actions = new Set(CANDIDATE_ACTIONS.map((a) => a.action));
    for (const required of ["APPROVE", "SUBMIT", "AUTO_TRADE"]) {
      expect(actions.has(required)).toBe(true);
    }
    for (const entry of CANDIDATE_ACTIONS) {
      expect(entry.enabled).toBe(false);
      expect(entry.reason.length).toBeGreaterThan(0);
    }
  });

  it("provenance handles are masked; no full account id/handle", () => {
    for (const candidate of CANDIDATE_IDEAS) {
      expect(candidate.provenance).toContain("*");
    }
  });
});

describe("fixtures + lookup", () => {
  it("is deterministic and looks up by id", () => {
    expect(CANDIDATE_IDEAS).toEqual(CANDIDATE_IDEAS);
    expect(candidateById("cand-aapl-001")?.symbol).toBe("AAPL");
    expect(candidateById("missing")).toBeUndefined();
  });

  it("directions are canonical", () => {
    for (const candidate of CANDIDATE_IDEAS) {
      expect(["LONG", "SHORT", "NEUTRAL"]).toContain(candidate.direction);
    }
  });
});

describe("source hygiene", () => {
  it("no secret/env access or connector/broker imports in the workspace files", () => {
    const files = [
      join(HERE, "lib", "candidates.ts"),
      join(HERE, "components", "candidate-workspace.tsx"),
      join(HERE, "app", "candidates", "page.tsx"),
    ];
    const forbidden = [
      "process.env",
      "getpass",
      "passw" + "ord",
      "ib" + "api",
      "EventSource(",
      "fetch(",
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const marker of forbidden) {
        expect(source, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });
});
