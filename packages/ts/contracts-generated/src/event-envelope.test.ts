import { describe, expect, it } from "vitest";

import {
  EVENT_ENVELOPE_REQUIRED_FIELDS,
  EventEnvelope,
  isEventEnvelope,
} from "./event-envelope";

const validEnvelope = (): Record<string, unknown> => ({
  event_id: "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
  type: "MESSAGE_CREATED",
  version: "1.0",
  occurred_at: "2026-07-05T10:00:00+00:00",
  observed_at: "2026-07-05T10:00:01+00:00",
  available_at: "2026-07-05T10:00:02Z",
  ingested_at: "2026-07-05T10:00:03.250Z",
  correlation_id: "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c",
});

describe("event envelope (generated)", () => {
  it("accepts a valid envelope, with and without causation", () => {
    expect(isEventEnvelope(validEnvelope())).toBe(true);
    expect(
      isEventEnvelope({
        ...validEnvelope(),
        causation_id: "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d",
      }),
    ).toBe(true);
    expect(isEventEnvelope({ ...validEnvelope(), causation_id: null })).toBe(true);
  });

  it("rejects an envelope missing any required field", () => {
    for (const field of EVENT_ENVELOPE_REQUIRED_FIELDS) {
      const broken = validEnvelope();
      delete broken[field];
      expect(isEventEnvelope(broken), `missing ${field}`).toBe(false);
    }
  });

  it("rejects null in a required field", () => {
    expect(isEventEnvelope({ ...validEnvelope(), ingested_at: null })).toBe(false);
  });

  it("rejects a timestamp without timezone information", () => {
    expect(
      isEventEnvelope({ ...validEnvelope(), occurred_at: "2026-07-05T10:00:00" }),
    ).toBe(false);
  });

  it("rejects malformed identifiers and versions", () => {
    expect(isEventEnvelope({ ...validEnvelope(), event_id: "not-a-uuid" })).toBe(false);
    expect(isEventEnvelope({ ...validEnvelope(), version: "v1" })).toBe(false);
    expect(isEventEnvelope({ ...validEnvelope(), type: "message_created" })).toBe(false);
  });

  it("rejects unknown fields (strict contract)", () => {
    expect(isEventEnvelope({ ...validEnvelope(), convenient: true })).toBe(false);
    expect(isEventEnvelope(null)).toBe(false);
    expect(isEventEnvelope([])).toBe(false);
  });

  it("exposes the required field list matching the schema", () => {
    expect([...EVENT_ENVELOPE_REQUIRED_FIELDS]).toEqual([
      "event_id",
      "type",
      "version",
      "occurred_at",
      "observed_at",
      "available_at",
      "ingested_at",
      "correlation_id",
    ]);
  });

  it("rejects a structurally incomplete envelope at type level", () => {
    // @ts-expect-error missing required envelope fields is a compile error
    const incomplete: EventEnvelope = { event_id: "only-an-id" };
    expect(isEventEnvelope(incomplete)).toBe(false);
  });
});
