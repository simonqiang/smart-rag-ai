import { useCallback, useState } from "react";

import { detailText, getJson, postJson } from "../auth/api";

export type Version = {
  source_id: string;
  version_id: string;
  state: string;
  filename: string;
  size_bytes: number;
  created_at: string;
};

/** Lifecycle actions a version currently allows (Task 16 states). */
export function versionActions(version: Pick<Version, "state">): {
  activate: boolean;
  rollback: boolean;
} {
  return {
    activate: version.state === "indexed",
    rollback: ["active", "indexed", "superseded"].includes(version.state),
  };
}

type Props = {
  sourceId: string;
  canManage: boolean;
};

const rowStyle = {
  display: "flex",
  flexWrap: "wrap" as const,
  gap: "0.5rem",
  alignItems: "center",
  padding: "0.25rem 0",
};

/** Expandable version history with cutover and rollback controls. */
export default function VersionHistory({ sourceId, canManage }: Props) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    const result = await getJson(`/api/sources/${sourceId}/versions`);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setVersions(result.payload as unknown as Version[]);
  }, [sourceId]);

  const toggle = useCallback(() => {
    if (!open && versions === null) void load();
    setOpen((was) => !was);
  }, [open, versions, load]);

  const run = useCallback(
    async (versionId: string, action: "activate" | "rollback") => {
      setBusy(true);
      setError(null);
      const suffix = action === "activate" ? "activate" : "rollback";
      const result = await postJson(
        `/api/sources/${sourceId}/versions/${versionId}/${suffix}`, {},
      );
      setBusy(false);
      if (!result.ok) {
        setError(detailText(result));
        return;
      }
      // Cutover/rollback change version states only; the panel stays open.
      await load();
    },
    [sourceId, load],
  );

  return (
    <section>
      <button type="button" onClick={toggle} aria-expanded={open}>
        {open ? "Hide versions" : "Version history"}
      </button>
      {error && <p role="alert">{error}</p>}
      {open && versions === null && !error && <p role="status">Loading versions…</p>}
      {open && versions !== null && (
        <ul style={{ listStyle: "none", padding: 0 }}>
          {versions.map((version) => {
            const actions = versionActions(version);
            return (
              <li key={version.version_id} style={rowStyle}>
                <span>{version.filename}</span>
                <span>· {version.state}</span>
                <span>· {new Date(version.created_at).toLocaleString()}</span>
                {canManage && actions.activate && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(version.version_id, "activate")}
                  >
                    Make active
                  </button>
                )}
                {canManage && actions.rollback && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(version.version_id, "rollback")}
                  >
                    Restore as new version
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
