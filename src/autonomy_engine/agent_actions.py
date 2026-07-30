"""Agent action layer -- the agent proposes an action and grades its own risk.

This is where the LLM connection is organic rather than bolted on. Instead of
inferring risk from the outside, we ask Claude to pick a tool *and*, in the same
structured tool call, classify what it is about to do: is it reversible, how many
records does it touch, is the data regulated, and how confident is it that this is
the right action at all? Those four values feed straight into
:func:`autonomy_engine.risk_scorer.score_action`.

Getting the self-assessment as part of the tool input (rather than as a second
call, or parsed out of prose) means one round trip and no free-text parsing. The
tool schemas are declared ``strict`` so the API guarantees the input validates
against them.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Final

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from autonomy_engine.risk_scorer import (
    RegulatoryCategory,
    Reversibility,
    RiskFactors,
)

# Load .env for local development. In Lambda the variables come from the
# function's environment and there is no .env file -- this is a no-op there.
load_dotenv()

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

#: Model used to propose actions. Overridable so a deployment can pin a
#: different model without a code change.
DEFAULT_MODEL: Final[str] = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

#: Effort level for the proposal call. This is a short, well-scoped structured
#: extraction, and it sits on the request path of a 30s Lambda -- "low" keeps
#: latency and token spend down. Raise to "medium" if classifications look sloppy.
MODEL_EFFORT: Final[str] = os.getenv("ANTHROPIC_EFFORT", "low")

#: Ceiling on thinking + response tokens. Generous relative to a single small
#: tool call so an adaptive-thinking turn can't truncate mid-call.
MAX_TOKENS: Final[int] = 8192

#: The plan calls for exactly one retry, so the SDK's own retry loop is disabled
#: (see ``_client``) and retries are handled here instead.
MAX_ATTEMPTS: Final[int] = 2
RETRY_DELAY_SECONDS: Final[float] = 1.0

SYSTEM_PROMPT: Final[str] = """\
You are an AI agent operating inside a graduated autonomy engine. You have access \
to customer-data tools. Choose exactly one tool call that fulfils the user's request.

Alongside the tool's own parameters, every tool requires a `self_assessment` object. \
This is not paperwork -- it is what determines whether your action runs \
automatically, pauses for a one-click human confirmation, or is blocked pending \
full human review. Fill it in honestly:

- reversibility: can this be undone afterwards?
    "reversible"             a read, or a change that can be cleanly rolled back
    "partially_reversible"   a change that can be reverted but may lose history
    "irreversible"           a deletion or external side effect that cannot be undone
- data_scope: your best estimate of how many records or users the action affects.
  A single-record lookup is 1. If the request implies "all" or a broad filter,
  estimate honestly rather than guessing low.
- regulatory_category: the sensitivity of the data involved.
    "none"                non-sensitive business data
    "internal_sensitive"  internal or personal data, not externally regulated
    "regulated"           data under a regime such as GDPR, HIPAA, or PCI
- confidence: 0.0-1.0, how sure you are that this specific tool call is the
  correct interpretation of the request. Understating your uncertainty is the
  more dangerous error: low confidence raises the risk score and pulls a human in.

