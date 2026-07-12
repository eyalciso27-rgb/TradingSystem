/**
 * Status-explanation helper drills (console UX). Every blocking status the
 * UI can show must map to a human sentence and a consistent chip tone.
 */

import { describe, expect, it } from "vitest";

import { type ChipTone, chipClass, explain, meaningOf } from "./lib/explain";

const VALID_TONES: readonly ChipTone[] = ["ok", "warn", "danger", "muted", "info"];

describe("status meanings", () => {
  it("every blocking status has a human explanation and a danger/warn tone", () => {
    for (const status of [
      "REFRESH_REQUIRED",
      "RECONCILE_REQUIRED",
      "UNKNOWN",
      "VETOED",
      "BLOCK_NEW_ENTRIES",
    ]) {
      const meaning = meaningOf(status);
      expect(meaning.explain.length, status).toBeGreaterThan(0);
      expect(["warn", "danger"], status).toContain(meaning.tone);
    }
  });

  it("is case-insensitive", () => {
    expect(explain("unknown")).toBe(explain("UNKNOWN"));
    expect(explain("unknown").length).toBeGreaterThan(0);
  });

  it("plainly-good states carry an ok tone and no scary text", () => {
    expect(meaningOf("HEALTHY").tone).toBe("ok");
    expect(meaningOf("LIVE").tone).toBe("ok");
    expect(explain("HEALTHY")).toBe("");
  });

  it("unknown tokens fall back to a muted chip with no explanation", () => {
    expect(meaningOf("SOMETHING_NEW")).toEqual({ tone: "muted", explain: "" });
  });

  it("chipClass always yields a valid tone class", () => {
    for (const status of ["OK", "REFRESH_REQUIRED", "STALE", "PENDING", "ZZZ"]) {
      const cls = chipClass(status);
      expect(cls.startsWith("chip chip-")).toBe(true);
      const tone = cls.replace("chip chip-", "") as ChipTone;
      expect(VALID_TONES).toContain(tone);
    }
  });
});
