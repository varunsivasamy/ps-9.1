// Mirrors the response/request shapes defined in
// src/autonomy_engine/main.py, risk_scorer.py, and audit_store.py.

export type RoutingDecision = "autonomous" | "confirm" | "full_review";

export type RiskBand = "low" | "medium" | "high";

export interface RiskScorePayload {
  /** The agent's own judgement. This is what routed the action. */
  risk_band: RiskBand;
  /** Numeric severity consistent with the band. Presentational only. */
  composite_score: number;
  /** One sentence on why the four dimensions add up to this band. */
  rationale: string;
  /** True if the model's severity contradicted its band and was overridden. */
  severity_was_clamped: boolean;
  breakdown: Record<string, string>;
}

export type ExecutionStatus = "success" | "failed" | "skipped";

/** What actually happened when the action ran against the customer store. */
export interface ExecutionResult {
  status: ExecutionStatus;
  detail: string;
  affected_count: number;
  rows: Record<string, string>[];
  truncated: boolean;
  /** Aggregate totals, present only for summarize_transactions. */
  summary?: {
    transactions: number;
    total_quantity: number;
    total_revenue: number;
    groups?: Record<string, { transactions: number; total_quantity: number; total_revenue: number }>;
  };
  snapshot?: string;
  scope_check?: string;
}

export interface ProposeAutonomousResponse {
  routing_decision: "autonomous";
  risk_score: RiskScorePayload;
  result: ExecutionResult;
  audit_record_id: string;
}

export interface ProposeConfirmResponse {
  routing_decision: "confirm";
  risk_score: RiskScorePayload;
  confirmation_id: string;
  preview: string;
}

export interface ProposeFullReviewResponse {
  routing_decision: "full_review";
  risk_score: RiskScorePayload;
  review_id: string;
  preview: string;
}

export type ProposeResponse =
  | ProposeAutonomousResponse
  | ProposeConfirmResponse
  | ProposeFullReviewResponse;

export type ConfirmationDecision = "confirm" | "reject";
export type ReviewDecision = "approve" | "reject";

export interface ResolveResponse {
  /** Authorisation outcome: was this approved? */
  status: string;
  reviewer: string;
  /** Whether it then actually ran. Null on records with nothing to execute. */
  execution_status: ExecutionStatus | null;
  execution_detail: string | null;
  /** Path to the pre-write snapshot, for rolling the change back. */
  snapshot_path: string | null;
}

export interface AuditEntry {
  record_id: string;
  timestamp: string;
  action_type: string | null;
  description: string | null;
  routing_decision: RoutingDecision | null;
  status: string | null;
  reviewer: string | null;
  composite_score: number | null;
  risk_breakdown: Record<string, string> | null;
  execution_status: ExecutionStatus | null;
  execution_detail: string | null;
}

export interface AuditTrailResponse {
  session_id: string;
  actions: AuditEntry[];
}

export interface HealthResponse {
  status: string;
  dynamodb: "reachable" | "unreachable";
}

export interface ApiErrorPayload {
  error: {
    message: string;
  };
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}
