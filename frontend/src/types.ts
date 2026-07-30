// Mirrors the response/request shapes defined in main.py and risk_scorer.py.

export type RoutingDecision = "autonomous" | "confirm" | "full_review";
export type RiskBand = "low" | "medium" | "high";
export type ExecutionStatus = "success" | "failed" | "skipped";

export interface RiskScorePayload {
  risk_band: RiskBand;
  composite_score: number;
  rationale: string;
  severity_was_clamped: boolean;
  escalated_by_floor?: boolean;
  actual_rows?: number;
  breakdown: Record<string, string>;
}

export interface ExecutionResult {
  status: ExecutionStatus;
  detail: string;
  affected_count: number;
  rows: Record<string, string>[];
  truncated: boolean;
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
  status: string;
  reviewer: string;
  execution_status: ExecutionStatus | null;
  execution_detail: string | null;
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
  error: { message: string };
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}
