"""Agent action layer — agentic risk measurement then action proposal.

Flow
----
The LLM runs a two-phase tool-calling loop:

Phase 1 — MEASURE
  The agent calls ``count_matching_rows`` with the filter for its intended
  action.  The real row count comes back from the CSV.  Data scope is a fact,
  not a guess.

Phase 2 — PROPOSE
  Armed with the real count, the agent calls ``propose_action_tool`` to submit:
    - its chosen tool + parameters
    - all four risk dimensions (with its reasoning for each)
    - the overall risk_band  ("low" | "medium" | "high")
    - a rationale tying the dimensions together

  ``main.py`` then runs the proposal through ``build_assessment`` / ``route_action``
  / ``apply_blast_radius_floor`` exactly as before.

Why two phases instead of one?
  The old design asked the model to guess ``data_scope`` inline.  A bulk delete
  that really hits 34 000 rows was guessed at ~5 000 and banded medium.  Two
  phases fix that: phase 1 measures, phase 2 judges.

ClarificationRequest
  If the request is ambiguous the agent may call ``ask_for_clarification``
  instead of proposing.  ``main.py`` surfaces that to the caller so the user
  can answer and re-submit.

reassess_action
  If ``executor.preflight`` finds the agent's ``data_scope`` estimate was
  wildly wrong even after phase-1 measurement (shouldn't normally happen, but
  possible if the agent filtered differently in phase 2), ``main.py`` calls
  this to let the agent re-score with the true number.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Final

import groq as groq_module
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from autonomy_engine import data_store
from autonomy_engine.data_store import Criterion, DataStoreError
from autonomy_engine.risk_scorer import (
    RegulatoryCategory,
    Reversibility,
    RiskBand,
    RiskFactors,
)

load_dotenv()
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

GROQ_MODEL:     Final[str]   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_LOOP_TURNS: Final[int]   = 12
MAX_ATTEMPTS:   Final[int]   = 2
RETRY_DELAY:    Final[float] = 1.0

# --------------------------------------------------------------------------
# Public errors / return types
# --------------------------------------------------------------------------


class AgentActionError(RuntimeError):
    """Raised when the agent could not produce a usable action proposal."""


class ClarificationRequest(BaseModel):
    """The agent needs more information before it can safely propose."""
    question: str
    why: str
    options: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# AgentAction — what main.py builds the audit record from
# --------------------------------------------------------------------------


class AgentAction(BaseModel):
    """A single proposed action plus the agent's own risk self-assessment."""

    action_type: str
    description: str
    tool_name: str
    parameters: dict[str, Any]

    # Risk dimensions
    reversibility: Reversibility
    data_scope: int = Field(ge=0)
    regulatory_category: RegulatoryCategory
    confidence: float = Field(ge=0.0, le=1.0)

    # Reasoning strings (used by build_assessment → breakdown)
    reversibility_reasoning: str = ""
    data_scope_reasoning:    str = ""
    regulatory_reasoning:    str = ""
    confidence_reasoning:    str = ""
    risk_band:    RiskBand = "medium"
    severity:     float | None = None
    rationale:    str = ""

    def to_risk_factors(self) -> RiskFactors:
        return RiskFactors(
            reversibility=self.reversibility,
            reversibility_reasoning=self.reversibility_reasoning,
            data_scope=self.data_scope,
            data_scope_reasoning=self.data_scope_reasoning,
            regulatory_category=self.regulatory_category,
            regulatory_reasoning=self.regulatory_reasoning,
            confidence=self.confidence,
            confidence_reasoning=self.confidence_reasoning,
            risk_band=self.risk_band,
            severity=self.severity,
            rationale=self.rationale,
        )


# --------------------------------------------------------------------------
# Tool action-type vocabulary
# --------------------------------------------------------------------------

TOOL_ACTION_TYPES: Final[dict[str, str]] = {
    "query_transactions":       "read",
    "summarize_transactions":   "read_aggregate",
    "update_transaction":       "single_record_write",
    "delete_transaction":       "single_record_delete",
    "bulk_delete_transactions": "bulk_delete",
}

# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------

_CATEGORY_HINT = "\n".join(
    f"  {field}: {', '.join(vals)}"
    for field, vals in data_store.FIELD_VALUES.items()
)

