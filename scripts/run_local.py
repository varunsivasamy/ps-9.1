"""
Local development server
========================
Starts an in-process moto DynamoDB mock, creates the audit-log table,
then runs the FastAPI app with uvicorn — all in one command.

Run from the project root:
    python scripts/run_local.py

Flags:
    --port PORT     uvicorn port (default 8000)
    --reload        enable uvicorn --reload (restarts on code changes)
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

# ── add src/ to path before any autonomy_engine import ────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# ── load .env ──────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── force local credentials so boto3 doesn't reject them ──────────────────
os.environ.setdefault("AWS_ACCESS_KEY_ID",     "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
os.environ.setdefault("AWS_REGION",            "us-east-1")

# ── start moto server BEFORE any boto3/dynamodb import ────────────────────
# moto.server starts a Flask HTTP server on the DYNAMODB_ENDPOINT_URL port,
# so boto3 talks to it exactly as it would talk to real AWS.
from moto.server import create_backend_app  # type: ignore

ENDPOINT = os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:5000")
_host, _port_str = ENDPOINT.rsplit(":", 1)
_host = _host.replace("http://", "").replace("https://", "")
MOTO_PORT = int(_port_str)


def _start_moto() -> None:
    """Run the moto DynamoDB server in a daemon thread."""
    flask_app = create_backend_app("dynamodb")
    flask_app.run(host=_host, port=MOTO_PORT, use_reloader=False, threaded=True)


moto_thread = threading.Thread(target=_start_moto, daemon=True, name="moto-dynamodb")
moto_thread.start()

# Give the Flask server a moment to be ready
import time
time.sleep(1.0)

# ── create the audit-log table ─────────────────────────────────────────────
import boto3
from autonomy_engine.audit_store import table_name, reset_cache

reset_cache()  # ensure the resource is built against the local endpoint

dynamodb = boto3.resource(
    "dynamodb",
    region_name=os.environ["AWS_REGION"],
    endpoint_url=ENDPOINT,
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)

name = table_name()
try:
    dynamodb.create_table(
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
    print(f"[run_local] Created DynamoDB table: {name}")
except Exception as exc:
    if "ResourceInUseException" in str(exc):
        print(f"[run_local] Table already exists: {name}")
    else:
        raise

reset_cache()  # rebuild resource now table exists
print(f"[run_local] Moto DynamoDB running at {ENDPOINT}")

# ── start uvicorn ──────────────────────────────────────────────────────────
import uvicorn

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port",   type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"[run_local] Starting API on http://127.0.0.1:{args.port}")
    uvicorn.run(
        "autonomy_engine.main:app",
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
        app_dir=str(Path(__file__).resolve().parent.parent / "src"),
        log_level="info",
    )


if __name__ == "__main__":
    main()
