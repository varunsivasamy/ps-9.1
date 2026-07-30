"""Confirmation and human-review queues.

Two queues, deliberately kept separate even though they share a shape:

- **confirmation** -- medium-risk actions. A human sees a preview and approves
  with one click. Decision vocabulary: ``confirm`` / ``reject``.
- **review** -- high-risk actions. Blocked until a human explicitly approves.
  Decision vocabulary: ``approve`` / ``reject``.

Keeping them apart is a safety property, not cosmetics: a high-risk action must
not be resolvable through the lighter confirmation path. The separation is
enforced all the way down in :func:`audit_store.resolve_record`, whose
conditional write refuses a record whose ``routing_decision`` does not match the
queue being used.

Pure functions only -- no web framework here. That arrives in Phase 4.
"""

from __future__ import annotations

from typing import Any, Final

from autonomy_engine import audit_store
from autonomy_engine.agent_actions import AgentAction
from autonomy_engine.risk_scorer import RiskScore

#: Decisions each queue accepts, mapped to the status they write.
CONFIRMATION_DECISIONS: Final[dict[str, str]] = {
    "confirm": audit_store.STATUS_CONFIRMED,
    "reject": audit_store.STATUS_REJECTED,
}

REVIEW_DECISIONS: Final[dict[str, str]] = {
    "approve": audit_store.STATUS_REVIEWED,
    "reject": audit_store.STATUS_REJECTED,
}


class InvalidDecisionError(ValueError):
    """The supplied decision is not valid for this queue."""


# --------------------------------------------------------------------------
# Creating queue entries
# --------------------------------------------------------------------------


def create_confirmation_request(
    action: AgentAction,
    score: RiskScore,
    *,
    session_id: str,
) -> str:
    """Queue a medium-risk action for one-click confirmation.

    Args:
        action: The action the agent proposed.
        score: Its risk score, including the breakdown that justified routing here.
        session_id: Session the action belongs to.

    Returns:
        The ``confirmation_id`` to pass back to :func:`resolve_confirmation`.
    """
    record = audit_store.write_audit_record(
        session_id=session_id,
        action_type=action.action_type,
        composite_score=score.composite_score,
        risk_breakdown=score.breakdown,
        routing_decision=audit_store.ROUTING_CONFIRM,
        status=audit_store.STATUS_PENDING,
        description=action.description,
        tool_name=action.tool_name,
        parameters=action.parameters,
    )
    return record["record_id"]


def create_review_request(
    action: AgentAction,
    score: RiskScore,
    *,
    session_id: str,
) -> str:
    """Queue a high-risk action for full human review.

    Same shape as :func:`create_confirmation_request`, different queue -- the
    ``routing_decision`` stored on the record is what keeps the two apart.

    Returns:
        The ``review_id`` to pass back to :func:`resolve_review`.
    """
    record = audit_store.write_audit_record(
        session_id=session_id,
        action_type=action.action_type,
        composite_score=score.composite_score,
        risk_breakdown=score.breakdown,
        routing_decision=audit_store.ROUTING_FULL_REVIEW,
        status=audit_store.STATUS_PENDING,
        description=action.description,
        tool_name=action.tool_name,
        parameters=action.parameters,
    )
    return record["record_id"]


def record_autonomous_execution(
    action: AgentAction,
    score: RiskScore,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Log a low-risk action that ran without a human.

    Written with status ``auto_executed`` and no reviewer, because there was no
    human in the loop -- that absence is itself the thing worth auditing.
    """
    return audit_store.write_audit_record(
        session_id=session_id,
        action_type=action.action_type,
        composite_score=score.composite_score,
        risk_breakdown=score.breakdown,
        routing_decision=audit_store.ROUTING_AUTONOMOUS,
        status=audit_store.STATUS_AUTO_EXECUTED,
        description=action.description,
        tool_name=action.tool_name,
        parameters=action.parameters,
    )


# --------------------------------------------------------------------------
# Resolving queue entries
# --------------------------------------------------------------------------


def resolve_confirmation(
    confirmation_id: str,
    decision: str,
    reviewer: str,
) -> dict[str, Any]:
    """Approve or reject a pending medium-risk confirmation.

    Args:
        confirmation_id: Id returned by :func:`create_confirmation_request`.
        decision: ``"confirm"`` or ``"reject"``.
        reviewer: Who decided. Recorded on the audit record.

    Returns:
        The updated audit record.

    Raises:
        InvalidDecisionError: ``decision`` is not valid for this queue.
        audit_store.InvalidRecordIdError: Malformed id.
        audit_store.RecordNotFoundError: No such confirmation.
        audit_store.AuditStoreError: Already resolved, or the record belongs to
            the review queue rather than the confirmation queue.
    """
    new_status = _validate_decision(decision, CONFIRMATION_DECISIONS, "confirmation")
    return audit_store.resolve_record(
        confirmation_id,
        new_status=new_status,
        reviewer=reviewer,
        expected_routing=audit_store.ROUTING_CONFIRM,
    )


def resolve_review(
    review_id: str,
    decision: str,
    reviewer: str,
) -> dict[str, Any]:
    """Approve or reject a pending high-risk review.

    Args:
        review_id: Id returned by :func:`create_review_request`.
        decision: ``"approve"`` or ``"reject"``.
        reviewer: Who decided. Recorded on the audit record.

    Returns:
        The updated audit record.

    Raises:
        InvalidDecisionError: ``decision`` is not valid for this queue. Note that
            ``"confirm"`` is rejected here on purpose -- a high-risk action needs
            an explicit approval, not a confirmation.
        audit_store.InvalidRecordIdError: Malformed id.
        audit_store.RecordNotFoundError: No such review.
        audit_store.AuditStoreError: Already resolved, or the record belongs to
            the confirmation queue rather than the review queue.
    """
    new_status = _validate_decision(decision, REVIEW_DECISIONS, "review")
    return audit_store.resolve_record(
        review_id,
        new_status=new_status,
        reviewer=reviewer,
        expected_routing=audit_store.ROUTING_FULL_REVIEW,
    )


def _validate_decision(decision: str, allowed: dict[str, str], queue: str) -> str:
    if decision not in allowed:
        raise InvalidDecisionError(
            f"{decision!r} is not a valid {queue} decision; "
            f"expected one of {sorted(allowed)}"
        )
    return allowed[decision]
