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

compose_answer
  Phase 3, after the action has actually run. Turns the raw
  :class:`~autonomy_engine.executor.ExecutionResult` back into a sentence that
  answers what the user originally asked. Without it the caller gets
  ``"Read 47 transaction(s)"`` and has to interpret the rows themselves, which
  is not an agent answering a question.
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

=== MANDATORY TWO-PHASE PROCESS ===

PHASE 1 — MEASURE (REQUIRED for bulk_delete_transactions)
You MUST call count_matching_rows before propose_action_tool whenever the
action is bulk_delete_transactions. Use your intended filter to get the real
row count. This is not optional.

For update_transaction and delete_transaction (single record): skip phase 1,
use data_scope = 1.
For query_transactions and summarize_transactions (reads): skip phase 1,
use data_scope = 0.

If the request is ambiguous and you cannot build a safe filter, call
ask_for_clarification instead.

PHASE 2 — PROPOSE
Call propose_action_tool with ALL of the following fields (every field is
required, every string field must be non-empty):

  tool_name       (string, required) one of:
                    query_transactions
                    summarize_transactions
                    update_transaction
                    delete_transaction
                    bulk_delete_transactions

  filter          (array) criteria for query/summarize/bulk_delete tools
  invoice_no      (string) for update_transaction and delete_transaction
  field           (string) for update_transaction
  new_value       (string) for update_transaction
  group_by        (string) for summarize_transactions; use "" for no grouping

  reversibility   (string, required) EXACTLY one of:
                    "reversible"           — reads only
                    "partially_reversible" — updates (snapshot kept)
                    "irreversible"         — ANY delete, single or bulk

  reversibility_reasoning   (string, required, non-empty)

  data_scope      (integer, required) — write as a bare integer, NOT a string:
                    0  for reads (query_transactions, summarize_transactions)
                    1  for single-record mutations (update_transaction, delete_transaction)
                    use the count returned by count_matching_rows for bulk_delete_transactions

  data_scope_reasoning      (string, required, non-empty)

  regulatory_category (string, required) EXACTLY one of:
                    "none"               — retail metrics only (category, mall,
                                           payment_method, quantity, price)
                    "internal_sensitive" — personal fields (customer_id, age, gender)
                    "regulated"          — financial + personal combined, or
                                           request labels data as sensitive

  regulatory_reasoning      (string, required, non-empty)

  confidence      (number, required) — write as a bare decimal, NOT a string:
                    0.0 to 1.0 — how sure you are filter/params match intent

  confidence_reasoning      (string, required, non-empty)

  risk_band       (string, required) EXACTLY one of — follow these rules strictly:
                    "low"    — reads only (query_transactions, summarize_transactions)
                    "medium" — single-record update (update_transaction)
                    "high"   — ANY delete (delete_transaction or bulk_delete_transactions)
                               regardless of row count

  rationale       (string, required, non-empty) — one sentence tying all four
                  dimensions to your band choice

