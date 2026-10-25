import { useCallback, useEffect, useState } from "react";

import { detailText, getJson, postJson, putJson } from "../auth/api";

type Collection = { collection_id: string; name: string };

type Schedule = {
  schedule_id: string;
  collection_id: string;
  source_id: string | null;
  name: string;
  start_url: string;
  interval_seconds: number;
  enabled: boolean;
  next_sync_at: string | null;
  last_synced_at: string | null;
  last_state: string | null;
  last_detail: string | null;
};

export const MIN_INTERVAL_MINUTES = 5;

/** Badge modifier for one schedule's freshness state (paused beats all). */
export function stateBadgeClass(schedule: Pick<Schedule, "enabled" | "last_state">): string {
  if (!schedule.enabled) return "badge badge-warn";
  if (schedule.last_state === "synced") return "badge badge-ok";
  if (schedule.last_state === "failed") return "badge badge-danger";
  return "badge badge-warn";
}

/** Lines -> trimmed non-empty prefixes, one per line. */
export function parsePrefixes(raw: string): string[] {
  return raw.split("\n").map((line) => line.trim()).filter(Boolean);
}

/** Free text -> whole minutes (>= minimum) in seconds, or null. */
export function intervalSeconds(raw: string): number | null {
  const value = Number(raw);
  return Number.isInteger(value) && value >= MIN_INTERVAL_MINUTES ? value * 60 : null;
}

/** ISO timestamp -> "YYYY-MM-DD HH:MM" (deterministic, no locale drift); null -> "—". */
export function formatTime(iso: string | null): string {
  return iso ? `${iso.slice(0, 16).replace("T", " ")}` : "—";
}

/** Chip text for one schedule: freshness state, or paused when disabled. */
export function stateChip(schedule: Pick<Schedule, "enabled" | "last_state">): string {
  if (!schedule.enabled) return "paused";
  return schedule.last_state ?? "never synced";
}

/** Add-website form plus the grant-scoped freshness list. */
export default function WebSyncPanel({ canManage }: { canManage: boolean }) {
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [collectionId, setCollectionId] = useState("");
  const [name, setName] = useState("");
  const [startUrl, setStartUrl] = useState("");
  const [sitemapUrl, setSitemapUrl] = useState("");
  const [prefixes, setPrefixes] = useState("");
  const [minutes, setMinutes] = useState("1440");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    const result = await getJson("/api/web-sync");
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setSchedules(result.payload as unknown as Schedule[]);
  }, []);

  useEffect(() => {
    void load();
    if (canManage) {
      void getJson("/api/collections").then((result) => {
        if (!result.ok) return;
        const loaded = result.payload as unknown as Collection[];
        setCollections(loaded);
        if (loaded.length > 0) setCollectionId(loaded[0].collection_id);
      });
    }
  }, [load, canManage]);

  async function addWebsite(event: React.FormEvent) {
    event.preventDefault();
    const interval = intervalSeconds(minutes);
    if (interval === null) {
      setError(`Sync interval must be whole minutes of at least ${MIN_INTERVAL_MINUTES}.`);
      return;
    }
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await postJson("/api/web-sync", {
      collection_id: collectionId,
      name,
      start_url: startUrl,
      sitemap_url: sitemapUrl || null,
      allowed_prefixes: parsePrefixes(prefixes),
      interval_seconds: interval,
    });
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setName("");
    setStartUrl("");
    setSitemapUrl("");
    setPrefixes("");
    setNotice("Website added; the first sync is scheduled.");
    await load();
  }

  async function syncNow(scheduleId: string) {
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await postJson(`/api/web-sync/${scheduleId}/run`, {});
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setNotice("Sync requested; the schedule refreshes when the crawl finishes.");
  }

  async function toggleEnabled(schedule: Schedule) {
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await putJson(`/api/web-sync/${schedule.schedule_id}/enabled`, {
      enabled: !schedule.enabled,
    });
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    await load();
  }

  return (
    <section aria-labelledby="web-sync-heading">
      <h2 id="web-sync-heading">Websites</h2>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      {schedules !== null && schedules.length === 0 && (
        <p className="empty-state">No websites synced yet.</p>
      )}
      {schedules !== null && schedules.length > 0 && (
        <ul className="msg-list">
          {schedules.map((schedule) => (
            <li key={schedule.schedule_id} className="card">
              <strong>{schedule.name}</strong>{" "}
              <span
                className={stateBadgeClass(schedule)}
                data-testid={`sync-state-${schedule.schedule_id}`}
              >
                {stateChip(schedule)}
              </span>
              <br />
              <span>{schedule.start_url}</span>
              <br />
              <span>
                last sync: <time dateTime={schedule.last_synced_at ?? undefined}>{formatTime(schedule.last_synced_at)}</time>
                {" · "}
                next sync: <time dateTime={schedule.next_sync_at ?? undefined}>{formatTime(schedule.next_sync_at)}</time>
              </span>
              {schedule.last_detail && (
                <><br /><span>{schedule.last_detail}</span></>
              )}
              {canManage && (
                <div className="row">
                  <button type="button" disabled={busy} onClick={() => void syncNow(schedule.schedule_id)}>
                    Sync now
                  </button>{" "}
                  <button type="button" disabled={busy} onClick={() => void toggleEnabled(schedule)}>
                    {schedule.enabled ? "Disable" : "Enable"}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {canManage && collections.length > 0 && (
        <form onSubmit={addWebsite} aria-label="Add website" className="card stack">
          <h3>Add a website</h3>
          <label>
            Collection
            <select
              value={collectionId}
              onChange={(event) => setCollectionId(event.target.value)}
            >
              {collections.map((collection) => (
                <option key={collection.collection_id} value={collection.collection_id}>
                  {collection.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Name
            <input
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Start URL
            <input
              type="url"
              required
              value={startUrl}
              onChange={(event) => setStartUrl(event.target.value)}
            />
          </label>
          <label>
            Sitemap URL (optional)
            <input
              type="url"
              value={sitemapUrl}
              onChange={(event) => setSitemapUrl(event.target.value)}
            />
          </label>
          <label>
            Allowed path prefixes (one per line, optional)
            <textarea
              rows={3}
              value={prefixes}
              onChange={(event) => setPrefixes(event.target.value)}
            />
          </label>
          <label>
            Sync every (minutes)
            <input
              type="number"
              min={MIN_INTERVAL_MINUTES}
              step={1}
              required
              value={minutes}
              onChange={(event) => setMinutes(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>Add website</button>
        </form>
      )}
    </section>
  );
}
