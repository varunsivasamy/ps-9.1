"""
Interactive human-approval demo
================================
Run:  python scripts/run_approval_demo.py

What it does:
1. Spins up a moto (in-process) mock of DynamoDB — no real AWS, no local server needed.
2. Sends a medium-risk query to the LLM (Groq by default; switch to Anthropic by
   unsetting AGENT_LLM_PROVIDER in your environment).
3. The agent scores the action and routes it.
   - If routed to "confirm"  -> pauses and asks YOU to approve/reject.
   - If routed to "autonomous" -> executes immediately (low risk).
   - If routed to "full_review" -> prints what a reviewer would see.
4. Prints the final audit record.

Set AGENT_LLM_PROVIDER=groq (or anthropic) in your shell before running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── make sure the src package is importable ──────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# ── load .env before touching any autonomy_engine module ─────────────────────
from dotenv import load_dotenv
load_dotenv()

# Force Groq as the LLM provider — no Anthropic calls
os.environ["AGENT_LLM_PROVIDER"] = "groq"

# ── moto must be activated BEFORE boto3/audit_store is imported ──────────────
import moto
mock = moto.mock_aws()
mock.start()

import boto3
from autonomy_engine import audit_store

# Create the in-memory DynamoDB table
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

# Override endpoint so audit_store uses the moto mock, not localhost:5000
os.environ["DYNAMODB_ENDPOINT_URL"] = ""
audit_store.reset_cache()
_create_table()
audit_store.reset_cache()

# ── now import the rest of the engine ─────────────────────────────────────────
from autonomy_engine import confirmation
from autonomy_engine.agent_actions import propose_action
from autonomy_engine import data_store
from autonomy_engine.risk_scorer import build_assessment, route_action, describe_routing

DIVIDER = "─" * 60

def main() -> None:
    # A high-risk request: bulk irreversible delete on regulated data
    user_request = (
        "Permanently delete every Clothing transaction from the database "
        "to comply with our data retention policy. This cannot be undone."
    )
    session_id = "demo-session-001"

    print(DIVIDER)
    print("PS-9.1  Graduated Autonomy Engine  –  Interactive Demo")
    print(DIVIDER)
    print(f"\nUser request : {user_request}")
    print(f"Session      : {session_id}")
    print(f"Transactions : {data_store.data_path()} ({len(data_store.load_rows()):,} rows)")
    print(f"LLM provider : {os.getenv('AGENT_LLM_PROVIDER', 'anthropic')}\n")

    # ── Step 1: agent proposes an action ─────────────────────────────────────
    print("⏳  Asking the agent to propose an action …")
    action = propose_action(user_request, {})
    print(f"\n✅  Agent chose tool   : {action.tool_name}")
    print(f"    Description        : {action.description}")
    print(f"    Reversibility      : {action.reversibility}")
    print(f"    Data scope         : {action.data_scope} record(s)")
    print(f"    Regulatory         : {action.regulatory_category}")
    print(f"    Model confidence   : {action.confidence:.2f}")
    print(f"    Filter to execute  : {action.parameters.get('filter')}")

    # ── Step 2: the agent's own risk judgement, and where it routes ──────────
    assessment = build_assessment(action.to_risk_factors())
    decision   = route_action(assessment)

    print(f"\n📊  Agent judged risk : {assessment.risk_band.upper()} ({assessment.composite_score:.2f})")
    print(f"    Routing           : {decision.upper()}")
    print(f"    Reason            : {describe_routing(assessment, decision)}")
    print()
    for dim, note in assessment.breakdown.items():
        print(f"    {dim:<20s} {note}")

    # ── Step 3: handle routing decision ──────────────────────────────────────
    print(f"\n{DIVIDER}")

    if decision == "autonomous":
        record, result = confirmation.execute_autonomously(
            action, assessment, session_id=session_id
        )
        print("🤖  LOW RISK — executed automatically (no human needed).")
        print(f"    Result          : {result.detail}")
        if result.scope_check:
            print(f"    Scope check     : {result.scope_check}")
        print(f"    Audit record id : {record['record_id']}")

    elif decision == "confirm":
        # Queue the action and ask the human
        confirmation_id = confirmation.create_confirmation_request(
            action, assessment, session_id=session_id
        )
        print("🟡  MEDIUM RISK — human approval required.\n")
        print(f"    Preview         : {action.description}")
        print(f"    Confirmation ID : {confirmation_id}\n")

        while True:
            choice = input("    ➤  Your decision [confirm / reject]: ").strip().lower()
            if choice in ("confirm", "reject"):
                break
            print("    Please type 'confirm' or 'reject'.")

        reviewer = input("    ➤  Your name (reviewer): ").strip() or "demo-user"
        record   = confirmation.resolve_confirmation(confirmation_id, choice, reviewer)

        status_icon = "✅" if choice == "confirm" else "❌"
        print(f"\n{status_icon}  Decision recorded: {record['status'].upper()} by {record['reviewer']}")

    else:  # full_review
        review_id = confirmation.create_review_request(
            action, assessment, session_id=session_id
        )
        print("🔴  HIGH RISK — blocked, pending full human review.\n")
        print(f"    Preview   : {action.description}")
        print(f"    Review ID : {review_id}\n")

        while True:
            choice = input("    ➤  Your decision [approve / reject]: ").strip().lower()
            if choice in ("approve", "reject"):
                break
            print("    Please type 'approve' or 'reject'.")

        reviewer = input("    ➤  Your name (reviewer): ").strip() or "demo-user"
        record   = confirmation.resolve_review(review_id, choice, reviewer)

        status_icon = "✅" if choice == "approve" else "❌"
        print(f"\n{status_icon}  Decision recorded: {record['status'].upper()} by {record['reviewer']}")

    # ── Step 4: print the full audit trail ───────────────────────────────────
    print(f"\n{DIVIDER}")
    print("📋  Audit trail for this session:\n")
    trail = audit_store.get_audit_trail(session_id)
    for entry in trail:
        print(f"  record_id   : {entry['record_id']}")
        print(f"  action_type : {entry.get('action_type')}")
        print(f"  routing     : {entry.get('routing_decision')}")
        print(f"  status      : {entry.get('status')}")
        print(f"  reviewer    : {entry.get('reviewer')}")
        print(f"  score       : {entry.get('composite_score')}")
        print(f"  execution   : {entry.get('execution_status')} — {entry.get('execution_detail')}")
        print()

    print(DIVIDER)
    mock.stop()


if __name__ == "__main__":
    main()
