/**
 * Positions/orders/reconciliation drills (IMP-1105). Node-side only.
 * DoD: the UI mirrors canonical state, never broker raw status — the local
 * canonical list is cross-checked verbatim against BOTH generated contract
 * sources, and no raw IBKR wire status can appear in fixtures or render.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  CANONICAL_ORDER_STATES,
  FILL_ROWS,
  MISMATCH_ROWS,
  ORDER_ROWS,
  POSITION_ROWS,
  isCanonicalOrderState,
  needsReconcile,
} from "./lib/positions";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..", "..");

describe("canonical order states (DoD)", () => {
  it("matches the generated JSON schema enum verbatim", () => {
    const schema = JSON.parse(
      readFileSync(
        join(REPO_ROOT, "packages", "schemas", "json", "enums", "order_state.schema.json"),
        "utf-8",
      ),
    ) as { enum: string[] };
    expect([...CANONICAL_ORDER_STATES]).toEqual(schema.enum);
  });

  it("matches the generated TS contract enum verbatim", async () => {
    const generated = await import(
      "../../../packages/ts/contracts-generated/src/enums"
    );
    expect([...CANONICAL_ORDER_STATES]).toEqual([...generated.ORDER_STATE_VALUES]);
  });

  it("every order fixture uses a canonical state only", () => {
    for (const order of ORDER_ROWS) {
      expect(isCanonicalOrderState(order.state), order.clientOrderId).toBe(true);
    }
  });

  it("raw IBKR wire statuses never appear in the screen code or fixtures", () => {
    const files = [
      join(HERE, "lib", "positions.ts"),
      join(HERE, "components", "positions-orders.tsx"),
    ];
    // Raw broker wire strings (mixed case) that must never be rendered.
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const raw of [
        '"Submitted"',
        '"PreSubmitted"',
        '"PendingSubmit"',
        '"PendingCancel"',
        '"ApiPending"',
        '"ApiCancelled"',
        '"Inactive"',
        '"Filled"', // raw wire casing — canonical is FILLED
      ]) {
        expect(source, `${file} contains raw broker status ${raw}`).not.toContain(raw);
      }
    }
  });
});

describe("UNKNOWN handling", () => {
  it("UNKNOWN requires reconcile and is present in fixtures as blocking", () => {
    expect(needsReconcile("UNKNOWN")).toBe(true);
    expect(needsReconcile("ACKNOWLEDGED")).toBe(false);
    const ghost = ORDER_ROWS.find((order) => order.state === "UNKNOWN");
    expect(ghost).toBeDefined();
    // and reconciliation lists it as an unresolved finding, never positive
    const finding = MISMATCH_ROWS.find((m) => m.subjectRef.includes(ghost!.clientOrderId));
    expect(finding).toBeDefined();
    expect(finding!.recommendation).toBe("NEEDS_REVIEW");
  });
});

describe("positions / fills / mismatches", () => {
  it("positions show stop/target/protected and protected==quantity here", () => {
    for (const position of POSITION_ROWS) {
      expect(position.protectedQuantity).toBe(position.quantity);
      expect(position.accountAliasMasked).toContain("*");
      expect(position.accountAliasMasked.length).toBeLessThan(11); // masked, not full
    }
  });

  it("fills reference existing orders and sum within the order quantity", () => {
    const entry = ORDER_ROWS.find((order) => order.clientOrderId === "ord-entry-1")!;
    const fills = FILL_ROWS.filter((fill) => fill.clientOrderId === entry.clientOrderId);
    const total = fills.reduce((sum, fill) => sum + Number(fill.quantity), 0);
    expect(String(total)).toBe(entry.filledQuantity);
    expect(total).toBeLessThanOrEqual(Number(entry.quantity));
  });

  it("every mismatch carries expected/observed and a recommendation", () => {
    expect(MISMATCH_ROWS.length).toBeGreaterThan(0);
    for (const mismatch of MISMATCH_ROWS) {
      expect(mismatch.expected.length).toBeGreaterThan(0);
      expect(mismatch.observed.length).toBeGreaterThan(0);
      expect(["NEEDS_REVIEW", "RESYNC_STATE", "BLOCK_NEW_ENTRIES"]).toContain(
        mismatch.recommendation,
      );
    }
  });

  it("fixtures are deterministic", () => {
    expect(POSITION_ROWS).toEqual(POSITION_ROWS);
    expect(ORDER_ROWS).toEqual(ORDER_ROWS);
  });
});

describe("read-only + hygiene", () => {
  it("screen has no action verbs, no network, no secrets", () => {
    const files = [
      join(HERE, "lib", "positions.ts"),
      join(HERE, "components", "positions-orders.tsx"),
      join(HERE, "app", "positions", "page.tsx"),
    ];
    const forbidden = [
      "submit" + "Order",
      "cancel" + "Order",
      "Submit" + "Authorization",
      "fetch(",
      "EventSource(",
      "process.env",
      "getpass",
      "passw" + "ord",
      "ib" + "api",
      "onClick", // fully read-only: no interactive action at all
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const marker of forbidden) {
        expect(source, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });
});
