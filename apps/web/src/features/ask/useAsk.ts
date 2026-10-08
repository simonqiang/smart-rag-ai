import { useCallback, useRef, useState } from "react";

import {
  feedAnswerFrames,
  initialAskState,
  parseSse,
  type AskState,
  type AskStatus,
} from "./contracts";

/** Fire-and-forget feedback; the endpoint arrives with Task 25. */
export async function sendFeedback(requestId: string, helpful: boolean): Promise<void> {
  try {
    await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, helpful }),
    });
  } catch {
    // Feedback is best-effort until Task 25 defines the endpoint.
  }
}

export type Ask = {
  state: AskState;
  ask: (question: string, conversationId?: string | null, sourceId?: string | null) => void;
  cancel: () => void;
  reset: () => void;
};

/** Read one SSE response into the running ask state. */
async function consume(
  response: Response,
  conversationId: string | null,
  onFrames: (frames: AskState) => void,
): Promise<AskState> {
  const reader = response.body!.getReader(); // callers check ok/body before consuming
  const decoder = new TextDecoder();
  let buffer = "";
  let finished: AskState = { ...initialAskState, conversationId };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const [frames, rest] = parseSse(buffer + decoder.decode(value, { stream: true }));
    buffer = rest;
    if (frames.length === 0) continue;
    finished = frames.reduce(feedAnswerFrames, finished);
    onFrames(finished);
  }
  return finished;
}

/** Stream one grounded answer through the /api/ask SSE protocol. */
export function useAsk(onDone?: (state: AskState) => void): Ask {
  const [state, setState] = useState<AskState>(initialAskState);
  const controller = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    controller.current?.abort();
  }, []);

  const ask = useCallback(
    (question: string, conversationId?: string | null, sourceId?: string | null) => {
      controller.current?.abort();
      const local = new AbortController();
      controller.current = local;
      setState({ ...initialAskState, status: "streaming" });

      void (async () => {
        try {
          const response = await fetch("/api/ask", {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
            body: JSON.stringify({
              question,
              conversation_id: conversationId ?? null,
              source_id: sourceId ?? null,
            }),
            signal: local.signal,
          });
          if (!response.ok || !response.body) {
            const detail = await response.json().catch(() => null);
            const message =
              detail && typeof detail.detail === "string"
                ? detail.detail
                : "answering failed; try again";
            setState((current) => ({ ...current, status: "error", error: message }));
            return;
          }
          let finished = await consume(response, conversationId ?? null, setState);

          // A stream that ends mid-answer resumes from the stored frames once.
          if (finished.status === "streaming" && finished.requestId) {
            const replay = await fetch(`/api/ask/${finished.requestId}/events`);
            if (replay.ok && replay.body) {
              finished = await consume(replay, conversationId ?? null, setState);
            }
          }
          if (finished.status === "streaming") {
            finished = {
              ...finished,
              status: "error",
              error: "connection lost before the answer completed; try again",
            };
          }
          setState(finished);
          onDone?.(finished);
        } catch (error) {
          if ((error as Error).name === "AbortError") {
            setState((current) => ({ ...current, status: "cancelled" }));
          } else {
            setState((current) => ({
              ...current,
              status: "error",
              error: "answering failed; try again",
            }));
          }
        }
      })();
    },
    [onDone],
  );

  const reset = useCallback(() => {
    controller.current?.abort();
    setState(initialAskState);
  }, []);

  return { state, ask, cancel, reset };
}

export function statusLabel(status: AskStatus): string {
  switch (status) {
    case "streaming":
      return "Answering…";
    case "cancelled":
      return "Answer cancelled.";
    default:
      return "";
  }
}
