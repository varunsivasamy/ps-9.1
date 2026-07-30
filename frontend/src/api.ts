import {
  ApiError,
  type ApiErrorPayload,
  type AuditTrailResponse,
  type CalibrationSnapshotResponse,
  type ConfirmationDecision,
  type HealthResponse,
  type ProposeResponse,
  type ResolveResponse,
  type ReviewDecision,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(0, `Could not reach the API at ${BASE_URL}. Is it running?`);
  }

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const payload = (await response.json()) as ApiErrorPayload;
      message = payload.error?.message ?? message;
    } catch {
      // body wasn't JSON — fall back to the generic message above
    }
    throw new ApiError(response.status, message);
  }

  return response.json() as Promise<T>;
}

export function proposeAction(userRequest: string, sessionId: string): Promise<ProposeResponse> {
  return request<ProposeResponse>("/actions/propose", {
    method: "POST",
    body: JSON.stringify({ user_request: userRequest, session_id: sessionId }),
  });
}

export function resolveConfirmation(
  confirmationId: string,
  decision: ConfirmationDecision,
  reviewer: string,
): Promise<ResolveResponse> {
  return request<ResolveResponse>(`/confirmations/${confirmationId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ decision, reviewer }),
  });
}

export function resolveReview(
  reviewId: string,
  decision: ReviewDecision,
  reviewer: string,
): Promise<ResolveResponse> {
  return request<ResolveResponse>(`/reviews/${reviewId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ decision, reviewer }),
  });
}

export function getAuditTrail(sessionId: string): Promise<AuditTrailResponse> {
  return request<AuditTrailResponse>(`/audit/${encodeURIComponent(sessionId)}`);
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function getCalibration(): Promise<CalibrationSnapshotResponse> {
  return request<CalibrationSnapshotResponse>("/calibration");
}

export { BASE_URL as apiBaseUrl };
