"""Tests for the risk scoring engine.

The three headline scenarios here are the same three used in the customer demo:
a read-only query, a single-record update, and a bulk delete. If these ever
stop routing the way they do below, the demo narrative breaks.
"""

import pytest

from autonomy_engine.risk_scorer import (
    DEFAULT_THRESHOLDS,
    WEIGHT_CONFIDENCE,
    WEIGHT_DATA_SCOPE,
    WEIGHT_REGULATORY,
    WEIGHT_REVERSIBILITY,
    RiskFactors,
    route_action,
    score_action,
)

# --------------------------------------------------------------------------
# The three demo scenarios
# --------------------------------------------------------------------------


def test_read_only_query_routes_to_autonomous():
    factors = RiskFactors(
        reversibility="reversible",
        data_scope=1,
        regulatory_category="none",
        confidence=0.95,
    )
    score = score_action(factors)
    assert score.composite_score < 0.3
    assert route_action(score, DEFAULT_THRESHOLDS) == "autonomous"
    assert "reversible" in score.breakdown["reversibility"]


def test_single_record_update_routes_to_confirm():
    factors = RiskFactors(
        reversibility="partially_reversible",
        data_scope=1,
        regulatory_category="internal_sensitive",
        confidence=0.9,
    )
    score = score_action(factors)
    assert 0.3 <= score.composite_score <= 0.7
    assert route_action(score, DEFAULT_THRESHOLDS) == "confirm"
    assert "partially reversible" in score.breakdown["reversibility"]
    assert "internally sensitive" in score.breakdown["regulatory_category"]


def test_bulk_delete_routes_to_full_review():
    factors = RiskFactors(
        reversibility="irreversible",
        data_scope=500,
        regulatory_category="regulated",
        confidence=0.6,
    )
    score = score_action(factors)
    assert score.composite_score > 0.7
    assert route_action(score, DEFAULT_THRESHOLDS) == "full_review"
    assert "irreversible" in score.breakdown["reversibility"]
    assert "500 records" in score.breakdown["data_scope"]


# --------------------------------------------------------------------------
# Threshold boundaries
# --------------------------------------------------------------------------


def test_score_exactly_on_low_threshold_routes_to_confirm():
    """A score sitting exactly on `low` gets supervised, not waved through.

    These factors are chosen to land on 0.3000 exactly:
      0.5*0.35 + 0.1*0.25 + 0.1*0.25 + 0.5*0.15 = 0.175 + 0.025 + 0.025 + 0.075
    """
    factors = RiskFactors(
        reversibility="partially_reversible",
        data_scope=1,
        regulatory_category="none",
        confidence=0.5,
    )
    score = score_action(factors)
    assert score.composite_score == pytest.approx(0.3)
    assert route_action(score, DEFAULT_THRESHOLDS) == "confirm"


def test_score_exactly_on_high_threshold_routes_to_confirm():
    """Exactly at `high` is still a confirmation -- only strictly above escalates."""
    factors = RiskFactors(
        reversibility="irreversible",
        data_scope=500,
        regulatory_category="regulated",
        confidence=0.6,
    )
    score = score_action(factors)
    # Pin `high` to this action's exact composite (0.825) to sit on the boundary.
    thresholds = {"low": 0.3, "high": score.composite_score}
    assert route_action(score, thresholds) == "confirm"
    # A hair below, and the same action escalates.
    assert route_action(score, {"low": 0.3, "high": score.composite_score - 0.0001}) == "full_review"


def test_thresholds_are_configurable():
    """The same action routes differently under stricter thresholds."""
    factors = RiskFactors(
        reversibility="reversible",
        data_scope=1,
        regulatory_category="none",
        confidence=0.95,
    )
    score = score_action(factors)
    assert route_action(score, {"low": 0.3, "high": 0.7}) == "autonomous"
    # A paranoid tenant that wants eyes on everything.
    assert route_action(score, {"low": 0.0, "high": 0.05}) == "full_review"


# --------------------------------------------------------------------------
# Scoring mechanics
# --------------------------------------------------------------------------


