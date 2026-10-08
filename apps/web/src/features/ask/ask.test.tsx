import { describe, expect, it, vi } from "vitest";

import AskPage from "./AskPage";
import ConversationList from "./ConversationList";
import EvidencePanel from "./EvidencePanel";
import { statusLabel, sendFeedback, useAsk } from "./useAsk";

describe("Ask feature components", () => {
  it("exposes renderable root components", () => {
    expect(typeof AskPage).toBe("function");
    expect(typeof ConversationList).toBe("function");
    expect(typeof EvidencePanel).toBe("function");
    expect(typeof useAsk).toBe("function");
  });
});

describe("statusLabel", () => {
  it("names only the transient states", () => {
    expect(statusLabel("streaming")).toBe("Answering…");
    expect(statusLabel("cancelled")).toBe("Answer cancelled.");
    expect(statusLabel("done")).toBe("");
    expect(statusLabel("idle")).toBe("");
    expect(statusLabel("error")).toBe("");
    expect(statusLabel("insufficient")).toBe("");
  });
});

describe("sendFeedback", () => {
  it("survives a missing endpoint (Task 25 wires the API)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("network down"))),
    );
    await expect(sendFeedback("req-1", true)).resolves.toBeUndefined();
    vi.unstubAllGlobals();
  });

  it("posts the answer id and helpfulness", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(new Response(null, { status: 204 })),
    );
    vi.stubGlobal("fetch", fetchMock);
    await sendFeedback("req-9", false);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/feedback",
      expect.objectContaining({ method: "POST" }),
    );
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      request_id: "req-9",
      helpful: false,
    });
    vi.unstubAllGlobals();
  });
});
