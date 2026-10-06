import { useCallback, useEffect, useState } from "react";

import { detailText, getJson, postForm } from "../auth/api";

type Collection = { collection_id: string; name: string };

const fieldStyle = { display: "block", width: "100%", marginBottom: "0.75rem", padding: "0.5rem" } as const;

/** Multipart body for POST /api/uploads. */
export function uploadBody(file: File, collectionId: string, name: string): FormData {
  const body = new FormData();
  body.append("file", file);
  body.append("collection_id", collectionId);
  body.append("name", name);
  return body;
}

/** Upload form for owners/admins; rejections surface the API's actionable reasons. */
export default function UploadSource() {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [collectionId, setCollectionId] = useState("");
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const result = await getJson("/api/collections");
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    const loaded = result.payload as unknown as Collection[];
    setCollections(loaded);
    if (loaded.length > 0) setCollectionId(loaded[0].collection_id);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await postForm("/api/uploads", uploadBody(file, collectionId, name));
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setNotice(
      result.payload.duplicate
        ? "source registered; note: this file duplicates an already-uploaded file"
        : "source registered",
    );
    setFile(null);
    setName("");
    (event.target as HTMLFormElement).reset();
    await load();
  }

  return (
    <section aria-labelledby="upload-heading" style={{ marginTop: "1.5rem" }}>
      <h2 id="upload-heading">Upload a source</h2>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form onSubmit={submit}>
        <label>
          Collection
          <select
            style={fieldStyle}
            value={collectionId}
            onChange={(event) => setCollectionId(event.target.value)}
            required
          >
            {collections.map((collection) => (
              <option key={collection.collection_id} value={collection.collection_id}>
                {collection.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Source name
          <input
            style={fieldStyle}
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label>
          File (PDF, TXT, or Markdown, up to 50 MB)
          <input
            style={fieldStyle}
            type="file"
            accept=".pdf,.txt,.md,.markdown"
            required
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
        </label>
        <button type="submit" disabled={busy || !file}>
          Upload
        </button>
      </form>
    </section>
  );
}