SYSTEM_PROMPT: Final[str] = f"""\
You are an AI agent inside a graduated autonomy engine backed by a retail
transaction CSV (~99 000 rows).

Schema columns: {', '.join(data_store.FIELDS)}

Known categorical values:
{_CATEGORY_HINT}

=== YOUR JOB (two phases, in order) ===

PHASE 1 — MEASURE
Call count_matching_rows with the filter that matches the rows your intended
action will touch.  Do this FIRST so your data_scope reflects the real row
count, not a guess.  For single-record actions (update/delete one invoice)
you may skip this and use data_scope = 1.

If you cannot safely interpret the request, call ask_for_clarification instead
of guessing.

PHASE 2 — PROPOSE
After measuring, call propose_action_tool with:

  tool_name     one of: query_transactions | summarize_transactions |
                        update_transaction | delete_transaction |
                        bulk_delete_transactions
  parameters    the arguments for that tool (use the filter/invoice_no/field/
                new_value/group_by keys as described per tool)
  reversibility
    "reversible"           reads (nothing changes)
    "partially_reversible" updates (old value kept in snapshot)
    "irreversible"         any delete (rows cannot be restored)
  data_scope    exact integer from count_matching_rows, or 1 for single-record
  regulatory_category
    "none"               non-sensitive retail data (quantity, mall, payment)
    "internal_sensitive" personal identifiers (customer_id, age, gender)
    "regulated"          financial amounts combined with personal data, or data
                         the request labels as sensitive / subject to compliance
  confidence    0.0-1.0  how sure you are your filter/parameters match the intent
  risk_band     "low" | "medium" | "high"  your overall judgement
  rationale     one sentence tying the four dimensions to your band choice

Reasoning fields (*_reasoning) are required — they appear verbatim in the
audit trail a human reviewer reads.

Do NOT inflate or deflate these to influence the routing outcome.
"""

# --------------------------------------------------------------------------
# Filter schema (shared between tools)
# --------------------------------------------------------------------------

_FILTER_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "List of AND-ed filter criteria.",
    "items": {
        "type": "object",
        "properties": {
            "field":    {"type": "string"},
            "operator": {
                "type": "string",
                "enum": ["equals", "not_equals", "contains",
                         "before", "after", "greater_than", "less_than"],
            },
            "value": {"type": "string"},
        },
        "required": ["field", "operator", "value"],
        "additionalProperties": False,
    },
}

# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------

PHASE1_TOOLS: Final[list[dict[str, Any]]] = [
    {
        "type": "function",
        "function": {
            "name": "count_matching_rows",
            "description": (
                "Count rows matching a filter in the transaction CSV. "
                "Call this FIRST to get the real data_scope."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": _FILTER_SCHEMA,
                    "intent": {
                        "type": "string",
                        "description": "One-line description of what you plan to do with these rows.",
                    },
                },
                "required": ["filter", "intent"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_for_clarification",
            "description": "Ask the user a question when the request is too ambiguous to act on safely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why":      {"type": "string", "description": "Why you need this to proceed."},
                    "options":  {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Suggested answers to speed up the user.",
                    },
                },
                "required": ["question", "why"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action_tool",
            "description": (
                "Submit your proposed action and risk assessment. "
                "Call this after count_matching_rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # ── tool selection ──────────────────────────────────────
                    "tool_name": {
                        "type": "string",
                        "enum": list(TOOL_ACTION_TYPES.keys()),
                    },
                    # ── parameters per tool ─────────────────────────────────
                    "filter":     _FILTER_SCHEMA,
                    "invoice_no": {"type": "string", "description": "For single-record tools."},
                    "field":      {"type": "string", "description": "For update_transaction."},
                    "new_value":  {"type": "string", "description": "For update_transaction."},
                    "group_by":   {
                        "type": "string",
                        "description": "For summarize_transactions; empty string for no grouping.",
                    },
                    # ── risk dimensions ─────────────────────────────────────
                    "reversibility": {
                        "type": "string",
                        "enum": ["reversible", "partially_reversible", "irreversible"],
                    },
                    "reversibility_reasoning": {"type": "string"},
                    "data_scope": {
                        "type": "integer",
                        "description": "Exact count from count_matching_rows, or 1 for single-record.",
                    },
                    "data_scope_reasoning": {"type": "string"},
                    "regulatory_category": {
                        "type": "string",
                        "enum": ["none", "internal_sensitive", "regulated"],
                    },
                    "regulatory_reasoning": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "description": "0.0-1.0 confidence your parameters match the user's intent.",
                    },
                    "confidence_reasoning": {"type": "string"},
                    "risk_band": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence tying the four dimensions to your band choice.",
                    },
                },
                "required": [
                    "tool_name",
                    "reversibility", "reversibility_reasoning",
                    "data_scope",    "data_scope_reasoning",
                    "regulatory_category", "regulatory_reasoning",
                    "confidence",    "confidence_reasoning",
                    "risk_band",     "rationale",
                ],
                "additionalProperties": False,
            },
        },
    },
]

# --------------------------------------------------------------------------
# Tool execution (phase 1 only — count_matching_rows)
# --------------------------------------------------------------------------


