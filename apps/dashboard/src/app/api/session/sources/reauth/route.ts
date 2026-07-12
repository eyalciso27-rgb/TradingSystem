/**
 * Source re-authentication request (IMP-1102).
 *
 * This route REQUESTS a re-auth; it never performs one and never touches a
 * credential. The owner completes the real re-login out of band (personal
 * TDLib / Green API / Discord / InvestingPro session). The route only marks
 * the source NEEDS_REAUTH and returns owner instructions, so no secret is
 * ever entered into or returned by the console (ADR-0014; no secrets rule).
 *
 * Auth: same session cookie as the rest of the console. Unauthenticated
 * requests get 401 (never a silent success). A source with no re-auth flow,
 * or an unknown kind, is refused (fail closed).
 */

import { sessionFromCookieHeader } from "../../../../../lib/auth";
import { sourceByKind } from "../../../../../lib/sources";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  const session = sessionFromCookieHeader(
    request.headers.get("cookie"),
    Math.floor(Date.now() / 1000),
  );
  if (session === null) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  let kind = "";
  try {
    const body: unknown = await request.json();
    if (typeof body === "object" && body !== null && "kind" in body) {
      const value = (body as { kind: unknown }).kind;
      kind = typeof value === "string" ? value : "";
    }
  } catch {
    kind = "";
  }

  const source = sourceByKind(kind);
  if (source === undefined) {
    return Response.json({ error: "unknown source kind" }, { status: 400 });
  }
  if (!source.reauthAvailable) {
    // Fail closed: key-configured sources have no interactive re-auth flow.
    return Response.json(
      { error: "no re-auth flow for this source; it is key-configured" },
      { status: 409 },
    );
  }

  // Never a real login here: record the request and hand the owner the
  // out-of-band action. Runtime becomes NEEDS_REAUTH, never CONNECTED.
  return Response.json(
    {
      kind: source.kind,
      runtime: "NEEDS_REAUTH",
      requiresOwnerAction: true,
      transport: source.transport,
      instruction:
        `Re-auth for ${source.displayName} is performed by the owner out of band` +
        ` via ${source.transport}. The console never accepts credentials.`,
      evidenceRef: source.evidenceRef,
      requestedBy: session.operator,
    },
    { status: 202 },
  );
}
