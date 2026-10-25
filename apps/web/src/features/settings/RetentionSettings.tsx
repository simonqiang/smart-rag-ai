import { useCallback, useEffect, useState } from "react";

import { detailText, getJson, putJson } from "../auth/api";

const fieldStyle = { marginBottom: "0.75rem" } as const;

/** Free text -> day count, or null when it is not a positive integer. */
export function toDays(raw: string): number | null {
  const value = Number(raw);
  return Number.isInteger(value) && value >= 1 ? value : null;
}

export default function RetentionSettings() {
  const [supersededDays, setSupersededDays] = useState("30");
  const [conversationDays, setConversationDays] = useState("90");
  const [backupDays, setBackupDays] = useState("30");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    const result = await getJson("/api/settings/retention");
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    const payload = result.payload as unknown as {
      superseded_days: number; conversation_days: number; backup_days: number;
    };
    setSupersededDays(String(payload.superseded_days));
    setConversationDays(String(payload.conversation_days));
    setBackupDays(String(payload.backup_days));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const superseded = toDays(supersededDays);
    const conversation = toDays(conversationDays);
    const backup = toDays(backupDays);
    if (superseded === null || conversation === null || backup === null) {
      setError("Retention windows must be whole days of at least 1.");
      return;
    }
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await putJson("/api/settings/retention", {
      superseded_days: superseded,
      conversation_days: conversation,
      backup_days: backup,
    });
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setNotice("Retention windows saved. The nightly purge applies them.");
  }

  const valid =
    toDays(supersededDays) !== null && toDays(conversationDays) !== null && toDays(backupDays) !== null;

  return (
    <main>
      <h1>Retention</h1>
      <p>
        Expired superseded source versions, retired pages, unreferenced files, stale
        conversations, and expired backups are purged once a day. Active knowledge is
        never purged.
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      <form onSubmit={save}>
        <label>
          Keep superseded versions and retired pages for (days)
          <input
            style={fieldStyle}
            type="number"
            min={1}
            step={1}
            required
            value={supersededDays}
            onChange={(event) => setSupersededDays(event.target.value)}
          />
        </label>
        <label>
          Keep idle conversations for (days)
          <input
            style={fieldStyle}
            type="number"
            min={1}
            step={1}
            required
            value={conversationDays}
            onChange={(event) => setConversationDays(event.target.value)}
          />
        </label>
        <label>
          Keep managed backups for (days)
          <input
            style={fieldStyle}
            type="number"
            min={1}
            step={1}
            required
            value={backupDays}
            onChange={(event) => setBackupDays(event.target.value)}
          />
        </label>
        <button type="submit" disabled={busy || !valid}>
          Save retention windows
        </button>
      </form>
    </main>
  );
}
