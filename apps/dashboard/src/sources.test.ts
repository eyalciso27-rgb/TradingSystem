/**
 * Source-connections screen drills (IMP-1102). Node-side only: capability vs
 * runtime separation, re-auth flow auth + fail-closed, no secrets, masking.
 * No browser, no real connection, no network beyond the in-process route.
 */

import { describe, expect, it } from "vitest";

import { issueSessionToken, SESSION_COOKIE } from "./lib/auth";
import { SOURCE_REAUTH_PATH } from "./lib/routes";
import {
  SOURCE_CONNECTIONS,
  type CapabilityLevel,
  isRuntimePositive,
  sourceByKind,
} from "./lib/sources";

function authedRequest(body: unknown): Request {
  const token = issueSessionToken("eyal", "VIEWER", Math.floor(Date.now() / 1000));
  return new Request("http://localhost" + SOURCE_REAUTH_PATH, {
    method: "POST",
    headers: { "content-type": "application/json", cookie: `${SESSION_COOKIE}=${token}` },
    body: JSON.stringify(body),
  });
}

describe("source model: capability vs runtime are separated (DoD)", () => {
  it("covers exactly the scoped sources", () => {
    const kinds = SOURCE_CONNECTIONS.map((s) => s.kind).sort();
    expect(kinds).toEqual(
      [
        "DISCORD_USER",
        "INVESTINGPRO",
        "MARKET_DATA",
        "NEWS",
        "TELEGRAM_BOT",
        "TELEGRAM_USER",
        "WHATSAPP_USER",
      ].sort(),
    );
  });

  it("keeps capability independent from runtime", () => {
    const tgUser = sourceByKind("TELEGRAM_USER");
    expect(tgUser?.capability).toBe<CapabilityLevel>("MANDATORY_PRIMARY");
    // permitted (mandatory) yet not connected at runtime — the two dimensions
    // must not collapse into one another
    expect(tgUser?.runtime).toBe("BLOCKED_NO_POI");
    expect(isRuntimePositive(tgUser!.runtime)).toBe(false);
  });

  it("the Telegram bot is supplemental and never marked as the primary path", () => {
    const bot = sourceByKind("TELEGRAM_BOT");
    expect(bot?.capability).toBe<CapabilityLevel>("SUPPLEMENTAL");
    expect(bot?.capabilityNote.toLowerCase()).toContain("supplemental");
  });

  it("no blocked/unknown source is ever shown as usable", () => {
    for (const source of SOURCE_CONNECTIONS) {
      if (source.runtime !== "CONNECTED") {
        expect(isRuntimePositive(source.runtime)).toBe(false);
      }
    }
  });

  it("exposes a masked account label and a per-source evidence ref, never a full id", () => {
    for (const source of SOURCE_CONNECTIONS) {
      expect(source.evidenceRef).toMatch(/evidence\//);
      expect(source.accountLabelMasked).toContain("*");
    }
  });
});

describe("re-auth flow (/api/session/sources/reauth)", () => {
  it("refuses unauthenticated requests with 401", async () => {
    const { POST } = await import("./app/api/session/sources/reauth/route");
    const response = await POST(
      new Request("http://localhost" + SOURCE_REAUTH_PATH, {
        method: "POST",
        body: JSON.stringify({ kind: "TELEGRAM_USER" }),
      }),
    );
    expect(response.status).toBe(401);
  });

  it("requests re-auth for an authenticated operator without touching credentials", async () => {
    const { POST } = await import("./app/api/session/sources/reauth/route");
    const response = await POST(authedRequest({ kind: "TELEGRAM_USER" }));
    expect(response.status).toBe(202);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.runtime).toBe("NEEDS_REAUTH"); // never CONNECTED
    expect(body.requiresOwnerAction).toBe(true);
    const serialized = JSON.stringify(body);
    expect(serialized.toLowerCase()).not.toContain("password");
    expect(serialized).not.toContain("token=");
  });

  it("fails closed on an unknown source kind", async () => {
    const { POST } = await import("./app/api/session/sources/reauth/route");
    const response = await POST(authedRequest({ kind: "NOPE" }));
    expect(response.status).toBe(400);
  });

  it("refuses re-auth for key-configured sources that have no interactive flow", async () => {
    const { POST } = await import("./app/api/session/sources/reauth/route");
    const response = await POST(authedRequest({ kind: "NEWS" }));
    expect(response.status).toBe(409);
    expect(sourceByKind("NEWS")?.reauthAvailable).toBe(false);
  });

  it("re-auth is available for every credentialed/session source", () => {
    for (const kind of [
      "TELEGRAM_USER",
      "TELEGRAM_BOT",
      "WHATSAPP_USER",
      "DISCORD_USER",
      "INVESTINGPRO",
    ] as const) {
      expect(sourceByKind(kind)?.reauthAvailable).toBe(true);
    }
  });
});

describe("session compatibility (IMP-1101 not broken)", () => {
  it("the re-auth route accepts the same session cookie the console issues", async () => {
    const { POST } = await import("./app/api/session/sources/reauth/route");
    const token = issueSessionToken("eyal", "VIEWER", Math.floor(Date.now() / 1000));
    const response = await POST(
      new Request("http://localhost" + SOURCE_REAUTH_PATH, {
        method: "POST",
        headers: { cookie: `${SESSION_COOKIE}=${token}` },
        body: JSON.stringify({ kind: "DISCORD_USER" }),
      }),
    );
    expect(response.status).toBe(202);
  });
});
