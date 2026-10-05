import { useCallback, useEffect, useState } from "react";

import {
  getJson,
  postJson,
  detailText,
  type DependencyCheck,
  type SetupStatus,
} from "./api";

const fieldStyle = { display: "block", width: "100%", marginBottom: "0.75rem", padding: "0.5rem" } as const;

function CheckList({ checks }: { checks: DependencyCheck[] }) {
  return (
    <ul aria-label="dependency checks">
      {checks.map((check) => (
        <li key={check.name}>
          {check.ok ? "OK" : "FAILED"} — {check.name}: {check.detail}
          {!check.ok && check.remediation ? (
            <div>
              fix: {check.remediation}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export default function SetupPage() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  const refresh = useCallback(async () => {
    setLoadError(null);
    const result = await getJson("/api/setup/status");
    if (result.ok) {
      setStatus(result.payload as unknown as SetupStatus);
    } else {
      setLoadError("cannot reach the server; is the stack running? (make up)");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const failing = status?.dependencies.filter((check) => !check.ok) ?? [];

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (password !== confirm) {
      setMessage("passwords do not match");
      return;
    }
    setBusy(true);
    const result = await postJson("/api/setup/owner", { email, password });
    setBusy(false);
    if (result.ok) {
      setDone(true);
      return;
    }
    if (result.status === 503) {
      const detail = result.payload.detail as { checks?: DependencyCheck[] } | undefined;
      if (detail?.checks) {
        setStatus({ initialized: false, dependencies: detail.checks });
      }
      setMessage(detailText(result));
    } else {
      setMessage(detailText(result));
    }
  }

  if (done) {
    return (
      <main>
        <h1>Workspace ready</h1>
        <p>
          The first owner account is created and you are signed in. Continue to{" "}
          <a href="/signin">sign in</a>.
        </p>
      </main>
    );
  }

  return (
    <main>
      <h1>Set up your workspace</h1>
      {loadError ? <p role="alert">{loadError}</p> : null}
      {status === null && !loadError ? <p>Checking dependencies…</p> : null}
      {status && !status.initialized && failing.length > 0 ? (
        <section aria-label="setup blockers">
          <p>Fix these, then retry — your progress is kept.</p>
          <CheckList checks={failing} />
          <button type="button" onClick={() => void refresh()}>
            Retry checks
          </button>
        </section>
      ) : null}
      {status && !status.initialized && failing.length === 0 ? (
        <form onSubmit={(event) => void submit(event)}>
          <label>
            Owner email
            <input
              style={fieldStyle}
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
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
          <label>
            Repeat password
            <input
              style={fieldStyle}
              type="password"
              required
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create owner account"}
          </button>
        </form>
      ) : null}
      {status?.initialized ? (
        <p>
          This workspace is already initialized. <a href="/signin">Sign in</a> or run{" "}
          <code>make reset-owner-password</code> on the host to recover access.
        </p>
      ) : null}
      {message ? (
        <p role="alert" aria-label="setup error">
          {message}
        </p>
      ) : null}
    </main>
  );
}
