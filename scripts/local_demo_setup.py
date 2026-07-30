"""Create the audit-log table against a local DynamoDB endpoint (moto server
or dynamodb-local). Not part of the deployed system -- infra/template.yaml
creates the real table via CloudFormation in Phase 5/6.

Run: python scripts/local_demo_setup.py
Requires DYNAMODB_ENDPOINT_URL and DYNAMODB_TABLE_PREFIX in the environment.
"""

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> None:
    endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")
    if not endpoint_url:
        print("DYNAMODB_ENDPOINT_URL not set", file=sys.stderr)
        sys.exit(1)

    from autonomy_engine.audit_store import table_name

    name = table_name()
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=endpoint_url,
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    try:
        dynamodb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "session_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        print(f"created table {name} at {endpoint_url}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceInUseException":
            print(f"table {name} already exists at {endpoint_url}")
        else:
            raise


if __name__ == "__main__":
    main()
