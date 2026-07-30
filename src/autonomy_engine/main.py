"""FastAPI app wiring risk_scorer, agent_actions, audit_store, and confirmation
into the working demo API.

Every request is logged as one structured JSON line (session_id, action_type,
routing_decision, latency_ms) so the deployed Lambda's CloudWatch stream is
queryable. Every error path returns structured JSON with the right HTTP status
and never leaks a raw stack trace to the caller.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from autonomy_engine import audit_store, confirmation, executor, risk_scorer
from autonomy_engine.agent_actions import (
    AgentActionError,
    propose_action,
)
from autonomy_engine.logging_config import configure_logging
from autonomy_engine.risk_scorer import RiskAssessment, build_assessment, route_action

configure_logging()
logger = logging.getLogger("autonomy_engine")

app = FastAPI(
    title="PS-9.1 Graduated Autonomy Engine",
    description=(
        "Scores every proposed AI agent action on risk and routes it to "
        "autonomous execution, human confirmation, or full review."
    ),
    version="0.1.0",
)

# --------------------------------------------------------------------------
# CORS
#
# The React front end is served from a different origin (localhost:5173 in
# dev, a Vercel domain in production), so the browser needs an explicit
# allow-list rather than the default same-origin policy. Comma-separated list
# in CORS_ALLOWED_ORIGINS; defaults to the two Vite dev ports so local
# development works with zero configuration.
# --------------------------------------------------------------------------

_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
_allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", _default_origins).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Request/response bodies
# --------------------------------------------------------------------------


class ProposeRequest(BaseModel):
    user_request: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class ResolveConfirmationRequest(BaseModel):
    decision: Literal["confirm", "reject"]
    reviewer: str = Field(min_length=1)


class ResolveReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1)


# --------------------------------------------------------------------------
# Structured error handling
#
# Every exception this app can raise funnels through one of these three
# handlers, so callers always get {"error": {...}} JSON with the right status
# and never a raw traceback.
# --------------------------------------------------------------------------


class ApiError(Exception):
    """An error with a known HTTP status, safe to show to the caller."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


@app.exception_handler(ApiError)
async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(exc.status_code, exc.message)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default is 422; the plan calls for 400 on bad input, so this
    # overrides Starlette's built-in handler.
    return _error_response(400, f"invalid request body: {exc.errors()}")


@app.exception_handler(audit_store.InvalidRecordIdError)
async def _handle_invalid_id(request: Request, exc: audit_store.InvalidRecordIdError) -> JSONResponse:
    return _error_response(400, str(exc))


@app.exception_handler(confirmation.InvalidDecisionError)
async def _handle_invalid_decision(
    request: Request, exc: confirmation.InvalidDecisionError
) -> JSONResponse:
    return _error_response(400, str(exc))


@app.exception_handler(audit_store.RecordNotFoundError)
async def _handle_not_found(request: Request, exc: audit_store.RecordNotFoundError) -> JSONResponse:
    return _error_response(404, str(exc))


@app.exception_handler(AgentActionError)
async def _handle_agent_error(request: Request, exc: AgentActionError) -> JSONResponse:
    # The agent itself failed (bad key, refusal, malformed output) -- that is a
    # problem with fulfilling the request, not with the caller's input.
    return _error_response(502, str(exc))


@app.exception_handler(audit_store.AuditStoreError)
async def _handle_audit_error(request: Request, exc: audit_store.AuditStoreError) -> JSONResponse:
    # Covers "already resolved" / "wrong queue" (a 409 conflict) as well as
    # genuine DynamoDB failures (500). The message itself distinguishes them for
    # a human; HTTP-wise both are "the request could not be completed as sent".
    message = str(exc)
    status = 409 if ("already" in message or "correct queue" in message) else 500
    return _error_response(status, message)


@app.exception_handler(executor.ExecutionError)
async def _handle_execution_error(request: Request, exc: executor.ExecutionError) -> JSONResponse:
    # A tool with no execution branch is a wiring bug on our side, not the
    # caller's -- 500, and loud enough to notice.
    logger.error("execution wiring error", extra={"path": request.url.path})
    return _error_response(500, str(exc))


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Last resort: never let a raw traceback reach the caller.
    logger.exception("unhandled exception", extra={"path": request.url.path})
    return _error_response(500, "internal server error")


