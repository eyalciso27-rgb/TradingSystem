/**
 * Canonical client-facing API paths (single source of truth).
 *
 * The dashboard EventSource and the SSE route live at the same URL; both
 * derive it from here so they can never drift apart again. (A Next.js
 * `route.ts` file may not export non-handler symbols, so the constant lives
 * in this plain module rather than in the route file.)
 */

export const STATUS_STREAM_PATH = "/api/session/events";
