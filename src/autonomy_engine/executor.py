"""Executes an authorised action against the transaction store.

The bridge between the two halves of the system: :mod:`agent_actions` decides
*what* to do, :mod:`risk_scorer` and :mod:`confirmation` decide *whether* it is
allowed, and this module is the only place that actually does it.

The one invariant worth stating plainly: nothing here checks permission. Every
caller must already have established that the action is authorised -- either
because the model banded it ``low``, or because a human confirmed or approved
it. Keeping the authorisation checks out of this module means there is exactly
one question to answer when auditing it ("who calls execute?") rather than one
per branch.

Snapshots are taken by :mod:`data_store` under the action's audit record id, so
every mutation in the audit log can be traced to the file that undoes it.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, ValidationError

from autonomy_engine import data_store
from autonomy_engine.data_store import Criterion, DataStoreError

logger = logging.getLogger(__name__)

#: Rows returned by a query. A read is low risk to *perform* but an unbounded
#: result set is still a way to exfiltrate the table in one call, so reads are
#: capped and the cap is reported in the result rather than applied silently.
#: With ~99k rows behind it, this cap is load-bearing rather than theoretical --
#: it is also why summarize_transactions exists.
QUERY_ROW_LIMIT: Final[int] = 25

ExecutionStatus = Literal["success", "failed", "skipped"]

#: Tools that change data. Everything else is a read, and a read's row count
#: never escalates supervision -- reads are capped and destroy nothing.
MUTATING_TOOLS: Final[frozenset[str]] = frozenset(
    {"update_transaction", "delete_transaction", "bulk_delete_transactions"}
)

#: Tools that destroy rows outright. Tracked separately from MUTATING_TOOLS
#: because size is not the only thing that makes an action dangerous: an update
#: to one row can be read back and corrected, whereas a delete of one row is
#: gone. Row count alone would treat those as equivalent, and a live run proved
#: it does -- the agent banded "delete invoice I317333" as low risk (correctly,
#: on its own terms: one row, unambiguous request) and it executed unattended.
#: Deleting data is not something an agent should be able to do alone.
DESTRUCTIVE_TOOLS: Final[frozenset[str]] = frozenset(
    {"delete_transaction", "bulk_delete_transactions"}
)


class ExecutionError(RuntimeError):
    """The engine asked for a tool it does not know how to run.

    A programming error rather than a data error -- a tool schema was added
    without a matching branch here -- so it is raised rather than turned into a
    failed result.
    """


class ExecutionResult(BaseModel):
    """What actually happened when the action ran."""

    status: ExecutionStatus
    detail: str = Field(description="One-line human-readable outcome.")
    affected_count: int = Field(default=0, description="Rows read, changed, or deleted.")
    rows: list[dict[str, str]] = Field(
        default_factory=list, description="Returned rows, for reads only."
    )
    truncated: bool = Field(
        default=False, description="True if more rows matched than were returned."
    )
    summary: dict[str, Any] | None = Field(
        default=None,
        description="Aggregate totals, for summarize_transactions only.",
    )
    snapshot: str | None = Field(
        default=None, description="Pre-write snapshot path, for mutations."
    )
    scope_check: str | None = Field(
        default=None,
        description="Whether the model's data_scope estimate matched reality.",
    )

    def to_payload(self) -> dict[str, Any]:
        """Trim to what the API returns and the audit log stores."""
        return self.model_dump(exclude_none=True)


def _parse_criteria(raw: Any) -> list[Criterion]:
    """Validate the model's filter into typed criteria.

    Raises:
        DataStoreError: If the filter is not a well-formed criteria list. Turned
            into a failed execution by :func:`execute` rather than a crash --
            a malformed filter is the model's mistake, not the caller's.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DataStoreError(f"filter must be a list of criteria, got {type(raw).__name__}")
    try:
        return [Criterion.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise DataStoreError(f"malformed filter criteria: {exc}") from exc


def is_scope_mismatch(claimed: int | None, actual: int) -> bool:
    """Whether the model's row estimate is wrong enough to invalidate its band.

    A tolerance band, because "about 500" against 487 is good estimating, not a
    red flag. Off by more than half means the band was reasoned from a premise
    the data does not support, and the model should be asked again.
    """
    if claimed is None:
        return False
    return abs(claimed - actual) > max(2, actual * 0.5)


def _check_scope(claimed: int | None, actual: int) -> str | None:
    """Compare the model's record-count estimate against what the filter matched.

    This is a factual check, not a second opinion on risk: the model's
    ``data_scope`` was one of the inputs to its own banding, so a wildly wrong
    count means the band was reasoned from a false premise. It is reported, not
    acted on -- routing stays the model's call -- but it is exactly the sort of
    thing a reviewer should see before approving.
    """
    if claimed is None:
        return None
    if claimed == actual:
        return f"model estimated {claimed} records; filter matched {actual} (accurate)"
    verdict = "MISMATCH" if is_scope_mismatch(claimed, actual) else "close"
    return f"model estimated {claimed} records; filter matched {actual} ({verdict})"


class ScopeReport(BaseModel):
    """What a proposed action would *really* touch, measured before it runs.

    The model's ``data_scope`` is an estimate it made while reading the request.
    This is the answer from the data itself. Everything downstream -- the
    re-judgement call and the blast-radius floor -- keys off this number rather
    than the estimate, because a band reasoned from "about 5 rows" is worthless
    when the filter matches fifteen thousand.
    """

    tool_name: str
    is_mutation: bool
    is_destructive: bool = Field(
        default=False, description="Whether the action deletes rows outright."
    )
    actual_rows: int = Field(description="Rows the action would read or change.")
    resolvable: bool = Field(
        default=True,
        description="False if the filter could not be resolved, e.g. malformed.",
    )
    detail: str = Field(default="", description="Why, when not resolvable.")


def preflight(tool_name: str, parameters: dict[str, Any]) -> ScopeReport:
    """Resolve a proposed action against the real data without performing it.

    Strictly read-only: it counts, it never writes, and it takes no snapshot.
    Safe to call on an action that a human may still reject.

    An unresolvable filter comes back as ``resolvable=False`` rather than
    raising. That case must not be mistaken for "affects 0 rows" -- zero would
    look harmless and sail through, when in fact we simply do not know what the
    action would do, which is strictly worse.

    Args:
        tool_name: The proposed tool.
        parameters: Its arguments, as the model supplied them.

    Returns:
        A :class:`ScopeReport` with the true affected-row count.
    """
    is_mutation = tool_name in MUTATING_TOOLS
    is_destructive = tool_name in DESTRUCTIVE_TOOLS

    try:
        if tool_name in ("query_transactions", "summarize_transactions"):
            criteria = _parse_criteria(parameters.get("filter"))
            return ScopeReport(
                tool_name=tool_name,
                is_mutation=False,
                is_destructive=False,
                actual_rows=data_store.count_matching(criteria),
            )

        if tool_name in ("update_transaction", "delete_transaction"):
            # Exactly one row by construction -- but only if that invoice
            # actually exists. A miss is 0 rows and a failed execution, not 1.
            invoice = str(parameters.get("invoice_no", ""))
            exists = bool(
                data_store.select(
                    [Criterion(field=data_store.ID_FIELD, operator="equals", value=invoice)]
                )
            )
            return ScopeReport(
                tool_name=tool_name,
                is_mutation=True,
                is_destructive=is_destructive,
                actual_rows=1 if exists else 0,
                resolvable=exists,
                detail="" if exists else f"no transaction with invoice_no={invoice!r}",
            )

        if tool_name == "bulk_delete_transactions":
            criteria = _parse_criteria(parameters.get("filter"))
            if not criteria:
                # An empty filter matches everything. delete_matching would
                # refuse it, but the count is what the floor needs to see so
                # this is never banded as though it were harmless.
                return ScopeReport(
                    tool_name=tool_name,
                    is_mutation=True,
                    is_destructive=True,
                    actual_rows=len(data_store.load_rows()),
                    resolvable=False,
                    detail="bulk delete has no filter criteria; it would match every row",
                )
            return ScopeReport(
                tool_name=tool_name,
                is_mutation=True,
                is_destructive=True,
                actual_rows=data_store.count_matching(criteria),
            )
    except DataStoreError as exc:
        return ScopeReport(
            tool_name=tool_name,
            is_mutation=is_mutation,
            is_destructive=is_destructive,
            actual_rows=0,
            resolvable=False,
            detail=str(exc),
        )

    raise ExecutionError(
        f"no preflight handler for tool {tool_name!r}; known tools are {sorted(_HANDLERS)}"
    )


def execute(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    record_id: str,
    claimed_scope: int | None = None,
) -> ExecutionResult:
    """Run an already-authorised action against the transaction store.

    Args:
        tool_name: Which tool to run.
        parameters: The tool's arguments, as the model supplied them.
        record_id: Audit record id for this action. Used as the snapshot tag so
            the snapshot and the authorising audit entry share a name.
        claimed_scope: The model's ``data_scope`` estimate, if available, so the
            result can report whether it matched reality.

    Returns:
        An :class:`ExecutionResult`. Data-level problems (unknown field, bad
        date, missing invoice, unbounded delete) come back as
        ``status="failed"`` with the reason in ``detail``, because they belong in
        the audit trail rather than as an opaque 500.

    Raises:
        ExecutionError: The tool name has no execution branch.
    """
    if tool_name not in _HANDLERS:
        raise ExecutionError(
            f"no execution handler for tool {tool_name!r}; "
            f"known tools are {sorted(_HANDLERS)}"
        )

    try:
        return _HANDLERS[tool_name](parameters, record_id, claimed_scope)
    except DataStoreError as exc:
        logger.warning("execution failed for %s: %s", tool_name, exc)
        return ExecutionResult(status="failed", detail=str(exc))


# --------------------------------------------------------------------------
# Per-tool handlers
# --------------------------------------------------------------------------


def _run_query(
    parameters: dict[str, Any], record_id: str, claimed_scope: int | None
) -> ExecutionResult:
    criteria = _parse_criteria(parameters.get("filter"))
    matched = data_store.select(criteria)
    rows = matched[:QUERY_ROW_LIMIT]
    truncated = len(matched) > len(rows)

    detail = f"Read {len(matched)} transaction(s)"
    if truncated:
        detail += f"; returning the first {QUERY_ROW_LIMIT}"

    return ExecutionResult(
        status="success",
        detail=detail,
        affected_count=len(matched),
        rows=rows,
        truncated=truncated,
        scope_check=_check_scope(claimed_scope, len(matched)),
    )


def _run_summarize(
    parameters: dict[str, Any], record_id: str, claimed_scope: int | None
) -> ExecutionResult:
    criteria = _parse_criteria(parameters.get("filter"))
    # The schema uses "" rather than null for "no grouping", because strict mode
    # requires the property to be present on every call.
    group_by = (parameters.get("group_by") or "").strip() or None
    totals = data_store.summarize(criteria, group_by=group_by)

    detail = (
        f"Summarised {totals['transactions']} transaction(s): "
        f"{totals['total_quantity']:.0f} items, "
        f"{totals['total_revenue']:,.2f} total revenue"
    )
    if group_by:
        detail += f", across {len(totals.get('groups', {}))} {group_by} group(s)"

    return ExecutionResult(
        status="success",
        detail=detail,
        affected_count=totals["transactions"],
        rows=[],
        summary=totals,
        scope_check=_check_scope(claimed_scope, totals["transactions"]),
    )


def _run_update(
    parameters: dict[str, Any], record_id: str, claimed_scope: int | None
) -> ExecutionResult:
    outcome = data_store.update_record(
        str(parameters["invoice_no"]),
        str(parameters["field"]),
        str(parameters["new_value"]),
        snapshot_tag=record_id,
    )
    return ExecutionResult(
        status="success",
        detail=(
            f"Updated invoice {outcome['invoice_no']}: {outcome['field']} "
            f"{outcome['old_value']!r} -> {outcome['new_value']!r}"
        ),
        affected_count=1,
        rows=[],
        snapshot=outcome["snapshot"],
        scope_check=_check_scope(claimed_scope, 1),
    )


def _run_delete_one(
    parameters: dict[str, Any], record_id: str, claimed_scope: int | None
) -> ExecutionResult:
    outcome = data_store.delete_record(
        str(parameters["invoice_no"]), snapshot_tag=record_id
    )
    return ExecutionResult(
        status="success",
        detail=(
            f"Deleted invoice {outcome['deleted_ids'][0]}; "
            f"{outcome['remaining']} transaction(s) remain"
        ),
        affected_count=1,
        # The deleted row is echoed back so the audit record shows what was
        # destroyed, not merely that something was.
        rows=[outcome["deleted_row"]],
        snapshot=outcome["snapshot"],
        scope_check=_check_scope(claimed_scope, 1),
    )


def _run_bulk_delete(
    parameters: dict[str, Any], record_id: str, claimed_scope: int | None
) -> ExecutionResult:
    criteria = _parse_criteria(parameters.get("filter"))
    outcome = data_store.delete_matching(criteria, snapshot_tag=record_id)

    detail = (
        f"Deleted {outcome['deleted_count']:,} transaction(s); "
        f"{outcome['remaining']:,} remain"
    )
    sample = outcome["deleted_ids"]
    if sample:
        shown = ", ".join(sample)
        detail += f". Sample: {shown}" + (", ..." if outcome["deleted_ids_truncated"] else "")

    return ExecutionResult(
        status="success",
        detail=detail,
        affected_count=outcome["deleted_count"],
        rows=[],
        snapshot=outcome["snapshot"],
        # delete_matching already partitioned the rows, so its count is what the
        # filter matched -- no second full scan needed to fact-check the estimate.
        scope_check=_check_scope(claimed_scope, outcome["deleted_count"]),
    )


_HANDLERS: Final[dict[str, Any]] = {
    "query_transactions": _run_query,
    "summarize_transactions": _run_summarize,
    "update_transaction": _run_update,
    "delete_transaction": _run_delete_one,
    "bulk_delete_transactions": _run_bulk_delete,
}


def rollback(record_id: str) -> int:
    """Undo the mutation made by action ``record_id``.

    Restores the snapshot taken immediately before it ran. Returns the number of
    rows in the restored file.
    """
    return data_store.restore_snapshot(record_id)