Do not inflate or deflate these to influence the routing outcome. Report what you
actually believe.
"""

# --------------------------------------------------------------------------
# Tool schemas
#
# Three tools spanning the risk spectrum used in the demo: a read, a
# single-record write, and a bulk destructive operation. Every tool carries the
# same `self_assessment` block so the risk scorer gets the same four inputs
# regardless of which tool the agent picks.
# --------------------------------------------------------------------------

_SELF_ASSESSMENT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "Your own risk classification of the action you are proposing.",
    "properties": {
        "reversibility": {
            "type": "string",
            "enum": ["reversible", "partially_reversible", "irreversible"],
            "description": "Whether this action can be undone once performed.",
        },
        "data_scope": {
            "type": "integer",
            "description": "Estimated number of records or users affected.",
        },
        "regulatory_category": {
            "type": "string",
            "enum": ["none", "internal_sensitive", "regulated"],
            "description": "Sensitivity class of the data this action touches.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence that this is the correct action.",
        },
    },
    "required": ["reversibility", "data_scope", "regulatory_category", "confidence"],
    "additionalProperties": False,
}

TOOL_SCHEMAS: Final[list[dict[str, Any]]] = [
    {
        "name": "query_customer_records",
        "description": (
            "Read-only lookup of customer records. Returns data but changes nothing. "
            "Use for questions about existing customers, their orders, or their status."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_description": {
                    "type": "string",
                    "description": "Which records to retrieve, in plain language.",
                },
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["filter_description", "self_assessment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_customer_record",
        "description": (
            "Write a new value to one field on one customer record. Affects exactly "
            "one record. The prior value is kept in history but the change is live "
            "immediately. Use for correcting or updating a single customer's details."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "Identifier of the single customer record to update.",
                },
                "field": {
                    "type": "string",
                    "description": "Name of the field being changed, e.g. email_address.",
                },
                "new_value": {
                    "type": "string",
                    "description": "The value to write to that field.",
                },
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["customer_id", "field", "new_value", "self_assessment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bulk_delete_records",
        "description": (
            "Permanently delete every customer record matching a filter. Destructive "
            "and NOT recoverable -- deleted records cannot be restored. Can affect "
            "many records at once. Use only when the request is explicitly a deletion."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_description": {
                    "type": "string",
                    "description": "Which records to delete, in plain language.",
                },
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["filter_description", "self_assessment"],
            "additionalProperties": False,
        },
    },
]

#: Maps each tool to the action_type recorded in the audit log. Kept separate
#: from the schemas so the audit vocabulary can evolve independently of the
#: tool names the model sees.
TOOL_ACTION_TYPES: Final[dict[str, str]] = {
    "query_customer_records": "read",
    "update_customer_record": "single_record_write",
    "bulk_delete_records": "bulk_delete",
}


# --------------------------------------------------------------------------
# Models and errors
# --------------------------------------------------------------------------


class AgentActionError(RuntimeError):
    """Raised when the agent could not produce a usable action proposal.

    Deliberately loud: a silent failure here would mean an unscored action, and
    an unscored action must never reach an execution path.
    """


class AgentAction(BaseModel):
    """A single action the agent proposes to take, with its own risk assessment."""

    action_type: str = Field(description="Audit vocabulary for this kind of action.")
    description: str = Field(description="Human-readable summary of the proposed action.")
    tool_name: str = Field(description="Which tool the agent chose.")
    parameters: dict[str, Any] = Field(description="Arguments for that tool call.")

    # The three classifications and the confidence score below are the agent's
    # own, and feed directly into RiskFactors.
    reversibility: Reversibility
    data_scope: int = Field(ge=0)
    regulatory_category: RegulatoryCategory
    confidence: float = Field(ge=0.0, le=1.0)

    def to_risk_factors(self) -> RiskFactors:
        """Project this action onto the four dimensions the risk scorer needs."""
        return RiskFactors(
            reversibility=self.reversibility,
            data_scope=self.data_scope,
            regulatory_category=self.regulatory_category,
            confidence=self.confidence,
        )


# --------------------------------------------------------------------------
# Anthropic client
# --------------------------------------------------------------------------


def _client() -> anthropic.Anthropic:
    """Build an Anthropic client, failing loudly if no key is configured.

    ``max_retries=0`` disables the SDK's own retry loop so that
    :data:`MAX_ATTEMPTS` here is the whole retry story -- otherwise the two
    layers would multiply and a rate-limited call could stall well past the
    Lambda timeout.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise AgentActionError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add a key, "
            "or set the variable in the Lambda environment."
        )
    return anthropic.Anthropic(api_key=api_key, max_retries=0)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def propose_action(user_request: str, tool_context: dict[str, Any]) -> AgentAction:
    """Ask Claude to propose one tool call and classify its own risk.

    Args:
        user_request: What the user asked for, in natural language.
        tool_context: Extra context handed to the agent. ``tools`` may override
            the tool schemas offered (defaults to :data:`TOOL_SCHEMAS`); any
            other keys are passed to the model as situational context, e.g.
            ``{"tenant": "acme", "environment": "production"}``.

    Returns:
        The proposed :class:`AgentAction`, including the agent's self-reported
        confidence and risk classifications.

    Raises:
        AgentActionError: If the API is unreachable after one retry, if the model
            declined the request, or if it returned no usable tool call.
    """
    tools = tool_context.get("tools", TOOL_SCHEMAS)
    situational = {k: v for k, v in tool_context.items() if k != "tools"}

    prompt = user_request
    if situational:
        context_lines = "\n".join(f"- {k}: {v}" for k, v in situational.items())
        prompt = f"Context for this request:\n{context_lines}\n\nRequest: {user_request}"

    response = _call_with_one_retry(prompt, tools)
    return _parse_response(response)


