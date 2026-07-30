"""Tests for the agent action layer.

Split in two: parsing/error-handling tests that run on mocks with no key needed,
and @pytest.mark.integration tests that hit the real Anthropic API and are
skipped unless ANTHROPIC_API_KEY is set. `pytest` runs the former; `pytest -m
integration` runs the latter.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from autonomy_engine.agent_actions import (
    TOOL_SCHEMAS,
    AgentAction,
    AgentActionError,
    propose_action,
)
from autonomy_engine.risk_scorer import DEFAULT_THRESHOLDS, route_action, score_action

requires_api_key = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; skipping live Anthropic calls",
)


# --------------------------------------------------------------------------
# Helpers for building fake API responses
# --------------------------------------------------------------------------


def _tool_use_block(name, tool_input):
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id="toolu_test")


def _fake_response(content, stop_reason="tool_use"):
    return SimpleNamespace(content=content, stop_reason=stop_reason)


def _assessment(**overrides):
    base = {
        "reversibility": "reversible",
        "data_scope": 1,
        "regulatory_category": "none",
        "confidence": 0.95,
    }
    return {**base, **overrides}


# --------------------------------------------------------------------------
# Tool schemas
# --------------------------------------------------------------------------


def test_three_demo_tools_are_defined():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "query_customer_records",
        "update_customer_record",
        "bulk_delete_records",
    }


@pytest.mark.parametrize("tool", TOOL_SCHEMAS, ids=lambda t: t["name"])
def test_every_tool_requires_a_self_assessment(tool):
    """No tool may be callable without the agent grading its own risk."""
    schema = tool["input_schema"]
    assert "self_assessment" in schema["properties"]
    assert "self_assessment" in schema["required"]
    fields = schema["properties"]["self_assessment"]["properties"]
    assert set(fields) == {
        "reversibility",
        "data_scope",
        "regulatory_category",
        "confidence",
    }


@pytest.mark.parametrize("tool", TOOL_SCHEMAS, ids=lambda t: t["name"])
def test_tool_schemas_satisfy_strict_mode(tool):
    """strict=True requires additionalProperties:false and every property required."""
    schema = tool["input_schema"]
    assert tool["strict"] is True
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


# --------------------------------------------------------------------------
# Parsing (mocked -- no API key needed)
# --------------------------------------------------------------------------


@patch("autonomy_engine.agent_actions._client")
def test_parses_a_read_action(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [
            _tool_use_block(
                "query_customer_records",
                {
                    "filter_description": "customer 12345",
                    "self_assessment": _assessment(),
                },
            )
        ]
    )
    action = propose_action("Look up customer 12345", {})
    assert action.tool_name == "query_customer_records"
    assert action.action_type == "read"
    assert action.reversibility == "reversible"
    assert action.confidence == pytest.approx(0.95)
    # The assessment must not leak into the tool parameters.
    assert action.parameters == {"filter_description": "customer 12345"}
    assert "customer 12345" in action.description


@patch("autonomy_engine.agent_actions._client")
def test_parses_a_single_record_update(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [
            _tool_use_block(
                "update_customer_record",
                {
                    "customer_id": "C-77",
                    "field": "email_address",
                    "new_value": "new@example.com",
                    "self_assessment": _assessment(
                        reversibility="partially_reversible",
                        regulatory_category="internal_sensitive",
                        confidence=0.9,
                    ),
                },
            )
        ]
    )
    action = propose_action("Change customer C-77's email", {})
    assert action.action_type == "single_record_write"
    assert action.reversibility == "partially_reversible"
    assert action.regulatory_category == "internal_sensitive"
    assert "email_address" in action.description


@patch("autonomy_engine.agent_actions._client")
def test_parses_a_bulk_delete(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [
            _tool_use_block(
                "bulk_delete_records",
                {
                    "filter_description": "all EU customers inactive since 2019",
                    "self_assessment": _assessment(
                        reversibility="irreversible",
                        data_scope=500,
                        regulatory_category="regulated",
                        confidence=0.6,
                    ),
                },
            )
        ]
    )
    action = propose_action("Delete all inactive EU customers", {})
    assert action.action_type == "bulk_delete"
    assert action.data_scope == 500
    assert "PERMANENTLY DELETE" in action.description


@patch("autonomy_engine.agent_actions._client")
def test_tool_context_is_passed_as_prompt_context(mock_client):
    create = mock_client.return_value.messages.create
    create.return_value = _fake_response(
        [
            _tool_use_block(
                "query_customer_records",
                {"filter_description": "x", "self_assessment": _assessment()},
            )
        ]
    )
    propose_action("Look up a customer", {"tenant": "acme", "environment": "production"})
    prompt = create.call_args.kwargs["messages"][0]["content"]
    assert "tenant: acme" in prompt
    assert "environment: production" in prompt
    # `tools` is a reserved key and must not be echoed into the prompt.
    assert create.call_args.kwargs["tools"] == TOOL_SCHEMAS


@patch("autonomy_engine.agent_actions._client")
def test_tool_context_can_override_the_tool_set(mock_client):
    create = mock_client.return_value.messages.create
    create.return_value = _fake_response(
        [
            _tool_use_block(
                "query_customer_records",
                {"filter_description": "x", "self_assessment": _assessment()},
            )
        ]
    )
    custom = [TOOL_SCHEMAS[0]]
    propose_action("Look up a customer", {"tools": custom})
    assert create.call_args.kwargs["tools"] == custom


@patch("autonomy_engine.agent_actions._client")
def test_agent_must_choose_a_tool(mock_client):
    create = mock_client.return_value.messages.create
    create.return_value = _fake_response(
        [
            _tool_use_block(
                "query_customer_records",
                {"filter_description": "x", "self_assessment": _assessment()},
            )
        ]
    )
    propose_action("Look up a customer", {})
    assert create.call_args.kwargs["tool_choice"] == {"type": "any"}


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


@patch("autonomy_engine.agent_actions._client")
def test_no_tool_call_raises_rather_than_returning_an_unscored_action(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [SimpleNamespace(type="text", text="I need more information.")],
        stop_reason="end_turn",
    )
    with pytest.raises(AgentActionError, match="no tool call"):
        propose_action("Do something vague", {})


@patch("autonomy_engine.agent_actions._client")
def test_refusal_raises_a_clear_error(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [], stop_reason="refusal"
    )
    with pytest.raises(AgentActionError, match="declined"):
        propose_action("Do something disallowed", {})


@patch("autonomy_engine.agent_actions._client")
def test_missing_self_assessment_raises(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [_tool_use_block("query_customer_records", {"filter_description": "x"})]
    )
    with pytest.raises(AgentActionError, match="missing its self_assessment"):
        propose_action("Look up a customer", {})


@patch("autonomy_engine.agent_actions._client")
def test_out_of_range_confidence_raises(mock_client):
    mock_client.return_value.messages.create.return_value = _fake_response(
        [
            _tool_use_block(
                "query_customer_records",
                {
                    "filter_description": "x",
                    "self_assessment": _assessment(confidence=1.7),
                },
            )
        ]
    )
    with pytest.raises(AgentActionError, match="unusable self_assessment"):
        propose_action("Look up a customer", {})


@patch("autonomy_engine.agent_actions.time.sleep")
@patch("autonomy_engine.agent_actions._client")
def test_transient_failure_is_retried_once_then_succeeds(mock_client, mock_sleep):
    create = mock_client.return_value.messages.create
    create.side_effect = [
        anthropic.APIConnectionError(request=MagicMock()),
        _fake_response(
            [
                _tool_use_block(
                    "query_customer_records",
                    {"filter_description": "x", "self_assessment": _assessment()},
                )
            ]
        ),
    ]
    action = propose_action("Look up a customer", {})
    assert action.tool_name == "query_customer_records"
    assert create.call_count == 2
    mock_sleep.assert_called_once()


@patch("autonomy_engine.agent_actions.time.sleep")
@patch("autonomy_engine.agent_actions._client")
def test_two_transient_failures_raise_a_clear_error(mock_client, mock_sleep):
    create = mock_client.return_value.messages.create
    create.side_effect = anthropic.APIConnectionError(request=MagicMock())
    with pytest.raises(AgentActionError, match="unreachable after 2 attempts"):
        propose_action("Look up a customer", {})
    assert create.call_count == 2


@patch("autonomy_engine.agent_actions.time.sleep")
@patch("autonomy_engine.agent_actions._client")
def test_bad_request_is_not_retried(mock_client, mock_sleep):
    """A 400 will fail identically on retry -- surface it immediately."""
    create = mock_client.return_value.messages.create
    create.side_effect = anthropic.BadRequestError(
        message="bad tool schema", response=MagicMock(status_code=400), body=None
    )
    with pytest.raises(AgentActionError, match="rejected the request"):
        propose_action("Look up a customer", {})
    assert create.call_count == 1
    mock_sleep.assert_not_called()


def test_missing_api_key_raises_an_actionable_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AgentActionError, match="ANTHROPIC_API_KEY is not set"):
        propose_action("Look up a customer", {})


# --------------------------------------------------------------------------
# Seam into the risk scorer
# --------------------------------------------------------------------------


def test_action_projects_onto_risk_factors():
    action = AgentAction(
        action_type="bulk_delete",
        description="delete everything",
        tool_name="bulk_delete_records",
        parameters={"filter_description": "all"},
        reversibility="irreversible",
        data_scope=500,
        regulatory_category="regulated",
        confidence=0.6,
    )
    factors = action.to_risk_factors()
    assert factors.reversibility == "irreversible"
    assert factors.data_scope == 500
    assert route_action(score_action(factors), DEFAULT_THRESHOLDS) == "full_review"


# --------------------------------------------------------------------------
# Integration -- real Anthropic API. Skipped unless ANTHROPIC_API_KEY is set.
# --------------------------------------------------------------------------


@pytest.mark.integration
@requires_api_key
@pytest.mark.parametrize(
    ("request_text", "expected_tool", "expected_route"),
    [
        (
            "What's the current email address on file for customer C-10482?",
            "query_customer_records",
            "autonomous",
        ),
        (
            "Update customer C-10482's phone number to +44 20 7946 0958.",
            "update_customer_record",
            "confirm",
        ),
        (
            "Purge all 500 EU customer records that have been inactive since 2019 "
            "to satisfy a GDPR erasure request.",
            "bulk_delete_records",
            "full_review",
        ),
    ],
    ids=["read", "single_update", "bulk_delete"],
)
def test_live_scenarios_route_as_expected(request_text, expected_tool, expected_route):
    action = propose_action(request_text, {"environment": "production"})
    score = score_action(action.to_risk_factors())
    decision = route_action(score, DEFAULT_THRESHOLDS)

    print(f"\n--- {request_text[:60]}...")
    print(f"  tool         {action.tool_name}")
    print(f"  reversibility {action.reversibility}")
    print(f"  data_scope    {action.data_scope}")
    print(f"  regulatory    {action.regulatory_category}")
    print(f"  confidence    {action.confidence}")
    print(f"  composite     {score.composite_score}")
    print(f"  routing       {decision}")

    assert action.tool_name == expected_tool
    assert 0.0 <= action.confidence <= 1.0
    assert decision == expected_route


@pytest.mark.integration
@requires_api_key
def test_live_confidence_is_actually_self_reported():
    """A deliberately ambiguous request should come back with lower confidence."""
    clear = propose_action("Show me customer C-10482's order history.", {})
    vague = propose_action("Deal with the thing for that customer from before.", {})
    print(f"\nclear request confidence: {clear.confidence}")
    print(f"vague request confidence: {vague.confidence}")
    assert vague.confidence < clear.confidence
