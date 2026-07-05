import { describe, expect, it } from "vitest";

import {
  DECISION_OUTCOME_VALUES,
  DecisionOutcome,
  ENVIRONMENT_VALUES,
  Environment,
  isDecisionOutcome,
  isOperatingMode,
  OPERATING_MODE_VALUES,
  OperatingMode,
} from "./enums";

describe("canonical enums (generated)", () => {
  it("decision outcomes match the canonical baseline values", () => {
    expect([...DECISION_OUTCOME_VALUES]).toEqual([
      "REJECT",
      "WATCH",
      "SHADOW",
      "APPROVE",
      "NEEDS_REVIEW",
      "EXPIRE",
      "INVALID",
    ]);
  });

  it("operating modes include REPLAY and exclude LIVE_AUTO", () => {
    expect(OPERATING_MODE_VALUES).toContain("REPLAY");
    expect(OPERATING_MODE_VALUES as readonly string[]).not.toContain("LIVE_AUTO");
  });

  it("environment axis is separate from operating modes", () => {
    expect([...ENVIRONMENT_VALUES]).toEqual(["RESEARCH", "PAPER", "LIVE"]);
    expect(ENVIRONMENT_VALUES.length).not.toBe(OPERATING_MODE_VALUES.length);
  });

  it("guards accept canonical values and reject unknown values", () => {
    expect(isDecisionOutcome("APPROVE")).toBe(true);
    expect(isDecisionOutcome("NOT_A_CANONICAL_VALUE")).toBe(false);
    expect(isOperatingMode("PAPER_MANUAL")).toBe(true);
    expect(isOperatingMode("LIVE_AUTO")).toBe(false);
  });

  it("constants expose the exact canonical strings", () => {
    expect(DecisionOutcome.NEEDS_REVIEW).toBe("NEEDS_REVIEW");
    expect(OperatingMode.PAPER_MANUAL).toBe("PAPER_MANUAL");
    expect(Environment.PAPER).toBe("PAPER");
  });

  it("rejects a non-canonical value at type level", () => {
    // @ts-expect-error a non-canonical string is not assignable to the enum type
    const invalid: DecisionOutcome = "NOT_A_CANONICAL_VALUE";
    expect(isDecisionOutcome(invalid)).toBe(false);
  });
});
