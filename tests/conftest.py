"""Shared fixtures.

The audit-store fixtures below give every test a fresh, isolated DynamoDB table
inside a moto mock, so the suite needs no AWS credentials and no network.

:func:`isolated_transaction_data` is autouse and matters more than it looks: now
that approving an action really executes it, a test that confirms a bulk delete
would otherwise delete rows from the real
``data/customer_shopping_data.csv``. Every test gets its own throwaway copy
instead.

That copy is a 300-row sample, not the full 99,457-row file. Copying 7.2 MB per
test across ~180 tests would spend well over a gigabyte of I/O to prove things
that hold just as well on 300 rows. The sample keeps the same schema, all eight
categories and all ten malls, so filters still have something to bite on.
Anything genuinely about scale belongs in a test that says so explicitly.
"""

import os
import shutil
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from autonomy_engine import audit_store

TEST_TABLE_PREFIX = "test-autonomy"

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Sampled from data/customer_shopping_data.csv; see the module docstring.
SEED_CSV = Path(__file__).resolve().parent / "fixtures" / "shopping_sample.csv"

#: Rows in that sample. Asserted in tests rather than hard-coded in each one.
SEED_ROW_COUNT = 300


@pytest.fixture(autouse=True)
def isolated_transaction_data(tmp_path, monkeypatch):
    """Give every test a private copy of the transaction CSV and snapshot dir.

    Autouse and unconditional: the protection is only worth anything if it is
    impossible to forget.
    """
    working_csv = tmp_path / "customer_shopping_data.csv"
    shutil.copy(SEED_CSV, working_csv)
    monkeypatch.setenv("CUSTOMER_DATA_PATH", str(working_csv))
    monkeypatch.setenv("CUSTOMER_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    yield working_csv


@pytest.fixture
def aws_env(monkeypatch):
    """Point boto3 at moto with dummy credentials.

    Real credentials must never leak into these tests -- if a credential file
    exists on the machine, boto3 would happily use it and the test would hit real
    AWS. Setting explicit dummy values prevents that.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", TEST_TABLE_PREFIX)
    # A stray DYNAMODB_ENDPOINT_URL from a developer's .env would send these
    # tests at a real local DynamoDB instead of moto. Setting it empty rather
    # than deleting it is deliberate: audit_store reads it as
    # `os.getenv(...) or None`, so "" is as good as absent -- and unlike a
    # deletion, an empty value survives a later load_dotenv(), which does not
    # overwrite variables that are already set. A module imported inside a test
    # would otherwise silently reinstate the developer's endpoint.
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", "")
    yield


@pytest.fixture
def audit_table(aws_env):
    """A live, empty audit table backed by moto.

    The cache is reset on both sides of the mock: the table resource has to be
    built *inside* the mock context, and must not leak out to the next test.
    """
    audit_store.reset_cache()
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName=f"{TEST_TABLE_PREFIX}-audit-log",
            KeySchema=[
                {"AttributeName": "session_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table
    audit_store.reset_cache()