IMPORTANT — TYPE RULES (Groq enforces strict JSON types):
  data_scope MUST be a JSON integer:  1   not "1"
  confidence MUST be a JSON number:   0.9 not "0.9"
  All other fields must be strings.
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
                "Call this FIRST to get the real data_scope before bulk_delete_transactions."
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
                        "description": (
                            "reversible=reads only; "
                            "partially_reversible=updates (snapshot kept); "
                            "irreversible=any delete (cannot be undone)"
                        ),
                    },
                    "reversibility_reasoning": {
                        "type": "string",
                        "description": "Why you chose this reversibility level.",
                    },
                    "data_scope": {
                        "type": "integer",
                        "description": (
                            "Row count from count_matching_rows. "
                            "Use 0 for reads (they change nothing). "
                            "Use 1 for single-record mutations. "
                            "Never guess for bulk operations — call count_matching_rows first."
                        ),
                    },
                    "data_scope_reasoning": {
                        "type": "string",
                        "description": "How you determined this count (measured vs estimated).",
                    },
                    "regulatory_category": {
                        "type": "string",
                        "enum": ["none", "internal_sensitive", "regulated"],
                        "description": (
                            "none=retail metrics (quantity, mall, payment_method, category); "
                            "internal_sensitive=personal identifiers (customer_id, age, gender); "
                            "regulated=financial+personal combined, or request labels data as sensitive"
                        ),
                    },
                    "regulatory_reasoning": {
                        "type": "string",
                        "description": "Why this data falls in that sensitivity class.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": (
                            "0.0-1.0. How sure you are your filter/parameters match the user's intent. "
                            "Use < 0.7 if the request is ambiguous."
                        ),
                    },
                    "confidence_reasoning": {
                        "type": "string",
                        "description": "What you are or are not sure about.",
                    },
                    "risk_band": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "low=reads; medium=single-record mutations; "
                            "high=any delete OR bulk mutations affecting many rows"
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence tying all four dimensions to your band choice.",
                    },
                },
                "required": [
                    "tool_name",
                    "reversibility",       "reversibility_reasoning",
                    "data_scope",          "data_scope_reasoning",
                    "regulatory_category", "regulatory_reasoning",
                    "confidence",          "confidence_reasoning",
                    "risk_band",           "rationale",
                ],
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
            # 400 "tool call validation failed" means the model sent wrong types
            # (e.g. "0" instead of 0). Extract the failed_generation JSON and
            # return it as a synthetic response so _build_action can coerce it.
            if exc.status_code == 400:
                # Build a search string from all available error info
                raw = str(exc)
                try:
                    body = exc.body if isinstance(exc.body, dict) else {}
                    fg = body.get("error", {}).get("failed_generation", "")
                    if fg:
                        raw = fg
                except Exception:
                    pass
                synthetic = _parse_failed_generation(raw)
                if synthetic:
                    logger.warning(
                        "Groq schema validation rejected tool call; "
                        "recovered via failed_generation parsing"
                    )
                    return synthetic
            raise AgentActionError(
                f"Groq API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
    raise AgentActionError(
        f"Groq API unreachable after {MAX_ATTEMPTS} attempts: {last_err}"
    ) from last_err


def _parse_failed_generation(error_text: str) -> Any | None:
    """Extract and parse a tool call from Groq's failed_generation error field.

    When Groq's strict validator rejects a tool call (e.g. integer sent as
    string), it includes the raw model output in the error message. We can
    parse it ourselves and coerce the types, bypassing the validator.
    """
    import re

    # Extract the <function=name>{...}</function> or <function=name>{...}> block
    match = re.search(
        r"<function=(\w+)>(\{.*?\})(?:</function>|>)",
        error_text,
        re.DOTALL,
    )
    if not match:
        return None

    tool_name = match.group(1)
    raw_json  = match.group(2)

    try:
        args = json.loads(raw_json)
    except json.JSONDecodeError:
        return None

    # Wrap in a fake response object that looks like a Groq response
    class _FakeFunction:
        def __init__(self, name: str, arguments: str):
            self.name = name
            self.arguments = arguments

    class _FakeToolCall:
        def __init__(self, name: str, args_str: str):
            self.id = "recovered-0"
            self.function = _FakeFunction(name, args_str)

    class _FakeMessage:
        def __init__(self, tc: _FakeToolCall):
            self.content = ""
            self.tool_calls = [tc]

    class _FakeChoice:
        def __init__(self, msg: _FakeMessage):
            self.message = msg
            self.finish_reason = "tool_calls"

    class _FakeResponse:
        def __init__(self, choice: _FakeChoice):
            self.choices = [choice]

    tc  = _FakeToolCall(tool_name, json.dumps(args))
    msg = _FakeMessage(tc)
    return _FakeResponse(_FakeChoice(msg))


# --------------------------------------------------------------------------
# Public API — propose_action
# --------------------------------------------------------------------------


def propose_action(
    user_request: str,
    tool_context: dict[str, Any],
) -> AgentAction:
    """Run the two-phase agentic loop and return an action proposal.

    Never asks for clarification — the LLM reasons and picks the best
    interpretation of any ambiguous request.
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
                continue

            # ── Clarification — disabled: LLM must reason and proceed ─────
            if name == "ask_for_clarification":
                # Ignore clarification request, instruct agent to make its
                # best interpretation and propose an action instead.
                messages.append(_tool_msg(tc.id, json.dumps({
                    "instruction": (
                        "Do not ask for clarification. Make your best reasonable "
                        "interpretation of the request, pick the safest matching "
                        "tool, and call propose_action_tool immediately."
                    )
                })))
                continue

            # ── Phase 2: propose ──────────────────────────────────────────
            if name == "propose_action_tool":
                # Safety net: if the agent proposes bulk_delete but skipped
                # count_matching_rows, measure now before accepting the proposal.
                if (
                    args.get("tool_name") == "bulk_delete_transactions"
                    and int(float(str(args.get("data_scope", 0)))) == 0
                    and args.get("filter")
                ):
                    real_count_str = _run_count({"filter": args["filter"], "intent": "auto-measure"})
                    real_count = json.loads(real_count_str).get("count", 0)
                    logger.info(
                        "auto-measured bulk_delete scope: %d rows (agent skipped phase 1)",
                        real_count,
                    )
                    args["data_scope"] = real_count
                    args["data_scope_reasoning"] = (
                        f"Auto-measured by engine: filter matched {real_count} rows"
                    )

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
    """Turn propose_action_tool arguments into an AgentAction.

    Coerces data_scope and confidence to the right types in case the model
    serialises them as strings (a known Groq/llama quirk).
    """
    tool_name = args["tool_name"]

    # Coerce numeric fields — llama-3.3 occasionally serialises integers and
    # floats as JSON strings even when the schema says otherwise.
    try:
        data_scope = int(float(str(args.get("data_scope", 0))))
    except (ValueError, TypeError):
        data_scope = 0

    try:
        confidence = float(str(args.get("confidence", 0.9)))
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        confidence = 0.9

    # Build parameters dict from whichever keys the tool expects
    parameters: dict[str, Any] = {}
    if args.get("filter"):
        parameters["filter"] = args["filter"]
    if args.get("invoice_no"):
        parameters["invoice_no"] = args["invoice_no"]
    if args.get("field"):
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
        data_scope=data_scope,
        data_scope_reasoning=args.get("data_scope_reasoning", ""),
        regulatory_category=args["regulatory_category"],
        regulatory_reasoning=args.get("regulatory_reasoning", ""),
        confidence=confidence,
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


ANSWER_SYSTEM_PROMPT: Final[str] = """\
You are answering a user who asked an AI agent to do something with a retail
transaction database. The action has already run. You are given the original
request, what the agent did, and the real result.

Write the answer the user is waiting for:
  - 1-3 sentences, plain prose, no preamble and no markdown.
  - Lead with the number or fact they actually asked for.
  - Use ONLY figures present in the result JSON. Never estimate, extrapolate,
    or invent a value that is not there.
  - If `truncated` is true, say the listing was capped but the count is complete.
  - If `status` is "failed", say plainly that it did not run and why.

`rows` may be a short sample given to you for inspection, and `_sample_note`
describes that sampling. Both are internal. Never mention them, and never
report the sample size as if it were the result — the real figure is
`affected_count`. The user is shown the full table separately.

Do not describe risk, routing, or approval — the interface already shows those.
"""

#: Rows sent to the answer model. The API returns up to QUERY_ROW_LIMIT (25),
#: but the model only needs a sample to characterise them, and a full 25-row
#: dump is mostly wasted tokens.
_ANSWER_ROW_SAMPLE: Final[int] = 10


def compose_answer(
    user_request: str,
    action_description: str,
    result: dict[str, Any],
) -> str:
    """Turn a finished execution back into an answer to the original question.

    Best-effort by design. This runs *after* the action has already happened, so
    a failure here must never turn a successful execution into an error
    response -- every failure path falls back to :func:`_fallback_answer`, which
    is derived from the same result dict without a network call.

    Args:
        user_request: What the user originally asked for.
        action_description: The agent's own description of the action it took.
        result: The :meth:`~autonomy_engine.executor.ExecutionResult.to_payload`
            dict from the run.

    Returns:
        A short prose answer. Never raises.
    """
    grounding = dict(result)
    rows = grounding.get("rows")
    if isinstance(rows, list) and len(rows) > _ANSWER_ROW_SAMPLE:
        grounding["rows"] = rows[:_ANSWER_ROW_SAMPLE]
        grounding["_sample_note"] = (
            f"internal: {_ANSWER_ROW_SAMPLE} of {len(rows)} listed rows, for your "
            "inspection only — do not report this number"
        )

    try:
        client = _groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            max_tokens=300,
            messages=[
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Original request: {user_request}\n\n"
                        f"Action taken: {action_description}\n\n"
                        f"Result: {json.dumps(grounding, default=str)}"
                    ),
                },
            ],
        )
        answer = (response.choices[0].message.content or "").strip()
        if answer:
            return answer
        logger.warning("answer model returned empty content; using fallback")
    except Exception:  # noqa: BLE001 - the action already ran; never fail here
        logger.warning("could not compose answer via model; using fallback", exc_info=True)

    return _fallback_answer(result)


def _fallback_answer(result: dict[str, Any]) -> str:
    """A grounded answer built from the result alone, with no model call.

    Deliberately plain. Its job is to make sure the user always gets the
    figures, even when the answer model is unreachable.
    """
    detail = str(result.get("detail") or "The action completed.")

    if result.get("status") == "failed":
        return f"That did not run: {detail}"
    if result.get("status") == "skipped":
        return f"Nothing was run: {detail}"

    parts = [detail.rstrip(".") + "."]

    summary = result.get("summary")
    if isinstance(summary, dict):
        groups = summary.get("groups")
        if isinstance(groups, dict) and groups:
            parts.append(f"Broken down across {len(groups)} group(s).")

    if result.get("truncated"):
        parts.append(
            f"The listing below is capped at {len(result.get('rows') or [])} rows; "
            "the count itself is complete."
        )

    return " ".join(parts)


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