def _parse_criteria(raw: Any) -> list[Criterion]:
    if not isinstance(raw, list):
        return []
    try:
        return [Criterion.model_validate(item) for item in raw]
    except Exception as exc:
        raise DataStoreError(f"malformed filter: {exc}") from exc


def _run_count(args: dict[str, Any]) -> str:
    try:
        criteria = _parse_criteria(args.get("filter", []))
        count = data_store.count_matching(criteria)
        return json.dumps({"count": count, "intent": args.get("intent", "")})
    except DataStoreError as exc:
        return json.dumps({"error": str(exc), "count": 0})


# --------------------------------------------------------------------------
# Groq client + retry
# --------------------------------------------------------------------------


def _groq_client() -> groq_module.Groq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise AgentActionError("GROQ_API_KEY is not set.")
    return groq_module.Groq(api_key=api_key, max_retries=0)


def _groq_call(
    client: groq_module.Groq,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    tool_choice: str = "required",
) -> Any:
    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return client.chat.completions.create(
                model=GROQ_MODEL,
                temperature=0,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )
        except (
            groq_module.APIConnectionError,
            groq_module.RateLimitError,
            groq_module.InternalServerError,
        ) as exc:
            last_err = exc
            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "Groq attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, type(exc).__name__, RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
        except groq_module.APIStatusError as exc:
            raise AgentActionError(
                f"Groq API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
    raise AgentActionError(
        f"Groq API unreachable after {MAX_ATTEMPTS} attempts: {last_err}"
    ) from last_err


# --------------------------------------------------------------------------
# Public API — propose_action
# --------------------------------------------------------------------------


def propose_action(
    user_request: str,
    tool_context: dict[str, Any],
) -> AgentAction | ClarificationRequest:
    """Run the two-phase agentic loop and return a proposal or a clarification.

    Args:
        user_request: What the user asked for.
        tool_context: Optional extra context (e.g. ``clarification from the user``).

    Returns:
        An :class:`AgentAction` ready for ``build_assessment`` / ``route_action``,
        or a :class:`ClarificationRequest` if the agent needs more information.

    Raises:
        AgentActionError: If the agent loop fails to produce either.
    """
    client  = _groq_client()
    prompt  = user_request
    situational = {k: v for k, v in tool_context.items()}
    if situational:
        context_lines = "\n".join(f"- {k}: {v}" for k, v in situational.items())
        prompt = f"Context:\n{context_lines}\n\nRequest: {user_request}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": prompt},
    ]

    for turn in range(MAX_LOOP_TURNS):
        response = _groq_call(client, messages, PHASE1_TOOLS, tool_choice="required")
        msg = response.choices[0].message

        # Append assistant message
        messages.append(_assistant_msg(msg))

        if not msg.tool_calls:
            raise AgentActionError(
                f"Agent stopped without proposing an action (turn {turn}). "
                f"Last text: {(msg.content or '')[:300]!r}"
            )

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as exc:
                raise AgentActionError(
                    f"Unparseable arguments from {name!r}: {exc}"
                ) from exc

            logger.info("agent called %s (turn %d)", name, turn)

            # ── Phase 1: measure ──────────────────────────────────────────
            if name == "count_matching_rows":
                result_str = _run_count(args)
                messages.append(_tool_msg(tc.id, result_str))
                # Continue loop — agent will now call propose_action_tool
                continue

            # ── Clarification ─────────────────────────────────────────────
            if name == "ask_for_clarification":
                messages.append(_tool_msg(tc.id, json.dumps({"acknowledged": True})))
                return ClarificationRequest(
                    question=args.get("question", ""),
                    why=args.get("why", ""),
                    options=args.get("options", []),
                )

            # ── Phase 2: propose ──────────────────────────────────────────
            if name == "propose_action_tool":
                messages.append(_tool_msg(tc.id, json.dumps({"acknowledged": True})))
                return _build_action(args)

            # Unknown tool — feed an error back so the agent can recover
            messages.append(_tool_msg(
                tc.id, json.dumps({"error": f"unknown tool: {name}"})
            ))

    raise AgentActionError(
        f"Agent loop did not produce a proposal within {MAX_LOOP_TURNS} turns."
    )


# --------------------------------------------------------------------------
# reassess_action — called by main.py when preflight finds a scope mismatch
# --------------------------------------------------------------------------


