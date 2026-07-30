import { useState } from "react";
import { resolveConfirmation, resolveReview } from "../api";
import type { ApiError } from "../types";
import type {
  ConfirmationDecision,
  ProposeResponse,
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
  | { phase: "resolved"; status: string; reviewer: string }
  | { phase: "error"; message: string };

export function ActionResult({ result, onResolved }: ActionResultProps) {
  const [reviewer, setReviewer] = useState("");
  const [resolution, setResolution] = useState<ResolutionState>({ phase: "pending" });

  const decisionClass = `action-result action-result--${result.routing_decision}`;

  async function handleConfirmationDecision(decision: ConfirmationDecision) {
    if (result.routing_decision !== "confirm") return;
    if (!reviewer.trim()) return;
    setResolution({ phase: "resolving" });
    try {
      const res = await resolveConfirmation(result.confirmation_id, decision, reviewer.trim());
      setResolution({ phase: "resolved", status: res.status, reviewer: res.reviewer });
      onResolved();
    } catch (err) {
      setResolution({ phase: "error", message: (err as ApiError).message });
    }
  }

  async function handleReviewDecision(decision: ReviewDecision) {
    if (result.routing_decision !== "full_review") return;
    if (!reviewer.trim()) return;
    setResolution({ phase: "resolving" });
    try {
      const res = await resolveReview(result.review_id, decision, reviewer.trim());
      setResolution({ phase: "resolved", status: res.status, reviewer: res.reviewer });
      onResolved();
    } catch (err) {
      setResolution({ phase: "error", message: (err as ApiError).message });
    }
  }

  return (
    <div className={decisionClass}>
      <div className="action-result__header">
        <span className="routing-badge" data-decision={result.routing_decision}>
          {routingLabel(result.routing_decision)}
        </span>
      </div>

      <RiskBreakdown score={result.risk_score} />

      {result.routing_decision === "autonomous" && (
        <div className="action-result__body">
          <p className="action-result__preview">Executed without a human in the loop.</p>
          <p className="action-result__detail">{result.result.detail}</p>
          <p className="action-result__meta">Audit record: {result.audit_record_id}</p>
        </div>
      )}

      {result.routing_decision !== "autonomous" && (
        <div className="action-result__body">
          <p className="action-result__preview">{result.preview}</p>

          {resolution.phase === "resolved" ? (
            <p className="action-result__resolved">
              Decision recorded: <strong>{resolution.status}</strong> by {resolution.reviewer}
            </p>
          ) : (
            <div className="approval-controls">
              <label htmlFor="reviewer-name">Reviewer name</label>
              <input
                id="reviewer-name"
                type="text"
                value={reviewer}
                onChange={(e) => setReviewer(e.target.value)}
                placeholder="Your name"
                disabled={resolution.phase === "resolving"}
              />
              <div className="approval-controls__buttons">
                {result.routing_decision === "confirm" ? (
                  <>
                    <button
                      type="button"
                      className="button button--approve"
                      disabled={!reviewer.trim() || resolution.phase === "resolving"}
                      onClick={() => handleConfirmationDecision("confirm")}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="button button--reject"
                      disabled={!reviewer.trim() || resolution.phase === "resolving"}
                      onClick={() => handleConfirmationDecision("reject")}
                    >
                      Reject
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="button button--approve"
                      disabled={!reviewer.trim() || resolution.phase === "resolving"}
                      onClick={() => handleReviewDecision("approve")}
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      className="button button--reject"
                      disabled={!reviewer.trim() || resolution.phase === "resolving"}
                      onClick={() => handleReviewDecision("reject")}
                    >
                      Reject
                    </button>
                  </>
                )}
              </div>
              {resolution.phase === "error" && (
                <p className="form-error">{resolution.message}</p>
              )}
            </div>
          )}

          {result.routing_decision === "confirm" && (
            <p className="action-result__meta">Confirmation ID: {result.confirmation_id}</p>
          )}
          {result.routing_decision === "full_review" && (
            <p className="action-result__meta">Review ID: {result.review_id}</p>
          )}
        </div>
      )}
    </div>
  );
}
