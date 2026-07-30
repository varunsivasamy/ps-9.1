"""Tests for the risk assessment and routing layer.

Since the model now decides the band, there is no arithmetic left to verify.
What these tests pin down instead is the contract around that judgement: the
band routes the action, the severity number is never allowed to contradict it,
and the model's reasoning survives into the breakdown a reviewer reads.
"""

import pytest

from autonomy_engine.risk_scorer import (
    BAND_SEVERITY_RANGES,
    BAND_TO_LEVEL,
    RiskFactors,
    build_assessment,
    describe_routing,
    route_action,
)


def factors(**overrides):
    """A well-formed assessment, overridable per test."""
    base = dict(
        reversibility="reversible",
        reversibility_reasoning="read-only, changes nothing",
        data_scope=1,
        data_scope_reasoning="single customer lookup",
        regulatory_category="none",
        regulatory_reasoning="non-sensitive business data",
        confidence=0.95,
        confidence_reasoning="request names the customer explicitly",
        risk_band="low",
        severity=0.1,
        rationale="a single read of non-sensitive data",
    )
    base.update(overrides)
    return RiskFactors(**base)


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("band", "expected"),
    [("low", "autonomous"), ("medium", "confirm"), ("high", "full_review")],
)
def test_band_determines_routing(band, expected):
    """The band the model chose is the routing decision. Nothing else is consulted."""
    assessment = build_assessment(factors(risk_band=band, severity=None))
    assert route_action(assessment) == expected


def test_routing_ignores_the_four_dimensions():
    """An irreversible, bulk, regulated, low-confidence action banded "low" still
    routes autonomously.

    This is the whole point of the redesign and deserves to be stated as a test
    rather than discovered: the dimensions inform the model's judgement, they do
    not override it. If this ever needs to fail, the design has changed.
    """
    assessment = build_assessment(
        factors(
            reversibility="irreversible",
            data_scope=5000,
            regulatory_category="regulated",
            confidence=0.2,
            risk_band="low",
            severity=0.05,
        )
    )
    assert route_action(assessment) == "autonomous"


def test_every_band_maps_to_a_level():
    """No band can be added without a routing target."""
    assert set(BAND_TO_LEVEL) == set(BAND_SEVERITY_RANGES)


# --------------------------------------------------------------------------
# Severity reconciliation
# --------------------------------------------------------------------------


def test_severity_within_band_is_kept():
    assessment = build_assessment(factors(risk_band="medium", severity=0.55))
    assert assessment.composite_score == 0.55
    assert assessment.severity_was_clamped is False


def test_missing_severity_falls_back_to_band_midpoint():
    assessment = build_assessment(factors(risk_band="high", severity=None))
    low, high = BAND_SEVERITY_RANGES["high"]
    assert low <= assessment.composite_score <= high
    assert assessment.severity_was_clamped is False


@pytest.mark.parametrize(
    ("band", "severity"),
    [("high", 0.05), ("low", 0.99), ("medium", 0.95), ("medium", 0.01)],
)
def test_severity_contradicting_the_band_is_clamped(band, severity):
    """A model that reasons its way to a band then types a number from a
    different one has contradicted itself; the band is the considered judgement
    and wins, and the override is recorded rather than hidden."""
    assessment = build_assessment(factors(risk_band=band, severity=severity))
    low, high = BAND_SEVERITY_RANGES[band]

    assert low <= assessment.composite_score <= high
    assert assessment.severity_was_clamped is True
    assert route_action(assessment) == BAND_TO_LEVEL[band]
    assert "clamped" in assessment.breakdown["composite"]


def test_clamping_never_changes_the_routing_decision():
    """Clamping adjusts a display number, never where the action goes."""
    for band in BAND_TO_LEVEL:
        contradictory = build_assessment(factors(risk_band=band, severity=0.5))
        assert route_action(contradictory) == BAND_TO_LEVEL[band]


# --------------------------------------------------------------------------
# Explainability
# --------------------------------------------------------------------------


def test_breakdown_carries_the_models_own_reasoning():
    """The audit trail must show why, in the model's words -- not a
    reconstruction from numbers."""
    assessment = build_assessment(
        factors(
            risk_band="high",
            severity=0.9,
            reversibility="irreversible",
            reversibility_reasoning="rows are deleted with no undo path",
            data_scope=487,
            data_scope_reasoning="filter matches every EU customer inactive since 2019",
            regulatory_category="regulated",
            regulatory_reasoning="EU residents, so GDPR applies",
            confidence=0.6,
            confidence_reasoning="'inactive' could mean churned or merely dormant",
            rationale="irreversible bulk deletion of regulated data on an ambiguous filter",
        )
    )

    assert "no undo path" in assessment.breakdown["reversibility"]
    assert "487" in assessment.breakdown["data_scope"]
    assert "GDPR" in assessment.breakdown["regulatory_category"]
    assert "dormant" in assessment.breakdown["confidence"]
    assert "HIGH" in assessment.breakdown["composite"]
    assert "irreversible bulk deletion" in assessment.breakdown["composite"]


def test_breakdown_covers_all_four_dimensions_plus_the_verdict():
    assessment = build_assessment(factors())
    assert set(assessment.breakdown) == {
        "reversibility",
        "data_scope",
        "regulatory_category",
        "confidence",
        "composite",
    }


def test_missing_reasoning_degrades_without_crashing():
    """Reasoning is requested from the model but not structurally guaranteed.
    A terse response should still produce a usable breakdown."""
    assessment = build_assessment(
        factors(
            reversibility_reasoning="",
            data_scope_reasoning="",
            regulatory_reasoning="",
            confidence_reasoning="",
            rationale="",
        )
    )
    assert assessment.breakdown["reversibility"] == "reversibility: reversible"
    assert "LOW" in assessment.breakdown["composite"]


def test_describe_routing_is_readable():
    assessment = build_assessment(
        factors(risk_band="high", severity=0.9, rationale="deletes regulated records")
    )
    summary = describe_routing(assessment, route_action(assessment))
    assert "high risk" in summary
    assert "deletes regulated records" in summary
    assert summary.endswith("-> full_review")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_band_is_required():
    """A missing band must never quietly become "low" -- it is the routing decision."""
    with pytest.raises(ValueError):
        RiskFactors(
            reversibility="reversible",
            data_scope=1,
            regulatory_category="none",
            confidence=0.9,
        )


@pytest.mark.parametrize("bad", ["critical", "LOW", "", "safe"])
def test_unknown_bands_are_rejected(bad):
    with pytest.raises(ValueError):
        factors(risk_band=bad)


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_out_of_range_values_are_rejected(bad):
    with pytest.raises(ValueError):
        factors(confidence=bad)
    with pytest.raises(ValueError):
        factors(severity=bad)


def test_negative_data_scope_is_rejected():
    with pytest.raises(ValueError):
        factors(data_scope=-1)
