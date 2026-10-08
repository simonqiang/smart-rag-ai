import { useEffect, useState } from "react";

import {
  answerSegments,
  type Citation,
  type StoredMessage,
} from "./contracts";
import ConversationList, { type ConversationDetail } from "./ConversationList";
import EvidencePanel from "./EvidencePanel";
import { sendFeedback, statusLabel, useAsk } from "./useAsk";
import { getJson } from "../auth/api";

const pageStyle = { maxWidth: "64rem", margin: "0 auto", padding: "1rem" } as const;
const layoutStyle = { display: "flex", flexWrap: "wrap", gap: "1rem" } as const;
const mainStyle = { flex: "2 1 24rem", minWidth: "0" } as const;
const asideStyle = { flex: "1 1 16rem", minWidth: "0" } as const;
const inputRowStyle = { display: "flex", gap: "0.5rem", flexWrap: "wrap" } as const;
const questionStyle = { flex: "1 1 16rem", minWidth: "0", padding: "0.65rem" } as const;
const buttonStyle = { padding: "0.65rem 0.9rem", minHeight: "44px" } as const;
const answerStyle = {
  border: "1px solid #ccc",
  borderRadius: "4px",
  padding: "0.75rem",
  marginTop: "0.75rem",
  whiteSpace: "pre-wrap",
} as const;
const badgeStyle = {
  display: "inline-block",
  border: "1px solid #ccc",
  borderRadius: "999px",
  padding: "0.2rem 0.7rem",
  fontSize: "0.85rem",
  marginTop: "0.5rem",
} as const;
const markerStyle = {
  margin: "0 0.15rem",
  padding: "0.1rem 0.45rem",
  border: "1px solid #0b57d0",
  borderRadius: "4px",
  color: "#0b57d0",
  background: "none",
  cursor: "pointer",
} as const;
const historyStyle = { listStyle: "none", padding: 0 } as const;
const historyRowStyle = {
  border: "1px solid #ddd",
  borderRadius: "4px",
  padding: "0.6rem",
  marginBottom: "0.5rem",
} as const;

type Source = { source_id: string; name: string };

const LOCAL_PROVIDER = { name: "ollama", local: true } as const; // Task 23 swaps profiles

function providerText(provider: { name: string; local: boolean } | null): string {
  if (!provider) return "";
  return provider.local
    ? `Local model (${provider.name}) — documents and answers stay on this PC`
    : `Hosted model (${provider.name}) — questions and evidence leave this PC`;
}

function MessageBody({
  content,
  citations,
  onCitation,
}: {
  content: string;
  citations: Citation[];
  onCitation: (citation: Citation) => void;
}) {
  const byLabel = new Map(citations.map((citation) => [citation.label, citation]));
  return (
    <>
      {answerSegments(content).map((segment, index) =>
        segment.kind === "text" ? (
          <span key={index}>{segment.text}</span>
        ) : (
          <button
            key={index}
            type="button"
            style={markerStyle}
            aria-label={`Open citation ${segment.label}`}
            onClick={() => {
              const citation = byLabel.get(segment.label);
              if (citation) onCitation(citation);
            }}
          >
            [{segment.label}]
          </button>
        ),
      )}
    </>
  );
}

