/**
 * Authenticated live status stream (SSE) for the console shell.
 *
 * Serves the deterministic PAPER-observation fixtures (ADR-0014); the real
 * feed replaces the provider, not this contract. Unauthenticated requests
 * are refused with 401 — the stream never leaks anything to an anonymous
 * client.
 */

import { sessionFromCookieHeader } from "../../../lib/auth";
import { encodeSseEvent } from "../../../lib/sse";
import { fixtureStatus } from "../../../lib/status";

export const dynamic = "force-dynamic";

const TICK_MILLISECONDS = 2_000;

export async function GET(request: Request): Promise<Response> {
  const session = sessionFromCookieHeader(
    request.headers.get("cookie"),
    Math.floor(Date.now() / 1000),
  );
  if (session === null) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }
  const encoder = new TextEncoder();
  let timer: ReturnType<typeof setInterval> | undefined;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      let tick = 0;
      controller.enqueue(encoder.encode(encodeSseEvent("status", fixtureStatus(tick))));
      timer = setInterval(() => {
        tick += 1;
        try {
          controller.enqueue(encoder.encode(encodeSseEvent("status", fixtureStatus(tick))));
        } catch {
          clearInterval(timer);
        }
      }, TICK_MILLISECONDS);
    },
    cancel() {
      if (timer !== undefined) {
        clearInterval(timer);
      }
    },
  });
  return new Response(stream, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
    },
  });
}
