/**
 * Emergency-controls drills (IMP-1106). Node-side only. DoD: the halt drill
 * passes; flatten remains hard-disabled — executeFlatten always refuses,
 * even with a re-auth token. Everything fixtureOnly: no broker, no order,
 * no cancel, no network.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  FLATTEN_DISABLED_REASON,
  FLATTEN_ENABLED,
  INITIAL_EMERGENCY_STATE,
  activateHaltDrill,
  executeFlatten,
  previewFlatten,
  resumeDrill,
} from "./lib/emergency";

const HERE = dirname(fileURLToPath(import.meta.url));
const NOW = "2026-07-10T17:00:00.000Z";

describe("halt drill (DoD: drill passes)", () => {
  it("activates GLOBAL_ENTRY_HALT with keep_protection and a fixtureOnly audit", () => {
    const state = activateHaltDrill(INITIAL_EMERGENCY_STATE, "eyal", NOW, "OPERATOR_DRILL");
    expect(state.haltActive).toBe(true);
    expect(state.scope).toBe("GLOBAL_ENTRY_HALT");
    expect(state.keepProtection).toBe(true);
    expect(state.auditTrail).toHaveLength(1);
    const entry = state.auditTrail[0]!;
    expect(entry.action).toBe("HALT_DRILL_ACTIVATED");
    expect(entry.fixtureOnly).toBe(true);
    expect(entry.detail).toContain("no broker call");
  });

  it("activating twice is a recorded no-op, never a second halt", () => {
    const once = activateHaltDrill(INITIAL_EMERGENCY_STATE, "eyal", NOW, "d");
    const twice = activateHaltDrill(once, "eyal", NOW, "d");
    expect(twice.haltActive).toBe(true);
    expect(twice.auditTrail.map((entry) => entry.action)).toEqual([
      "HALT_DRILL_ACTIVATED",
      "HALT_DRILL_NOOP",
    ]);
  });
});

describe("resume drill (fail-closed, §42.2)", () => {
  const halted = activateHaltDrill(INITIAL_EMERGENCY_STATE, "eyal", NOW, "d");

  it("is blocked unless BOTH health and reconciliation are PASS", () => {
    for (const gates of [
      { health: "FAIL", reconciliation: "PASS" } as const,
      { health: "PASS", reconciliation: "FAIL" } as const,
      { health: "FAIL", reconciliation: "FAIL" } as const,
    ]) {
      const result = resumeDrill(halted, "eyal", NOW, gates);
      expect(result.ok).toBe(false);
      if (!result.ok) {
        expect(result.reason).toContain("RESUME_BLOCKED");
      }
    }
  });

  it("passes only with both gates PASS, clearing the halt with audit", () => {
    const result = resumeDrill(halted, "eyal", NOW, {
      health: "PASS",
      reconciliation: "PASS",
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.state.haltActive).toBe(false);
      expect(result.state.auditTrail.at(-1)?.action).toBe("RESUME_DRILL");
      expect(result.state.keepProtection).toBe(true);
    }
  });

  it("resume without an active halt is refused", () => {
    const result = resumeDrill(INITIAL_EMERGENCY_STATE, "eyal", NOW, {
      health: "PASS",
      reconciliation: "PASS",
    });
    expect(result.ok).toBe(false);
  });
});

describe("flatten stays hard-disabled (DoD)", () => {
  it("FLATTEN_ENABLED is the literal false", () => {
    expect(FLATTEN_ENABLED).toBe(false);
  });

  it("executeFlatten always returns REFUSED_DISABLED — even with reauth", () => {
    for (const token of [undefined, "", "reauth-token-123"]) {
      const refusal = executeFlatten(token);
      expect(refusal.executed).toBe(false);
      expect(refusal.result).toBe("REFUSED_DISABLED");
      expect(refusal.reason).toBe(FLATTEN_DISABLED_REASON);
    }
  });

  it("previewFlatten is pure: returns rows, changes nothing, twice identical", () => {
    const first = previewFlatten();
    const second = previewFlatten();
    expect(first).toEqual(second);
    expect(first.length).toBeGreaterThan(0);
    for (const row of first) {
      expect(row.note).toContain("preview only");
    }
  });
});

describe("hygiene: no real action path, no secrets", () => {
  it("emergency code never references broker/order/cancel/submit/network/env", () => {
    const files = [
      join(HERE, "lib", "emergency.ts"),
      join(HERE, "components", "emergency-controls.tsx"),
      join(HERE, "app", "emergency", "page.tsx"),
    ];
    const forbidden = [
      "submit" + "Order",
      "cancel" + "Order",
      "place" + "Order",
      "Broker" + "Port",
      "Emergency" + "ControlService",
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

  it("the emergency screen exposes no action button at all", () => {
    const source = readFileSync(join(HERE, "components", "emergency-controls.tsx"), "utf-8");
    expect(source).not.toContain("<button");
    expect(source).not.toContain("FLATTEN_ENABLED");
  });
});