def reassess_action(action: AgentAction, actual_rows: int) -> AgentAction:
    """Re-run just the risk judgement with the corrected row count.

    The agent already chose the tool and built the parameters.  This only asks
    it to re-evaluate the four dimensions with a corrected ``data_scope``.

    Args:
        action: The original proposal.
        actual_rows: The true row count from ``executor.preflight``.

    Returns:
        A new :class:`AgentAction` with an updated assessment.
    """
    client = _groq_client()

    prompt = (
        f"You previously proposed {action.tool_name!r} and estimated "
        f"data_scope={action.data_scope}.  The engine measured the real count: "
        f"{actual_rows} rows.  Re-submit your risk assessment via "
        f"propose_action_tool using data_scope={actual_rows} and update your "
        f"risk_band and rationale accordingly.  Keep tool_name and parameters "
        f"exactly as before."
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Original request: {action.description}\n\n"
                f"Original parameters: {json.dumps(action.parameters)}\n\n"
                f"{prompt}"
            ),
        },
    ]

    for turn in range(4):
        response = _groq_call(client, messages, PHASE1_TOOLS, tool_choice="required")
        msg = response.choices[0].message
        messages.append(_assistant_msg(msg))

        if not msg.tool_calls:
            # Agent gave up — return original with corrected scope
            logger.warning("reassess_action: agent returned no tool call; using corrected scope only")
            return action.model_copy(update={
                "data_scope": actual_rows,
                "data_scope_reasoning": f"corrected by preflight: {actual_rows} rows",
            })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                continue

            if name == "propose_action_tool":
                messages.append(_tool_msg(tc.id, json.dumps({"acknowledged": True})))
                reassessed = _build_action(args)
                # Preserve original tool and parameters — only the assessment changes
                return reassessed.model_copy(update={
                    "tool_name":   action.tool_name,
                    "action_type": action.action_type,
                    "parameters":  action.parameters,
                    "description": action.description,
                })

            if name == "count_matching_rows":
                messages.append(_tool_msg(tc.id, json.dumps({"count": actual_rows})))
                continue

            messages.append(_tool_msg(tc.id, json.dumps({"error": f"unknown tool: {name}"})))

    logger.warning("reassess_action loop exhausted; applying corrected scope only")
    return action.model_copy(update={
        "data_scope": actual_rows,
        "data_scope_reasoning": f"corrected by preflight: {actual_rows} rows",
    })


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _build_action(args: dict[str, Any]) -> AgentAction:
    """Turn propose_action_tool arguments into an AgentAction."""
    tool_name = args["tool_name"]

    # Build parameters dict from whichever keys the tool expects
    parameters: dict[str, Any] = {}
    if "filter" in args and args.get("filter") is not None:
        parameters["filter"] = args["filter"]
    if "invoice_no" in args and args.get("invoice_no"):
        parameters["invoice_no"] = args["invoice_no"]
    if "field" in args and args.get("field"):
        parameters["field"] = args["field"]
    if "new_value" in args and args.get("new_value") is not None:
        parameters["new_value"] = args["new_value"]
    if "group_by" in args:
        parameters["group_by"] = args.get("group_by") or ""

    return AgentAction(
        action_type=TOOL_ACTION_TYPES.get(tool_name, tool_name),
        description=_describe(tool_name, parameters),
        tool_name=tool_name,
        parameters=parameters,
        reversibility=args["reversibility"],
        reversibility_reasoning=args.get("reversibility_reasoning", ""),
        data_scope=int(args["data_scope"]),
        data_scope_reasoning=args.get("data_scope_reasoning", ""),
        regulatory_category=args["regulatory_category"],
        regulatory_reasoning=args.get("regulatory_reasoning", ""),
        confidence=float(args["confidence"]),
        confidence_reasoning=args.get("confidence_reasoning", ""),
        risk_band=args["risk_band"],
        severity=args.get("severity"),
        rationale=args.get("rationale", ""),
    )


def _assistant_msg(msg: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {
                "id":       tc.id,
                "type":     "function",
                "function": {
                    "name":      tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (msg.tool_calls or [])
        ] or None,
    }


def _tool_msg(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _describe(tool_name: str, parameters: dict[str, Any]) -> str:
    if tool_name == "query_transactions":
        return f"Read transactions matching: {parameters.get('filter')}"
    if tool_name == "summarize_transactions":
        gb = parameters.get("group_by") or ""
        return f"Summarise transactions{f', grouped by {gb}' if gb else ''}: {parameters.get('filter')}"
    if tool_name == "update_transaction":
        return (
            f"Set {parameters.get('field')!r} → {parameters.get('new_value')!r} "
            f"on invoice {parameters.get('invoice_no')}"
        )
    if tool_name == "delete_transaction":
        return f"Permanently delete invoice {parameters.get('invoice_no')}"
    if tool_name == "bulk_delete_transactions":
        return f"Permanently delete all transactions matching: {parameters.get('filter')}"
    return f"{tool_name}({parameters})"