def test_weights_sum_to_one():
    total = WEIGHT_REVERSIBILITY + WEIGHT_DATA_SCOPE + WEIGHT_REGULATORY + WEIGHT_CONFIDENCE
    assert total == pytest.approx(1.0)


def test_confidence_is_inverted():
    """High confidence must lower risk, low confidence must raise it."""
    confident = score_action(
        RiskFactors(
            reversibility="reversible",
            data_scope=1,
            regulatory_category="none",
            confidence=0.95,
        )
    )
    unsure = score_action(
        RiskFactors(
            reversibility="reversible",
            data_scope=1,
            regulatory_category="none",
            confidence=0.4,
        )
    )
    assert confident.confidence_score == pytest.approx(0.05)
    assert unsure.confidence_score == pytest.approx(0.6)
    assert unsure.composite_score > confident.composite_score


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        (0, 0.1),
        (1, 0.1),
        (2, 0.3),
        (10, 0.3),
        (11, 0.6),
        (100, 0.6),
        (101, 0.9),
        (500, 0.9),
        (1_000_000, 0.9),
    ],
)
def test_data_scope_buckets(records, expected):
    score = score_action(
        RiskFactors(
            reversibility="reversible",
            data_scope=records,
            regulatory_category="none",
            confidence=1.0,
        )
    )
    assert score.data_scope_score == pytest.approx(expected)


def test_composite_is_the_weighted_sum_of_its_parts():
    score = score_action(
        RiskFactors(
            reversibility="irreversible",
            data_scope=50,
            regulatory_category="internal_sensitive",
            confidence=0.75,
        )
    )
    expected = (
        score.reversibility_score * WEIGHT_REVERSIBILITY
        + score.data_scope_score * WEIGHT_DATA_SCOPE
        + score.regulatory_score * WEIGHT_REGULATORY
        + score.confidence_score * WEIGHT_CONFIDENCE
    )
    assert score.composite_score == pytest.approx(expected)


def test_breakdown_covers_every_dimension():
    score = score_action(
        RiskFactors(
            reversibility="irreversible",
            data_scope=500,
            regulatory_category="regulated",
            confidence=0.6,
        )
    )
    assert set(score.breakdown) == {
        "reversibility",
        "data_scope",
        "regulatory_category",
        "confidence",
        "composite",
    }
    # Every line must show its numeric contribution -- this is the audit trail.
    for dimension in ("reversibility", "data_scope", "regulatory_category", "confidence"):
        assert "raw" in score.breakdown[dimension]
        assert "weight" in score.breakdown[dimension]


# --------------------------------------------------------------------------
# Input validation and error handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reversibility": "maybe"},                 # not one of the three literals
        {"regulatory_category": "top_secret"},      # not one of the three literals
        {"confidence": 1.5},                        # out of 0-1 range
        {"confidence": -0.1},                       # out of 0-1 range
        {"data_scope": -5},                         # negative record count
    ],
)
def test_invalid_factors_are_rejected(kwargs):
    base = {
        "reversibility": "reversible",
        "data_scope": 1,
        "regulatory_category": "none",
        "confidence": 0.9,
    }
    with pytest.raises(Exception):  # pydantic.ValidationError
        RiskFactors(**{**base, **kwargs})


def test_route_action_rejects_malformed_thresholds():
    score = score_action(
        RiskFactors(
            reversibility="reversible",
            data_scope=1,
            regulatory_category="none",
            confidence=0.9,
        )
    )
    with pytest.raises(ValueError, match="missing required key"):
        route_action(score, {"low": 0.3})
    with pytest.raises(ValueError, match="must not exceed"):
        route_action(score, {"low": 0.8, "high": 0.2})


def test_route_action_defaults_to_standard_thresholds():
    """Omitting thresholds behaves the same as passing DEFAULT_THRESHOLDS."""
    score = score_action(
        RiskFactors(
            reversibility="partially_reversible",
            data_scope=1,
            regulatory_category="internal_sensitive",
            confidence=0.9,
        )
    )
    assert route_action(score) == route_action(score, DEFAULT_THRESHOLDS) == "confirm"
