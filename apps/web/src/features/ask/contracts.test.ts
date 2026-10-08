import { describe, expect, it } from "vitest";

import {
  answerSegments,
  feedAnswerFrames,
  initialAskState,
  parseSse,
  type AnswerStreamFrame,
} from "./contracts";

const started: AnswerStreamFrame = {
  kind: "started",
  request_id: "r1",
  seq: 1,
  language: "zh-Hans",
};
const completed: AnswerStreamFrame = {
  kind: "completed",
  request_id: "r1",
  seq: 4,
  text: "答案 [1]",
  insufficient_evidence: false,
};

describe("parseSse", () => {
  it("splits complete data frames and buffers the tail", () => {
    const [frames, rest] = parseSse(
      'data: {"kind":"started"}\n\ndata: {"kind":"del',
    );
    expect(frames).toEqual([{ kind: "started" }]);
    expect(rest).toBe('data: {"kind":"del');
  });

  it("keeps the buffer across calls", () => {
    let buffer = "";
    let frames: unknown[] = [];
    [frames, buffer] = parseSse(buffer + 'data: {"kind":"sta');
    expect(frames).toEqual([]);
    [frames, buffer] = parseSse(buffer + 'rted"}\n\n');
    expect(frames).toEqual([{ kind: "started" }]);
  });
});

describe("feedAnswerFrames", () => {
  it("accumulates deltas, citations, and the completed text", () => {
    let state = initialAskState;
    state = feedAnswerFrames(state, started);
    state = feedAnswerFrames(state, {
      kind: "delta",
      request_id: "r1",
      seq: 2,
      text: "答案 ",
    });
    state = feedAnswerFrames(state, {
      kind: "citation",
      request_id: "r1",
      seq: 3,
      citation: {
        label: 1,
        chunk_id: "c1",
        source_id: "s1",
        source_name: "handbook",
        page: 2,
        block_start: 0,
        block_end: 1,
        quote: "原文引用",
      },
    });
    state = feedAnswerFrames(state, completed);

    expect(state.status).toBe("done");
    expect(state.language).toBe("zh-Hans");
    expect(state.rawText).toBe("答案 ");
    expect(state.citations).toHaveLength(1);
    expect(state.answerText).toBe("答案 [1]");
    expect(state.insufficient).toBe(false);
  });

  it("marks insufficient evidence without citations", () => {
    let state = feedAnswerFrames(initialAskState, {
      kind: "started",
      request_id: "r1",
      seq: 1,
      language: "en",
    });
    state = feedAnswerFrames(state, {
      kind: "delta",
      request_id: "r1",
      seq: 2,
      text: "not enough evidence",
    });
    state = feedAnswerFrames(state, {
      kind: "completed",
      request_id: "r1",
      seq: 3,
      text: "not enough evidence",
      insufficient_evidence: true,
    });

    expect(state.status).toBe("insufficient");
    expect(state.insufficient).toBe(true);
    expect(state.citations).toHaveLength(0);
  });

  it("surfaces typed error frames with their safe message", () => {
    let state = feedAnswerFrames(initialAskState, started);
    state = feedAnswerFrames(state, {
      kind: "error",
      request_id: "r1",
      seq: 2,
      reason: "timeout",
      message: "the model host timed out; try again",
    });

    expect(state.status).toBe("error");
    expect(state.error).toBe("the model host timed out; try again");
  });

  it("renders a cancelled state when the stream is aborted", () => {
    const state = feedAnswerFrames(
      feedAnswerFrames(initialAskState, started),
      { kind: "cancelled", request_id: "r1", seq: 9 },
    );
    expect(state.status).toBe("cancelled");
  });
});

describe("answerSegments", () => {
  it("splits display text into prose and citation markers", () => {
    expect(answerSegments("plain answer")).toEqual([
      { kind: "text", text: "plain answer" },
    ]);
    expect(answerSegments("前半 [1] 后半 [2]")).toEqual([
      { kind: "text", text: "前半 " },
      { kind: "citation", label: 1 },
      { kind: "text", text: " 后半 " },
      { kind: "citation", label: 2 },
    ]);
  });
});
