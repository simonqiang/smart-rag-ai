import { useCallback, useEffect, useState } from "react";

import { detailText, getJson } from "../auth/api";

type Summary = {
  exchanges: number;
  answered: number;
  insufficient_evidence: number;
  unanswered_conversations: number;
  helpful: number;
  unhelpful: number;
};

type Session = { role: string };

const cardStyle = {
  border: "1px solid #ccc",
  borderRadius: "4px",
  padding: "0.75rem",
  minWidth: "10rem",
  display: "inline-block",
  marginRight: "0.5rem",
  marginBottom: "0.5rem",
} as const;
const cardWrapStyle = { display: "flex", flexWrap: "wrap" } as const;

/** Coverage of feedback over completed exchanges, as a percent string. */
export function feedbackCoverage(summary: Summary): string {
  if (summary.exchanges === 0) return "—";
  const flagged = summary.helpful + summary.unhelpful;
  return `${Math.round((flagged / summary.exchanges) * 100)}%`;
}

/** Operator analytics: aggregate answer feedback and unanswered-question counts. */
export default function AnalyticsPage() {
  const [role, setRole] = useState<string | null>(null);
  const [roleLoaded, setRoleLoaded] = useState(false);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isOperator = role === "owner" || role === "admin";

  const load = useCallback(async () => {
    setError(null);
    const result = await getJson("/api/analytics/summary");
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setSummary(result.payload as unknown as Summary);
  }, []);

  useEffect(() => {
    void getJson("/api/session").then((result) => {
      if (result.ok) setRole((result.payload as unknown as Session).role);
      setRoleLoaded(true);
    });
    void load();
  }, [load]);

  if (roleLoaded && !isOperator) {
    return (
      <section aria-labelledby="analytics-heading">
        <h1 id="analytics-heading">Analytics</h1>
        <p>Analytics are limited to administrators and owners.</p>
      </section>
    );
  }

  return (
    <section aria-labelledby="analytics-heading">
      <h1 id="analytics-heading">Analytics</h1>
      <p>Aggregate answer feedback and unanswered questions. No question text is shown.</p>
      {error && <p role="alert">{error}</p>}
      {summary !== null && summary.exchanges + summary.unanswered_conversations === 0 && (
        <p role="status">No questions asked yet.</p>
      )}
      {summary !== null && summary.exchanges + summary.unanswered_conversations > 0 && (
        <div style={cardWrapStyle}>
          <div style={cardStyle} data-testid="card-exchanges">
            <strong>{summary.exchanges}</strong>
            <br />
            answered exchanges
          </div>
          <div style={cardStyle} data-testid="card-helpful">
            <strong>{summary.helpful}</strong>
            <br />
            marked helpful
          </div>
          <div style={cardStyle} data-testid="card-unhelpful">
            <strong>{summary.unhelpful}</strong>
            <br />
            marked not helpful
          </div>
          <div style={cardStyle} data-testid="card-coverage">
            <strong>{feedbackCoverage(summary)}</strong>
            <br />
            feedback coverage
          </div>
          <div style={cardStyle} data-testid="card-insufficient">
            <strong>{summary.insufficient_evidence}</strong>
            <br />
            no evidence found
          </div>
          <div style={cardStyle} data-testid="card-unanswered">
            <strong>{summary.unanswered_conversations}</strong>
            <br />
            unanswered questions
          </div>
        </div>
      )}
    </section>
  );
}
