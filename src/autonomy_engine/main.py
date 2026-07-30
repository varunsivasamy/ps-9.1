"""FastAPI app wiring risk_scorer, agent_actions, audit_store, and confirmation
into the working demo API.

Every request is logged as one structured JSON line (session_id, action_type,
routing_decision, latency_ms) so the deployed Lambda's CloudWatch stream is
queryable. Every error path returns structured JSON with the right HTTP status
and never leaks a raw stack trace to the caller.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from autonomy_engine import audit_store, confirmation
from autonomy_engine.agent_actions import AgentActionError, propose_action
from autonomy_engine.logging_config import configure_logging
from autonomy_engine.risk_scorer import DEFAULT_THRESHOLDS, route_action, score_action

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
# Request/response bodies
# --------------------------------------------------------------------------


class ProposeRequest(BaseModel):
    user_request: str = Field(min_length=1, description="What the user asked the agent to do.")
    session_id: str = Field(min_length=1, description="Groups actions into one audit trail.")


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
    """Propose an action, score its risk, and route it.

    - autonomous: executes immediately (a mock success result for the demo),
      logged as auto_executed.
    - confirm: creates a pending confirmation, returns a human-readable preview.
    - full_review: creates a pending review entry.
    """
    request.state.session_id = body.session_id

    action = propose_action(body.user_request, {})
    score = score_action(action.to_risk_factors())
    decision = route_action(score, DEFAULT_THRESHOLDS)

    request.state.action_type = action.action_type
    request.state.routing_decision = decision

    if decision == "autonomous":
        record = confirmation.record_autonomous_execution(
            action, score, session_id=body.session_id
        )
        return {
            "routing_decision": decision,
            "risk_score": _score_payload(score),
            "result": {
                "status": "success",
                "detail": f"Executed automatically: {action.description}",
            },
            "audit_record_id": record["record_id"],
        }

    if decision == "confirm":
        confirmation_id = confirmation.create_confirmation_request(
            action, score, session_id=body.session_id
        )
        return {
            "routing_decision": decision,
            "risk_score": _score_payload(score),
            "confirmation_id": confirmation_id,
            "preview": action.description,
        }

    review_id = confirmation.create_review_request(action, score, session_id=body.session_id)
    return {
        "routing_decision": decision,
        "risk_score": _score_payload(score),
        "review_id": review_id,
        "preview": action.description,
    }


@app.post("/confirmations/{confirmation_id}/resolve")
async def resolve_confirmation_endpoint(
    confirmation_id: str, body: ResolveConfirmationRequest, request: Request
) -> dict:
    """Resolve a pending medium-risk confirmation."""
    record = confirmation.resolve_confirmation(confirmation_id, body.decision, body.reviewer)
    request.state.session_id = record.get("session_id")
    request.state.routing_decision = record.get("routing_decision")
    return {"status": record["status"], "reviewer": record["reviewer"]}


@app.post("/reviews/{review_id}/resolve")
async def resolve_review_endpoint(
    review_id: str, body: ResolveReviewRequest, request: Request
) -> dict:
    """Resolve a pending high-risk review."""
    record = confirmation.resolve_review(review_id, body.decision, body.reviewer)
    request.state.session_id = record.get("session_id")
    request.state.routing_decision = record.get("routing_decision")
    return {"status": record["status"], "reviewer": record["reviewer"]}


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


def _score_payload(score) -> dict:
    return {
        "composite_score": score.composite_score,
        "breakdown": score.breakdown,
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
    }
