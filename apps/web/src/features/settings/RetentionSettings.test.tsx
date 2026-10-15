import { describe, expect, it } from "vitest";

import RetentionSettings, { toDays } from "./RetentionSettings";

describe("RetentionSettings", () => {
  it("exposes a renderable root component", () => {
    expect(typeof RetentionSettings).toBe("function");
  });
});

describe("toDays", () => {
  it("accepts positive whole days", () => {
    expect(toDays("30")).toBe(30);
    expect(toDays("1")).toBe(1);
  });

  it("rejects zero, negatives, and non-integers", () => {
    expect(toDays("0")).toBeNull();
    expect(toDays("-3")).toBeNull();
    expect(toDays("1.5")).toBeNull();
  });

  it("rejects blank and non-numeric input", () => {
    expect(toDays("")).toBeNull();
    expect(toDays("soon")).toBeNull();
  });
});
