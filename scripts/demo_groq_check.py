"""One-off local verification: prove the propose -> score -> route pipeline
works against a REAL tool-calling LLM, using Groq as a stand-in.

Why this exists: the project's Anthropic account currently has no API credits,
so src/autonomy_engine/agent_actions.py (which is intentionally built against
the Anthropic SDK, per the build plan) cannot be exercised live. This script is
NOT part of the deployed system and is not imported by anything under src/ --
it independently re-implements the same tool schemas and system prompt against
Groq's OpenAI-compatible chat completions API, purely so we can see real LLM
output flow through score_action() / route_action() before AWS credentials and
Anthropic credits are both available.

Run: python scripts/demo_groq_check.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from autonomy_engine.risk_scorer import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    RiskFactors,
    describe_routing,
    route_action,
    score_action,
)

load_dotenv()

MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """\
You are an AI agent operating inside a graduated autonomy engine. You have access
to customer-data tools. Choose exactly one tool call that fulfils the user's request.

Alongside the tool's own parameters, every tool requires a `self_assessment` object.
This is not paperwork -- it determines whether your action runs automatically,
pauses for a one-click human confirmation, or is blocked pending full human review.
Fill it in honestly:

- reversibility: "reversible" (a read, or a cleanly-rollback-able change),
  "partially_reversible" (revertible but may lose history), or "irreversible"
  (a deletion or external side effect that cannot be undone).
- data_scope: your best estimate of how many records or users the action affects.
  A single-record lookup is 1. If the request implies "all" or a broad filter,
  estimate honestly rather than guessing low.
- regulatory_category: "none" (non-sensitive business data), "internal_sensitive"
  (internal/personal data, not externally regulated), or "regulated" (data under
  a regime such as GDPR, HIPAA, or PCI).
- confidence: 0.0-1.0, how sure you are this specific tool call is the correct
  interpretation of the request. Understating your uncertainty is the more
  dangerous error: low confidence raises the risk score and pulls a human in.

Do not inflate or deflate these to influence the routing outcome.
"""

SELF_ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reversibility": {
            "type": "string",
            "enum": ["reversible", "partially_reversible", "irreversible"],
        },
        "data_scope": {"type": "integer"},
        "regulatory_category": {
            "type": "string",
            "enum": ["none", "internal_sensitive", "regulated"],
        },
        "confidence": {"type": "number"},
    },
    "required": ["reversibility", "data_scope", "regulatory_category", "confidence"],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_customer_records",
            "description": (
                "Read-only lookup of customer records. Returns data but changes "
                "nothing. Use for questions about existing customers, their "
                "orders, or their status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_description": {
                        "type": "string",
                        "description": "Which records to retrieve, in plain language.",
                    },
                    "self_assessment": SELF_ASSESSMENT_SCHEMA,
                },
                "required": ["filter_description", "self_assessment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_customer_record",
            "description": (
                "Write a new value to one field on one customer record. Affects "
                "exactly one record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "field": {"type": "string"},
                    "new_value": {"type": "string"},
                    "self_assessment": SELF_ASSESSMENT_SCHEMA,
                },
                "required": ["customer_id", "field", "new_value", "self_assessment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_delete_records",
            "description": (
                "Permanently delete every customer record matching a filter. "
                "Destructive and NOT recoverable. Can affect many records at once."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_description": {"type": "string"},
                    "self_assessment": SELF_ASSESSMENT_SCHEMA,
                },
                "required": ["filter_description", "self_assessment"],
            },
        },
    },
]

SCENARIOS = [
    ("A. read-only query", "What's the current email address on file for customer C-10482?"),
    (
        "B. single-record update",
        "Update customer C-10482's phone number to +44 20 7946 0958.",
    ),
    (
        "C. bulk delete",
        "Purge all 500 EU customer records that have been inactive since 2019 "
        "to satisfy a GDPR erasure request.",
    ),
]


def propose(client: Groq, user_request: str) -> dict:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_request},
        ],
        tools=TOOLS,
        tool_choice="required",
        temperature=0,
    )
    message = response.choices[0].message
    if not message.tool_calls:
        raise RuntimeError(f"model returned no tool call: {message.content!r}")
    call = message.tool_calls[0]
    args = json.loads(call.function.arguments)
    return {"tool_name": call.function.name, "arguments": args}


def main() -> None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    client = Groq(api_key=api_key)

    print("=" * 78)
    print(f"LOCAL DEMO CHECK -- live tool-calling via Groq ({MODEL})")
    print("Standing in for Anthropic while the account has no API credits.")
    print("=" * 78)

    all_ok = True
    for label, request_text in SCENARIOS:
        print(f"\n{label}: {request_text}")
        try:
            proposal = propose(client, request_text)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED to get a proposal: {exc}")
            all_ok = False
            continue

        args = proposal["arguments"]
        assessment = args.pop("self_assessment", None)
        if assessment is None:
            print(f"  FAILED: tool call {proposal['tool_name']} had no self_assessment")
            all_ok = False
            continue

        print(f"  tool called   {proposal['tool_name']}")
        print(f"  parameters    {args}")
        print(f"  self_assessment  {assessment}")

        try:
            factors = RiskFactors(
                reversibility=assessment["reversibility"],
                data_scope=assessment["data_scope"],
                regulatory_category=assessment["regulatory_category"],
                confidence=assessment["confidence"],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: self_assessment did not validate: {exc}")
            all_ok = False
            continue

        score = score_action(factors)
        decision = route_action(score, DEFAULT_THRESHOLDS)
        print(f"  composite_score  {score.composite_score}")
        print(f"  routing          {describe_routing(score, decision)}")

    print("\n" + "=" * 78)
    if all_ok:
        print("ALL SCENARIOS: real LLM output parsed and routed successfully.")
        print("The propose -> score -> route pipeline works end-to-end locally.")
    else:
        print("One or more scenarios failed -- see above.")
        sys.exit(1)
    print("=" * 78)


if __name__ == "__main__":
    main()
