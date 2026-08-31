import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import * as React from "react";
import { describe, expect, it } from "vitest";
import { EmergencyReadOnlyScreen, ExecutionHistoryScreen, OperationsLoadState, OperationsOverview, PositionsScreen, DecisionsScreen, orderedReachedStages } from "./components/operations-screens";
import { EXECUTION_LIFECYCLE, MOCK_OPERATIONS_SNAPSHOT, displayValue } from "./lib/operations";

const HERE = dirname(fileURLToPath(import.meta.url));
const props = { operator: "operator", role: "VIEWER", snapshot: MOCK_OPERATIONS_SNAPSHOT };

describe("fail-closed operations rendering", () => {
  it("never coerces missing or UNKNOWN values to healthy, false, empty, or zero", () => {
    expect(displayValue(null)).toBe("UNKNOWN");
    expect(displayValue(undefined)).toBe("UNKNOWN");
    expect(displayValue(0)).toBe("0");
    const html = renderToStaticMarkup(<OperationsOverview {...props} />);
    expect(html).toContain("Session P&amp;L");
    expect(html).toContain("UNKNOWN");
    expect(html).not.toContain("Session P&amp;L</dt><dd>0");
    expect(html).toContain("BLOCKED");
    expect(html).toContain("PAPER_AUTO_DISABLED");
    expect(html).toContain("LIVE PROHIBITED");
  });

  it("makes active halt and unknown positions prominent", () => {
    const halt = renderToStaticMarkup(<EmergencyReadOnlyScreen {...props} />);
    expect(halt).toContain("HALT ACTIVE");
    expect(halt).toContain("New entries blocked?</dt><dd><span>YES</span>");
    const positions = renderToStaticMarkup(<PositionsScreen {...props} />);
    expect(positions).toContain("unknown-row");
    expect(positions).toContain("UNKNOWN / unattributed position");
    expect(positions).toContain("block new entries");
  });

  it("explains no-trade and renders lifecycle in deterministic canonical order", () => {
    const decisions = renderToStaticMarkup(<DecisionsScreen {...props} />);
    expect(decisions).toContain("NO_TRADE");
    expect(decisions).toContain("no complete entry setup");
    expect(orderedReachedStages([...EXECUTION_LIFECYCLE].reverse())).toEqual(EXECUTION_LIFECYCLE);
    const execution = renderToStaticMarkup(<ExecutionHistoryScreen {...props} />);
    let cursor = -1;
    for (const stage of EXECUTION_LIFECYCLE) { const next = execution.indexOf(`>${stage}</b>`, cursor + 1); expect(next).toBeGreaterThan(cursor); cursor = next; }
  });

  it("keeps scoped proof authorization distinct from disabled autonomous Paper Auto", () => {
    expect(MOCK_OPERATIONS_SNAPSHOT.runtime.authorizationMode).toBe("PAPER_AUTO_DISABLED");
    const protectedProof = MOCK_OPERATIONS_SNAPSHOT.executions.find((execution) => execution.executionId === "exec-msft-088");
    expect(protectedProof?.quantity).toBe("1");
    expect(protectedProof?.authorizationOutcome).toBe("SCOPED_PAPER_PROOF_AUTHORIZATION");
    expect(protectedProof?.authorizationOutcome).not.toContain("PAPER_AUTO");
    const html = renderToStaticMarkup(<ExecutionHistoryScreen {...props} />);
    expect(html).toContain("Scoped/manual proof authorization is distinct from autonomous PAPER_AUTO authorization");
  });

  it("shows explicit loading and error states", () => {
    expect(renderToStaticMarkup(<OperationsLoadState state={{ status: "loading" }} />)).toContain("Loading operations state");
    const error = renderToStaticMarkup(<OperationsLoadState state={{ status: "error", message: "fixture failure" }} />);
    expect(error).toContain("UNKNOWN");
    expect(error).toContain("fixture failure");
  });

  it("labels every operations screen as mock data", () => {
    for (const html of [renderToStaticMarkup(<OperationsOverview {...props} />), renderToStaticMarkup(<PositionsScreen {...props} />), renderToStaticMarkup(<ExecutionHistoryScreen {...props} />)]) {
      expect(html).toContain("MOCK DATA");
      expect(html).toContain("NOT BROKER TRUTH");
    }
  });
});

describe("read-only trading boundary", () => {
  it("renders no buttons on operations, positions, decisions, executions, or emergency pages", () => {
    const screens = [<OperationsOverview {...props} />, <PositionsScreen {...props} />, <DecisionsScreen {...props} />, <ExecutionHistoryScreen {...props} />, <EmergencyReadOnlyScreen {...props} />];
    for (const screen of screens) expect(renderToStaticMarkup(screen)).not.toContain("<button");
  });

  it("contains no submit/cancel/authorization write endpoint calls", () => {
    const roots = [join(HERE, "components"), join(HERE, "app"), join(HERE, "lib")];
    const files: string[] = [];
    const walk = (dir: string): void => { for (const name of readdirSync(dir)) { const full = join(dir, name); if (statSync(full).isDirectory()) walk(full); else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".test.ts") && !name.endsWith(".test.tsx")) files.push(full); } };
    roots.forEach(walk);
    const forbiddenEndpoint = /fetch\s*\([^)]*(submit[-_/]?order|cancel[-_/]?order|authoriz(?:e|ation))/i;
    for (const file of files) expect(readFileSync(file, "utf8"), file).not.toMatch(forbiddenEndpoint);
  });
});
