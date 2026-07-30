"""Tests for audit persistence and the confirmation/review queues.

All of these run against moto, so no AWS credentials and no network are needed.
"""

import json

import pytest

from autonomy_engine import audit_store, confirmation
from autonomy_engine.agent_actions import AgentAction
from autonomy_engine.risk_scorer import RiskFactors, build_assessment

SESSION = "sess-demo-001"


# --------------------------------------------------------------------------
# Fixtures for the three demo scenarios
#
# These run against the real customer CSV (a throwaway copy, via the autouse
# isolated_customer_data fixture), so the filters below name columns that
# actually exist -- approving one of these now really executes it.
# --------------------------------------------------------------------------


def _action(kind):
    """Build one of the demo actions.

    These run against the real transaction schema (a throwaway copy, via the
    autouse isolated_transaction_data fixture), so the filters below name
    columns and values that actually exist -- approving one really executes it.
    """
    if kind == "read":
        return AgentAction(
            action_type="read",
            description="Read transactions where category = 'Books'",
            tool_name="query_transactions",
            parameters={
                "filter_description": "Books transactions",
                "filter": [
                    {"field": "category", "operator": "equals", "value": "Books"}
                ],
            },
            reversibility="reversible",
            data_scope=12,
            regulatory_category="none",
            confidence=0.95,
            risk_band="low",
            severity=0.1,
            rationale="a read of non-sensitive sales data",
        )
    if kind == "update":
        return AgentAction(
            action_type="single_record_write",
            description="Set payment_method to 'Cash' on invoice I757064",
            tool_name="update_transaction",
            parameters={
                "invoice_no": "I757064",
                "field": "payment_method",
                "new_value": "Cash",
            },
            reversibility="partially_reversible",
            data_scope=1,
            regulatory_category="internal_sensitive",
            confidence=0.9,
            risk_band="medium",
            severity=0.45,
            rationale="a reversible write to one transaction",
        )
    if kind == "delete_one":
        return AgentAction(
            action_type="single_record_delete",
            description="PERMANENTLY DELETE invoice I757064 (1 row)",
            tool_name="delete_transaction",
            parameters={"invoice_no": "I757064"},
            reversibility="irreversible",
            data_scope=1,
            regulatory_category="internal_sensitive",
            confidence=0.92,
            risk_band="medium",
            severity=0.5,
            rationale="irreversible but scoped to a single named invoice",
        )
    return AgentAction(
        action_type="bulk_delete",
        description="PERMANENTLY DELETE all transactions where category = 'Clothing'",
        tool_name="bulk_delete_transactions",
        parameters={
            "filter_description": "all Clothing transactions",
            "filter": [
                {"field": "category", "operator": "equals", "value": "Clothing"}
            ],
        },
        reversibility="irreversible",
        data_scope=112,
        regulatory_category="regulated",
        confidence=0.6,
        risk_band="high",
        severity=0.9,
        rationale="irreversible bulk deletion across thousands of rows",
    )


def _score(kind):
    return build_assessment(_action(kind).to_risk_factors())


# --------------------------------------------------------------------------
# Record ids
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("session_id", "timestamp"),
    [
        ("sess-1", "2026-07-30T10:00:00+00:00"),
        ("session|with|pipes", "2026-07-30T10:00:00.123456+00:00"),
        ("sess-with-ünïcode", "2026-01-01T00:00:00+00:00"),
    ],
)
def test_record_ids_round_trip(session_id, timestamp):
    encoded = audit_store.encode_record_id(session_id, timestamp)
    assert audit_store.decode_record_id(encoded) == (session_id, timestamp)


def test_record_ids_are_url_safe():
    """Ids travel in a URL path, so they must not need escaping."""
    encoded = audit_store.encode_record_id("sess/with+chars=", "2026-07-30T10:00:00+00:00")
    assert "/" not in encoded
    assert "+" not in encoded
    assert "=" not in encoded


@pytest.mark.parametrize("bad_id", ["not-base64!!", "", "AAAA", "Zm9v"])
def test_malformed_record_ids_raise(bad_id):
    with pytest.raises(audit_store.InvalidRecordIdError):
        audit_store.decode_record_id(bad_id)


# --------------------------------------------------------------------------
# Writes and reads
# --------------------------------------------------------------------------


