"""
Interactive human-approval demo  (multi-turn agentic loop)
===========================================================
Run:  python scripts/run_approval_demo.py

What it does
------------
1. Spins up a moto (in-process) mock of DynamoDB.
2. Sends a high-risk query to Groq.
3. The agent runs a three-phase loop:
     Phase 1 — calls count_matching_rows → gets real row count from CSV
     Phase 2 — calls submit_risk_assessment → engine scores and routes
     Phase 3 — if autonomous: executes immediately
               if confirm:    pauses here, asks YOU to approve/reject
               if full_review: pauses here, asks YOU to approve/reject
4. Prints the final audit record.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv()

# Force Groq — no Anthropic
os.environ["AGENT_LLM_PROVIDER"] = "groq"

# Moto must start before boto3/audit_store are imported
import moto
mock = moto.mock_aws()
mock.start()

import boto3
from autonomy_engine import audit_store

def _create_table() -> None:
    prefix = os.getenv("DYNAMODB_TABLE_PREFIX", "ps-9-1-autonomy-engine-local")
    name = f"{prefix}-audit-log"
    dynamo = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    dynamo.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "session_id", "KeyType": "HASH"},
            {"AttributeName": "timestamp",  "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "timestamp",  "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    ).wait_until_exists()

os.environ["DYNAMODB_ENDPOINT_URL"] = ""
audit_store.reset_cache()
_create_table()
audit_store.reset_cache()

from autonomy_engine import confirmation
from autonomy_engine.agent_actions import run_agent_loop

DIVIDER = "─" * 64


def main() -> None:
    # ── choose your query here ────────────────────────────────────────────
    user_request = (
        "Permanently delete all Clothing transactions from Kanyon mall. "
        "This is for a data retention audit and cannot be undone."
    )
    session_id = "demo-session-001"

    print(DIVIDER)
    print("PS-9.1  Graduated Autonomy Engine  —  Interactive Demo")
    print(DIVIDER)
    print(f"\nUser request : {user_request}")
    print(f"Session      : {session_id}\n")

    print("⏳  Agent is measuring risk (calling real tools) …\n")
    result = run_agent_loop(user_request)
    action = result.action

    print(f"✅  Agent chose tool   : {action.tool_name}")
    print(f"    Description        : {action.description}")
    print(f"    Reversibility      : {action.reversibility}")
    print(f"    Data scope (real)  : {action.data_scope} row(s) counted from CSV")
    print(f"    Regulatory         : {action.regulatory_category}")
    print(f"    Model confidence   : {action.confidence:.2f}")
    print(f"\n📊  Composite score : {result.composite_score:.4f}")
    print(f"    Routing         : {result.routing.upper()}\n")
    for dim, note in result.score_breakdown.items():
        print(f"    {dim:<22s} {note}")

    print(f"\n{DIVIDER}")

    # ── route ─────────────────────────────────────────────────────────────
    if result.routing == "autonomous":
        record = confirmation.record_autonomous_execution(
            action, _fake_score(result), session_id=session_id
        )
        print("🤖  LOW RISK — executed automatically.")
        if result.execution_result:
            print(f"    Result: {result.execution_result}")
        print(f"    Audit record id : {record['record_id']}")

    elif result.routing == "confirm":
        confirmation_id = confirmation.create_confirmation_request(
            action, _fake_score(result), session_id=session_id
        )
        print("🟡  MEDIUM RISK — human approval required.\n")
        print(f"    Preview         : {action.description}")
        print(f"    Rows affected   : {action.data_scope}")
        print(f"    Confirmation ID : {confirmation_id}\n")

        choice = _prompt("confirm", "reject")
        reviewer = input("    ➤  Your name (reviewer): ").strip() or "demo-user"
        record = confirmation.resolve_confirmation(confirmation_id, choice, reviewer)
        _print_decision(choice, record)

    else:  # full_review
        review_id = confirmation.create_review_request(
            action, _fake_score(result), session_id=session_id
        )
        print("🔴  HIGH RISK — blocked, pending full human review.\n")
        print(f"    Preview       : {action.description}")
        print(f"    Rows affected : {action.data_scope}")
        print(f"    Review ID     : {review_id}\n")

        choice = _prompt("approve", "reject")
        reviewer = input("    ➤  Your name (reviewer): ").strip() or "demo-user"
        record = confirmation.resolve_review(review_id, choice, reviewer)
        _print_decision(choice, record)

    # ── audit trail ───────────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("📋  Audit trail:\n")
    for entry in audit_store.get_audit_trail(session_id):
        print(f"  record_id   : {entry['record_id']}")
        print(f"  action_type : {entry.get('action_type')}")
        print(f"  routing     : {entry.get('routing_decision')}")
        print(f"  status      : {entry.get('status')}")
        print(f"  reviewer    : {entry.get('reviewer')}")
        print(f"  score       : {entry.get('composite_score')}")
        print()

    print(DIVIDER)
    mock.stop()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _prompt(opt_a: str, opt_b: str) -> str:
    while True:
        choice = input(f"    ➤  Your decision [{opt_a} / {opt_b}]: ").strip().lower()
        if choice in (opt_a, opt_b):
            return choice
        print(f"    Please type '{opt_a}' or '{opt_b}'.")


def _print_decision(choice: str, record: dict) -> None:
    icon = "✅" if choice in ("confirm", "approve") else "❌"
    print(f"\n{icon}  Decision recorded: {record['status'].upper()} by {record['reviewer']}")


def _fake_score(result) -> object:
    """Shim: confirmation functions want a RiskScore object."""
    from autonomy_engine.risk_scorer import RiskScore
    return RiskScore(
        reversibility_score=0.0,
        data_scope_score=0.0,
        regulatory_score=0.0,
        confidence_score=0.0,
        composite_score=result.composite_score,
        breakdown=result.score_breakdown,
    )


if __name__ == "__main__":
    main()
