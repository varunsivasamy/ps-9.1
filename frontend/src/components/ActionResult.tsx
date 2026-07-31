import { useState } from "react";
import { resolveConfirmation, resolveReview } from "../api";
import type {
  ApiError,
  ConfirmationDecision,
  ExecutionResult,
  ProposeResponse,
  ResolveResponse,
  ReviewDecision,
} from "../types";
import { DataTable } from "./DataTable";
import { GroupChart, type GroupDatum } from "./GroupChart";
import { RiskBreakdown, routingLabel } from "./RiskBreakdown";

interface ActionResultProps {
  result: ProposeResponse;
  onResolved: () => void;
  onNotify?: (message: string, tone: "good" | "bad") => void;
}

type ResolutionState =
  | { phase: "pending" }
  | { phase: "resolving" }
  | { phase: "done"; response: ResolveResponse }
  | { phase: "error"; message: string };

export function ActionResult({ result, onResolved, onNotify }: ActionResultProps) {
  const [reviewer, setReviewer] = useState("");
  const [resolution, setResolution] = useState<ResolutionState>({ phase: "pending" });

  async function handleConfirmation(decision: ConfirmationDecision) {
    if (result.routing_decision !== "confirm") return;
    if (!reviewer.trim()) return;
    setResolution({ phase: "resolving" });
    try {
      const res = await resolveConfirmation(result.confirmation_id, decision, reviewer.trim());
      setResolution({ phase: "done", response: res });
      onNotify?.(
        decision === "confirm" ? "Action confirmed and executed" : "Action rejected",
        decision === "confirm" ? "good" : "bad",
      );
      onResolved();
    } catch (err) {
      const message = (err as ApiError).message;
      setResolution({ phase: "error", message });
      onNotify?.(message, "bad");
    }
  }

  async function handleReview(decision: ReviewDecision) {
    if (result.routing_decision !== "full_review") return;
    if (!reviewer.trim()) return;
    setResolution({ phase: "resolving" });
    try {
      const res = await resolveReview(result.review_id, decision, reviewer.trim());
      setResolution({ phase: "done", response: res });
      onNotify?.(
        decision === "approve" ? "Action approved and executed" : "Action rejected",
        decision === "approve" ? "good" : "bad",
      );
      onResolved();
    } catch (err) {
      const message = (err as ApiError).message;
      setResolution({ phase: "error", message });
      onNotify?.(message, "bad");
    }
  }

  return (
    <div className={`action-result action-result--${result.routing_decision}`}>
      <div className="action-result__header">
        <span className="routing-badge" data-decision={result.routing_decision}>
          {routingLabel(result.routing_decision)}
        </span>
      </div>

      {result.routing_decision === "autonomous" && (
        <>
          <AnswerCard answer={result.answer} status={result.result.status} />
          <ExecutionResultPane result={result.result} auditId={result.audit_record_id} />
        </>
      )}

      {result.routing_decision === "confirm" && (
        <div className="action-result__body">
          <p className="action-result__preview">{result.preview}</p>
          {resolution.phase === "done" ? (
            <AfterResolution response={resolution.response} />
          ) : (
            <ApprovalControls
              loading={resolution.phase === "resolving"}
              reviewer={reviewer}
              onReviewerChange={setReviewer}
              error={resolution.phase === "error" ? resolution.message : null}
              primaryLabel="Confirm"
              secondaryLabel="Reject"
              onPrimary={() => handleConfirmation("confirm")}
              onSecondary={() => handleConfirmation("reject")}
            />
          )}
          <p className="action-result__meta">Confirmation ID: {result.confirmation_id}</p>
        </div>
      )}

      {result.routing_decision === "full_review" && (
        <div className="action-result__body">
          <p className="action-result__label action-result__label--high">
            High risk — full review required
          </p>
          <p className="action-result__preview">{result.preview}</p>
          {resolution.phase === "done" ? (
            <AfterResolution response={resolution.response} />
          ) : (
            <ApprovalControls
              loading={resolution.phase === "resolving"}
              reviewer={reviewer}
              onReviewerChange={setReviewer}
              error={resolution.phase === "error" ? resolution.message : null}
              primaryLabel="Approve"
              secondaryLabel="Reject"
              onPrimary={() => handleReview("approve")}
              onSecondary={() => handleReview("reject")}
            />
          )}
          <p className="action-result__meta">Review ID: {result.review_id}</p>
        </div>
      )}

      {"risk_score" in result && <RiskBreakdown score={result.risk_score} />}
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────

function AnswerCard({
  answer,
  status,
}: {
  answer?: string | null;
  status?: ExecutionResult["status"];
}) {
  const [copied, setCopied] = useState(false);
  if (!answer) return null;

  async function copy() {
    try {
      await navigator.clipboard.writeText(answer as string);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard is unavailable over plain http on some browsers; the text is
      // selectable either way, so there is nothing to recover from.
    }
  }

  return (
    <div className="answer-card" data-status={status}>
      <div className="answer-card__head">
        <span className="answer-card__label">Answer</span>
        <button type="button" className="answer-card__copy" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="answer-card__text">{answer}</p>
    </div>
  );
}

function formatCurrency(value: number): string {
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function ExecutionResultPane({
  result,
  auditId,
}: {
  result: ExecutionResult;
  auditId?: string;
}) {
  const badge =
    result.status === "success" ? "Done" : result.status === "failed" ? "Failed" : "Skipped";

  return (
    <div className={`exec-result exec-result--${result.status}`}>
      <div className="exec-result__top">
        <span className="exec-result__badge">{badge}</span>
        <p className="exec-result__detail">{result.detail}</p>
      </div>

      {result.affected_count > 0 && (
        <p className="exec-result__count">
          {result.affected_count.toLocaleString()} row
          {result.affected_count !== 1 ? "s" : ""} affected
          {result.truncated ? " · listing capped" : ""}
        </p>
      )}

      {result.summary && <SummaryPane summary={result.summary} />}

      {result.rows.length > 0 && (
        <DataTable
          rows={result.rows}
          caption="Matching records"
          truncated={result.truncated}
        />
      )}

      {result.scope_check && <p className="exec-result__scope">{result.scope_check}</p>}
      {result.snapshot && (
        <p className="action-result__meta">Rollback snapshot: {result.snapshot}</p>
      )}
      {auditId && <p className="action-result__meta">Audit ID: {auditId}</p>}
    </div>
  );
}

function SummaryPane({ summary }: { summary: NonNullable<ExecutionResult["summary"]> }) {
  const groups = summary.groups ?? {};
  const chartData: GroupDatum[] = Object.entries(groups).map(([name, totals]) => ({
    name,
    value: totals.total_revenue,
  }));

  const groupRows = Object.entries(groups)
    .sort((a, b) => b[1].total_revenue - a[1].total_revenue)
    .map(([name, totals]) => ({
      group: name,
      transactions: String(totals.transactions),
      items: String(totals.total_quantity),
      revenue: totals.total_revenue.toFixed(2),
    }));

  return (
    <div className="summary-pane">
      <div className="stat-row">
        <Stat label="Transactions" value={summary.transactions.toLocaleString()} />
        <Stat label="Items" value={summary.total_quantity.toLocaleString()} />
        <Stat label="Revenue" value={formatCurrency(summary.total_revenue)} emphasis />
      </div>

      {chartData.length > 1 && (
        <GroupChart data={chartData} format={formatCurrency} measure="Revenue" />
      )}

      {groupRows.length > 0 && <DataTable rows={groupRows} caption="Group breakdown" />}
    </div>
  );
}

function Stat({
  label,
  value,
  emphasis,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
}) {
  return (
    <div className={`stat${emphasis ? " stat--emphasis" : ""}`}>
      <span className="stat__value">{value}</span>
      <span className="stat__label">{label}</span>
    </div>
  );
}

function AfterResolution({ response }: { response: ResolveResponse }) {
  const approved = response.status === "confirmed" || response.status === "reviewed";
  return (
    <div className="after-resolution">
      <p
        className={`after-resolution__decision after-resolution__decision--${approved ? "yes" : "no"}`}
      >
        {approved ? "Approved" : "Rejected"} by {response.reviewer}
      </p>

      <AnswerCard answer={response.answer} status={response.execution_status ?? undefined} />

      {response.result ? (
        <ExecutionResultPane result={response.result} />
      ) : (
        response.execution_status && (
          <div className={`exec-result exec-result--${response.execution_status}`}>
            <div className="exec-result__top">
              <span className="exec-result__badge">
                {response.execution_status === "success"
                  ? "Done"
                  : response.execution_status === "failed"
                    ? "Failed"
                    : "Skipped"}
              </span>
              {response.execution_detail && (
                <p className="exec-result__detail">{response.execution_detail}</p>
              )}
            </div>
          </div>
        )
      )}
    </div>
  );
}

function ApprovalControls({
  loading,
  reviewer,
  onReviewerChange,
  error,
  primaryLabel,
  secondaryLabel,
  onPrimary,
  onSecondary,
}: {
  loading: boolean;
  reviewer: string;
  onReviewerChange: (v: string) => void;
  error: string | null;
  primaryLabel: string;
  secondaryLabel: string;
  onPrimary: () => void;
  onSecondary: () => void;
}) {
  return (
    <div className="approval-controls">
      <label htmlFor="reviewer-name">Reviewer name</label>
      <input
        id="reviewer-name"
        type="text"
        value={reviewer}
        onChange={(e) => onReviewerChange(e.target.value)}
        placeholder="Your name"
        disabled={loading}
      />
      <div className="approval-controls__buttons">
        <button
          type="button"
          className="button button--approve"
          disabled={!reviewer.trim() || loading}
          onClick={onPrimary}
        >
          {loading ? "Processing…" : primaryLabel}
        </button>
        <button
          type="button"
          className="button button--reject"
          disabled={!reviewer.trim() || loading}
          onClick={onSecondary}
        >
          {secondaryLabel}
        </button>
      </div>
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}
