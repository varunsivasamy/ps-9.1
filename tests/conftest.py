"""Shared fixtures.

The audit-store fixtures below give every test a fresh, isolated DynamoDB table
inside a moto mock, so the suite needs no AWS credentials and no network.
"""

import os

import boto3
import pytest
from moto import mock_aws

from autonomy_engine import audit_store

TEST_TABLE_PREFIX = "test-autonomy"


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
    # tests at a real local DynamoDB instead of moto.
    monkeypatch.delenv("DYNAMODB_ENDPOINT_URL", raising=False)
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
