import { describe, expect, it } from "vitest";

import WebSyncPanel, {
  formatTime,
  intervalSeconds,
  parsePrefixes,
  stateChip,
} from "./WebSyncPanel";

describe("WebSyncPanel", () => {
  it("exposes a renderable root component", () => {
    expect(typeof WebSyncPanel).toBe("function");
  });
});

describe("parsePrefixes", () => {
  it("trims lines and drops blanks", () => {
    expect(parsePrefixes("/docs/\n  /blog/ \n\n")).toEqual(["/docs/", "/blog/"]);
  });

  it("returns empty for blank input", () => {
    expect(parsePrefixes("")).toEqual([]);
  });
});

describe("intervalSeconds", () => {
  it("accepts whole minutes at or above the minimum", () => {
    expect(intervalSeconds("5")).toBe(300);
    expect(intervalSeconds("1440")).toBe(86_400);
  });

  it("rejects below minimum, fractional, and non-numeric input", () => {
    expect(intervalSeconds("4")).toBeNull();
    expect(intervalSeconds("1.5")).toBeNull();
    expect(intervalSeconds("")).toBeNull();
    expect(intervalSeconds("daily")).toBeNull();
  });
});

describe("formatTime", () => {
  it("trims ISO stamps to minute precision without locale drift", () => {
    expect(formatTime("2026-10-20T09:30:00+00:00")).toBe("2026-10-20 09:30");
  });

  it("renders a dash for never-synced", () => {
    expect(formatTime(null)).toBe("—");
  });
});

describe("stateChip", () => {
  it("prefers paused over the freshness state", () => {
    expect(stateChip({ enabled: false, last_state: "synced" })).toBe("paused");
  });

  it("falls back to never synced before the first attempt", () => {
    expect(stateChip({ enabled: true, last_state: null })).toBe("never synced");
  });

  it("shows the recorded freshness state", () => {
    expect(stateChip({ enabled: true, last_state: "failed" })).toBe("failed");
  });
});
