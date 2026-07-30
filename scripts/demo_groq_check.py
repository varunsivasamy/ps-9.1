"""One-off local verification: prove the propose -> score -> route pipeline
works against a REAL tool-calling LLM, using Groq as a stand-in.

Why this exists: the project's Anthropic account currently has no API credits,
so src/autonomy_engine/agent_actions.py (which is intentionally built against
the Anthropic SDK, per the build plan) cannot be exercised live. This script is
NOT part of the deployed system and is not imported by anything under src/ --
it independently re-implements the same tool schemas and system prompt against
Groq's OpenAI-compatible chat completions API, purely so we can see real LLM
output flow through build_assessment() / route_action() before AWS credentials and
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
    RiskFactors,
    build_assessment,
    describe_routing,
    route_action,
)

load_dotenv()

MODEL = "llama-3.3-70b-versatile"

# The prompt and tool schemas come straight from the engine rather than being
# copied here. They previously were, and a copy is exactly how a check script
# ends up validating a schema the engine no longer uses -- the self_assessment
# block has since grown a required risk_band, and only one of the two copies
# would have gained it.
from autonomy_engine.agent_actions import (  # noqa: E402
    SYSTEM_PROMPT,
    TOOL_SCHEMAS,
    _to_openai_tools,
)

TOOLS = _to_openai_tools(TOOL_SCHEMAS)

SCENARIOS = [
    ("A. aggregate read", "Which product category made the most revenue?"),
    (
        "B. single-invoice delete",
        "Delete invoice I138884 from the database, it was entered by mistake.",
    ),
    (
        "C. bulk delete",
        "Purge every Clothing transaction from the database "
        "to satisfy a data retention policy.",
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
                reversibility_reasoning=assessment.get("reversibility_reasoning", ""),
                data_scope=assessment["data_scope"],
                data_scope_reasoning=assessment.get("data_scope_reasoning", ""),
                regulatory_category=assessment["regulatory_category"],
                regulatory_reasoning=assessment.get("regulatory_reasoning", ""),
                confidence=assessment["confidence"],
                confidence_reasoning=assessment.get("confidence_reasoning", ""),
                risk_band=assessment["risk_band"],
                severity=assessment.get("severity"),
                rationale=assessment.get("rationale", ""),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: self_assessment did not validate: {exc}")
            all_ok = False
            continue

        judged = build_assessment(factors)
        decision = route_action(judged)
        print(f"  risk_band        {judged.risk_band}")
        print(f"  routing          {describe_routing(judged, decision)}")

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
