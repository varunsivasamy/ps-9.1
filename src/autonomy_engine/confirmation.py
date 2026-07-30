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

This module also owns the moment an action becomes real. Approving a queued item
does not merely stamp it approved -- it runs it, via :mod:`executor`, against the
transaction store. Rejection runs nothing. Together with
:func:`execute_autonomously`, these are the only three doors to the executor, so
"what is allowed to touch the data, and on whose authority" is a question
answered entirely within this file.

No web framework here; that lives in :mod:`main`.
"""

from __future__ import annotations

from typing import Any, Final

from autonomy_engine import audit_store, executor
from autonomy_engine.agent_actions import AgentAction
from autonomy_engine.executor import ExecutionResult
from autonomy_engine.risk_scorer import RiskAssessment

#: Decisions each queue accepts, mapped to the status they write.
CONFIRMATION_DECISIONS: Final[dict[str, str]] = {
    "confirm": audit_store.STATUS_CONFIRMED,
    "reject": audit_store.STATUS_REJECTED,
}

REVIEW_DECISIONS: Final[dict[str, str]] = {
    "approve": audit_store.STATUS_REVIEWED,
    "reject": audit_store.STATUS_REJECTED,
}

#: Statuses that authorise execution. A rejected action is never run, and this
#: set is the single place that fact is encoded.
EXECUTING_STATUSES: Final[frozenset[str]] = frozenset(
    {audit_store.STATUS_CONFIRMED, audit_store.STATUS_REVIEWED}
)


class InvalidDecisionError(ValueError):
    """The supplied decision is not valid for this queue."""


# --------------------------------------------------------------------------
# Creating queue entries
# --------------------------------------------------------------------------


def create_confirmation_request(
    action: AgentAction,
    assessment: RiskAssessment,
    *,
    session_id: str,
) -> str:
    """Queue a medium-risk action for one-click confirmation.

    Nothing is executed here. The action is parked until a human resolves it
    through :func:`resolve_confirmation`, which is where execution happens.

    Args:
        action: The action the agent proposed.
        assessment: Its risk assessment, including the reasoning that justified
            routing here.
        session_id: Session the action belongs to.

    Returns:
        The ``confirmation_id`` to pass back to :func:`resolve_confirmation`.
    """
    record = audit_store.write_audit_record(
        session_id=session_id,
        action_type=action.action_type,
        composite_score=assessment.composite_score,
        risk_breakdown=assessment.breakdown,
        routing_decision=audit_store.ROUTING_CONFIRM,
        status=audit_store.STATUS_PENDING,
        description=action.description,
        tool_name=action.tool_name,
        parameters=action.parameters,
        data_scope=action.data_scope,
    )
    return record["record_id"]


def create_review_request(
    action: AgentAction,
    assessment: RiskAssessment,
    *,
    session_id: str,
) -> str:
    """Queue a high-risk action for full human review.

    Same shape as :func:`create_confirmation_request`, different queue -- the
    ``routing_decision`` stored on the record is what keeps the two apart.
    Nothing is executed until :func:`resolve_review` approves it.

    Returns:
        The ``review_id`` to pass back to :func:`resolve_review`.
    """
    record = audit_store.write_audit_record(
        session_id=session_id,
        action_type=action.action_type,
        composite_score=assessment.composite_score,
        risk_breakdown=assessment.breakdown,
        routing_decision=audit_store.ROUTING_FULL_REVIEW,
        status=audit_store.STATUS_PENDING,
        description=action.description,
        tool_name=action.tool_name,
        parameters=action.parameters,
        data_scope=action.data_scope,
    )
    return record["record_id"]


def execute_autonomously(
    action: AgentAction,
    assessment: RiskAssessment,
    *,
    session_id: str,
) -> tuple[dict[str, Any], ExecutionResult]:
    """Run a low-risk action immediately, with no human in the loop.

    The audit record is written *before* the action runs, for two reasons: its
    id is the snapshot tag, so a mutation cannot happen without a named
    rollback point already existing; and if execution then dies mid-flight, the
    attempt is already on the record rather than vanishing.

    Logged with status ``auto_executed`` and no reviewer, because there was no
    human in the loop -- that absence is itself the thing worth auditing.

    Returns:
        ``(audit_record, execution_result)``. The record is the post-execution
        one, carrying the outcome.
    """
    record = audit_store.write_audit_record(
        session_id=session_id,
        action_type=action.action_type,
        composite_score=assessment.composite_score,
        risk_breakdown=assessment.breakdown,
        routing_decision=audit_store.ROUTING_AUTONOMOUS,
        status=audit_store.STATUS_AUTO_EXECUTED,
        description=action.description,
        tool_name=action.tool_name,
        parameters=action.parameters,
        data_scope=action.data_scope,
    )

    result = executor.execute(
        action.tool_name,
        action.parameters,
        record_id=record["record_id"],
        claimed_scope=action.data_scope,
    )
    updated = audit_store.record_execution(
        record["record_id"],
        execution_status=result.status,
        detail=result.detail,
        result=result.to_payload(),
        snapshot=result.snapshot,
    )
    return updated, result


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
        The updated audit record. On ``"confirm"`` the action has been executed
        by the time this returns, and the record carries the outcome.

    Raises:
        InvalidDecisionError: ``decision`` is not valid for this queue.
        audit_store.InvalidRecordIdError: Malformed id.
        audit_store.RecordNotFoundError: No such confirmation.
        audit_store.AuditStoreError: Already resolved, or the record belongs to
            the review queue rather than the confirmation queue.
    """
    new_status = _validate_decision(decision, CONFIRMATION_DECISIONS, "confirmation")
    return _resolve_and_maybe_execute(
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
        The updated audit record. On ``"approve"`` the action has been executed
        by the time this returns, and the record carries the outcome.

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
    return _resolve_and_maybe_execute(
        review_id,
        new_status=new_status,
        reviewer=reviewer,
        expected_routing=audit_store.ROUTING_FULL_REVIEW,
    )


def _resolve_and_maybe_execute(
    record_id: str,
    *,
    new_status: str,
    reviewer: str,
    expected_routing: str,
) -> dict[str, Any]:
    """Resolve a queued action and, if it was approved, run it.

    The order is deliberate and not interchangeable: the status is written
    first, through :func:`audit_store.resolve_record`'s conditional write, and
    only a record that write actually moved out of ``pending`` gets executed.
    That is what makes a double-click harmless -- the second call fails the
    condition and raises, so it can never reach the executor and delete the same
    rows twice.

    A rejection returns here untouched: :data:`EXECUTING_STATUSES` gates the
    call, so nothing runs.

    Returns:
        The audit record, carrying the execution outcome if one occurred.
    """
    record = audit_store.resolve_record(
        record_id,
        new_status=new_status,
        reviewer=reviewer,
        expected_routing=expected_routing,
    )

    if new_status not in EXECUTING_STATUSES:
        return audit_store.record_execution(
            record_id,
            execution_status="skipped",
            detail=f"Rejected by {reviewer}; the action was not executed.",
        )

    tool_name = record.get("tool_name")
    if not tool_name:
        # Pre-execution records (or a hand-written row) carry no tool to run.
        # Approving one is not an error, but it must not look like a success.
        return audit_store.record_execution(
            record_id,
            execution_status="skipped",
            detail="Approved, but the record carries no tool call to execute.",
        )

    result = executor.execute(
        tool_name,
        record.get("parameters") or {},
        record_id=record_id,
        claimed_scope=record.get("data_scope"),
    )
    return audit_store.record_execution(
        record_id,
        execution_status=result.status,
        detail=result.detail,
        result=result.to_payload(),
        snapshot=result.snapshot,
    )


def _validate_decision(decision: str, allowed: dict[str, str], queue: str) -> str:
    if decision not in allowed:
        raise InvalidDecisionError(
            f"{decision!r} is not a valid {queue} decision; "
            f"expected one of {sorted(allowed)}"
        )
    return allowed[decision]