/** The Ask workflow: question, streamed cited answer, evidence, conversations. */
export default function AskPage() {
  const [question, setQuestion] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [openCitation, setOpenCitation] = useState<Citation | null>(null);
  const [feedbackGiven, setFeedbackGiven] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [history, setHistory] = useState<StoredMessage[] | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [provider, setProvider] = useState<{ name: string; local: boolean } | null>(null);
  const { state, ask, cancel, reset } = useAsk(() => setEpoch((value) => value + 1));

  useEffect(() => {
    void getJson("/api/sources").then((result) => {
      if (result.ok) setSources(result.payload as unknown as Source[]);
    });
  }, []);

  const busy = state.status === "streaming";

  function stopAndCancel() {
    // Defer past this click's default action: flipping type=button to
    // type=submit inside the handler makes the browser submit the form.
    setTimeout(cancel, 0);
  }

  function runAsk() {
    if (!question.trim() || busy) return;
    setOpenCitation(null);
    setFeedbackGiven(false);
    setHistory(null);
    setProvider(LOCAL_PROVIDER);
    ask(question.trim(), activeConversationId, sourceId || null);
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    runAsk();
  }

  function startNewConversation() {
    reset();
    setActiveConversationId(null);
    setHistory(null);
    setOpenCitation(null);
    setFeedbackGiven(false);
  }

  function reopen(detail: ConversationDetail) {
    reset();
    setActiveConversationId(detail.id);
    setHistory(detail.messages);
    setOpenCitation(null);
    setFeedbackGiven(false);
  }

  const answer = state.answerText || state.rawText;

  return (
    <main style={pageStyle}>
      <h1>Ask</h1>
      <div style={layoutStyle}>
        <div style={mainStyle}>
          <form onSubmit={submit} style={inputRowStyle}>
            <label style={{ flex: "1 1 100%" }} htmlFor="question">
              Your question
            </label>
            <input
              id="question"
              name="question"
              style={questionStyle}
              value={question}
              placeholder="Ask about your indexed documents…"
              onChange={(event) => setQuestion(event.target.value)}
            />
            <select
              aria-label="Filter sources"
              value={sourceId}
              onChange={(event) => setSourceId(event.target.value)}
              style={buttonStyle}
            >
              <option value="">All sources</option>
              {sources.map((source) => (
                <option key={source.source_id} value={source.source_id}>
                  {source.name}
                </option>
              ))}
            </select>
            {/* One stable control: swapping buttons under a click re-targets it. */}
            <button
              type={busy ? "button" : "submit"}
              style={buttonStyle}
              onClick={busy ? stopAndCancel : undefined}
              disabled={!busy && !question.trim()}
            >
              {busy ? "Stop" : "Ask"}
            </button>
            <button type="button" style={buttonStyle} onClick={startNewConversation}>
              New conversation
            </button>
          </form>

          <div aria-live="polite">
            {state.status === "idle" && !history && (
              <p>Ask a question and a cited answer will appear here.</p>
            )}
            {statusLabel(state.status) && <p role="status">{statusLabel(state.status)}</p>}
            {state.status === "error" && (
              <p role="alert">
                {state.error}{" "}
                <button type="button" onClick={runAsk}>
                  Retry
                </button>
              </p>
            )}
            {answer && (
              <section style={answerStyle} lang={state.language} aria-label="Answer">
                <MessageBody
                  content={answer}
                  citations={state.citations}
                  onCitation={setOpenCitation}
                />
              </section>
            )}
            {state.status === "insufficient" && (
              <p role="status">
                Not enough indexed evidence. Try a different question or add sources.
              </p>
            )}
          </div>

          {(state.status === "done" || state.status === "insufficient") && (
            <>
              <p style={badgeStyle}>{providerText(provider)}</p>
              {state.status === "done" && (
                <p>
                  {feedbackGiven ? (
                    <span role="status">Thanks for the feedback.</span>
                  ) : (
                    <>
                      <button
                        type="button"
                        style={buttonStyle}
                        onClick={() => {
                          setFeedbackGiven(true);
                          void sendFeedback(state.requestId ?? "", true);
                        }}
                      >
                        Helpful
                      </button>{" "}
                      <button
                        type="button"
                        style={buttonStyle}
                        onClick={() => {
                          setFeedbackGiven(true);
                          void sendFeedback(state.requestId ?? "", false);
                        }}
                      >
                        Not helpful
                      </button>
                    </>
                  )}
                </p>
              )}
            </>
          )}

          <EvidencePanel
            citation={openCitation}
            onClose={() => setOpenCitation(null)}
          />

          {history && (
            <section aria-label="Conversation history">
              <h2>Conversation history</h2>
              <ol style={historyStyle}>
                {history.map((message, index) => (
                  <li key={index} style={historyRowStyle}>
                    <strong>{message.role === "user" ? "You" : "Assistant"}</strong>{" "}
                    <span lang={message.language ?? undefined}>
                      <MessageBody
                        content={message.content}
                        citations={message.citations ?? []}
                        onCitation={setOpenCitation}
                      />
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          )}
        </div>
        <aside style={asideStyle}>
          <ConversationList
            epoch={epoch}
            onReopen={reopen}
            onDeleted={(id) => {
              if (id === activeConversationId) startNewConversation();
            }}
          />
        </aside>
      </div>
    </main>
  );
}
