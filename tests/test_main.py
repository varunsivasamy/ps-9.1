"""Tests for the FastAPI app.

propose_action() is mocked throughout, so these never touch the Groq API.
DynamoDB is provided by the moto-backed `audit_table` fixture from conftest.py.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from autonomy_engine.agent_actions import AgentAction, AgentActionError, ClarificationRequest
from autonomy_engine.main import app

SESSION = "sess-web-001"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def client(audit_table):
    """A TestClient bound to a fresh, moto-backed table for every test.

    raise_server_exceptions=False: our request-logging middleware is a
    BaseHTTPMiddleware, and Starlette re-raises the original exception through
    it for debugging even after our catch-all handler has already built the
    correct response. That re-raise only happens in the test client -- a real
    ASGI server (uvicorn, Mangum) just returns the response -- so it would
    make test_unexpected_internal_error_returns_generic_500 fail on a
    difference that doesn't exist in production.
    """
    return TestClient(app, raise_server_exceptions=False)


def _action(kind: str) -> AgentAction:
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


def _propose(client: TestClient, kind: str, session_id: str = SESSION) -> dict:
    """Propose an action with both LLM calls stubbed.

    reassess_action is patched as well as propose_action, and deliberately so:
    the propose path calls it whenever the estimate misses, so leaving it live
    would let a fixture drift quietly turn a unit test into a network call.
    The pass-through keeps the original assessment, which is what a re-judgement
    failure does anyway.
    """
    with patch("autonomy_engine.main.propose_action", return_value=_action(kind)), patch(
        "autonomy_engine.main.reassess_action", side_effect=lambda action, rows: action
    ):
        response = client.post(
            "/actions/propose",
            json={"user_request": "irrelevant, mocked", "session_id": session_id},
        )
    return response


# --------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------


def test_health_reports_reachable(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dynamodb": "reachable"}


def test_health_reports_unreachable_without_a_table(aws_env):
    """No audit_table fixture here -- moto isn't active, so the table is missing."""
    from autonomy_engine import audit_store

    audit_store.reset_cache()
    try:
        response = TestClient(app).get("/health")
        assert response.status_code == 200
        assert response.json()["dynamodb"] == "unreachable"
    finally:
        audit_store.reset_cache()


# --------------------------------------------------------------------------
# POST /actions/propose -- the three core scenarios
# --------------------------------------------------------------------------


def test_propose_read_routes_autonomous_and_executes(client):
    response = _propose(client, "read")
    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "autonomous"
    assert body["result"]["status"] == "success"
    assert body["risk_score"]["risk_band"] == "low"
    assert "reversibility" in body["risk_score"]["breakdown"]
    assert "audit_record_id" in body


def test_propose_update_routes_confirm_with_preview(client):
    response = _propose(client, "update")
    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "confirm"
    assert "confirmation_id" in body
    assert "payment_method" in body["preview"]
    assert body["risk_score"]["risk_band"] == "medium"


def test_propose_bulk_delete_routes_full_review(client):
    response = _propose(client, "bulk_delete")
    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "full_review"
    assert "review_id" in body
    assert body["risk_score"]["risk_band"] == "high"
    assert "PERMANENTLY DELETE" in body["preview"]


# --------------------------------------------------------------------------
# POST /actions/propose -- the agent asking instead of acting
#
# The fourth outcome, and the only one that reaches none of the routing
# machinery. Nothing is preflighted, banded, queued, executed or written to the
# audit log, because there is no action yet for any of that to apply to.
# --------------------------------------------------------------------------


def _clarify(client: TestClient, session_id: str = SESSION, **body):
    clarification = ClarificationRequest(
        question="Which rows count as 'bad'?",
        why="'bad' is not a column and not a value in any column.",
        options=["quantity of 0", "a specific date range"],
    )
    with patch("autonomy_engine.main.propose_action", return_value=clarification):
        return client.post(
            "/actions/propose",
            json={"user_request": "clean up the bad records", "session_id": session_id, **body},
        )


def test_propose_can_come_back_as_a_question(client):
    response = _clarify(client)
    assert response.status_code == 200
    body = response.json()
    assert body["routing_decision"] == "needs_clarification"
    assert body["question"] == "Which rows count as 'bad'?"
    assert "not a column" in body["why"]
    assert body["options"] == ["quantity of 0", "a specific date range"]


def test_a_question_carries_no_risk_score_or_queue_entry(client):
    """Nothing was proposed, so there is nothing to band and nothing to approve.
    Emitting a risk score here would be inventing a number to describe nothing."""
    body = _clarify(client).json()
    assert "risk_score" not in body
    assert "confirmation_id" not in body
    assert "review_id" not in body
    assert "audit_record_id" not in body


