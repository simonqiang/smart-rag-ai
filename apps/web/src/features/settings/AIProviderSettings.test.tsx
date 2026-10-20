import { describe, expect, it } from "vitest";

import AIProviderSettings, { formatTime, stateSummary } from "./AIProviderSettings";

const state = {
  provider: "ollama",
  endpoint: null,
  model: null,
  rewrite_enabled: false,
  consent_version: null,
  consented_at: null,
  consent_version_current: "2026-10-20",
};

describe("AIProviderSettings", () => {
  it("exposes a renderable root component", () => {
    expect(typeof AIProviderSettings).toBe("function");
  });
});

describe("stateSummary", () => {
  it("describes the local default", () => {
    expect(stateSummary(state)).toContain("locally by Ollama");
  });

  it("describes the hosted provider with its accepted consent", () => {
    const glm = {
      ...state,
      provider: "glm",
      endpoint: "https://glm.example/v1",
      model: "glm-4",
      rewrite_enabled: true,
      consent_version: "2026-10-20",
      consented_at: "2026-10-21T08:05:00+00:00",
    };
    const summary = stateSummary(glm);
    expect(summary).toContain("GLM at https://glm.example/v1 using glm-4");
    expect(summary).toContain("rewriting on");
    expect(summary).toContain("Consent version 2026-10-20 accepted 2026-10-21 08:05");
  });
});

describe("formatTime", () => {
  it("trims ISO timestamps to minutes and renders null as a dash", () => {
    expect(formatTime("2026-10-21T08:05:00+00:00")).toBe("2026-10-21 08:05");
    expect(formatTime(null)).toBe("—");
  });
});
