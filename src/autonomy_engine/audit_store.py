"""DynamoDB persistence for the audit log.

Every routing decision the engine makes lands here with its full risk breakdown.
That is the point of the system: a decision nobody can explain after the fact is
not usable in a regulated environment.

Table layout (created by infra/template.yaml in Phase 5):

    table          {DYNAMODB_TABLE_PREFIX}-audit-log
    partition key  session_id  (S)
    sort key       timestamp   (S, ISO-8601 UTC)

Querying a session's trail is therefore a single Query, returned in chronological
order for free.

Record identity
---------------
A confirmation or review has to be resolvable from an opaque id alone, but the
item's key is a (session_id, timestamp) pair. Rather than add a GSI on a
``confirmation_id`` attribute, the id *encodes* the key pair. Two reasons: a
GetItem on the real key is strongly consistent, whereas a GSI read is only
eventually consistent -- and a reviewer resolving a confirmation seconds after it
was created would intermittently get "not found". It also keeps the deployed
infrastructure to one table with no secondary indexes.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Final

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Lifecycle states an audit record can hold.
STATUS_PENDING: Final[str] = "pending"
STATUS_CONFIRMED: Final[str] = "confirmed"
STATUS_REJECTED: Final[str] = "rejected"
STATUS_AUTO_EXECUTED: Final[str] = "auto_executed"
STATUS_REVIEWED: Final[str] = "reviewed"

VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {
        STATUS_PENDING,
        STATUS_CONFIRMED,
        STATUS_REJECTED,
        STATUS_AUTO_EXECUTED,
        STATUS_REVIEWED,
    }
)

#: Routing decisions, mirroring risk_scorer.AutonomyLevel.
ROUTING_AUTONOMOUS: Final[str] = "autonomous"
ROUTING_CONFIRM: Final[str] = "confirm"
ROUTING_FULL_REVIEW: Final[str] = "full_review"

_ID_SEPARATOR: Final[str] = "|"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class AuditStoreError(RuntimeError):
    """Base class for audit-store failures."""


class RecordNotFoundError(AuditStoreError):
    """No audit record exists for the given id."""


class InvalidRecordIdError(AuditStoreError):
    """The supplied confirmation/review id is not a well-formed record id."""


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

_table_cache: Any = None


def table_name() -> str:
    """Name of the audit table, derived from DYNAMODB_TABLE_PREFIX."""
    prefix = os.getenv("DYNAMODB_TABLE_PREFIX", "ps-9-1-autonomy-engine")
    return f"{prefix}-audit-log"


def _table() -> Any:
    """Return the cached DynamoDB Table resource, creating it on first use.

    Honours DYNAMODB_ENDPOINT_URL so local development can point at
    ``dynamodb-local`` instead of real AWS. The resource is cached because
    building one costs real milliseconds on a cold Lambda; call
    :func:`reset_cache` when the environment changes (tests do this).
    """
    global _table_cache
    if _table_cache is None:
        endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL") or None
        resource = boto3.resource(
            "dynamodb",
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            endpoint_url=endpoint_url,
        )
        _table_cache = resource.Table(table_name())
    return _table_cache


def reset_cache() -> None:
    """Drop the cached table resource.

    Needed whenever the environment changes underneath us -- notably in tests,
    where the resource must be built *inside* the moto mock context.
    """
    global _table_cache
    _table_cache = None


def is_reachable() -> bool:
    """Cheap liveness probe for the /health endpoint."""
    try:
        _table().table_status
        return True
    except Exception:  # noqa: BLE001 - health checks must never raise
        logger.warning("audit table %s unreachable", table_name(), exc_info=True)
        return False


# --------------------------------------------------------------------------
# Record ids
# --------------------------------------------------------------------------


def encode_record_id(session_id: str, timestamp: str) -> str:
    """Pack a (session_id, timestamp) key into one opaque, URL-safe id."""
    raw = f"{session_id}{_ID_SEPARATOR}{timestamp}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_record_id(record_id: str) -> tuple[str, str]:
    """Unpack a record id back into (session_id, timestamp).

    Raises:
        InvalidRecordIdError: If the id is not decodable. Callers turn this into
            a 400 rather than a 404 -- a malformed id is a bad request, not a
            missing resource.
    """
    try:
        padding = "=" * (-len(record_id) % 4)
        raw = base64.urlsafe_b64decode(record_id + padding).decode()
        # Split from the right: session_id is caller-supplied and may itself
        # contain the separator, whereas the timestamp is generated here and is
        # always ISO-8601, which never does.
        session_id, timestamp = raw.rsplit(_ID_SEPARATOR, 1)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidRecordIdError(f"malformed record id: {record_id!r}") from exc
    if not session_id or not timestamp:
        raise InvalidRecordIdError(f"malformed record id: {record_id!r}")
    return session_id, timestamp


# --------------------------------------------------------------------------
# Serialisation helpers
#
# DynamoDB has no float type: the boto3 resource layer rejects floats on write
# and hands back Decimal on read. These two functions are the only places that
# needs to know, so the rest of the codebase deals in plain floats.
# --------------------------------------------------------------------------


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def _hydrate(item: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw DynamoDB item into the shape the API returns.

    ``risk_breakdown`` is stored as a JSON string (the plan's schema) so the
    breakdown survives round-tripping without DynamoDB flattening it into a map;
    this parses it back into a dict for callers.
    """
    hydrated = _from_dynamo(dict(item))
    raw_breakdown = hydrated.get("risk_breakdown")
    if isinstance(raw_breakdown, str):
        try:
            hydrated["risk_breakdown"] = json.loads(raw_breakdown)
        except json.JSONDecodeError:
            logger.warning("unparseable risk_breakdown on %s", hydrated.get("record_id"))
            hydrated["risk_breakdown"] = {"error": raw_breakdown}
    return hydrated


