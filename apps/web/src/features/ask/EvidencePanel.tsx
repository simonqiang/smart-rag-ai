import { useEffect, useState } from "react";

import type { Citation } from "./contracts";
import { getJson } from "../auth/api";

function locationText(citation: Citation): string {
  const where =
    citation.page !== null
      ? `page ${citation.page}, blocks ${citation.block_start}–${citation.block_end}`
      : `blocks ${citation.block_start}–${citation.block_end}`;
  return `Source: ${citation.source_name} (${where})`;
}

function EvidenceDetails({ citation }: { citation: Citation }) {
  const [available, setAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    let live = true;
    // Re-authorize against the catalog: deleted or ungranted sources refuse here.
    void getJson(`/api/sources/${citation.source_id}`).then((result) => {
      if (live) setAvailable(result.ok);
    });
    return () => {
      live = false;
    };
  }, [citation.source_id]);

  if (available === null) return <p role="status">Checking evidence access…</p>;
  if (!available) {
    return <p role="status">This source is no longer available.</p>;
  }
  return (
    <>
      <p>{locationText(citation)}</p>
      <blockquote>“{citation.quote}”</blockquote>
    </>
  );
}

/** Citation evidence: source, location, and the source-language quote. */
export default function EvidencePanel({
  citation,
  onClose,
}: {
  citation: Citation | null;
  onClose: () => void;
}) {
  if (!citation) return null;
  return (
    <aside className="evidence-panel" aria-label="Evidence">
      <div className="row">
        <strong>Evidence [{citation.label}]</strong>
        <button type="button" className="btn-ghost evidence-actions" onClick={onClose}>
          Close evidence
        </button>
      </div>
      <EvidenceDetails citation={citation} />
    </aside>
  );
}
