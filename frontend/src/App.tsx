import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { getAuditTrail, getCalibration, getHealth, proposeAction } from "./api";
import { ActionResult } from "./components/ActionResult";
import { AuditTrail } from "./components/AuditTrail";
import { CalibrationPanel } from "./components/CalibrationPanel";
import { QueryCard } from "./components/QueryCard";
import { Sidebar } from "./components/Sidebar";
import { ToastStack, useToasts } from "./components/Toasts";
import { TopHeader } from "./components/TopHeader";
import type {
  ApiError,
  AuditEntry,
  CalibrationEntry,
  HealthResponse,
  Turn,
} from "./types";

function generateSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto)
    return `session-${crypto.randomUUID()}`;
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const PROGRESS_STEPS = [
  "Sending request to agent…",
  "Measuring real data scope…",
  "Scoring risk dimensions…",
  "Routing the decision…",
  "Composing the answer…",
];

export default function App() {
  const [collapsed, setCollapsed]       = useState(false);
  const [navOpen, setNavOpen]           = useState(false);
  const [activePage, setActivePage]     = useState("query");
  const [sessionId, setSessionId]       = useState(generateSessionId);
  const [health, setHealth]             = useState<HealthResponse | null>(null);
  const [healthError, setHealthError]   = useState<string | null>(null);
  const [turns, setTurns]               = useState<Turn[]>([]);
  const [submitting, setSubmitting]     = useState(false);
  const [progressStep, setProgressStep] = useState(0);
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError]     = useState<string | null>(null);
  const [calibration, setCalibration]   = useState<Record<string, CalibrationEntry>>({});
  const [calibrationLoading, setCalibrationLoading] = useState(false);
  const [calibrationError, setCalibrationError]     = useState<string | null>(null);
  const { toasts, notify, dismiss } = useToasts();
  const progressTimer                   = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshAudit = useCallback((id: string) => {
    setAuditLoading(true);
    setAuditError(null);
    getAuditTrail(id)
      .then((r) => setAuditEntries(r.actions))
      .catch((e: ApiError) => setAuditError(e.message))
      .finally(() => setAuditLoading(false));
  }, []);

  const refreshCalibration = useCallback(() => {
    setCalibrationLoading(true);
    setCalibrationError(null);
    getCalibration()
      .then((r) => setCalibration(r.calibration))
      .catch((e: ApiError) => setCalibrationError(e.message))
      .finally(() => setCalibrationLoading(false));
  }, []);

  useEffect(() => {
    getHealth()
      .then((r) => { setHealth(r); setHealthError(null); })
      .catch((e: ApiError) => setHealthError(e.message));
  }, []);

  useEffect(() => {
    refreshAudit(sessionId);
    setTurns([]);
  }, [sessionId, refreshAudit]);

  useEffect(() => {
    refreshCalibration();
  }, [refreshCalibration]);

  function startProgress() {
    setProgressStep(0);
    let step = 0;
    progressTimer.current = setInterval(() => {
      step = Math.min(step + 1, PROGRESS_STEPS.length - 1);
      setProgressStep(step);
    }, 1500);
  }

  function stopProgress() {
    if (progressTimer.current) { clearInterval(progressTimer.current); progressTimer.current = null; }
  }

  useEffect(() => stopProgress, []);

  async function handleSubmit(userRequest: string) {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const startedAt = performance.now();

    setTurns((c) => [{ id, request: userRequest, askedAt: Date.now(), state: "pending" }, ...c]);
    setSubmitting(true);
    startProgress();
    // Auto-switch to query page
    setActivePage("query");

    try {
      const result = await proposeAction(userRequest, sessionId);
      const elapsedMs = Math.round(performance.now() - startedAt);
      setTurns((c) => c.map((t) => t.id === id ? { ...t, state: "done", result, elapsedMs } : t));
      refreshAudit(sessionId);
    } catch (err) {
      const message = (err as ApiError).message;
      setTurns((c) => c.map((t) => t.id === id ? { ...t, state: "error", error: message } : t));
      notify(message, "bad");
    } finally {
      stopProgress();
      setSubmitting(false);
    }
  }


  return (
    // 100dvh rather than 100vh: on mobile browsers 100vh includes the
    // collapsing URL bar, which pushes the composer off the bottom of the screen.
    <div className="flex h-[100dvh] bg-surface-subtle font-sans overflow-hidden">
      {/* Sidebar */}
      <Sidebar
        collapsed={collapsed}
        onToggle={() => setCollapsed((c) => !c)}
        activePage={activePage}
        onNavigate={setActivePage}
        mobileOpen={navOpen}
        onMobileClose={() => setNavOpen(false)}
      />

      {/* Drawer backdrop — mobile only, so the open drawer is dismissible */}
      {navOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
          className="md:hidden fixed inset-0 z-40 bg-ink/40 backdrop-blur-[1px]"
        />
      )}

      {/* Main area */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        {/* Top header */}
        <TopHeader
          health={health}
          healthError={healthError}
          sessionId={sessionId}
          onSessionIdChange={setSessionId}
          onNewSession={() => setSessionId(generateSessionId())}
          onMenuClick={() => setNavOpen(true)}
        />

        {/* Page body */}
        <div className="flex flex-1 min-h-0 overflow-hidden">
          {/* Left: query + transcript */}
          <main className="flex-1 overflow-y-auto p-3 sm:p-6 flex flex-col gap-3 sm:gap-4 min-w-0">
            {activePage === "audit" && (
              <AuditTrail
                sessionId={sessionId}
                entries={auditEntries}
                loading={auditLoading}
                error={auditError}
                onRefresh={() => refreshAudit(sessionId)}
              />
            )}

            {activePage === "calibration" && (
              <CalibrationPanel
                table={calibration}
                loading={calibrationLoading}
                error={calibrationError}
                onRefresh={refreshCalibration}
              />
            )}

            {activePage === "query" && (
              <>
            <QueryCard onSubmit={handleSubmit} disabled={submitting} />

            {/* Transcript */}
            {turns.length === 0 && !submitting && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="flex flex-col items-center justify-center py-12 text-center"
              >
                <div className="w-14 h-14 rounded-2xl bg-brand-soft flex items-center
                                justify-center mb-4 text-2xl">
                  ⌘
                </div>
                <p className="font-bold text-ink mb-1">No queries yet</p>
                <p className="text-sm text-ink-muted max-w-xs">
                  Pick an example or type your own. Low-risk queries execute instantly;
                  higher-risk ones wait for your approval.
                </p>
              </motion.div>
            )}

            <AnimatePresence initial={false}>
              {turns.map((turn) => (
                <motion.div
                  key={turn.id}
                  initial={{ opacity: 0, y: -8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.25 }}
                  className="flex flex-col gap-3"
                >
                  {/* Question bubble */}
                  <div className="flex items-start gap-3 bg-white border border-gray-200
                                  rounded-xl px-4 py-3 shadow-card">
                    <span className="shrink-0 text-[10px] font-bold uppercase tracking-widest
                                     text-brand bg-brand-soft border border-brand/20
                                     rounded-full px-2 py-0.5 mt-0.5">
                      You
                    </span>
                    <p className="text-sm font-medium text-ink flex-1 leading-relaxed">
                      {turn.request}
                    </p>
                    {turn.elapsedMs != null && (
                      <span className="text-[11px] text-ink-faint font-mono shrink-0 mt-0.5">
                        {(turn.elapsedMs / 1000).toFixed(1)}s
                      </span>
                    )}
                  </div>

                  {/* Pending */}
                  {turn.state === "pending" && (
                    <div className="flex items-center gap-3 bg-white border border-gray-200
                                    rounded-xl px-4 py-3 shadow-card">
                      <div className="w-4 h-4 border-2 border-gray-200 border-t-brand
                                      rounded-full animate-spin shrink-0" />
                      <div className="flex-1">
                        <p className="text-sm text-ink-muted font-medium">
                          {PROGRESS_STEPS[progressStep]}
                        </p>
                        <div className="mt-1.5 h-1 bg-gray-100 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-brand rounded-full transition-all duration-700"
                            style={{ width: `${((progressStep + 1) / PROGRESS_STEPS.length) * 100}%` }}
                          />
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Error */}
                  {turn.state === "error" && (
                    <div className="bg-risk-high-bg border border-risk-high/20 rounded-xl
                                    px-4 py-3 text-sm text-risk-high font-medium">
                      {turn.error}
                    </div>
                  )}

                  {/* Result */}
                  {turn.state === "done" && turn.result && (
                    <ActionResult
                      result={turn.result}
                      onResolved={() => {
                        refreshAudit(sessionId);
                        // A confirm/reject is also a calibration signal, so the
                        // learned table is stale the moment one is resolved.
                        refreshCalibration();
                      }}
                      onNotify={notify}
                    />
                  )}
                </motion.div>
              ))}
            </AnimatePresence>
              </>
            )}
          </main>
        </div>
      </div>

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