# --------------------------------------------------------------------------
# Request logging middleware
# --------------------------------------------------------------------------


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "request handled",
        extra={
            "path": request.url.path,
            "method": request.method,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "session_id": getattr(request.state, "session_id", None),
            "action_type": getattr(request.state, "action_type", None),
            "routing_decision": getattr(request.state, "routing_decision", None),
        },
    )
    return response


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.post("/actions/propose")
async def propose(body: ProposeRequest, request: Request) -> dict:
    """Propose an action, let the agent judge its risk, and route it.

    - needs_clarification: the agent could not safely interpret the request and
      asked a question instead. Nothing is scored, queued, or executed.
    - autonomous: executes against the customer store immediately, logged as
      auto_executed with the real outcome.
    - confirm: creates a pending confirmation and executes nothing until a human
      resolves it.
    - full_review: creates a pending review entry and executes nothing until a
      human approves it.
    """
    request.state.session_id = body.session_id

    context: dict[str, object] = {}

    action = propose_action(body.user_request, context)

    # Ground the band in what the action really touches.
    # preflight is strictly read-only — safe on an action a human may reject.
    scope = executor.preflight(action.tool_name, action.parameters)

    assessment = build_assessment(action.to_risk_factors()).with_measured_scope(
        scope.actual_rows
    )
    decision = route_action(assessment)

    # Blast-radius floor — can only escalate, never lower supervision.
    decision, floor_note = risk_scorer.apply_blast_radius_floor(
        decision,
        actual_rows=scope.actual_rows,
        is_mutation=scope.is_mutation,
        is_destructive=scope.is_destructive,
        resolvable=scope.resolvable,
    )
    if floor_note:
        logger.warning(
            "blast-radius floor escalated routing",
            extra={
                "session_id": body.session_id,
                "tool_name": action.tool_name,
                "actual_rows": scope.actual_rows,
                "final_decision": decision,
            },
        )
        assessment = assessment.with_override(floor_note)

    request.state.action_type = action.action_type
    request.state.routing_decision = decision

    if decision == "autonomous":
        record, result = confirmation.execute_autonomously(
            action, assessment, session_id=body.session_id
        )
        return {
            "routing_decision": decision,
            "risk_score": _score_payload(assessment),
            "result": result.to_payload(),
            "audit_record_id": record["record_id"],
        }

    if decision == "confirm":
        confirmation_id = confirmation.create_confirmation_request(
            action, assessment, session_id=body.session_id
        )
        return {
            "routing_decision": decision,
            "risk_score": _score_payload(assessment),
            "confirmation_id": confirmation_id,
            "preview": action.description,
        }

    review_id = confirmation.create_review_request(
        action, assessment, session_id=body.session_id
    )
    return {
        "routing_decision": decision,
        "risk_score": _score_payload(assessment),
        "review_id": review_id,
        "preview": action.description,
    }


@app.post("/confirmations/{confirmation_id}/resolve")
async def resolve_confirmation_endpoint(
    confirmation_id: str, body: ResolveConfirmationRequest, request: Request
) -> dict:
    """Resolve a pending medium-risk confirmation, executing it if confirmed."""
    record = confirmation.resolve_confirmation(confirmation_id, body.decision, body.reviewer)
    request.state.session_id = record.get("session_id")
    request.state.routing_decision = record.get("routing_decision")
    return _resolution_payload(record)


@app.post("/reviews/{review_id}/resolve")
async def resolve_review_endpoint(
    review_id: str, body: ResolveReviewRequest, request: Request
) -> dict:
    """Resolve a pending high-risk review, executing it if approved."""
    record = confirmation.resolve_review(review_id, body.decision, body.reviewer)
    request.state.session_id = record.get("session_id")
    request.state.routing_decision = record.get("routing_decision")
    return _resolution_payload(record)


@app.get("/audit/{session_id}")
async def audit_trail(session_id: str, request: Request) -> dict:
    """The full, human-readable audit trail for a session."""
    request.state.session_id = session_id
    trail = audit_store.get_audit_trail(session_id)
    return {
        "session_id": session_id,
        "actions": [_trail_entry(record) for record in trail],
    }


@app.get("/health")
async def health() -> dict:
    """Liveness probe: reports whether DynamoDB is reachable."""
    reachable = audit_store.is_reachable()
    return {"status": "ok", "dynamodb": "reachable" if reachable else "unreachable"}


# --------------------------------------------------------------------------
# Response shaping helpers
# --------------------------------------------------------------------------


def _score_payload(assessment: RiskAssessment) -> dict:
    return {
        "risk_band": assessment.risk_band,
        "composite_score": assessment.composite_score,
        "rationale": assessment.rationale,
        "severity_was_clamped": assessment.severity_was_clamped,
        # Surfaced, not hidden: if the engine overrode the agent, the caller and
        # the reviewer both need to know that is what happened.
        "escalated_by_floor": assessment.escalated_by_floor,
        "actual_rows": assessment.actual_rows,
        "breakdown": assessment.breakdown,
    }


def _resolution_payload(record: dict) -> dict:
    """What a reviewer gets back after deciding: the decision *and* its effect.

    ``status`` is the authorisation outcome and ``execution_status`` is whether
    the action then worked. Both are returned because a reviewer who clicks
    approve needs to know if the deletion actually happened.
    """
    return {
        "status": record["status"],
        "reviewer": record["reviewer"],
        "execution_status": record.get("execution_status"),
        "execution_detail": record.get("execution_detail"),
        "snapshot_path": record.get("snapshot_path"),
    }


def _trail_entry(record: dict) -> dict:
    return {
        "record_id": record["record_id"],
        "timestamp": record["timestamp"],
        "action_type": record.get("action_type"),
        "description": record.get("description"),
        "routing_decision": record.get("routing_decision"),
        "status": record.get("status"),
        "reviewer": record.get("reviewer"),
        "composite_score": record.get("composite_score"),
        "risk_breakdown": record.get("risk_breakdown"),
        "execution_status": record.get("execution_status"),
        "execution_detail": record.get("execution_detail"),
    }
