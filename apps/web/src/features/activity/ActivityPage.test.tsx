import { describe, expect, it } from "vitest";

import ActivityPage, { canRetry, checkChip, formatTime } from "./ActivityPage";

describe("ActivityPage", () => {
  it("exposes a renderable root component", () => {
    expect(typeof ActivityPage).toBe("function");
  });
});

describe("canRetry", () => {
  it("allows exactly the failed state", () => {
    expect(canRetry("failed")).toBe(true);
    expect(canRetry("queued")).toBe(false);
    expect(canRetry("retrying")).toBe(false);
    expect(canRetry("running")).toBe(false);
    expect(canRetry("stalled")).toBe(false);
    expect(canRetry("completed")).toBe(false);
  });
});

describe("checkChip", () => {
  it("renders warnings and failures distinctly from ok", () => {
    expect(checkChip("ok")).toBe("ok");
    expect(checkChip("warn")).toBe("warning");
    expect(checkChip("fail")).toBe("fail");
  });
});

describe("formatTime", () => {
  it("trims ISO stamps to minute precision", () => {
    expect(formatTime("2026-10-20T09:30:00+00:00")).toBe("2026-10-20 09:30");
  });

  it("renders a dash for missing stamps", () => {
    expect(formatTime(null)).toBe("—");
  });
});