def _call_with_one_retry(prompt: str, tools: list[dict[str, Any]]) -> Any:
    """Call the Messages API, retrying once on transient failures.

    Retries only what is worth retrying: connection failures, rate limits, and
    5xx. A 400 or 401 means the request or the key is wrong and will fail
    identically the second time, so it is surfaced immediately.
    """
    client = _client()
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": MODEL_EFFORT},
                system=SYSTEM_PROMPT,
                tools=tools,
                tool_choice={"type": "any"},  # the agent must pick an action
                messages=[{"role": "user", "content": prompt}],
            )
        except (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "propose_action attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    MAX_ATTEMPTS,
                    type(exc).__name__,
                    RETRY_DELAY_SECONDS,
                )
                time.sleep(RETRY_DELAY_SECONDS)
        except anthropic.APIStatusError as exc:
            # Not retryable: bad request, bad key, missing model.
            raise AgentActionError(
                f"Anthropic API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc

    raise AgentActionError(
        f"Anthropic API unreachable after {MAX_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _parse_response(response: Any) -> AgentAction:
    """Turn a Messages API response into an :class:`AgentAction`."""
    if response.stop_reason == "refusal":
        raise AgentActionError(
            "The model declined to propose an action for this request "
            "(stop_reason=refusal). Nothing was executed."
        )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        raise AgentActionError(
            "The model returned no tool call, so there is no action to score. "
            f"stop_reason={response.stop_reason!r}, text={text[:200]!r}"
        )

    # `strict: True` guarantees the shape, but this layer also runs against
    # mocked responses in tests, so validate rather than assume.
    payload = dict(tool_use.input)
    assessment = payload.pop("self_assessment", None)
    if not isinstance(assessment, dict):
        raise AgentActionError(
            f"Tool call {tool_use.name!r} is missing its self_assessment block; "
            "the action cannot be risk-scored."
        )

    try:
        return AgentAction(
            action_type=TOOL_ACTION_TYPES.get(tool_use.name, tool_use.name),
            description=_describe(tool_use.name, payload),
            tool_name=tool_use.name,
            parameters=payload,
            reversibility=assessment["reversibility"],
            data_scope=assessment["data_scope"],
            regulatory_category=assessment["regulatory_category"],
            confidence=assessment["confidence"],
        )
    except (KeyError, ValueError) as exc:
        raise AgentActionError(
            f"Tool call {tool_use.name!r} returned an unusable self_assessment "
            f"({assessment!r}): {exc}"
        ) from exc


def _describe(tool_name: str, parameters: dict[str, Any]) -> str:
    """Render a preview line a human can approve or reject without reading JSON."""
    if tool_name == "query_customer_records":
        return f"Read customer records matching: {parameters.get('filter_description')}"
    if tool_name == "update_customer_record":
        return (
            f"Set {parameters.get('field')} to {parameters.get('new_value')!r} "
            f"on customer {parameters.get('customer_id')}"
        )
    if tool_name == "bulk_delete_records":
        return (
            "PERMANENTLY DELETE all customer records matching: "
            f"{parameters.get('filter_description')}"
        )
    return f"{tool_name} with {parameters}"
