import { useCallback, useEffect, useState } from "react";

import { type ApiResult, deleteJson, detailText, getJson, postJson, putJson } from "../auth/api";

const fieldStyle = { display: "block", width: "100%", marginBottom: "0.75rem", padding: "0.5rem" } as const;
const rowStyle = { border: "1px solid #ccc", borderRadius: "4px", padding: "0.75rem", marginBottom: "0.75rem" } as const;

type ManagedUser = {
  user_id: string;
  email: string;
  role: string;
  status: string;
  must_change_password: boolean;
  collections: string[];
};

type PendingInvitation = {
  invitation_id: string;
  email: string;
  role: string;
  collections: string[];
  expires_at: string;
};

/** Comma-separated free text -> clean collection id list (deduped, order kept). */
export function parseCollections(raw: string): string[] {
  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const id = part.trim();
    if (id) seen.add(id);
  }
  return [...seen];
}

export default function UserAccessPage() {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [invitations, setInvitations] = useState<PendingInvitation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin">("member");
  const [inviteCollections, setInviteCollections] = useState("");
  const [drafts, setDrafts] = useState<Record<string, { collections: string; password: string }>>({});

  const load = useCallback(async () => {
    setError(null);
    const [usersResult, invitationsResult] = await Promise.all([
      getJson("/api/users"),
      getJson("/api/users/invitations"),
    ]);
    if (!usersResult.ok || !invitationsResult.ok) {
      setError(detailText(usersResult.ok ? invitationsResult : usersResult));
      return;
    }
    const loadedUsers = usersResult.payload as unknown as ManagedUser[];
    setUsers(loadedUsers);
    setInvitations(invitationsResult.payload as unknown as PendingInvitation[]);
    setDrafts(
      Object.fromEntries(
        loadedUsers.map((user) => [
          user.user_id,
          { collections: user.collections.join(", "), password: "" },
        ]),
      ),
    );
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(action: () => Promise<ApiResult>, done: string): Promise<ApiResult> {
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await action();
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return result;
    }
    setNotice(done);
    await load();
    return result;
  }

  async function invite(event: React.FormEvent) {
    event.preventDefault();
    const result = await act(
      () =>
        postJson("/api/users/invitations", {
          email: inviteEmail,
          role: inviteRole,
          collections: parseCollections(inviteCollections),
        }),
      "invitation created",
    );
    if (result.ok) {
      setNotice(`invitation created; share this token with the invitee: ${String(result.payload.token)}`);
      setInviteEmail("");
      setInviteCollections("");
    }
  }

  if (error && users.length === 0) {
    return (
      <main style={{ maxWidth: "48rem", margin: "0 auto", padding: "1rem" }}>
        <h1>User access</h1>
        <p role="alert">{error}</p>
      </main>
    );
  }

  return (
    <main style={{ maxWidth: "48rem", margin: "0 auto", padding: "1rem" }}>
      <h1>User access</h1>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      <section aria-labelledby="invite-heading">
        <h2 id="invite-heading">Invite a user</h2>
        <form onSubmit={invite}>
          <label>
            Email
            <input
              style={fieldStyle}
              type="email"
              required
              value={inviteEmail}
              onChange={(event) => setInviteEmail(event.target.value)}
            />
          </label>
          <label>
            Role
            <select
              style={fieldStyle}
              value={inviteRole}
              onChange={(event) => setInviteRole(event.target.value as "member" | "admin")}
            >
              <option value="member">member</option>
              <option value="admin">admin</option>
            </select>
          </label>
          <label>
            Collections (comma-separated)
            <input
              style={fieldStyle}
              value={inviteCollections}
              onChange={(event) => setInviteCollections(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            Create invitation
          </button>
        </form>
      </section>

      {invitations.length > 0 && (
        <section aria-labelledby="pending-heading">
          <h2 id="pending-heading">Pending invitations</h2>
          {invitations.map((invitation) => (
            <div key={invitation.invitation_id} style={rowStyle}>
              {invitation.email} · {invitation.role} · {invitation.collections.join(", ") || "no collections"}
              <button
                type="button"
                disabled={busy}
                style={{ marginLeft: "0.5rem" }}
                onClick={() =>
                  act(
                    () => deleteJson(`/api/users/invitations/${invitation.invitation_id}`),
                    "invitation revoked",
                  )
                }
              >
                Revoke
              </button>
            </div>
          ))}
        </section>
      )}

      <section aria-labelledby="users-heading">
        <h2 id="users-heading">Users</h2>
        {users.map((user) => {
          const draft = drafts[user.user_id] ?? { collections: "", password: "" };
          const setDraft = (patch: Partial<typeof draft>) =>
            setDrafts((current) => ({ ...current, [user.user_id]: { ...draft, ...patch } }));
          return (
            <div key={user.user_id} style={rowStyle}>
              <strong>{user.email}</strong> · {user.role} · {user.status}
              {user.must_change_password && " · must change password"}
              <label>
                Collections (comma-separated)
                <input
                  style={fieldStyle}
                  value={draft.collections}
                  onChange={(event) => setDraft({ collections: event.target.value })}
                />
              </label>
              <label>
                New password (reset)
                <input
                  style={fieldStyle}
                  type="text"
                  autoComplete="off"
                  value={draft.password}
                  onChange={(event) => setDraft({ password: event.target.value })}
                />
              </label>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  act(
                    () =>
                      putJson(`/api/users/${user.user_id}/collections`, {
                        collections: parseCollections(draft.collections),
                      }),
                    "collections updated",
                  )
                }
              >
                Save collections
              </button>{" "}
              <button
                type="button"
                disabled={busy || draft.password.length === 0}
                onClick={() =>
                  act(
                    () =>
                      postJson(`/api/users/${user.user_id}/password-reset`, {
                        new_password: draft.password,
                      }),
                    "password reset; the user must change it at next sign-in",
                  ).then(() => setDraft({ collections: draft.collections, password: "" }))
                }
              >
                Reset password
              </button>{" "}
              <button
                type="button"
                disabled={busy || user.status !== "active"}
                onClick={() =>
                  act(() => postJson(`/api/users/${user.user_id}/disable`, undefined), "user disabled")
                }
              >
                Disable
              </button>
            </div>
          );
        })}
      </section>
    </main>
  );
}
