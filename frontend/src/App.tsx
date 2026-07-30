import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getAuditTrail,
  getHealth,
  proposeAction,
  resolveConfirmation,
  resolveReview,
} from "./api";
import { CalibrationPanel } from "./components/CalibrationPanel";
import type {
  ApiError,
  AuditEntry,
  HealthResponse,
  ProposeResponse,
  RiskBand,
} from "./types";

function generateSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `session-${crypto.randomUUID()}`;
  }
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const PROGRESS_STEPS = [
  "sending request to agent…",
  "measuring data scope…",
  "scoring risk dimensions…",
  "routing decision…",
] as const;

function bandFromScore(score: number | null): RiskBand {
  if (score == null) return "low";
  if (score >= 0.7)  return "high";
  if (score >= 0.35) return "medium";
  return "low";
}

function timeShort(ts: string): string {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toISOString().slice(11, 19);
}

function truncate(s: string | null | undefined, n: number): string {
  if (!s) return "—";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export default function App() {
  const [sessionId, setSessionId]       = useState(generateSessionId);
  const [sessionDraft, setSessionDraft] = useState(sessionId);
  const [health, setHealth]             = useState<HealthResponse | null>(null);
  const [healthError, setHealthError]   = useState<string | null>(null);
  const [submitting, setSubmitting]     = useState(false);
  const [progressStep, setProgressStep] = useState(0);
  const [submitError, setSubmitError]   = useState<string | null>(null);
  const [latestResult, setLatestResult] = useState<ProposeResponse | null>(null);
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError]     = useState<string | null>(null);
  const [calibrationTick, setCalibrationTick] = useState(0);

  const [buffer, setBuffer]     = useState("");
  const [reviewer, setReviewer] = useState("");
  const [filter, setFilter]     = useState("");
  const [cursor, setCursor]     = useState(0);

  const progressTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const cmdInputRef   = useRef<HTMLInputElement>(null);

  // ── data ────────────────────────────────────────────────────────────────

  const refreshAudit = useCallback((id: string) => {
    setAuditLoading(true);
    setAuditError(null);
    getAuditTrail(id)
      .then((res) => setAuditEntries(res.actions))
      .catch((err: ApiError) => setAuditError(err.message))
      .finally(() => setAuditLoading(false));
  }, []);

  useEffect(() => {
    getHealth()
      .then((res) => { setHealth(res); setHealthError(null); })
      .catch((err: ApiError) => setHealthError(err.message));
  }, []);

  useEffect(() => {
    setSessionDraft(sessionId);
    refreshAudit(sessionId);
    setLatestResult(null);
    setCursor(0);
  }, [sessionId, refreshAudit]);

  function startProgress() {
    setProgressStep(0);
    let step = 0;
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

  const handleSubmit = useCallback(async (userRequest: string) => {
    setSubmitting(true);
    setSubmitError(null);
    setLatestResult(null);
    startProgress();
    try {
      const result = await proposeAction(userRequest, sessionId);
      setLatestResult(result);
      refreshAudit(sessionId);
      setCalibrationTick((n) => n + 1);
    } catch (err) {
      setSubmitError((err as ApiError).message);
    } finally {
      stopProgress();
      setSubmitting(false);
    }
  }, [sessionId, refreshAudit]);

  function onNewSession() {
    setSessionId(generateSessionId());
  }

  function commitSessionDraft() {
    const trimmed = sessionDraft.trim();
    if (!trimmed || trimmed === sessionId) {
      setSessionDraft(sessionId);
      return;
    }
    setSessionId(trimmed);
  }

  // ── derived ─────────────────────────────────────────────────────────────

  const rows = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const list = auditEntries.slice().reverse();
    return q
      ? list.filter((r) =>
          [r.description, r.action_type, r.status, r.reviewer]
            .filter(Boolean)
            .some((f) => (f as string).toLowerCase().includes(q))
        )
      : list;
  }, [auditEntries, filter]);

  useEffect(() => {
    if (cursor >= rows.length) setCursor(Math.max(0, rows.length - 1));
  }, [rows.length, cursor]);

  const selected = rows[cursor] ?? null;

  // Clarification is not in the audit trail; render it in DETAIL instead.
  const showClarification =
    latestResult?.routing_decision === "needs_clarification" && !submitting;

  const pending = auditEntries.filter((e) => e.status === "pending").length;
  const totals = useMemo(() => {
    let high = 0, medium = 0, low = 0;
    for (const e of auditEntries) {
      const b = bandFromScore(e.composite_score);
      if      (b === "high")   high++;
      else if (b === "medium") medium++;
      else                     low++;
    }
    return { high, medium, low };
  }, [auditEntries]);

  // ── keyboard: j/k, /, n, R ─────────────────────────────────────────────

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement;
      const inField =
        t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable;
      if (inField) return;

      if      (e.key === "j") { e.preventDefault(); setCursor((c) => Math.min(rows.length - 1, c + 1)); }
      else if (e.key === "k") { e.preventDefault(); setCursor((c) => Math.max(0, c - 1)); }
      else if (e.key === "/") { e.preventDefault(); cmdInputRef.current?.focus(); }
      else if (e.key === "n") { e.preventDefault(); onNewSession(); }
      else if (e.key === "R") { e.preventDefault(); refreshAudit(sessionId); setCalibrationTick((n) => n + 1); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows.length, refreshAudit, sessionId]);

  function onCommandSubmit(e: React.FormEvent) {
    e.preventDefault();
    const t = buffer.trim();
    if (!t) return;
    handleSubmit(t);
    setBuffer("");
  }

  // ── render ──────────────────────────────────────────────────────────────

  return (
    <div className="tui">
      <div className="tui__frame">
        {/* Header */}
        <div className="tui-topline">
          <span className="tui-brand">autonomy-engine</span>
          <span>—</span>
          <label>
            session:
            <input
              className="tui-topline__session"
              value={sessionDraft}
              onChange={(e) => setSessionDraft(e.target.value)}
              onBlur={commitSessionDraft}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); commitSessionDraft(); (e.currentTarget as HTMLInputElement).blur(); }
                if (e.key === "Escape") { setSessionDraft(sessionId); (e.currentTarget as HTMLInputElement).blur(); }
              }}
              spellCheck={false}
            />
          </label>
          <span>pending:<b className="tui-strong">{pending}</b></span>
          <span>total:<b className="tui-strong">{auditEntries.length}</b></span>
          <span>H:<b className="tui-fg-r">{totals.high}</b> M:<b className="tui-fg-y">{totals.medium}</b> L:<b className="tui-fg-g">{totals.low}</b></span>
          <span className="tui-spacer" />
          <HealthChip health={health} error={healthError} />
        </div>

        {/* Audit table */}
        <div className="tui-panel tui-panel--table">
          <div className="tui-panel__title">
            <span>[1] AUDIT</span>
            <span className="tui-panel__meta">
              {rows.length}/{auditEntries.length} rows
              {auditLoading && "  · refreshing…"}
              {auditError && `  · error: ${auditError}`}
            </span>
          </div>
          <div className="tui-table">
            <div className="tui-table__head">
              <span className="tui-col">B</span>
              <span className="tui-col">TIME</span>
              <span className="tui-col">TYPE</span>
              <span className="tui-col">DESCRIPTION</span>
              <span className="tui-col">SCORE</span>
              <span className="tui-col">STATUS</span>
              <span className="tui-col">REVIEWER</span>
            </div>
            <div className="tui-table__body">
              {rows.length === 0 && (
                <div className="tui-empty">
                  no rows{filter && ` matching /${filter}/`}
                </div>
              )}
              {rows.map((r, i) => {
                const band = bandFromScore(r.composite_score);
                return (
                  <div
                    key={r.record_id}
                    className={`tui-row ${i === cursor ? "tui-row--sel" : ""}`}
                    onClick={() => setCursor(i)}
                  >
                    <span className="tui-col">
                      <span className={`tui-b tui-b--${band}`}>{band[0].toUpperCase()}</span>
                    </span>
                    <span className="tui-col">{timeShort(r.timestamp)}</span>
                    <span className="tui-col">{truncate(r.action_type, 20)}</span>
                    <span className="tui-col tui-col--desc">{truncate(r.description, 60)}</span>
                    <span className="tui-col">{r.composite_score?.toFixed(2) ?? "—"}</span>
                    <span className="tui-col">{r.status ?? "—"}</span>
                    <span className="tui-col">{truncate(r.reviewer, 12)}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* Detail | Hotkeys | Calibration */}
        <div className="tui-grid">
          <div className="tui-panel">
            <div className="tui-panel__title">
              <span>[2] DETAIL</span>
              <span className="tui-panel__meta">
                {showClarification ? "clarification requested"
                 : selected         ? `record ${selected.record_id.slice(0, 12)}…`
                                    : "(none)"}
              </span>
            </div>
            {showClarification ? (
              <ClarificationBlock
                result={latestResult!}
                onClarify={(answer) =>
                  handleSubmit(
                    `${(latestResult as { question: string }).question} — clarification: ${answer}`
                  )
                }
              />
            ) : selected ? (
              <DetailPanel
                entry={selected}
                reviewer={reviewer}
                onReviewerChange={setReviewer}
                onResolved={() => {
                  refreshAudit(sessionId);
                  setCalibrationTick((n) => n + 1);
                }}
              />
            ) : (
              <p className="tui-empty">Select a row with the cursor (j/k) or by clicking.</p>
            )}
          </div>

          <div className="tui-panel">
            <div className="tui-panel__title"><span>[3] HOTKEYS</span></div>
            <ul className="tui-keys">
              <li><kbd>j</kbd><kbd>k</kbd> move cursor</li>
              <li><kbd>a</kbd> approve selected</li>
              <li><kbd>r</kbd> reject selected</li>
              <li><kbd>/</kbd> focus command bar</li>
              <li><kbd>n</kbd> new session</li>
              <li><kbd>⇧R</kbd> refresh audit + calibration</li>
            </ul>
            <div className="tui-help">
              <p className="tui-help__title">// legend</p>
              <p><span className="tui-b tui-b--low">L</span> low → autonomous</p>
              <p><span className="tui-b tui-b--medium">M</span> medium → confirm</p>
              <p><span className="tui-b tui-b--high">H</span> high → full_review</p>
            </div>
          </div>

          <CalibrationPanel refreshTick={calibrationTick} />
        </div>

        {/* Command bar */}
        <form className="tui-cmdbar" onSubmit={onCommandSubmit}>
          <span className="tui-cmdbar__prompt">:</span>
          <input
            ref={cmdInputRef}
            className="tui-cmdbar__input"
            value={buffer}
            onChange={(e) => setBuffer(e.target.value)}
            placeholder="propose an action    (Enter to submit)   |   use /filter on the right to search"
            spellCheck={false}
            disabled={submitting}
            onKeyDown={(e) => { if (e.key === "Escape") (e.currentTarget as HTMLInputElement).blur(); }}
          />
          <input
            className="tui-cmdbar__filter"
            placeholder="/filter rows"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            spellCheck={false}
          />
        </form>

        {/* Status lines */}
        {submitting  && <div className="tui-log">→ {PROGRESS_STEPS[progressStep]}</div>}
        {submitError && <div className="tui-log tui-log--err">✗ {submitError}</div>}
      </div>
    </div>
  );
}

// ── DetailPanel ─────────────────────────────────────────────────────────────

function DetailPanel({
  entry, reviewer, onReviewerChange, onResolved,
}: {
  entry: AuditEntry;
  reviewer: string;
  onReviewerChange: (v: string) => void;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr]   = useState<string | null>(null);

  const isPending     = entry.status === "pending";
  const isConfirmable = isPending && entry.routing_decision === "confirm";
  const isReviewable  = isPending && entry.routing_decision === "full_review";

  const decide = useCallback(async (kind: "primary" | "secondary") => {
    if (!reviewer.trim() || (!isConfirmable && !isReviewable)) return;
    setBusy(true); setErr(null);
    try {
      if (isConfirmable) {
        await resolveConfirmation(entry.record_id, kind === "primary" ? "confirm" : "reject", reviewer.trim());
      } else if (isReviewable) {
        await resolveReview(entry.record_id, kind === "primary" ? "approve" : "reject", reviewer.trim());
      }
      onResolved();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }, [entry.record_id, isConfirmable, isReviewable, reviewer, onResolved]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement;
      const inField =
        t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable;
      if (inField) return;
      if (!isPending || !reviewer.trim()) return;
      if (e.key === "a") { e.preventDefault(); decide("primary"); }
      if (e.key === "r") { e.preventDefault(); decide("secondary"); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isPending, reviewer, decide]);

  return (
    <div className="tui-detail">
      <DetailRow k="description"        v={entry.description ?? "—"} />
      <DetailRow k="action_type"        v={entry.action_type ?? "—"} />
      <DetailRow k="routing"            v={entry.routing_decision ?? "—"} />
      <DetailRow k="status"             v={entry.status ?? "—"} />
      <DetailRow k="score"              v={entry.composite_score?.toFixed(2) ?? "—"} />
      {entry.risk_breakdown &&
        Object.entries(entry.risk_breakdown).map(([k, v]) => (
          <DetailRow key={k} k={k} v={v} />
        ))
      }
      {entry.execution_detail && (
        <div className={`tui-detail__row tui-detail__row--exec tui-detail__row--${entry.execution_status ?? "unknown"}`}>
          <span className="tui-detail__k">execution</span>
          <span className="tui-detail__v">{entry.execution_detail}</span>
        </div>
      )}

      {isPending && (
        <div className="tui-approve">
          <input
            className="tui-cmdbar__filter"
            placeholder="reviewer name"
            value={reviewer}
            onChange={(e) => onReviewerChange(e.target.value)}
            disabled={busy}
          />
          <button
            className="tui-btn tui-btn--go"
            onClick={() => decide("primary")}
            disabled={!reviewer.trim() || busy}
          >
            {busy ? "…" : isConfirmable ? "a: confirm" : "a: approve"}
          </button>
          <button
            className="tui-btn tui-btn--stop"
            onClick={() => decide("secondary")}
            disabled={!reviewer.trim() || busy}
          >
            r: reject
          </button>
        </div>
      )}
      {err && <p className="tui-log tui-log--err">✗ {err}</p>}
    </div>
  );
}

function DetailRow({ k, v }: { k: string; v: string }) {
  return (
    <div className="tui-detail__row">
      <span className="tui-detail__k">{k}</span>
      <span className="tui-detail__v">{v}</span>
    </div>
  );
}

// ── ClarificationBlock ──────────────────────────────────────────────────────

function ClarificationBlock({
  result, onClarify,
}: {
  result: ProposeResponse;
  onClarify: (answer: string) => void;
}) {
  const [answer, setAnswer] = useState("");
  if (result.routing_decision !== "needs_clarification") return null;
  return (
    <div className="tui-clarify">
      <p className="tui-clarify__q">? {result.question}</p>
      <p className="tui-clarify__why">// {result.why}</p>
      {result.options.length > 0 && (
        <div className="tui-clarify__chips">
          {result.options.map((o) => (
            <button
              key={o}
              type="button"
              className="tui-clarify__chip"
              onClick={() => setAnswer(o)}
            >
              {o}
            </button>
          ))}
        </div>
      )}
      <div className="tui-clarify__row">
        <input
          className="tui-cmdbar__filter"
          placeholder="clarification"
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
        />
        <button
          className="tui-btn tui-btn--go"
          disabled={!answer.trim()}
          onClick={() => onClarify(answer.trim())}
        >
          submit
        </button>
      </div>
    </div>
  );
}

// ── HealthChip ──────────────────────────────────────────────────────────────

function HealthChip({ health, error }: {
  health: HealthResponse | null; error: string | null;
}) {
  const state = error ? "down" : health?.dynamodb === "reachable" ? "up" : "warn";
  const label =
    error   ? "api:down" :
    health  ? `api:${health.status} · ddb:${health.dynamodb}` :
              "api:…";
  return (
    <span className={`tui-health tui-health--${state}`} title={error ?? undefined}>
      <span className="tui-health__dot" />{label}
    </span>
  );
}
