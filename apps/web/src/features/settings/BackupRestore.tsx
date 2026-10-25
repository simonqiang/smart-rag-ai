import { useCallback, useEffect, useState } from "react";

import { detailText, getJson, postJson } from "../auth/api";

type BackupRow = {
  id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
  source_count: number;
};

/** Bytes -> compact human size for the list rows. */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = next;
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

/** ISO timestamp trimmed to the minute (locale-independent list rows). */
export function formatWhen(created_at: string): string {
  return created_at.slice(0, 16).replace("T", " ");
}

export default function BackupRestore() {
  const [backups, setBackups] = useState<BackupRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    const result = await getJson("/api/settings/backup");
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setBackups((result.payload as unknown as { backups: BackupRow[] }).backups);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await postJson("/api/settings/backup", {});
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setNotice(
      `Backup created: ${(result.payload as unknown as { filename: string }).filename}`,
    );
    await load();
  }

  return (
    <main>
      <h1>Backup and restore</h1>
      <p>
        Backups are encrypted with the recovery key from the SMART_RAG_BACKUP_KEY
        environment variable; the key is never stored. Restoring is a host action —
        run <code>make restore</code> on a clean installation.
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      <form onSubmit={create}>
        <button type="submit" disabled={busy}>
          Create encrypted backup
        </button>
      </form>

      <h2>Existing backups</h2>
      {backups === null ? null : backups.length === 0 ? (
        <p className="empty-state">No backups yet.</p>
      ) : (
        <ul className="msg-list">
          {backups.map((backup) => (
            <li key={backup.id} className="msg">
              <code>{backup.filename}</code> — {formatSize(backup.size_bytes)} —{" "}
              {formatWhen(backup.created_at)} — {backup.source_count} source
              {backup.source_count === 1 ? "" : "s"}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
