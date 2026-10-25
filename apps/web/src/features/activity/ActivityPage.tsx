import { useCallback, useEffect, useState } from "react";

import "../../styles/ops.css";
import { detailText, getJson, postJson } from "../auth/api";

type Job = {
  job_id: string;
  type: string;
  state: string;
  attempts: number;
  last_error: string | null;
  retryable: boolean;
  created_at: string;
};

type SourceHealth = {
  source_id: string;
  name: string;
  state: string;
  latest_version_state: string | null;
  created_at: string;
};

type AuditEntry = {
  event_id: string;
  type: string;
  actor: string;
  created_at: string;
  metadata: Record<string, unknown>;
};

type DiagnosticCheck = {
  name: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  remediation: string;
};

type Session = { role: string };

/** Job state -> badge variant: failed is danger, completed is ok, rest warn. */
function stateBadgeClass(state: string): string {
  if (state === "failed") return "badge badge-danger";
  if (state === "completed") return "badge badge-ok";
  return "badge badge-warn";
}

/** Doctor status -> badge variant. */
function checkBadgeClass(status: DiagnosticCheck["status"]): string {
  if (status === "fail") return "badge badge-danger";
  if (status === "warn") return "badge badge-warn";
  return "badge badge-ok";
}

/** ISO timestamp -> "YYYY-MM-DD HH:MM"; null -> "—". */
export function formatTime(iso: string | null): string {
  return iso ? `${iso.slice(0, 16).replace("T", " ")}` : "—";
}

/** Only failed jobs may be retried — mirrors the service-side CAS rule. */
export function canRetry(state: string): boolean {
  return state === "failed";
}

/** Doctor status -> chip text; fail covers provider outage and storage errors. */
export function checkChip(status: DiagnosticCheck["status"]): string {
  if (status === "warn") return "warning";
  return status;
}

type Page<T> = { items: T[]; total: number };

/** Signed-in operators see jobs, source health, the audit log, and diagnostics. */
export default function ActivityPage() {
  const [role, setRole] = useState<string | null>(null);
  const [roleLoaded, setRoleLoaded] = useState(false);
  const [jobs, setJobs] = useState<Page<Job> | null>(null);
  const [sources, setSources] = useState<Page<SourceHealth> | null>(null);
  const [audit, setAudit] = useState<Page<AuditEntry> | null>(null);
  const [checks, setChecks] = useState<DiagnosticCheck[] | null>(null);
  const [host, setHost] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isOperator = role === "owner" || role === "admin";

  const load = useCallback(async () => {
    setError(null);
    const [jobsPage, sourcesPage, auditPage, diagnostics] = await Promise.all([
      getJson("/api/operations/jobs"),
      getJson("/api/operations/sources/health"),
      getJson("/api/operations/audit"),
      getJson("/api/operations/diagnostics"),
    ]);
    if (!jobsPage.ok || !sourcesPage.ok || !auditPage.ok) {
      const failed = [jobsPage, sourcesPage, auditPage].find((r) => !r.ok);
      setError(detailText(failed!));
      return;
    }
    setJobs(jobsPage.payload as unknown as Page<Job>);
    setSources(sourcesPage.payload as unknown as Page<SourceHealth>);
    setAudit(auditPage.payload as unknown as Page<AuditEntry>);
    if (diagnostics.ok) {
      const payload = diagnostics.payload as unknown as { host: string; checks: DiagnosticCheck[] };
      setHost(payload.host);
      setChecks(payload.checks);
    }
  }, []);

  useEffect(() => {
    void getJson("/api/session").then((result) => {
      if (result.ok) setRole((result.payload as unknown as Session).role);
      setRoleLoaded(true);
    });
    void load();
  }, [load]);

  async function retry(jobId: string) {
    setError(null);
    setNotice(null);
    setBusy(true);
    const result = await postJson(`/api/operations/jobs/${jobId}/retry`, {});
    setBusy(false);
    if (!result.ok) {
      setError(detailText(result));
      return;
    }
    setNotice("Retry requested; the job re-enters the queue.");
    await load();
  }

  if (roleLoaded && !isOperator) {
    return (
      <section aria-labelledby="activity-heading">
        <h1 id="activity-heading">Activity</h1>
        <p>Operator access is limited to administrators and owners.</p>
      </section>
    );
  }

  return (
    <>
      <h1>Activity</h1>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      <section aria-labelledby="jobs-heading" className="ops-section">
        <h2 id="jobs-heading">Jobs</h2>
        {jobs !== null && jobs.items.length === 0 && <p>No background jobs yet.</p>}
        {jobs !== null && jobs.items.length > 0 && (
          <ul className="msg-list">
            {jobs.items.map((job) => (
              <li key={job.job_id} className="ops-row">
                <strong>{job.type}</strong>{" "}
                <span className={stateBadgeClass(job.state)} data-testid={`job-state-${job.job_id}`}>
                  {job.state}
                </span>
                <br />
                <span>
                  attempts: {job.attempts} · created: {formatTime(job.created_at)}
                </span>
                {job.last_error && (
                  <>
                    <br />
                    <span>last error: {job.last_error}</span>
                  </>
                )}
                {isOperator && canRetry(job.state) && (
                  <div className="ops-actions">
                    <button type="button" disabled={busy} onClick={() => void retry(job.job_id)}>
                      Retry
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="health-heading" className="ops-section">
        <h2 id="health-heading">Source health</h2>
        {sources !== null && sources.items.length === 0 && <p>No sources yet.</p>}
        {sources !== null && sources.items.length > 0 && (
          <ul className="msg-list">
            {sources.items.map((source) => (
              <li key={source.source_id} className="ops-row">
                <strong>{source.name}</strong>{" "}
                <span>{source.latest_version_state ?? "no versions"}</span>
                <br />
                <span>
                  source: {source.state} · added: {formatTime(source.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="audit-heading" className="ops-section">
        <h2 id="audit-heading">Audit log</h2>
        {audit !== null && audit.items.length === 0 && <p>No audit events yet.</p>}
        {audit !== null && audit.items.length > 0 && (
          <ul className="msg-list">
            {audit.items.map((entry) => (
              <li key={entry.event_id} className="ops-row">
                <strong>{entry.type}</strong>
                <br />
                <span>
                  {entry.actor} · {formatTime(entry.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="diagnostics-heading" className="ops-section">
        <h2 id="diagnostics-heading">Diagnostics</h2>
        {checks !== null && (
          <p>Host: {host}</p>
        )}
        {checks !== null && checks.length === 0 && <p>No diagnostics available.</p>}
        {checks !== null && checks.length > 0 && (
          <ul className="msg-list">
            {checks.map((check) => (
              <li key={check.name} className="ops-row">
                <strong>{check.name}</strong>{" "}
                <span className={checkBadgeClass(check.status)} data-testid={`check-${check.name}`}>
                  {checkChip(check.status)}
                </span>
                <br />
                <span>{check.detail}</span>
                {check.remediation && (
                  <>
                    <br />
                    <span>fix: {check.remediation}</span>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
