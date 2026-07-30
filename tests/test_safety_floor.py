"""The safety net: what stops a bad judgement becoming a bad outcome.

Everything here exists because the agent's band is a judgement, and judgements
can be made on false premises. Two independent mechanisms guard that:

- :func:`executor.preflight` measures what an action *really* touches, from the
  data, before anything is banded or run.
- :func:`risk_scorer.apply_blast_radius_floor` uses that measurement to set a
  minimum supervision level, which can only ever escalate.

The scenario that motivates all of it: the agent proposes something that edits
or deletes hundreds of rows, calls it low risk, and it executes unattended.
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

# Imported at module scope, not inside the fixture. agent_actions calls
# load_dotenv() on import, which would re-inject the developer's real
# DYNAMODB_ENDPOINT_URL *after* aws_env deleted it, and the app would then try
# to reach a live DynamoDB instead of moto.
from autonomy_engine import data_store, executor, risk_scorer
from autonomy_engine.agent_actions import AgentAction
from autonomy_engine.main import app
from autonomy_engine.risk_scorer import apply_blast_radius_floor, scope_floor
from tests.conftest import SEED_ROW_COUNT

LEVELS = ("autonomous", "confirm", "full_review")


def where(field, operator, value):
    return {"filter": [{"field": field, "operator": operator, "value": value}]}


def first_invoice():
    return data_store.load_rows()[0][data_store.ID_FIELD]


# --------------------------------------------------------------------------
# preflight: the tools must report exact data, not a guess
# --------------------------------------------------------------------------


def test_preflight_reports_the_true_row_count():
    expected = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Clothing")]
    )
    scope = executor.preflight(
        "bulk_delete_transactions", where("category", "equals", "Clothing")
    )
    assert scope.actual_rows == expected
    assert scope.is_mutation is True
    assert scope.is_destructive is True


def test_preflight_changes_nothing():
    """It runs on actions a human may still reject, so it must be inert."""
    for tool, params in [
        ("bulk_delete_transactions", where("category", "equals", "Clothing")),
        ("delete_transaction", {"invoice_no": first_invoice()}),
        ("update_transaction", {"invoice_no": first_invoice(), "field": "age", "new_value": "1"}),
    ]:
        executor.preflight(tool, params)
    assert len(data_store.load_rows()) == SEED_ROW_COUNT
    assert not data_store.snapshot_dir().exists() or not list(
        data_store.snapshot_dir().glob("*.csv")
    )


def test_preflight_marks_reads_as_non_mutating():
    scope = executor.preflight("query_transactions", {"filter": []})
    assert scope.is_mutation is False
    assert scope.actual_rows == SEED_ROW_COUNT


def test_preflight_flags_a_missing_invoice_as_zero_rows():
    scope = executor.preflight("delete_transaction", {"invoice_no": "I000000"})
    assert scope.actual_rows == 0
    assert scope.resolvable is False


def test_preflight_reports_an_unfiltered_bulk_delete_at_full_size():
    """An empty filter matches everything. If preflight reported 0 here, the
    floor would see a harmless-looking action and wave through the one call
    that could empty the table."""
    scope = executor.preflight("bulk_delete_transactions", {"filter": []})
    assert scope.actual_rows == SEED_ROW_COUNT
    assert scope.resolvable is False


def test_preflight_does_not_mistake_a_broken_filter_for_an_empty_result():
    """Zero rows looks harmless; "we don't know" is strictly worse and must not
    be flattened into it."""
    scope = executor.preflight(
        "bulk_delete_transactions", where("nonexistent_column", "equals", "x")
    )
    assert scope.resolvable is False
    assert scope.detail


# --------------------------------------------------------------------------
# The floor: the disaster case
# --------------------------------------------------------------------------


def test_a_large_edit_banded_low_cannot_run_unattended():
    """The headline scenario. The agent says low; it really edits 500 rows.

    If this ever routes autonomous again, the engine has lost the only property
    that makes it safe to point at real data.
    """
    final, note = apply_blast_radius_floor(
        "autonomous", actual_rows=500, is_mutation=True, is_destructive=False
    )
    assert final == "full_review"
    assert "500" in note


def test_deletion_never_runs_unattended_however_small():
    """Found by running the real model: it banded "delete invoice I317333" low
    -- defensibly, one row, unambiguous request -- and the row was destroyed
    with no human involved. Size is not the only axis of danger."""
    final, note = apply_blast_radius_floor(
        "autonomous", actual_rows=1, is_mutation=True, is_destructive=True
    )
    assert final == "confirm"
    assert "deletes" in note


def test_unknown_blast_radius_is_treated_as_maximally_risky():
    final, note = apply_blast_radius_floor(
        "autonomous", actual_rows=0, is_mutation=True, is_destructive=True, resolvable=False
    )
    assert final == "full_review"
    assert "could not be determined" in note


@pytest.mark.parametrize("start", LEVELS)
@pytest.mark.parametrize(
    "scope",
    [
        dict(actual_rows=1, is_mutation=False, is_destructive=False),
        dict(actual_rows=1, is_mutation=True, is_destructive=False),
        dict(actual_rows=1, is_mutation=True, is_destructive=True),
        dict(actual_rows=500, is_mutation=True, is_destructive=False),
        dict(actual_rows=99_457, is_mutation=True, is_destructive=True),
        dict(actual_rows=0, is_mutation=True, is_destructive=True, resolvable=False),
    ],
)
def test_the_floor_never_lowers_supervision(start, scope):
    """The one invariant the whole mechanism rests on. If the floor could ever
    de-escalate, it would be a way to *launder* a cautious band into a permissive
    one, which is worse than having no floor at all."""
    final, _ = apply_blast_radius_floor(start, **scope)
    assert LEVELS.index(final) >= LEVELS.index(start)


def test_a_cautious_agent_is_never_overridden_downward():
    """An agent that asks for more supervision than the floor requires gets it."""
    final, note = apply_blast_radius_floor(
        "full_review", actual_rows=1, is_mutation=True, is_destructive=False
    )
    assert final == "full_review"
    assert note is None


def test_reads_are_not_escalated_by_size():
    """A 99k-row read destroys nothing and is capped at the executor, so row
    count alone should not drag a human in."""
    final, note = apply_blast_radius_floor(
        "autonomous", actual_rows=99_457, is_mutation=False, is_destructive=False
    )
    assert final == "autonomous"
    assert note is None


@pytest.mark.parametrize(
    ("rows", "expected"),
    [(1, "autonomous"), (2, "confirm"), (100, "confirm"), (101, "full_review")],
)
def test_edit_size_thresholds(rows, expected):
    assert scope_floor(actual_rows=rows, is_mutation=True) == expected


# --------------------------------------------------------------------------
# The override must be visible
# --------------------------------------------------------------------------


def test_an_override_is_recorded_not_silent():
    """An override nobody can see afterwards is barely better than none."""
    from autonomy_engine.risk_scorer import RiskFactors, build_assessment

    assessment = build_assessment(
        RiskFactors(
            reversibility="irreversible",
            data_scope=5,
            regulatory_category="none",
            confidence=0.9,
            risk_band="low",
            rationale="thought it was small",
        )
    )
    _, note = apply_blast_radius_floor(
        "autonomous", actual_rows=15_097, is_mutation=True, is_destructive=True
    )
    overridden = assessment.with_measured_scope(15_097).with_override(note)

    assert overridden.escalated_by_floor is True
    assert overridden.actual_rows == 15_097
    assert "15,097" in overridden.breakdown["blast_radius"]
    # The agent's own judgement survives verbatim beside the override.
    assert overridden.risk_band == "low"
    assert "thought it was small" in overridden.breakdown["composite"]


# --------------------------------------------------------------------------
# End to end through the API
# --------------------------------------------------------------------------


def test_api_blocks_a_bulk_delete_the_agent_called_low(client):
    """Full path: a mocked agent insists a whole-category delete is low risk.
    The engine must still refuse to run it."""
    rogue = AgentAction(
        action_type="bulk_delete",
        description="PERMANENTLY DELETE all Clothing transactions",
        tool_name="bulk_delete_transactions",
        parameters={
            "filter_description": "all Clothing",
            "filter": [{"field": "category", "operator": "equals", "value": "Clothing"}],
        },
        reversibility="reversible",
        data_scope=1,
        regulatory_category="none",
        confidence=0.99,
        risk_band="low",
        severity=0.02,
        rationale="claims this is a trivial one-row cleanup",
    )

    before = len(data_store.load_rows())
    with patch("autonomy_engine.main.propose_action", return_value=rogue), patch(
        "autonomy_engine.main.reassess_action", side_effect=lambda action, rows: action
    ):
        response = client.post(
            "/actions/propose",
            json={"user_request": "tidy up clothing", "session_id": "sess-rogue"},
        )

    body = response.json()
    assert body["routing_decision"] == "full_review"
    assert body["risk_score"]["escalated_by_floor"] is True
    assert body["risk_score"]["actual_rows"] > 1
    assert len(data_store.load_rows()) == before, "nothing may be deleted"


def test_api_reports_the_true_row_count_not_the_agents_guess(client):
    understated = AgentAction(
        action_type="bulk_delete",
        description="delete Clothing",
        tool_name="bulk_delete_transactions",
        parameters={
            "filter_description": "all Clothing",
            "filter": [{"field": "category", "operator": "equals", "value": "Clothing"}],
        },
        reversibility="irreversible",
        data_scope=3,
        regulatory_category="none",
        confidence=0.8,
        risk_band="high",
        severity=0.9,
        rationale="",
    )
    expected = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Clothing")]
    )

    with patch("autonomy_engine.main.propose_action", return_value=understated), patch(
        "autonomy_engine.main.reassess_action", side_effect=lambda action, rows: action
    ):
        response = client.post(
            "/actions/propose",
            json={"user_request": "delete clothing", "session_id": "sess-count"},
        )

    assert response.json()["risk_score"]["actual_rows"] == expected


def test_a_wrong_estimate_triggers_a_rejudgement(client):
    """The engine must ask the model again rather than routing on a band that
    was reasoned from a false premise."""
    action = AgentAction(
        action_type="bulk_delete",
        description="delete Clothing",
        tool_name="bulk_delete_transactions",
        parameters={
            "filter_description": "all Clothing",
            "filter": [{"field": "category", "operator": "equals", "value": "Clothing"}],
        },
        reversibility="irreversible",
        data_scope=2,
        regulatory_category="none",
        confidence=0.9,
        risk_band="low",
        severity=0.1,
        rationale="assumed a couple of rows",
    )

    seen = {}

    def fake_reassess(proposed, rows):
        seen["rows"] = rows
        return proposed.model_copy(update={"risk_band": "high", "data_scope": rows})

    with patch("autonomy_engine.main.propose_action", return_value=action), patch(
        "autonomy_engine.main.reassess_action", side_effect=fake_reassess
    ):
        client.post(
            "/actions/propose",
            json={"user_request": "delete clothing", "session_id": "sess-rejudge"},
        )

    assert seen["rows"] == data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Clothing")]
    )


def test_an_accurate_estimate_is_not_rejudged(client):
    """Re-judging costs an LLM round trip; it should only happen when the
    premise was actually wrong."""
    from autonomy_engine.agent_actions import AgentAction

    exact = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Clothing")]
    )
    action = AgentAction(
        action_type="bulk_delete",
        description="delete Clothing",
        tool_name="bulk_delete_transactions",
        parameters={
            "filter_description": "all Clothing",
            "filter": [{"field": "category", "operator": "equals", "value": "Clothing"}],
        },
        reversibility="irreversible",
        data_scope=exact,
        regulatory_category="none",
        confidence=0.9,
        risk_band="high",
        severity=0.9,
        rationale="",
    )

    with patch("autonomy_engine.main.propose_action", return_value=action), patch(
        "autonomy_engine.main.reassess_action"
    ) as reassess:
        client.post(
            "/actions/propose",
            json={"user_request": "delete clothing", "session_id": "sess-exact"},
        )

    reassess.assert_not_called()


@pytest.fixture
def client(audit_table):
    return TestClient(app, raise_server_exceptions=False)
