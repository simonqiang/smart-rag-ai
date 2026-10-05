import { useState } from "react";

import { getJson, postJson, detailText } from "./api";

const fieldStyle = { display: "block", width: "100%", marginBottom: "0.75rem", padding: "0.5rem" } as const;

type Me = { email: string; must_change_password: boolean };

export default function SignInPage() {
  const [mode, setMode] = useState<"signin" | "invite">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    const result =
      mode === "signin"
        ? await postJson("/api/session", { email, password })
        : await postJson("/api/invitations/accept", { token, password });
    if (!result.ok) {
      setBusy(false);
      setError(detailText(result));
      return;
    }
    const who = await getJson("/api/session");
    setBusy(false);
    if (who.ok) {
      setMe(who.payload as unknown as Me);
    } else {
      setMe({ email, must_change_password: false });
    }
  }

  async function changePassword(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    const result = await postJson("/api/session/password", {
      current_password: password,
      new_password: newPassword,
    });
    setBusy(false);
    if (result.ok) {
      setMe({ email: me?.email ?? email, must_change_password: false });
      setError("password changed; sign in again with the new password");
    } else {
      setError(detailText(result));
    }
  }

  if (me?.must_change_password) {
    return (
      <main>
        <h1>Choose a new password</h1>
        <p>Your account uses a temporary password; change it to continue.</p>
        <form onSubmit={(event) => void changePassword(event)}>
          <label>
            New password (at least 10 characters)
            <input
              style={fieldStyle}
              type="password"
              required
              minLength={10}
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Change password"}
          </button>
        </form>
        {error ? <p role="alert">{error}</p> : null}
      </main>
    );
  }

  if (me) {
    return (
      <main>
        <h1>Signed in</h1>
        <p>Welcome back, {me.email}.</p>
      </main>
    );
  }

  return (
    <main>
      <h1>{mode === "signin" ? "Sign in" : "Accept invitation"}</h1>
      <form onSubmit={(event) => void submit(event)}>
        {mode === "signin" ? (
          <label>
            Email
            <input
              style={fieldStyle}
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
        ) : (
          <label>
            Invitation token
            <input
              style={fieldStyle}
              type="text"
              required
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
          </label>
        )}
        <label>
          Password (at least 10 characters)
          <input
            style={fieldStyle}
            type="password"
            required
            minLength={10}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? "Working…" : mode === "signin" ? "Sign in" : "Accept invitation"}
        </button>
      </form>
      <p>
        {mode === "signin" ? (
          <a
            href="/signin"
            onClick={(event) => {
              event.preventDefault();
              setMode("invite");
            }}
          >
            I have an invitation token
          </a>
        ) : (
          <a
            href="/signin"
            onClick={(event) => {
              event.preventDefault();
              setMode("signin");
            }}
          >
            Sign in with email instead
          </a>
        )}
      </p>
      {error ? (
        <p role="alert" aria-label="sign-in error">
          {error}
        </p>
      ) : null}
    </main>
  );
}