def test_write_and_read_back_a_record(audit_table):
    score = _score("read")
    record = audit_store.write_audit_record(
        session_id=SESSION,
        action_type="read",
        composite_score=score.composite_score,
        risk_breakdown=score.breakdown,
        routing_decision="autonomous",
        status="auto_executed",
        description="Read customer C-10482",
    )
    fetched = audit_store.get_record(record["record_id"])
    assert fetched["session_id"] == SESSION
    assert fetched["action_type"] == "read"
    assert fetched["routing_decision"] == "autonomous"
    assert fetched["status"] == "auto_executed"
    assert fetched["reviewer"] is None
    assert fetched["composite_score"] == pytest.approx(score.composite_score)


def test_risk_breakdown_survives_the_round_trip(audit_table):
    """The breakdown is the explainability payload -- it must come back intact."""
    score = _score("bulk_delete")
    record = audit_store.write_audit_record(
        session_id=SESSION,
        action_type="bulk_delete",
        composite_score=score.composite_score,
        risk_breakdown=score.breakdown,
        routing_decision="full_review",
        status="pending",
    )
    fetched = audit_store.get_record(record["record_id"])
    assert fetched["risk_breakdown"] == score.breakdown
    assert "irreversible" in fetched["risk_breakdown"]["reversibility"]
    assert "112" in fetched["risk_breakdown"]["data_scope"]


def test_breakdown_is_stored_as_a_json_string(audit_table):
    """The schema in the build plan calls for a JSON string, not a nested map."""
    score = _score("read")
    record = audit_store.write_audit_record(
        session_id=SESSION,
        action_type="read",
        composite_score=score.composite_score,
        risk_breakdown=score.breakdown,
        routing_decision="autonomous",
        status="auto_executed",
    )
    raw = audit_table.get_item(
        Key={"session_id": SESSION, "timestamp": record["timestamp"]}
    )["Item"]
    assert isinstance(raw["risk_breakdown"], str)
    assert json.loads(raw["risk_breakdown"]) == score.breakdown


def test_float_scores_survive_dynamodb(audit_table):
    """DynamoDB has no float type; the Decimal conversion must be invisible."""
    record = audit_store.write_audit_record(
        session_id=SESSION,
        action_type="read",
        composite_score=0.0925,
        risk_breakdown={},
        routing_decision="autonomous",
        status="auto_executed",
    )
    fetched = audit_store.get_record(record["record_id"])
    assert isinstance(fetched["composite_score"], float)
    assert fetched["composite_score"] == pytest.approx(0.0925)


def test_audit_trail_is_chronological(audit_table):
    for i, kind in enumerate(["read", "update", "bulk_delete"]):
        score = _score(kind)
        audit_store.write_audit_record(
            session_id=SESSION,
            action_type=kind,
            composite_score=score.composite_score,
            risk_breakdown=score.breakdown,
            routing_decision="autonomous",
            status="auto_executed",
            timestamp=f"2026-07-30T10:0{i}:00+00:00",
        )
    trail = audit_store.get_audit_trail(SESSION)
    assert [r["action_type"] for r in trail] == ["read", "update", "bulk_delete"]


def test_audit_trail_is_scoped_to_one_session(audit_table):
    for session in ("sess-a", "sess-b"):
        audit_store.write_audit_record(
            session_id=session,
            action_type="read",
            composite_score=0.1,
            risk_breakdown={},
            routing_decision="autonomous",
            status="auto_executed",
        )
    assert len(audit_store.get_audit_trail("sess-a")) == 1
    assert audit_store.get_audit_trail("sess-a")[0]["session_id"] == "sess-a"


def test_audit_trail_of_unknown_session_is_empty(audit_table):
    assert audit_store.get_audit_trail("no-such-session") == []


def test_get_record_of_unknown_id_raises_not_found(audit_table):
    missing = audit_store.encode_record_id("nope", "2026-07-30T10:00:00+00:00")
    with pytest.raises(audit_store.RecordNotFoundError):
        audit_store.get_record(missing)


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "half_done"), ("routing_decision", "maybe")],
)
def test_unknown_vocabulary_is_rejected_on_write(audit_table, field, value):
    """A typo here would orphan the record from the queue meant to pick it up."""
    kwargs = {
        "session_id": SESSION,
        "action_type": "read",
        "composite_score": 0.1,
        "risk_breakdown": {},
        "routing_decision": "autonomous",
        "status": "auto_executed",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="unknown"):
        audit_store.write_audit_record(**kwargs)


# --------------------------------------------------------------------------
# Pending queues
# --------------------------------------------------------------------------


