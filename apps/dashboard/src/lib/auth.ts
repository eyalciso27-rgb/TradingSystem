/**
 * Session auth for the console shell (IMP-1101).
 *
 * NO SECRETS anywhere: no credential material, no ENV secret, nothing
 * persisted. The
 * signing key is generated per process at startup, so sessions are valid
 * only for the running server — good enough for the authenticated shell.
 * The identity check itself is a pluggable verifier: the DEFAULT verifier
 * only accepts a non-empty operator name and always issues the read-only
 * VIEWER role. Real credentials + RBAC arrive by wiring the trading_api
 * auth module (IMP-0401) behind the same verifier port — never by putting
 * secrets here.
 */

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "ops_console_session";
export const SESSION_TTL_SECONDS = 8 * 60 * 60;

/** Read-only shell role; no mutating role exists in this task. */
export type Role = "VIEWER";

export interface Session {
  readonly operator: string;
  readonly role: Role;
  readonly expiresAt: number; // epoch seconds
}

export interface OperatorVerifier {
  /** Returns the role for a valid operator, or null to refuse. */
  verify(operator: string): Role | null;
}

/** Fixture verifier: non-empty operator name => read-only viewer. */
export const fixtureVerifier: OperatorVerifier = {
  verify(operator: string): Role | null {
    return operator.trim().length > 0 ? "VIEWER" : null;
  },
};

// Per-process signing key. It must be identical for every module instance
// in the process: Next.js evaluates this module separately in the RSC layer
// (the page's server component) and in the route-handler layer (the login +
// SSE routes), so a plain module-level `randomBytes` would produce TWO keys
// and a cookie signed by one layer would be rejected by the other — the page
// would bounce a freshly-logged-in operator back to /login and the SSE would
// 401 ("disconnected"). Stash the key on the process global so both layers
// share it. Still no stored secret and no ENV: it rotates on a real process
// restart, which is the intended shell behaviour.
const KEY_GLOBAL = Symbol.for("trading.ops-console.session-key");
type KeyHolder = { [KEY_GLOBAL]?: Buffer };
const holder = globalThis as unknown as KeyHolder;
holder[KEY_GLOBAL] ??= randomBytes(32);
const signingKey: Buffer = holder[KEY_GLOBAL];

function sign(payload: string): string {
  return createHmac("sha256", signingKey).update(payload).digest("base64url");
}

export function issueSessionToken(operator: string, role: Role, nowEpochSeconds: number): string {
  const payload = Buffer.from(
    JSON.stringify({ operator, role, expiresAt: nowEpochSeconds + SESSION_TTL_SECONDS }),
    "utf-8",
  ).toString("base64url");
  return `${payload}.${sign(payload)}`;
}

export function verifySessionToken(
  token: string | undefined,
  nowEpochSeconds: number,
): Session | null {
  if (!token) {
    return null;
  }
  const parts = token.split(".");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    return null;
  }
  const [payload, signature] = parts;
  const expected = sign(payload);
  const provided = Buffer.from(signature, "utf-8");
  const wanted = Buffer.from(expected, "utf-8");
  if (provided.length !== wanted.length || !timingSafeEqual(provided, wanted)) {
    return null; // tampered or foreign token: fail closed
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf-8"));
  } catch {
    return null;
  }
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    typeof (parsed as Session).operator !== "string" ||
    (parsed as Session).role !== "VIEWER" ||
    typeof (parsed as Session).expiresAt !== "number"
  ) {
    return null;
  }
  const session = parsed as Session;
  if (session.expiresAt <= nowEpochSeconds) {
    return null; // expired: fail closed
  }
  return session;
}

export function sessionFromCookieHeader(
  cookieHeader: string | null,
  nowEpochSeconds: number,
): Session | null {
  if (!cookieHeader) {
    return null;
  }
  for (const part of cookieHeader.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === SESSION_COOKIE) {
      return verifySessionToken(rest.join("="), nowEpochSeconds);
    }
  }
  return null;
}
