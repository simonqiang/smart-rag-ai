import { useCallback, useEffect, useState } from "react";

import UploadSource from "./UploadSource";
import VersionHistory from "./VersionHistory";
import { detailText, getJson, postJson } from "../auth/api";

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
  const [canUpload, setCanUpload] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

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

  const toggleArchive = useCallback(
    async (sourceId: string, state: string) => {
      setBusyId(sourceId);
      setError(null);
      const result = await postJson(
        `/api/sources/${sourceId}/${state === "active" ? "archive" : "unarchive"}`, {},
      );
      setBusyId(null);
      if (!result.ok) {
        setError(detailText(result));
        return;
      }
      await load();
    },
    [load],
  );

  useEffect(() => {
    void load();
    void getJson("/api/session").then((me) => {
      if (me.ok) {
        const role = String(me.payload.role);
        setCanUpload(role === "owner" || role === "admin");
      }
    });
  }, [load]);

  return (
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
      {sources !== null && sources.length === 0 && !canUpload && (
        <p>No sources yet. An administrator can add the first source.</p>
      )}
      {sources !== null && sources.length === 0 && canUpload && (
        <p>No sources yet. Upload the first one below.</p>
      )}
      {canUpload && <UploadSource />}
      {sources !== null && sources.length > 0 && (
        <ul style={listStyle}>
          {sources.map((source) => (
            <li key={source.source_id} style={rowStyle}>
              <strong>{source.name}</strong> · {source.state}{" "}
              {canUpload && (source.state === "active" || source.state === "archived") && (
                <button
                  type="button"
                  disabled={busyId !== null}
                  onClick={() => void toggleArchive(source.source_id, source.state)}
                >
                  {source.state === "active" ? "Archive" : "Unarchive"}
                </button>
              )}
              <VersionHistory sourceId={source.source_id} canManage={canUpload} />
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
