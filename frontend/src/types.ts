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
  calibrated?: boolean;
  actual_rows?: number;
  breakdown: Record<string, string>;
}

export interface SummaryTotals {
  transactions: number;
  total_quantity: number;
  total_revenue: number;
}

export interface ExecutionResult {
  status: ExecutionStatus;
  detail: string;
  affected_count: number;
  rows: Record<string, string>[];
  truncated: boolean;
  summary?: SummaryTotals & {
    groups?: Record<string, SummaryTotals>;
  };
  snapshot?: string;
  scope_check?: string;
}

export interface ProposeAutonomousResponse {
  routing_decision: "autonomous";
  risk_score: RiskScorePayload;
  result: ExecutionResult;
  /** The agent's prose answer to the original request, grounded in `result`. */
  answer?: string | null;
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
  /** Full execution payload, so an approved read still shows its data. */
  result?: ExecutionResult | null;
  answer?: string | null;
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

/** One row of the adaptive-calibration table, keyed by action_type. */
export interface CalibrationEntry {
  confirms_without_modification: number;
  rejects_or_modifications: number;
  band_offset: number;
}

export interface CalibrationResponse {
  calibration: Record<string, CalibrationEntry>;
}

/**
 * One request/response exchange. The console keeps every turn of a session
 * rather than replacing the last one, so an answer stays readable while the
 * next question is being asked.
 */
export interface Turn {
  id: string;
  request: string;
  askedAt: number;
  state: "pending" | "done" | "error";
  result?: ProposeResponse;
  error?: string;
  /** Wall-clock time the agent took, in ms. */
  elapsedMs?: number;
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
