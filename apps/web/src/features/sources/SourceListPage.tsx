import { useCallback, useEffect, useState } from "react";

import "../../styles/sources.css";
import DeleteSourceDialog from "./DeleteSourceDialog";
import UploadSource from "./UploadSource";
import VersionHistory from "./VersionHistory";
import WebSyncPanel from "./WebSyncPanel";
import { detailText, getJson, postJson } from "../auth/api";

type Source = {
  source_id: string;
  name: string;
  state: string;
};

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
    <main>
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
        <p className="empty-state">No sources yet. An administrator can add the first source.</p>
      )}
      {sources !== null && sources.length === 0 && canUpload && (
        <p className="empty-state">No sources yet. Upload the first one below.</p>
      )}
      {canUpload && <UploadSource />}
      {sources !== null && sources.length > 0 && (
        <ul className="msg-list">
          {sources.map((source) => (
            <li key={source.source_id} className="card stack">
              <div className="row">
                <strong>{source.name}</strong> ·{" "}
                <span className="source-state">{source.state}</span>{" "}
                {canUpload && (source.state === "active" || source.state === "archived") && (
                  <button
                    type="button"
                    disabled={busyId !== null}
                    onClick={() => void toggleArchive(source.source_id, source.state)}
                  >
                    {source.state === "active" ? "Archive" : "Unarchive"}
                  </button>
                )}
              </div>
              <VersionHistory sourceId={source.source_id} canManage={canUpload} />
              {canUpload && source.state !== "deleted" && (
                <DeleteSourceDialog
                  sourceId={source.source_id}
                  sourceName={source.name}
                  onDeleted={load}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      <WebSyncPanel canManage={canUpload} />
    </main>
  );
}
