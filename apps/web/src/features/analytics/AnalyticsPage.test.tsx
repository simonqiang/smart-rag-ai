import { describe, expect, it } from "vitest";

import AnalyticsPage, { feedbackCoverage } from "./AnalyticsPage";

const EMPTY = {
  exchanges: 0,
  answered: 0,
  insufficient_evidence: 0,
  unanswered_conversations: 0,
  helpful: 0,
  unhelpful: 0,
};

describe("AnalyticsPage", () => {
  it("exposes a renderable root component", () => {
    expect(typeof AnalyticsPage).toBe("function");
  });
});

describe("feedbackCoverage", () => {
  it("reports a dash before any exchange exists", () => {
    expect(feedbackCoverage(EMPTY)).toBe("—");
  });

  it("rounds flagged exchanges over total exchanges", () => {
    expect(feedbackCoverage({ ...EMPTY, exchanges: 3, helpful: 1, unhelpful: 1 })).toBe(
      "67%",
    );
  });

  it("reaches 100% when every exchange is flagged", () => {
    expect(feedbackCoverage({ ...EMPTY, exchanges: 2, unhelpful: 2 })).toBe("100%");
  });
});
