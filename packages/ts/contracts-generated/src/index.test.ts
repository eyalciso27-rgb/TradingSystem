import { describe, expect, it } from "vitest";

import { packageName } from "./index";

describe("skeleton", () => {
  it("exposes its package name", () => {
    expect(packageName).toBe("@trading-system/contracts-generated");
  });
});
