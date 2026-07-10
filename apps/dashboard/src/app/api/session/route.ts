/** Login/logout for the console shell. No secrets: fixture verifier only. */

import {
  SESSION_COOKIE,
  SESSION_TTL_SECONDS,
  fixtureVerifier,
  issueSessionToken,
} from "../../../lib/auth";

export async function POST(request: Request): Promise<Response> {
  let operator = "";
  try {
    const body: unknown = await request.json();
    if (typeof body === "object" && body !== null && "operator" in body) {
      const value = (body as { operator: unknown }).operator;
      operator = typeof value === "string" ? value : "";
    }
  } catch {
    operator = "";
  }
  const role = fixtureVerifier.verify(operator);
  if (role === null) {
    return Response.json({ error: "operator name required" }, { status: 401 });
  }
  const token = issueSessionToken(operator.trim(), role, Math.floor(Date.now() / 1000));
  return new Response(JSON.stringify({ operator: operator.trim(), role }), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "set-cookie":
        `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict;` +
        ` Max-Age=${SESSION_TTL_SECONDS}`,
    },
  });
}

export async function DELETE(): Promise<Response> {
  return new Response(null, {
    status: 204,
    headers: {
      "set-cookie": `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0`,
    },
  });
}
