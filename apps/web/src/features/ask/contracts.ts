/** Ask API and stream contracts (Task 15a).

The plan's ``packages/contracts`` package does not exist in this scaffold;
these types live with the feature they describe and are re-exported where
needed. ``parseSse`` and ``feedAnswerFrames`` are the pure core of the
streaming hook; ``useAsk`` wires them to fetch and React state.
*/

export type Citation = {
  label: number;
  chunk_id: string;
  source_id: string;
  source_name: string;
  page: number | null;
  block_start: number;
  block_end: number;
  quote: string;
};

export type AnswerStreamFrame = {
  kind: "started" | "delta" | "citation" | "completed" | "error" | "cancelled";
  request_id: string;
  seq: number;
  text?: string;
  citation?: Citation;
  reason?: string;
  message?: string;
  language?: string;
  insufficient_evidence?: boolean;
  model?: string;
  conversation_id?: string;
  provider?: { name: string; local: boolean };
};

export type AskResponse = {
  request_id: string;
  conversation_id: string;
  question_language: string;
  answer: { text: string; citations: Citation[] };
  insufficient_evidence: boolean;
  provider: { name: string; local: boolean };
};

export type ConversationSummary = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type StoredMessage = {
  role: string;
  content: string;
  citations: Citation[];
  language: string | null;
  request_id: string | null;
  insufficient: boolean;
  created_at: string;
};

export type ConversationDetail = ConversationSummary & {
  messages: StoredMessage[];
};

export type AskStatus =
  | "idle"
  | "streaming"
  | "done"
  | "insufficient"
  | "error"
  | "cancelled";

export type AskState = {
  status: AskStatus;
  language: string;
  rawText: string;
  answerText: string;
  citations: Citation[];
  insufficient: boolean;
  error: string | null;
  conversationId: string | null;
  requestId: string | null;
};

export const initialAskState: AskState = {
  status: "idle",
  language: "en",
  rawText: "",
  answerText: "",
  citations: [],
  insufficient: false,
  error: null,
  conversationId: null,
  requestId: null,
};

/** Split an SSE byte-decoded chunk into complete JSON frames plus the tail. */
export function parseSse(buffer: string): [frames: AnswerStreamFrame[], rest: string] {
  const frames: AnswerStreamFrame[] = [];
  let rest = buffer;
  while (true) {
    const boundary = rest.indexOf("\n\n");
    if (boundary === -1) break;
    const raw = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);
    const line = raw.split("\n").find((part) => part.startsWith("data: "));
    if (line) frames.push(JSON.parse(line.slice("data: ".length)) as AnswerStreamFrame);
  }
  return [frames, rest];
}

const MARKER = /\[(\d+)\]/g;

/** Display text with `[N]` markers split into prose and citation segments. */
export function answerSegments(
  text: string,
): Array<{ kind: "text"; text: string } | { kind: "citation"; label: number }> {
  const segments: Array<
    { kind: "text"; text: string } | { kind: "citation"; label: number }
  > = [];
  let cursor = 0;
  for (const match of text.matchAll(MARKER)) {
    const index = match.index ?? 0;
    if (index > cursor) segments.push({ kind: "text", text: text.slice(cursor, index) });
    segments.push({ kind: "citation", label: Number(match[1]) });
    cursor = index + match[0].length;
  }
  if (cursor < text.length) segments.push({ kind: "text", text: text.slice(cursor) });
  return segments;
}

/** Fold one stream frame into the running ask state. */
export function feedAnswerFrames(state: AskState, frame: AnswerStreamFrame): AskState {
  const identified = {
    ...state,
    requestId: frame.request_id,
    conversationId: frame.conversation_id ?? state.conversationId,
  };
  switch (frame.kind) {
    case "started":
      return { ...identified, status: "streaming", language: frame.language ?? "en" };
    case "delta":
      return { ...identified, rawText: identified.rawText + (frame.text ?? "") };
    case "citation":
      return frame.citation
        ? { ...identified, citations: [...identified.citations, frame.citation] }
        : identified;
    case "completed":
      return {
        ...identified,
        status: frame.insufficient_evidence ? "insufficient" : "done",
        answerText: frame.text ?? identified.rawText,
        insufficient: frame.insufficient_evidence ?? false,
      };
    case "error":
      return {
        ...identified,
        status: "error",
        error: frame.message ?? "answering failed",
      };
    case "cancelled":
      return { ...identified, status: "cancelled" };
    default:
      return identified;
  }
}
