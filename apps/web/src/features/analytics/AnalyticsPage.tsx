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
        <div className="stat-grid">
          <div className="stat-tile" data-testid="card-exchanges">
            <span className="stat-value">{summary.exchanges}</span>
            <span className="stat-label">answered exchanges</span>
          </div>
          <div className="stat-tile" data-testid="card-helpful">
            <span className="stat-value">{summary.helpful}</span>
            <span className="stat-label">marked helpful</span>
          </div>
          <div className="stat-tile" data-testid="card-unhelpful">
            <span className="stat-value">{summary.unhelpful}</span>
            <span className="stat-label">marked not helpful</span>
          </div>
          <div className="stat-tile" data-testid="card-coverage">
            <span className="stat-value">{feedbackCoverage(summary)}</span>
            <span className="stat-label">feedback coverage</span>
          </div>
          <div className="stat-tile" data-testid="card-insufficient">
            <span className="stat-value">{summary.insufficient_evidence}</span>
            <span className="stat-label">no evidence found</span>
          </div>
          <div className="stat-tile" data-testid="card-unanswered">
            <span className="stat-value">{summary.unanswered_conversations}</span>
            <span className="stat-label">unanswered questions</span>
          </div>
        </div>
      )}
    </section>
  );
}