def utc_timestamp() -> str:
    """Current time as a sortable ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def write_audit_record(
    *,
    session_id: str,
    action_type: str,
    composite_score: float,
    risk_breakdown: dict[str, str],
    routing_decision: str,
    status: str,
    reviewer: str | None = None,
    description: str | None = None,
    tool_name: str | None = None,
    parameters: dict[str, Any] | None = None,
    data_scope: int | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Persist one routing decision and return the stored record.

    Args:
        session_id: Groups all actions in one user session; the partition key.
        action_type: Audit vocabulary for the kind of action, e.g. ``bulk_delete``.
        composite_score: Numeric severity consistent with the band the model
            chose, 0-1. Presentational; the band is what routed the action.
        risk_breakdown: Per-dimension explanation from :class:`RiskAssessment`.
        routing_decision: ``autonomous`` / ``confirm`` / ``full_review``.
        status: One of :data:`VALID_STATUSES`.
        reviewer: Who resolved it, if already resolved.
        description: Human-readable summary of the proposed action.
        tool_name: The tool the agent chose.
        parameters: Arguments for that tool call.
        data_scope: The model's estimate of how many records it would affect.
            Stored as its own attribute so that a review resolved hours later can
            still check that estimate against what the filter really matches.
        timestamp: Override the sort key. Only for tests and backfills.

    Returns:
        The stored item, with ``record_id`` populated.

    Raises:
        ValueError: If ``status`` or ``routing_decision`` is outside the known
            vocabulary -- a typo here would silently orphan a record from the
            queue that is supposed to pick it up.
        AuditStoreError: If DynamoDB rejects the write.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {sorted(VALID_STATUSES)}")
    if routing_decision not in {ROUTING_AUTONOMOUS, ROUTING_CONFIRM, ROUTING_FULL_REVIEW}:
        raise ValueError(f"unknown routing_decision {routing_decision!r}")

    ts = timestamp or utc_timestamp()
    record_id = encode_record_id(session_id, ts)

    item: dict[str, Any] = {
        "session_id": session_id,
        "timestamp": ts,
        "record_id": record_id,
        "action_type": action_type,
        "composite_score": composite_score,
        "risk_breakdown": json.dumps(risk_breakdown),
        "routing_decision": routing_decision,
        "status": status,
        "reviewer": reviewer,
    }
    if description is not None:
        item["description"] = description
    if tool_name is not None:
        item["tool_name"] = tool_name
    if parameters is not None:
        item["parameters"] = parameters
    if data_scope is not None:
        item["data_scope"] = data_scope

    try:
        _table().put_item(Item=_to_dynamo(item))
    except ClientError as exc:
        raise AuditStoreError(f"failed to write audit record: {exc}") from exc

    logger.info(
        "audit record written",
        extra={
            "session_id": session_id,
            "action_type": action_type,
            "routing_decision": routing_decision,
            "status": status,
            "composite_score": composite_score,
        },
    )
    return _hydrate(item)


def resolve_record(
    record_id: str,
    *,
    new_status: str,
    reviewer: str,
    expected_routing: str,
) -> dict[str, Any]:
    """Move a pending record to a resolved status, atomically.

    The guard is a DynamoDB ``ConditionExpression``, not a read-then-write in
    Python. That matters twice over: it stops two reviewers racing to resolve the
    same item, and it makes it impossible to sneak a ``full_review`` item through
    the lighter confirmation endpoint (``expected_routing`` is part of the
    condition, so the wrong queue simply fails the write).

    Args:
        record_id: Opaque id from :func:`encode_record_id`.
        new_status: Status to move to.
        reviewer: Who made the decision. Recorded for the audit trail.
        expected_routing: The routing decision this record must have.

    Returns:
        The updated record.

    Raises:
        InvalidRecordIdError: The id is malformed.
        RecordNotFoundError: No such record.
        AuditStoreError: Already resolved, wrong queue, or DynamoDB failure.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"unknown status {new_status!r}")

    session_id, timestamp = decode_record_id(record_id)

    try:
        response = _table().update_item(
            Key={"session_id": session_id, "timestamp": timestamp},
            UpdateExpression="SET #status = :new_status, reviewer = :reviewer, resolved_at = :now",
            ConditionExpression=(
                Attr("session_id").exists()
                & Attr("status").eq(STATUS_PENDING)
                & Attr("routing_decision").eq(expected_routing)
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":new_status": new_status,
                ":reviewer": reviewer,
                ":now": utc_timestamp(),
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise _explain_failed_condition(
                session_id, timestamp, record_id, expected_routing
            ) from exc
        raise AuditStoreError(f"failed to resolve {record_id}: {exc}") from exc

    logger.info(
        "audit record resolved",
        extra={
            "session_id": session_id,
            "record_id": record_id,
            "status": new_status,
            "reviewer": reviewer,
        },
    )
    return _hydrate(response["Attributes"])


def _explain_failed_condition(
    session_id: str, timestamp: str, record_id: str, expected_routing: str
) -> AuditStoreError:
    """Read the record back to say *why* the conditional write failed.

    The condition covers three distinct situations and DynamoDB reports all of
    them identically. Since the caller turns these into different HTTP statuses
    (404 vs 409), it is worth one extra read to distinguish them.
    """
    try:
        item = _table().get_item(
            Key={"session_id": session_id, "timestamp": timestamp},
            ConsistentRead=True,
        ).get("Item")
    except ClientError as exc:
        return AuditStoreError(f"failed to resolve {record_id}: {exc}")

    if item is None:
        return RecordNotFoundError(f"no audit record with id {record_id}")

    record = _hydrate(item)
    if record.get("routing_decision") != expected_routing:
        return AuditStoreError(
            f"record {record_id} was routed to {record.get('routing_decision')!r}, "
            f"not {expected_routing!r} -- resolve it through the correct queue"
        )
    return AuditStoreError(
        f"record {record_id} is already {record.get('status')!r} "
        f"(resolved by {record.get('reviewer')!r}); it cannot be resolved twice"
    )


def record_execution(
    record_id: str,
    *,
    execution_status: str,
    detail: str,
    result: dict[str, Any] | None = None,
    snapshot: str | None = None,
) -> dict[str, Any]:
    """Attach the outcome of actually running an action to its audit record.

    Kept separate from the lifecycle ``status`` on purpose. ``status`` answers
    "was this authorised?" and ``execution_status`` answers "did it then work?".
    Collapsing them would lose the case that matters most in a review: an action
    a human approved that subsequently failed.

    Args:
        record_id: The record to annotate.
        execution_status: ``success``, ``failed``, or ``skipped``.
        detail: One-line human-readable outcome.
        result: Structured result payload, stored as JSON.
        snapshot: Path to the pre-write snapshot, if the action mutated data.
            This is what an operator needs to roll the change back.

    Returns:
        The updated record.
    """
    session_id, timestamp = decode_record_id(record_id)

    expression = "SET execution_status = :st, execution_detail = :detail, executed_at = :now"
    values: dict[str, Any] = {
        ":st": execution_status,
        ":detail": detail,
        ":now": utc_timestamp(),
    }
    if result is not None:
        expression += ", execution_result = :result"
        values[":result"] = json.dumps(result, default=str)
    if snapshot is not None:
        expression += ", snapshot_path = :snap"
        values[":snap"] = snapshot

    try:
        response = _table().update_item(
            Key={"session_id": session_id, "timestamp": timestamp},
            UpdateExpression=expression,
            ConditionExpression=Attr("session_id").exists(),
            ExpressionAttributeValues=_to_dynamo(values),
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RecordNotFoundError(f"no audit record with id {record_id}") from exc
        raise AuditStoreError(f"failed to record execution for {record_id}: {exc}") from exc

    logger.info(
        "action executed",
        extra={
            "session_id": session_id,
            "record_id": record_id,
            "execution_status": execution_status,
        },
    )
    return _hydrate(response["Attributes"])


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_audit_trail(session_id: str) -> list[dict[str, Any]]:
    """Every recorded action for one session, oldest first.

    A single Query on the partition key -- the sort key is the timestamp, so
    chronological order comes free.
    """
    try:
        response = _table().query(
            KeyConditionExpression=Key("session_id").eq(session_id),
            ScanIndexForward=True,
        )
    except ClientError as exc:
        raise AuditStoreError(f"failed to read audit trail for {session_id}: {exc}") from exc
    return [_hydrate(item) for item in response.get("Items", [])]


def get_record(record_id: str) -> dict[str, Any]:
    """Fetch one record by its opaque id.

    Raises:
        InvalidRecordIdError: The id is malformed.
        RecordNotFoundError: No such record.
    """
    session_id, timestamp = decode_record_id(record_id)
    try:
        item = _table().get_item(
            Key={"session_id": session_id, "timestamp": timestamp},
            ConsistentRead=True,
        ).get("Item")
    except ClientError as exc:
        raise AuditStoreError(f"failed to read record {record_id}: {exc}") from exc
    if item is None:
        raise RecordNotFoundError(f"no audit record with id {record_id}")
    return _hydrate(item)


def _list_pending(routing_decision: str) -> list[dict[str, Any]]:
    """Scan for pending records in one queue, oldest first.

    A Scan is honest about what this is: at demo scale the table is tiny, and a
    Scan keeps the deployed infrastructure to a single table with no secondary
    indexes. A GSI on (status, routing_decision) is the obvious fix before this
    carries production volume -- flagged as roadmap, not pretended away.
    """
    try:
        response = _table().scan(
            FilterExpression=Attr("status").eq(STATUS_PENDING)
            & Attr("routing_decision").eq(routing_decision)
        )
    except ClientError as exc:
        raise AuditStoreError(f"failed to list pending {routing_decision}: {exc}") from exc
    records = [_hydrate(item) for item in response.get("Items", [])]
    return sorted(records, key=lambda r: r["timestamp"])


def list_pending_confirmations() -> list[dict[str, Any]]:
    """Medium-risk actions waiting on a one-click confirmation."""
    return _list_pending(ROUTING_CONFIRM)


def list_pending_reviews() -> list[dict[str, Any]]:
    """High-risk actions blocked pending full human review."""
    return _list_pending(ROUTING_FULL_REVIEW)
