import { useCallback, useState } from "react";

import { deleteJson, detailText } from "../auth/api";

type Props = {
  sourceId: string;
  sourceName: string;
  onDeleted: () => void;
};

/**
 * Destructive confirmation for permanent owner-only deletion (Task 17).
 * Two explicit steps: "Delete forever" only appears after "Delete…", and
 * the second click schedules the purge — no hover-only or accidental path.
 */
export default function DeleteSourceDialog({ sourceId, sourceName, onDeleted }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const request = useCallback(() => {
    setError(null);
    setConfirming(true);
  }, []);

  const confirm = useCallback(async () => {
    setBusy(true);
    setError(null);
    const result = await deleteJson(`/api/sources/${sourceId}`);
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      setConfirming(false);
      return;
    }
    setConfirming(false);
    onDeleted();
  }, [sourceId, onDeleted]);

  const cancel = useCallback(() => {
    setError(null);
    setConfirming(false);
  }, []);

  if (!confirming) {
    return (
      <button type="button" onClick={request}>
        Delete…
      </button>
    );
  }
  return (
    <span role="group" aria-label={`Permanently delete ${sourceName}`}>
      Delete forever? Answers citing it will show a deletion notice.
      <button type="button" disabled={busy} onClick={() => void confirm()}>
        Delete forever
      </button>
      <button type="button" disabled={busy} onClick={cancel}>
        Keep
      </button>
      {error && <p role="alert">{error}</p>}
    </span>
  );
}
