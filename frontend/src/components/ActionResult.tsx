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
import { RiskBreakdown, routingLabel } from "./RiskBreakdown";

interface ActionResultProps {
  result: ProposeResponse;
  onResolved: () => void;
}

type ResolutionState =
  | { phase: "pending" }
  | { phase: "resolving" }
  | { phase: "done"; response: ResolveResponse }
  | { phase: "error"; message: string };

export function ActionResult({ result, onResolved }: ActionResultProps) {
  const [reviewer, setReviewer] = useState("");
  const [resolution, setResolution] = useState<ResolutionState>({ phase: "pending" });

  async function handleConfirmation(decision: ConfirmationDecision) {
    if (result.routing_decision !== "confirm") return;
    if (!reviewer.trim()) return;
    setResolution({ phase: "resolving" });
    try {
      const res = await resolveConfirmation(result.confirmation_id, decision, reviewer.trim());
      setResolution({ phase: "done", response: res });
      onResolved();
    } catch (err) {
      setResolution({ phase: "error", message: (err as ApiError).message });
    }
  }

  async function handleReview(decision: ReviewDecision) {
    if (result.routing_decision !== "full_review") return;
    if (!reviewer.trim()) return;
    setResolution({ phase: "resolving" });
    try {
      const res = await resolveReview(result.review_id, decision, reviewer.trim());
      setResolution({ phase: "done", response: res });
      onResolved();
    } catch (err) {
      setResolution({ phase: "error", message: (err as ApiError).message });
    }
  }

  return (
    <div className={`action-result action-result--${result.routing_decision}`}>
      <div className="action-result__header">
        <span className="routing-badge" data-decision={result.routing_decision}>
          {routingLabel(result.routing_decision)}
        </span>
      </div>

      <RiskBreakdown score={result.risk_score} />

      {/* ── LOW RISK: auto-executed ─────────────────────────────────── */}
      {result.routing_decision === "autonomous" && (
        <ExecutionResultPane
          result={result.result}
          label="Executed automatically"
          auditId={result.audit_record_id}
        />
      )}

      {/* ── MEDIUM RISK: confirm/reject ─────────────────────────────── */}
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

      {/* ── HIGH RISK: approve/reject ───────────────────────────────── */}
      {result.routing_decision === "full_review" && (
        <div className="action-result__body">
          <p className="action-result__label action-result__label--high">
            ⚠ High risk — full review required
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
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────

function ExecutionResultPane({
  result,
  label,
  auditId,
}: {
  result: ExecutionResult;
  label: string;
  auditId?: string;
}) {
  const isSuccess = result.status === "success";
  return (
    <div className={`exec-result exec-result--${result.status}`}>
      <span className="exec-result__badge">
        {isSuccess ? "✓ Done" : result.status === "failed" ? "✗ Failed" : "— Skipped"}
      </span>
      <p className="exec-result__label">{label}</p>
      <p className="exec-result__detail">{result.detail}</p>
      {result.affected_count > 0 && (
        <p className="exec-result__count">
          {result.affected_count} row{result.affected_count !== 1 ? "s" : ""} affected
          {result.truncated ? " (results capped)" : ""}
        </p>
      )}
      {result.scope_check && (
        <p className="exec-result__scope">{result.scope_check}</p>
      )}
      {result.summary && <SummaryPane summary={result.summary} />}
      {auditId && <p className="action-result__meta">Audit ID: {auditId}</p>}
    </div>
  );
}

function SummaryPane({ summary }: { summary: NonNullable<ExecutionResult["summary"]> }) {
  return (
    <div className="exec-summary">
      <span>{summary.transactions.toLocaleString()} transactions</span>
      <span>{summary.total_quantity.toLocaleString()} items</span>
      <span>{summary.total_revenue.toLocaleString(undefined, { style: "currency", currency: "USD" })} revenue</span>
    </div>
  );
}

function AfterResolution({ response }: { response: ResolveResponse }) {
  const approved = response.status === "confirmed" || response.status === "reviewed";
  return (
    <div className="after-resolution">
      <p className="after-resolution__decision">
        {approved ? "✓ Approved" : "✗ Rejected"} by {response.reviewer}
      </p>
      {response.execution_status && (
        <div
          className={`exec-result exec-result--${response.execution_status}`}
        >
          <span className="exec-result__badge">
            {response.execution_status === "success"
              ? "✓ Done"
              : response.execution_status === "failed"
                ? "✗ Failed"
                : "— Skipped"}
          </span>
          {response.execution_detail && (
            <p className="exec-result__detail">{response.execution_detail}</p>
          )}
        </div>
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
