import { useCallback, useEffect, useState } from "react";

import AppShell from "../../AppShell";
import { detailText, getJson } from "../auth/api";

type Source = {
  source_id: string;
  name: string;
  state: string;
};

const listStyle = { listStyle: "none", padding: 0 } as const;
const rowStyle = {
  border: "1px solid #ccc",
  borderRadius: "4px",
  padding: "0.75rem",
  marginBottom: "0.5rem",
} as const;

/** Catalog of the sources the signed-in user may see. */
export default function SourceListPage() {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setSources(null);
    const result = await getJson("/api/sources");
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setSources(result.payload as unknown as Source[]);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <AppShell>
      <main style={{ maxWidth: "48rem", margin: "0 auto", padding: "1rem" }}>
        <h1>Sources</h1>
        {error && (
          <p role="alert">
            {error}{" "}
            <button type="button" onClick={() => void load()}>
              Retry
            </button>
          </p>
        )}
        {sources === null && !error && <p role="status">Loading sources…</p>}
        {sources !== null && sources.length === 0 && (
          <p>No sources yet. An administrator can add the first source.</p>
        )}
        {sources !== null && sources.length > 0 && (
          <ul style={listStyle}>
            {sources.map((source) => (
              <li key={source.source_id} style={rowStyle}>
                <strong>{source.name}</strong> · {source.state}
              </li>
            ))}
          </ul>
        )}
      </main>
    </AppShell>
  );
}
