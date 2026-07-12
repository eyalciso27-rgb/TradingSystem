/**
 * Research/calibration drills (IMP-1107). Node-side only. DoD: no UI action
 * auto-promotes a policy/model — the module exposes no status-changing
 * function, every promotion control is permanently disabled with a
 * governance reason, and fixtures are deterministic and read-only.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import * as research from "./lib/research";
import {
  BACKTEST_RUNS,
  CHALLENGER_ROWS,
  FUNNEL_STAGES,
  MODEL_CARDS,
  PROMOTION_ACTIONS,
  STARVATION_INDICATORS,
} from "./lib/research";

const HERE = dirname(fileURLToPath(import.meta.url));

describe("DoD: no UI action auto-promotes policy/model", () => {
  it("every promotion action is permanently disabled with a governance reason", () => {
    const actions = new Set(PROMOTION_ACTIONS.map((entry) => entry.action));
    for (const required of ["PROMOTE_CHALLENGER", "PROMOTE_POLICY_VERSION", "RETIRE_MODEL"]) {
      expect(actions.has(required)).toBe(true);
    }
    for (const entry of PROMOTION_ACTIONS) {
      expect(entry.enabled).toBe(false);
      expect(entry.reason).toContain("owner");
    }
  });

  it("the research module exports no callable that could change a status", () => {
    // Data-only module: every export is a frozen fixture/constant, not a
    // function — so there is nothing a UI handler could call to promote.
    for (const [name, value] of Object.entries(research)) {
      expect(typeof value, `${name} must not be a function`).not.toBe("function");
    }
  });

  it("no promote/retire/status-mutation verbs exist in the screen code", () => {
    const files = [
      join(HERE, "lib", "research.ts"),
      join(HERE, "components", "research-screen.tsx"),
      join(HERE, "app", "research", "page.tsx"),
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const marker of ["promote(", "setStatus", "onClick", "useState", "fetch("]) {
        expect(source, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });

  it("challengers stay SHADOW in fixtures; champions are the ACTIVE ones", () => {
    for (const row of CHALLENGER_ROWS) {
      expect(row.challengerStatus).toBe("SHADOW");
    }
    const activeModels = MODEL_CARDS.filter((card) => card.status === "ACTIVE").map(
      (card) => card.model,
    );
    expect(activeModels).toContain("technical-v1");
    expect(activeModels).not.toContain("technical-v2"); // the shadow challenger
  });
});

describe("funnel / starvation", () => {
  it("funnel stages are ordered and monotonically narrowing", () => {
    expect(FUNNEL_STAGES.length).toBeGreaterThanOrEqual(4);
    for (let index = 1; index < FUNNEL_STAGES.length; index += 1) {
      expect(FUNNEL_STAGES[index]!.count).toBeLessThanOrEqual(FUNNEL_STAGES[index - 1]!.count);
    }
  });

  it("starvation is flagged exactly when past threshold, with a human explanation", () => {
    for (const indicator of STARVATION_INDICATORS) {
      expect(indicator.starving).toBe(
        indicator.minutesSinceLastItem > indicator.thresholdMinutes,
      );
      expect(indicator.explanation.length).toBeGreaterThan(10);
    }
    expect(STARVATION_INDICATORS.some((indicator) => indicator.starving)).toBe(true);
  });
});

describe("backtests / models", () => {
  it("backtest runs carry policy version and a deterministic seed", () => {
    for (const run of BACKTEST_RUNS) {
      expect(run.policyVersion.length).toBeGreaterThan(0);
      expect(run.deterministicSeed).toMatch(/^seed:/);
    }
  });

  it("a running backtest shows no fabricated metrics", () => {
    const running = BACKTEST_RUNS.find((run) => run.status === "RUNNING");
    expect(running).toBeDefined();
    expect(running!.hitRate).toBe("—");
    expect(running!.profitFactor).toBe("—");
  });

  it("fixtures are deterministic", () => {
    expect(FUNNEL_STAGES).toEqual(FUNNEL_STAGES);
    expect(MODEL_CARDS).toEqual(MODEL_CARDS);
  });
});

describe("hygiene", () => {
  it("no secrets/env/broker/connector references", () => {
    const files = [
      join(HERE, "lib", "research.ts"),
      join(HERE, "components", "research-screen.tsx"),
      join(HERE, "app", "research", "page.tsx"),
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const marker of [
        "process.env",
        "getpass",
        "passw" + "ord",
        "ib" + "api",
        "EventSource(",
        "submit" + "Order",
      ]) {
        expect(source, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });

  it("AppNav includes the research section", () => {
    const source = readFileSync(join(HERE, "components", "app-nav.tsx"), "utf-8");
    expect(source).toContain('{ id: "research", label: "Research", href: "/research" }');
  });
});
