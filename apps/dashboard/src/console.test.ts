/**
 * Operations Console shell drills (IMP-1101). Node-side tests only: auth
 * fail-closed, SSE stream auth + payload, masking, disabled dangerous
 * actions, no-secret hygiene. No browser, no broker, no network.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  SESSION_COOKIE,
  fixtureVerifier,
  issueSessionToken,
  sessionFromCookieHeader,
  verifySessionToken,
} from "./lib/auth";
import { encodeSseEvent } from "./lib/sse";
import { DANGEROUS_ACTIONS, fixtureStatus, maskAccountId } from "./lib/status";

const NOW = 1_780_000_000;
const HERE = dirname(fileURLToPath(import.meta.url));

describe("session auth", () => {
  it("round-trips a valid session token", () => {
    const token = issueSessionToken("eyal", "VIEWER", NOW);
    const session = verifySessionToken(token, NOW + 60);
    expect(session).not.toBeNull();
    expect(session?.operator).toBe("eyal");
    expect(session?.role).toBe("VIEWER");
  });

  it("rejects tampered, malformed, expired and missing tokens", () => {
    const token = issueSessionToken("eyal", "VIEWER", NOW);
    expect(verifySessionToken(`${token}x`, NOW)).toBeNull();
    const [payload] = token.split(".");
    expect(verifySessionToken(`${payload}.forged`, NOW)).toBeNull();
    expect(verifySessionToken("not-a-token", NOW)).toBeNull();
    expect(verifySessionToken(undefined, NOW)).toBeNull();
    expect(verifySessionToken(token, NOW + 9 * 60 * 60)).toBeNull(); // expired
  });

  it("parses the session cookie header and refuses foreign cookies", () => {
    const token = issueSessionToken("eyal", "VIEWER", NOW);
    const header = `other=1; ${SESSION_COOKIE}=${token}`;
    expect(sessionFromCookieHeader(header, NOW)?.operator).toBe("eyal");
    expect(sessionFromCookieHeader("other=1", NOW)).toBeNull();
    expect(sessionFromCookieHeader(null, NOW)).toBeNull();
  });

  it("fixture verifier refuses empty operators and only grants VIEWER", () => {
    expect(fixtureVerifier.verify("  ")).toBeNull();
    expect(fixtureVerifier.verify("eyal")).toBe("VIEWER");
  });
});

describe("session route", () => {
  it("refuses login without an operator name", async () => {
    const { POST } = await import("./app/api/session/route");
    const response = await POST(
      new Request("http://localhost/api/session", {
        method: "POST",
        body: JSON.stringify({ operator: " " }),
      }),
    );
    expect(response.status).toBe(401);
  });

  it("issues an http-only session cookie on login", async () => {
    const { POST } = await import("./app/api/session/route");
    const response = await POST(
      new Request("http://localhost/api/session", {
        method: "POST",
        body: JSON.stringify({ operator: "eyal" }),
      }),
    );
    expect(response.status).toBe(200);
    const cookie = response.headers.get("set-cookie") ?? "";
    expect(cookie).toContain(`${SESSION_COOKIE}=`);
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("SameSite=Strict");
  });
});

describe("SSE status stream", () => {
  it("refuses unauthenticated subscribers", async () => {
    const { GET } = await import("./app/api/events/route");
    const response = await GET(new Request("http://localhost/api/events"));
    expect(response.status).toBe(401);
  });

  it("streams the fixture status to an authenticated subscriber", async () => {
    const { GET } = await import("./app/api/events/route");
    const token = issueSessionToken("eyal", "VIEWER", Math.floor(Date.now() / 1000));
    const response = await GET(
      new Request("http://localhost/api/events", {
        headers: { cookie: `${SESSION_COOKIE}=${token}` },
      }),
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    const reader = response.body?.getReader();
    expect(reader).toBeDefined();
    const chunk = await reader!.read();
    const text = new TextDecoder().decode(chunk.value);
    expect(text).toContain("event: status");
    expect(text).toContain('"mode":"PAPER_MANUAL"');
    expect(text).toContain('"environment":"PAPER"');
    await reader!.cancel();
  });

  it("encodes SSE frames correctly", () => {
    expect(encodeSseEvent("status", { a: 1 })).toBe('event: status\ndata: {"a":1}\n\n');
  });
});

describe("status fixtures", () => {
  it("is deterministic per tick and masks account ids", () => {
    expect(fixtureStatus(3)).toEqual(fixtureStatus(3));
    const serialized = JSON.stringify(fixtureStatus(0));
    expect(serialized).not.toContain("DU" + "1234567"); // full id never present
    expect(serialized).toContain("DU*****67");
  });

  it("masking never reveals the middle of an account id", () => {
    expect(maskAccountId("DU" + "1234567")).toBe("DU*****67");
    expect(maskAccountId("DU12")).toBe("DU**");
    expect(maskAccountId("DU" + "1234567")).not.toContain("12345");
  });
});

describe("dangerous actions", () => {
  it("every dangerous action is disabled with a reason", () => {
    const actions = new Set(DANGEROUS_ACTIONS.map((entry) => entry.action));
    for (const required of [
      "SUBMIT_ORDER",
      "CANCEL_ORDER",
      "FLATTEN",
      "ACTIVATE_KILL_SWITCH",
      "RESUME_TRADING",
    ]) {
      expect(actions.has(required)).toBe(true);
    }
    for (const entry of DANGEROUS_ACTIONS) {
      expect(entry.enabled).toBe(false);
      expect(entry.reason.length).toBeGreaterThan(0);
    }
  });
});

describe("source hygiene", () => {
  it("no secret/env access and no broker calls anywhere in the app source", () => {
    const roots = [join(HERE, "lib"), join(HERE, "app"), join(HERE, "components")];
    const files: string[] = [];
    const walk = (dir: string): void => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
          walk(full);
        } else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".test.ts")) {
          files.push(full);
        }
      }
    };
    roots.forEach(walk);
    expect(files.length).toBeGreaterThan(5);
    const forbidden = [
      "process.env",
      "getpass",
      "passw" + "ord",
      "place" + "Order",
      "ib" + "api",
    ];
    for (const file of files) {
      const source = readFileSync(file, "utf-8");
      for (const marker of forbidden) {
        expect(source, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });
});
