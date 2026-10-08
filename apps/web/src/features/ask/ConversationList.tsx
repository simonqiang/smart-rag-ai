import { useCallback, useEffect, useState } from "react";

import type { ConversationDetail, ConversationSummary } from "./contracts";
import { deleteJson, getJson, putJson } from "../auth/api";

export type { ConversationDetail };

const rowStyle = {
  border: "1px solid #ddd",
  borderRadius: "4px",
  padding: "0.5rem",
  marginBottom: "0.5rem",
} as const;
const rowActions = { display: "flex", gap: "0.5rem", flexWrap: "wrap" } as const;
const buttonStyle = { padding: "0.4rem 0.6rem", minHeight: "44px" } as const;

export type { ConversationSummary };

/** Own conversations: reopen, rename, delete (two-step confirm, no hover-only). */
export default function ConversationList({
  epoch,
  onReopen,
  onDeleted,
}: {
  epoch: number;
  onReopen: (detail: ConversationDetail) => void;
  onDeleted: (id: string) => void;
}) {
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    const result = await getJson("/api/conversations");
    if (!result.ok) {
      setError("conversations are unavailable; try again shortly");
      return;
    }
    setConversations(result.payload as unknown as ConversationSummary[]);
  }, []);

  useEffect(() => {
    void load();
  }, [load, epoch]);

  async function reopen(id: string) {
    const result = await getJson(`/api/conversations/${id}`);
    if (!result.ok) {
      setError("this conversation is unavailable");
      return;
    }
    onReopen(result.payload as unknown as ConversationDetail);
  }

  async function rename(id: string) {
    await putJson(`/api/conversations/${id}`, { title: draftTitle });
    setRenaming(null);
    void load();
  }

  async function remove(id: string) {
    await deleteJson(`/api/conversations/${id}`);
    setConfirming(null);
    onDeleted(id);
    void load();
  }

  return (
    <section aria-label="Conversations">
      <h2>Conversations</h2>
      {error && (
        <p role="alert">
          {error} <button type="button" onClick={() => void load()}>Retry</button>
        </p>
      )}
      {conversations === null && !error && <p role="status">Loading conversations…</p>}
      {conversations !== null && conversations.length === 0 && (
        <p>No conversations yet. Your questions will appear here.</p>
      )}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {conversations?.map((conversation) => (
          <li key={conversation.id} style={rowStyle}>
            {renaming === conversation.id ? (
              <span style={rowActions}>
                <input
                  aria-label="Conversation title"
                  value={draftTitle}
                  onChange={(event) => setDraftTitle(event.target.value)}
                  style={{ flex: "1 1 8rem", minWidth: 0 }}
                />
                <button
                  type="button"
                  style={buttonStyle}
                  onClick={() => void rename(conversation.id)}
                >
                  Save title
                </button>
                <button
                  type="button"
                  style={buttonStyle}
                  onClick={() => setRenaming(null)}
                >
                  Cancel
                </button>
              </span>
            ) : confirming === conversation.id ? (
              <span style={rowActions}>
                <span role="status">Delete this conversation?</span>
                <button
                  type="button"
                  style={buttonStyle}
                  onClick={() => void remove(conversation.id)}
                >
                  Yes, delete
                </button>
                <button
                  type="button"
                  style={buttonStyle}
                  onClick={() => setConfirming(null)}
                >
                  Keep
                </button>
              </span>
            ) : (
              <>
                <button
                  type="button"
                  style={{ ...buttonStyle, textDecoration: "underline", border: "none" }}
                  onClick={() => void reopen(conversation.id)}
                >
                  {conversation.title}
                </button>
                <span style={rowActions}>
                  <button
                    type="button"
                    style={buttonStyle}
                    onClick={() => {
                      setRenaming(conversation.id);
                      setDraftTitle(conversation.title);
                    }}
                  >
                    Rename
                  </button>
                  <button
                    type="button"
                    style={buttonStyle}
                    onClick={() => setConfirming(conversation.id)}
                  >
                    Delete
                  </button>
                </span>
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