def test_pending_queues_are_separate(audit_table):
    confirmation.create_confirmation_request(
        _action("update"), _score("update"), session_id=SESSION
    )
    confirmation.create_review_request(
        _action("bulk_delete"), _score("bulk_delete"), session_id=SESSION
    )
    confirmation.execute_autonomously(
        _action("read"), _score("read"), session_id=SESSION
    )

    confirmations = audit_store.list_pending_confirmations()
    reviews = audit_store.list_pending_reviews()

    assert len(confirmations) == 1
    assert confirmations[0]["action_type"] == "single_record_write"
    assert len(reviews) == 1
    assert reviews[0]["action_type"] == "bulk_delete"


def test_resolved_items_leave_the_pending_queue(audit_table):
    cid = confirmation.create_confirmation_request(
        _action("update"), _score("update"), session_id=SESSION
    )
    assert len(audit_store.list_pending_confirmations()) == 1
    confirmation.resolve_confirmation(cid, "confirm", "alice@example.com")
    assert audit_store.list_pending_confirmations() == []


def test_autonomous_executions_never_enter_a_queue(audit_table):
    confirmation.execute_autonomously(
        _action("read"), _score("read"), session_id=SESSION
    )
    assert audit_store.list_pending_confirmations() == []
    assert audit_store.list_pending_reviews() == []


# --------------------------------------------------------------------------
# Creating queue entries
# --------------------------------------------------------------------------


def test_confirmation_request_captures_the_action_preview(audit_table):
    """A human approving this needs to see what they are approving."""
    action = _action("update")
    cid = confirmation.create_confirmation_request(action, _score("update"), session_id=SESSION)
    record = audit_store.get_record(cid)
    assert record["status"] == "pending"
    assert record["routing_decision"] == "confirm"
    assert record["description"] == action.description
    assert record["tool_name"] == "update_transaction"
    assert record["parameters"]["invoice_no"] == "I757064"


def test_review_request_is_pending_full_review(audit_table):
    rid = confirmation.create_review_request(
        _action("bulk_delete"), _score("bulk_delete"), session_id=SESSION
    )
    record = audit_store.get_record(rid)
    assert record["status"] == "pending"
    assert record["routing_decision"] == "full_review"
    assert "PERMANENTLY DELETE" in record["description"]


def test_autonomous_execution_records_no_reviewer(audit_table):
    """No human was involved, and that absence is the thing worth auditing."""
    record, _ = confirmation.execute_autonomously(
        _action("read"), _score("read"), session_id=SESSION
    )
    assert record["status"] == "auto_executed"
    assert record["reviewer"] is None


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("confirm", "confirmed"), ("reject", "rejected")],
)
def test_resolve_confirmation(audit_table, decision, expected_status):
    cid = confirmation.create_confirmation_request(
        _action("update"), _score("update"), session_id=SESSION
    )
    updated = confirmation.resolve_confirmation(cid, decision, "alice@example.com")
    assert updated["status"] == expected_status
    assert updated["reviewer"] == "alice@example.com"
    assert "resolved_at" in updated


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("approve", "reviewed"), ("reject", "rejected")],
)
def test_resolve_review(audit_table, decision, expected_status):
    rid = confirmation.create_review_request(
        _action("bulk_delete"), _score("bulk_delete"), session_id=SESSION
    )
    updated = confirmation.resolve_review(rid, decision, "bob@example.com")
    assert updated["status"] == expected_status
    assert updated["reviewer"] == "bob@example.com"


def test_resolving_preserves_the_risk_breakdown(audit_table):
    """Resolution must not clobber the evidence for why it was routed here."""
    score = _score("bulk_delete")
    rid = confirmation.create_review_request(_action("bulk_delete"), score, session_id=SESSION)
    updated = confirmation.resolve_review(rid, "approve", "bob@example.com")
    assert updated["risk_breakdown"] == score.breakdown
    assert updated["composite_score"] == pytest.approx(score.composite_score)


# --------------------------------------------------------------------------
# Resolution guards -- the safety-critical part
# --------------------------------------------------------------------------


def test_high_risk_review_cannot_be_resolved_through_the_confirmation_queue(audit_table):
    """The core safety property: full_review must not be waved through as a confirm."""
    rid = confirmation.create_review_request(
        _action("bulk_delete"), _score("bulk_delete"), session_id=SESSION
    )
    with pytest.raises(audit_store.AuditStoreError, match="correct queue"):
        confirmation.resolve_confirmation(rid, "confirm", "attacker@example.com")
    # And it is still sitting in the review queue, untouched.
    assert audit_store.get_record(rid)["status"] == "pending"
    assert len(audit_store.list_pending_reviews()) == 1


