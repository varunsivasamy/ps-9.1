import { useCallback, useEffect, useState } from "react";
import { getAuditTrail, getHealth, proposeAction } from "./api";
import { ActionResult } from "./components/ActionResult";
import { AuditTrail } from "./components/AuditTrail";
import { RequestForm } from "./components/RequestForm";
import { SessionBar } from "./components/SessionBar";
import type { ApiError, AuditEntry, HealthResponse, ProposeResponse } from "./types";

function generateSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `session-${crypto.randomUUID()}`;
  }
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export default function App() {
  const [sessionId, setSessionId] = useState(generateSessionId);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [latestResult, setLatestResult] = useState<ProposeResponse | null>(null);

  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);

  const refreshAudit = useCallback((forSessionId: string) => {
    setAuditLoading(true);
    setAuditError(null);
    getAuditTrail(forSessionId)
      .then((res) => setAuditEntries(res.actions))
      .catch((err: ApiError) => setAuditError(err.message))
      .finally(() => setAuditLoading(false));
  }, []);

  useEffect(() => {
    getHealth()
      .then((res) => {
        setHealth(res);
        setHealthError(null);
      })
      .catch((err: ApiError) => setHealthError(err.message));
  }, []);

  useEffect(() => {
    refreshAudit(sessionId);
    setLatestResult(null);
  }, [sessionId, refreshAudit]);

  async function handleSubmit(userRequest: string) {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const result = await proposeAction(userRequest, sessionId);
      setLatestResult(result);
      refreshAudit(sessionId);
    } catch (err) {
      setSubmitError((err as ApiError).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>PS-9.1 Autonomy Console</h1>
        <p className="app__subtitle">
          Send a request to the agent. It proposes an action, scores its own risk, and either
          runs it, asks for a quick confirmation, or blocks for full human review.
        </p>
      </header>

      <SessionBar
        sessionId={sessionId}
        onSessionIdChange={setSessionId}
        onNewSession={() => setSessionId(generateSessionId())}
        health={health}
        healthError={healthError}
      />

      <main className="app__main">
        <section className="app__column">
          <RequestForm onSubmit={handleSubmit} disabled={submitting} />
          {submitError && <p className="form-error">{submitError}</p>}
          {latestResult && (
            <ActionResult result={latestResult} onResolved={() => refreshAudit(sessionId)} />
          )}
        </section>

        <section className="app__column">
          <AuditTrail
            sessionId={sessionId}
            entries={auditEntries}
            loading={auditLoading}
            error={auditError}
            onRefresh={() => refreshAudit(sessionId)}
          />
        </section>
      </main>
    </div>
  );
}
