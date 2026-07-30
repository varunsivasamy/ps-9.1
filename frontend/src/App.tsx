import { useCallback, useEffect, useRef, useState } from "react";
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

// Progress steps shown while the agent is thinking
const PROGRESS_STEPS = [
  "Sending request to agent…",
  "Agent measuring data scope…",
  "Scoring risk dimensions…",
  "Routing decision…",
];

export default function App() {
  const [sessionId, setSessionId]       = useState(generateSessionId);
  const [health, setHealth]             = useState<HealthResponse | null>(null);
  const [healthError, setHealthError]   = useState<string | null>(null);
  const [submitting, setSubmitting]     = useState(false);
  const [progressStep, setProgressStep] = useState(0);
  const [submitError, setSubmitError]   = useState<string | null>(null);
  const [latestResult, setLatestResult] = useState<ProposeResponse | null>(null);
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError]     = useState<string | null>(null);
  const progressTimer                   = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshAudit = useCallback((id: string) => {
    setAuditLoading(true);
    setAuditError(null);
    getAuditTrail(id)
      .then((res) => setAuditEntries(res.actions))
      .catch((err: ApiError) => setAuditError(err.message))
      .finally(() => setAuditLoading(false));
  }, []);

  // Health check once on mount
  useEffect(() => {
    getHealth()
      .then((res) => { setHealth(res); setHealthError(null); })
      .catch((err: ApiError) => setHealthError(err.message));
  }, []);

  // Reload audit when session changes
  useEffect(() => {
    refreshAudit(sessionId);
    setLatestResult(null);
  }, [sessionId, refreshAudit]);

  function startProgress() {
    setProgressStep(0);
    let step = 0;
    // Advance through steps every ~1.5s while waiting for the LLM
    progressTimer.current = setInterval(() => {
      step = Math.min(step + 1, PROGRESS_STEPS.length - 1);
      setProgressStep(step);
    }, 1500);
  }

  function stopProgress() {
    if (progressTimer.current) {
      clearInterval(progressTimer.current);
      progressTimer.current = null;
    }
  }

  async function handleSubmit(userRequest: string) {
    setSubmitting(true);
    setSubmitError(null);
    setLatestResult(null);
    startProgress();
    try {
      const result = await proposeAction(userRequest, sessionId);
      setLatestResult(result);
      // Optimistically prepend the new entry so the audit trail updates
      // immediately — the real refresh follows asynchronously
      refreshAudit(sessionId);
    } catch (err) {
      setSubmitError((err as ApiError).message);
    } finally {
      stopProgress();
      setSubmitting(false);
    }
  }

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__title-row">
          <span className="app__logo" aria-hidden="true">⬡</span>
          <h1>Agent Risk Console</h1>
        </div>
        <p className="app__subtitle">
          Send a request to the agent. It measures actual data scope, scores risk across
          four dimensions, then either runs it automatically, asks for your confirmation,
          or blocks for full review.
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

          {/* Progress indicator while agent is thinking */}
          {submitting && (
            <div className="progress-card" role="status" aria-live="polite">
              <span className="progress-card__spinner" aria-hidden="true" />
              <span className="progress-card__text">{PROGRESS_STEPS[progressStep]}</span>
            </div>
          )}

          {submitError && <p className="form-error">{submitError}</p>}

          {latestResult && !submitting && (
            <ActionResult
              result={latestResult}
              onResolved={() => refreshAudit(sessionId)}
            />
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