def test_confirmation_cannot_be_resolved_through_the_review_queue(audit_table):
    cid = confirmation.create_confirmation_request(
        _action("update"), _score("update"), session_id=SESSION
    )
    with pytest.raises(audit_store.AuditStoreError, match="correct queue"):
        confirmation.resolve_review(cid, "approve", "bob@example.com")
    assert audit_store.get_record(cid)["status"] == "pending"


def test_a_record_cannot_be_resolved_twice(audit_table):
    cid = confirmation.create_confirmation_request(
        _action("update"), _score("update"), session_id=SESSION
    )
    confirmation.resolve_confirmation(cid, "confirm", "alice@example.com")
    with pytest.raises(audit_store.AuditStoreError, match="already"):
        confirmation.resolve_confirmation(cid, "reject", "mallory@example.com")
    # The first decision stands.
    record = audit_store.get_record(cid)
    assert record["status"] == "confirmed"
    assert record["reviewer"] == "alice@example.com"


def test_autonomous_record_cannot_be_resolved(audit_table):
    """Already executed -- there is nothing to approve."""
    record, _ = confirmation.execute_autonomously(
        _action("read"), _score("read"), session_id=SESSION
    )
    with pytest.raises(audit_store.AuditStoreError):
        confirmation.resolve_confirmation(record["record_id"], "confirm", "alice@example.com")


def test_resolving_an_unknown_id_raises_not_found(audit_table):
    missing = audit_store.encode_record_id("nope", "2026-07-30T10:00:00+00:00")
    with pytest.raises(audit_store.RecordNotFoundError):
        confirmation.resolve_confirmation(missing, "confirm", "alice@example.com")


def test_resolving_a_malformed_id_raises_invalid_id(audit_table):
    with pytest.raises(audit_store.InvalidRecordIdError):
        confirmation.resolve_confirmation("not-an-id!!", "confirm", "alice@example.com")


@pytest.mark.parametrize("decision", ["approve", "yes", "CONFIRM", ""])
def test_confirmation_queue_rejects_review_vocabulary(audit_table, decision):
    """'approve' belongs to the review queue; the queues do not share a vocabulary."""
    cid = confirmation.create_confirmation_request(
        _action("update"), _score("update"), session_id=SESSION
    )
    with pytest.raises(confirmation.InvalidDecisionError):
        confirmation.resolve_confirmation(cid, decision, "alice@example.com")


@pytest.mark.parametrize("decision", ["confirm", "yes", "APPROVE", ""])
def test_review_queue_rejects_confirmation_vocabulary(audit_table, decision):
    """A high-risk action needs an explicit approval, not a confirmation."""
    rid = confirmation.create_review_request(
        _action("bulk_delete"), _score("bulk_delete"), session_id=SESSION
    )
    with pytest.raises(confirmation.InvalidDecisionError):
        confirmation.resolve_review(rid, decision, "bob@example.com")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_table_name_follows_the_prefix(monkeypatch):
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "my-stack")
    assert audit_store.table_name() == "my-stack-audit-log"


def test_local_endpoint_url_is_honoured(aws_env, monkeypatch):
    """Local dev must be able to point at dynamodb-local instead of real AWS."""
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000")
    audit_store.reset_cache()
    try:
        assert audit_store._table().meta.client.meta.endpoint_url == "http://localhost:8000"
    finally:
        audit_store.reset_cache()


def test_health_probe_reports_unreachable_without_a_table(aws_env):
    """A missing table must return False, not raise -- /health has to stay up."""
    audit_store.reset_cache()
    monkeypatch_free_prefix = "definitely-not-a-real-table"
    os_environ_backup = audit_store.os.environ.get("DYNAMODB_TABLE_PREFIX")
    audit_store.os.environ["DYNAMODB_TABLE_PREFIX"] = monkeypatch_free_prefix
    try:
        assert audit_store.is_reachable() is False
    finally:
        if os_environ_backup is not None:
            audit_store.os.environ["DYNAMODB_TABLE_PREFIX"] = os_environ_backup
        audit_store.reset_cache()


def test_health_probe_reports_reachable_with_a_table(audit_table):
    assert audit_store.is_reachable() is True