def test_a_question_writes_nothing_to_the_audit_log(client):
    """The audit log is a record of actions. A question is not one, and padding
    the trail with non-actions makes the actions harder to find."""
    _clarify(client, session_id="sess-clarify-audit")
    trail = client.get("/audit/sess-clarify-audit").json()
    assert trail["actions"] == []


def test_a_question_executes_nothing(client):
    """The load-bearing assertion: this path must not reach the executor at all."""
    with patch("autonomy_engine.executor.execute") as execute:
        _clarify(client)
    execute.assert_not_called()


def test_the_answer_is_passed_back_to_the_agent(client):
    """The round trip. Answering re-proposes with the original request *and* the
    answer, so the agent can commit properly instead of guessing again."""
    with patch(
        "autonomy_engine.main.propose_action", return_value=_action("read")
    ) as propose, patch(
        "autonomy_engine.main.reassess_action", side_effect=lambda action, rows: action
    ):
        client.post(
            "/actions/propose",
            json={
                "user_request": "clean up the bad records",
                "session_id": SESSION,
                "clarification": "rows with quantity 0",
            },
        )

    user_request, context = propose.call_args.args
    assert user_request == "clean up the bad records"
    assert "rows with quantity 0" in str(context)


def test_propose_requires_session_id(client):
    response = client.post("/actions/propose", json={"user_request": "do something"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_propose_requires_user_request(client):
    response = client.post("/actions/propose", json={"session_id": SESSION})
    assert response.status_code == 400


def test_propose_surfaces_agent_failure_as_502(client):
    with patch(
        "autonomy_engine.main.propose_action",
        side_effect=AgentActionError("model declined"),
    ):
        response = client.post(
            "/actions/propose",
            json={"user_request": "do something disallowed", "session_id": SESSION},
        )
    assert response.status_code == 502
    assert "model declined" in response.json()["error"]["message"]


# --------------------------------------------------------------------------
# POST /confirmations/{id}/resolve
# --------------------------------------------------------------------------


def test_resolve_confirmation_confirm(client):
    """Confirming does not merely stamp the record approved -- it runs the action."""
    from autonomy_engine import data_store

    confirmation_id = _propose(client, "update").json()["confirmation_id"]
    before = data_store.select(
        [data_store.Criterion(field="invoice_no", operator="equals", value="I757064")]
    )[0]

    response = client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "confirm", "reviewer": "alice@example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["reviewer"] == "alice@example.com"
    assert body["execution_status"] == "success"

    after = data_store.select(
        [data_store.Criterion(field="invoice_no", operator="equals", value="I757064")]
    )[0]
    assert after["payment_method"] == "Cash"


def test_confirming_reports_a_snapshot_to_roll_back_from(client):
    confirmation_id = _propose(client, "update").json()["confirmation_id"]
    body = client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "confirm", "reviewer": "alice@example.com"},
    ).json()
    assert body["snapshot_path"]


def test_resolve_confirmation_reject(client):
    confirmation_id = _propose(client, "update").json()["confirmation_id"]
    response = client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "reject", "reviewer": "alice@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_resolve_confirmation_rejects_bad_decision_vocabulary(client):
    confirmation_id = _propose(client, "update").json()["confirmation_id"]
    response = client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "approve", "reviewer": "alice@example.com"},
    )
    # "approve" is not in the Literal["confirm", "reject"] -> pydantic 400.
    assert response.status_code == 400


def test_resolve_unknown_confirmation_is_404(client):
    response = client.post(
        "/confirmations/bm9wZXxub3Blfm5vcGU/resolve",
        json={"decision": "confirm", "reviewer": "alice@example.com"},
    )
    assert response.status_code == 404


def test_resolve_malformed_confirmation_id_is_400(client):
    response = client.post(
        "/confirmations/not-a-valid-id!!/resolve",
        json={"decision": "confirm", "reviewer": "alice@example.com"},
    )
    assert response.status_code == 400


def test_resolving_a_review_through_the_confirmation_endpoint_is_409(client):
    review_id = _propose(client, "bulk_delete").json()["review_id"]
    response = client.post(
        f"/confirmations/{review_id}/resolve",
        json={"decision": "confirm", "reviewer": "attacker@example.com"},
    )
    assert response.status_code == 409


def test_double_resolving_a_confirmation_is_409(client):
    confirmation_id = _propose(client, "update").json()["confirmation_id"]
    first = client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "confirm", "reviewer": "alice@example.com"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "reject", "reviewer": "mallory@example.com"},
    )
    assert second.status_code == 409


