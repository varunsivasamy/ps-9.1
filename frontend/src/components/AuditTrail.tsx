import type { AuditEntry } from "../types";

interface AuditTrailProps {
  sessionId: string;
  entries: AuditEntry[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}

function formatTimestamp(ts: string): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString();
}

export function AuditTrail({ sessionId, entries, loading, error, onRefresh }: AuditTrailProps) {
  return (
    <div className="audit-trail">
      <div className="audit-trail__header">
        <h2>Audit trail</h2>
        <button type="button" className="button button--ghost" onClick={onRefresh} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      <p className="audit-trail__session">Session: {sessionId}</p>

      {error && <p className="form-error">{error}</p>}

      {!error && entries.length === 0 && (
        <p className="audit-trail__empty">No actions recorded yet for this session.</p>
      )}

      {entries.length > 0 && (
        <ul className="audit-trail__list">
          {entries.map((entry) => (
            <li key={entry.record_id} className="audit-trail__entry">
              <div className="audit-trail__entry-top">
                <span className="routing-badge routing-badge--sm" data-decision={entry.routing_decision ?? undefined}>
                  {entry.routing_decision ?? "unknown"}
                </span>
                <span className={`status-pill status-pill--${entry.status ?? "unknown"}`}>
                  {entry.status ?? "unknown"}
                </span>
                <span className="audit-trail__timestamp">{formatTimestamp(entry.timestamp)}</span>
              </div>
              <p className="audit-trail__description">{entry.description}</p>
              {/*
                Two different questions, so two different lines: the status pill
                above says whether the action was authorised, and this says
                whether it then actually worked. An approved action that failed
                is the case a reviewer most needs to see.
              */}
              {entry.execution_detail && (
                <p
                  className="audit-trail__execution"
                  data-execution={entry.execution_status ?? undefined}
                >
                  {entry.execution_detail}
                </p>
              )}
              <div className="audit-trail__meta">
                <span>action: {entry.action_type ?? "—"}</span>
                <span>score: {entry.composite_score ?? "—"}</span>
                {entry.reviewer && <span>reviewer: {entry.reviewer}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
