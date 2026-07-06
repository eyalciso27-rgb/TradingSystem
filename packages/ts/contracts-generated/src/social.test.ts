import { describe, expect, it } from "vitest";

import {
  CanonicalSocialMessage,
  isCanonicalSocialMessage,
  isSocialAttachment,
  isSocialConnectionProfile,
  isSocialMessageObservation,
  SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE,
  SocialConnectionProfile,
} from "./social";

const CONTENT_HASH = "sha256:" + "a".repeat(64);

const validProfile = (): Record<string, unknown> => ({
  provider: "TELEGRAM",
  access_mode: "TELEGRAM_USER_TDLIB",
  identity_ref: "ref-1",
  required_capability: true,
  enabled: true,
  health_status: "HEALTHY",
});

const validMessage = (): Record<string, unknown> => ({
  source_type: "WHATSAPP",
  channel_external_id: "group-1",
  external_message_id: "wamid.ABC",
  content_hash: CONTENT_HASH,
  dedup_key: CONTENT_HASH,
});

const validObservation = (): Record<string, unknown> => ({
  source_type: "TELEGRAM",
  access_mode: "TELEGRAM_BOT",
  connector_id: "conn-1",
  source_account_id: "srcacct-1",
  channel_external_id: "-100999",
  received_at: "2026-07-05T10:00:00+00:00",
  message_dedup_key: CONTENT_HASH,
});

describe("social contracts (generated)", () => {
  it("accepts each valid access-mode/provider combination", () => {
    for (const [mode, provider] of Object.entries(SOURCE_TYPE_BY_SOCIAL_ACCESS_MODE)) {
      expect(
        isSocialConnectionProfile({ ...validProfile(), access_mode: mode, provider }),
        `${mode}/${provider}`,
      ).toBe(true);
    }
  });

  it("rejects illegal access-mode/provider combinations", () => {
    expect(
      isSocialConnectionProfile({
        ...validProfile(),
        access_mode: "WHATSAPP_USER",
        provider: "TELEGRAM",
      }),
    ).toBe(false);
    expect(
      isSocialConnectionProfile({
        ...validProfile(),
        access_mode: "TELEGRAM_BOT",
        provider: "DISCORD",
      }),
    ).toBe(false);
  });

  it("validates canonical messages and rejects non-social sources", () => {
    expect(isCanonicalSocialMessage(validMessage())).toBe(true);
    expect(isCanonicalSocialMessage({ ...validMessage(), source_type: "NEWS" })).toBe(false);
    expect(isCanonicalSocialMessage({ ...validMessage(), source_type: "MYSPACE" })).toBe(false);
  });

  it("rejects missing required fields and null in required fields", () => {
    const missing = validMessage();
    delete missing.dedup_key;
    expect(isCanonicalSocialMessage(missing)).toBe(false);
    expect(isCanonicalSocialMessage({ ...validMessage(), channel_external_id: null })).toBe(false);
  });

  it("rejects extra (e.g. credential-looking) fields", () => {
    expect(isCanonicalSocialMessage({ ...validMessage(), session_token: "x" })).toBe(false);
    expect(isSocialConnectionProfile({ ...validProfile(), cookie: "x" })).toBe(false);
  });

  it("rejects malformed hashes and non-timezone datetimes", () => {
    expect(isCanonicalSocialMessage({ ...validMessage(), dedup_key: "not-a-hash" })).toBe(false);
    expect(
      isSocialMessageObservation({ ...validObservation(), received_at: "2026-07-05T10:00:00" }),
    ).toBe(false);
  });

  it("validates observations in user and bot modes with correct source", () => {
    expect(isSocialMessageObservation(validObservation())).toBe(true);
    expect(
      isSocialMessageObservation({ ...validObservation(), access_mode: "WHATSAPP_USER" }),
    ).toBe(false);
  });

  it("validates attachment references and rejects raw byte fields", () => {
    expect(isSocialAttachment({ media_type: "image/png" })).toBe(true);
    expect(isSocialAttachment({ media_type: "image/png", raw_bytes: "AAAA" })).toBe(false);
  });

  it("rejects an incomplete profile at type level", () => {
    // @ts-expect-error missing required profile fields is a compile error
    const incomplete: SocialConnectionProfile = { provider: "TELEGRAM" };
    expect(isSocialConnectionProfile(incomplete)).toBe(false);
  });

  it("rejects an invalid literal for an enum field at type level", () => {
    const message: CanonicalSocialMessage = {
      source_type: "WHATSAPP",
      channel_external_id: "g1",
      content_hash: CONTENT_HASH,
      dedup_key: CONTENT_HASH,
      // @ts-expect-error revision must be a number
      revision: "1",
    };
    expect(isCanonicalSocialMessage(message)).toBe(false);
  });
});