# --------------------------------------------------------------------------
# POST /reviews/{id}/resolve
# --------------------------------------------------------------------------


def test_resolve_review_approve(client):
    """Approving a high-risk deletion actually deletes."""
    from autonomy_engine import data_store

    review_id = _propose(client, "bulk_delete").json()["review_id"]
    before = len(data_store.load_rows())

    response = client.post(
        f"/reviews/{review_id}/resolve",
        json={"decision": "approve", "reviewer": "bob@example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reviewed"
    assert body["reviewer"] == "bob@example.com"
    assert body["execution_status"] == "success"
    assert len(data_store.load_rows()) < before


def test_high_risk_action_is_not_executed_before_approval(client):
    """The whole promise of the high-risk path: proposing changes nothing."""
    from autonomy_engine import data_store

    before = len(data_store.load_rows())
    response = _propose(client, "bulk_delete")

    assert response.json()["routing_decision"] == "full_review"
    assert len(data_store.load_rows()) == before


def test_rejecting_a_review_executes_nothing(client):
    from autonomy_engine import data_store

    review_id = _propose(client, "bulk_delete").json()["review_id"]
    before = len(data_store.load_rows())

    body = client.post(
        f"/reviews/{review_id}/resolve",
        json={"decision": "reject", "reviewer": "bob@example.com"},
    ).json()

    assert body["status"] == "rejected"
    assert body["execution_status"] == "skipped"
    assert len(data_store.load_rows()) == before


def test_resolve_review_reject(client):
    review_id = _propose(client, "bulk_delete").json()["review_id"]
    response = client.post(
        f"/reviews/{review_id}/resolve",
        json={"decision": "reject", "reviewer": "bob@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_resolve_review_rejects_confirm_vocabulary(client):
    """A high-risk action needs an explicit approval, not a confirmation."""
    review_id = _propose(client, "bulk_delete").json()["review_id"]
    response = client.post(
        f"/reviews/{review_id}/resolve",
        json={"decision": "confirm", "reviewer": "bob@example.com"},
    )
    assert response.status_code == 400


def test_unknown_review_id_is_404(client):
    response = client.post(
        "/reviews/bm9wZXxub3Blfm5vcGU/resolve",
        json={"decision": "approve", "reviewer": "bob@example.com"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# GET /audit/{session_id}
# --------------------------------------------------------------------------


def test_audit_trail_shows_all_three_scenarios_with_breakdowns(client):
    _propose(client, "read")
    _propose(client, "update")
    _propose(client, "bulk_delete")

    response = client.get(f"/audit/{SESSION}")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == SESSION
    assert len(body["actions"]) == 3

    by_type = {a["action_type"]: a for a in body["actions"]}
    assert by_type["read"]["routing_decision"] == "autonomous"
    assert by_type["single_record_write"]["routing_decision"] == "confirm"
    assert by_type["bulk_delete"]["routing_decision"] == "full_review"
    # The whole point of the audit trail: the breakdown must be present and readable.
    assert "irreversible" in by_type["bulk_delete"]["risk_breakdown"]["reversibility"]


def test_audit_trail_reflects_resolution(client):
    confirmation_id = _propose(client, "update").json()["confirmation_id"]
    client.post(
        f"/confirmations/{confirmation_id}/resolve",
        json={"decision": "confirm", "reviewer": "alice@example.com"},
    )
    trail = client.get(f"/audit/{SESSION}").json()["actions"]
    assert trail[0]["status"] == "confirmed"
    assert trail[0]["reviewer"] == "alice@example.com"


def test_audit_trail_for_unknown_session_is_empty_not_404(client):
    response = client.get("/audit/no-such-session")
    assert response.status_code == 200
    assert response.json()["actions"] == []


def test_audit_trail_is_scoped_per_session(client):
    _propose(client, "read", session_id="sess-a")
    _propose(client, "bulk_delete", session_id="sess-b")
    assert len(client.get("/audit/sess-a").json()["actions"]) == 1
    assert len(client.get("/audit/sess-b").json()["actions"]) == 1


# --------------------------------------------------------------------------
# Error handling never leaks a raw traceback
# --------------------------------------------------------------------------


def test_unexpected_internal_error_returns_generic_500(client):
    with patch(
        "autonomy_engine.main.propose_action",
        side_effect=RuntimeError("boom: something internal broke"),
    ):
        response = client.post(
            "/actions/propose",
            json={"user_request": "trigger a crash", "session_id": SESSION},
        )
    assert response.status_code == 500
    body = response.json()
    assert body == {"error": {"message": "internal server error"}}
    assert "boom" not in response.text
