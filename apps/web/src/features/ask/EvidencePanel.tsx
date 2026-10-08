import { useEffect, useState } from "react";

import type { Citation } from "./contracts";
import { getJson } from "../auth/api";

const panelStyle = {
  border: "1px solid #0b57d0",
  borderRadius: "4px",
  padding: "0.75rem",
  marginTop: "0.75rem",
} as const;
const quoteStyle = { margin: "0.5rem 0 0", whiteSpace: "pre-wrap" } as const;

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
      <blockquote style={quoteStyle}>“{citation.quote}”</blockquote>
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
    <aside style={panelStyle} aria-label="Evidence">
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <strong>Evidence [{citation.label}]</strong>
        <span style={{ marginLeft: "auto" }}>
          <button type="button" onClick={onClose} style={{ minHeight: "44px" }}>
            Close evidence
          </button>
        </span>
      </div>
      <EvidenceDetails citation={citation} />
    </aside>
  );
}
