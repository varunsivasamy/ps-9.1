"""Risk scoring and autonomy routing.

This module is the heart of PS-9.1 and is deliberately pure: no AWS calls, no
network, no I/O. Given a set of factors describing a proposed agent action, it
produces a composite risk score with a human-readable breakdown, and maps that
score onto one of three autonomy levels.

The four risk dimensions are scored independently on a 0-1 scale and combined
as a weighted sum:

    reversibility        35%   can this action be undone?
    data_scope           25%   how many records/users does it touch?
    regulatory_category  25%   is the data regulated or internally sensitive?
    confidence           15%   how sure is the model? (inverted -- low
                               confidence is itself a risk)

Every score carries a ``breakdown`` explaining how it was reached, because a
routing decision nobody can explain is not usable in a regulated environment.
"""

from __future__ import annotations

from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Tunables
#
# These are the knobs an operator is most likely to want to adjust, so they
# live at the top of the file as named constants rather than being buried in
# the scoring logic.
# --------------------------------------------------------------------------

#: Relative contribution of each dimension to the composite score. Must sum to 1.
WEIGHT_REVERSIBILITY: Final[float] = 0.35
WEIGHT_DATA_SCOPE: Final[float] = 0.25
WEIGHT_REGULATORY: Final[float] = 0.25
WEIGHT_CONFIDENCE: Final[float] = 0.15

#: Per-dimension raw risk values.
REVERSIBILITY_SCORES: Final[dict[str, float]] = {
    "reversible": 0.1,
    "partially_reversible": 0.5,
    "irreversible": 0.9,
}
REGULATORY_SCORES: Final[dict[str, float]] = {
    "none": 0.1,
    "internal_sensitive": 0.5,
    "regulated": 0.9,
}

#: Upper bound (inclusive) of each data-scope bucket, paired with its risk value.
#: ``None`` is the open-ended top bucket.
DATA_SCOPE_BUCKETS: Final[tuple[tuple[int | None, float], ...]] = (
    (1, 0.1),      # a single record
    (10, 0.3),     # a handful
    (100, 0.6),    # a meaningful slice
    (None, 0.9),   # bulk -- more than 100 records
)

#: Routing thresholds. Scores strictly below ``low`` run autonomously; scores
#: strictly above ``high`` go to a human; everything in between is confirmed.
DEFAULT_THRESHOLDS: Final[dict[str, float]] = {"low": 0.3, "high": 0.7}

#: Decimal places kept on emitted scores. Keeps floating-point noise out of the
#: audit log and makes threshold comparisons predictable.
SCORE_PRECISION: Final[int] = 4

Reversibility: TypeAlias = Literal["reversible", "partially_reversible", "irreversible"]
RegulatoryCategory: TypeAlias = Literal["none", "internal_sensitive", "regulated"]
AutonomyLevel: TypeAlias = Literal["autonomous", "confirm", "full_review"]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class RiskFactors(BaseModel):
    """The raw, un-scored description of a proposed action."""

    reversibility: Reversibility = Field(
        description="Whether the action can be undone once performed.",
    )
    data_scope: int = Field(
        ge=0,
        description="Number of records or users the action affects.",
    )
    regulatory_category: RegulatoryCategory = Field(
        description="Sensitivity class of the data the action touches.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="The model's self-reported confidence that this is the correct action.",
    )


class RiskScore(BaseModel):
    """A scored action: per-dimension risk, the composite, and the reasoning."""

    reversibility_score: float = Field(ge=0.0, le=1.0)
    data_scope_score: float = Field(ge=0.0, le=1.0)
    regulatory_score: float = Field(ge=0.0, le=1.0)
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Inverse of confidence: low model confidence is high risk.",
    )
    composite_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Weighted sum of the four dimension scores.",
    )
    breakdown: dict[str, str] = Field(
        description="Human-readable explanation of each dimension's contribution.",
    )


# --------------------------------------------------------------------------
# Per-dimension scoring
#
# Each helper returns (raw_score, explanation). The explanation is written for
# a human reading an audit trail, not for a machine.
# --------------------------------------------------------------------------


def _score_reversibility(reversibility: str) -> tuple[float, str]:
    """Score how hard the action is to undo."""
    if reversibility == "reversible":
        raw = REVERSIBILITY_SCORES["reversible"]
        label = "reversible action"
    elif reversibility == "partially_reversible":
        raw = REVERSIBILITY_SCORES["partially_reversible"]
        label = "partially reversible action"
    elif reversibility == "irreversible":
        raw = REVERSIBILITY_SCORES["irreversible"]
        label = "irreversible action"
    else:  # pragma: no cover - pydantic rejects this before we get here
        raise ValueError(f"unknown reversibility: {reversibility!r}")
    return raw, _explain(label, raw, WEIGHT_REVERSIBILITY)


def _score_data_scope(data_scope: int) -> tuple[float, str]:
    """Score the blast radius by record count."""
    if data_scope <= 1:
        raw = 0.1
    elif data_scope <= 10:
        raw = 0.3
    elif data_scope <= 100:
        raw = 0.6
    else:
        raw = 0.9

    noun = "record" if data_scope == 1 else "records"
    return raw, _explain(f"{data_scope} {noun} affected", raw, WEIGHT_DATA_SCOPE)


def _score_regulatory(regulatory_category: str) -> tuple[float, str]:
    """Score the sensitivity class of the data involved."""
    if regulatory_category == "none":
        raw = REGULATORY_SCORES["none"]
        label = "no regulatory category"
    elif regulatory_category == "internal_sensitive":
        raw = REGULATORY_SCORES["internal_sensitive"]
        label = "internally sensitive data"
    elif regulatory_category == "regulated":
        raw = REGULATORY_SCORES["regulated"]
        label = "regulated data"
    else:  # pragma: no cover - pydantic rejects this before we get here
        raise ValueError(f"unknown regulatory_category: {regulatory_category!r}")
    return raw, _explain(label, raw, WEIGHT_REGULATORY)


def _score_confidence(confidence: float) -> tuple[float, str]:
    """Invert model confidence into a risk contribution.

    A model that is only 40% sure of its own action is a risk in itself, so
    ``risk = 1 - confidence``.
    """
    raw = 1.0 - confidence
    label = f"model {confidence:.2f} confident in this action"
    return raw, _explain(label, raw, WEIGHT_CONFIDENCE)


def _explain(label: str, raw: float, weight: float) -> str:
    """Render one breakdown line, e.g. ``irreversible action (+0.90 raw x 35% = +0.315)``."""
    return f"{label} (+{raw:.2f} raw x {weight:.0%} weight = +{raw * weight:.3f})"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def score_action(factors: RiskFactors) -> RiskScore:
    """Score a proposed action across all four risk dimensions.

    Args:
        factors: The raw description of the action being proposed.

    Returns:
        A :class:`RiskScore` with each dimension scored 0-1, the weighted
        composite, and a per-dimension human-readable breakdown.
    """
    reversibility_score, reversibility_note = _score_reversibility(factors.reversibility)
    data_scope_score, data_scope_note = _score_data_scope(factors.data_scope)
    regulatory_score, regulatory_note = _score_regulatory(factors.regulatory_category)
    confidence_score, confidence_note = _score_confidence(factors.confidence)

    composite = (
        reversibility_score * WEIGHT_REVERSIBILITY
        + data_scope_score * WEIGHT_DATA_SCOPE
        + regulatory_score * WEIGHT_REGULATORY
        + confidence_score * WEIGHT_CONFIDENCE
    )
    composite = round(composite, SCORE_PRECISION)

    breakdown = {
        "reversibility": reversibility_note,
        "data_scope": data_scope_note,
        "regulatory_category": regulatory_note,
        "confidence": confidence_note,
        "composite": f"weighted composite risk score = {composite:.4f}",
    }

    return RiskScore(
        reversibility_score=round(reversibility_score, SCORE_PRECISION),
        data_scope_score=round(data_scope_score, SCORE_PRECISION),
        regulatory_score=round(regulatory_score, SCORE_PRECISION),
        confidence_score=round(confidence_score, SCORE_PRECISION),
        composite_score=composite,
        breakdown=breakdown,
    )


def route_action(
    score: RiskScore,
    thresholds: dict[str, float] | None = None,
) -> AutonomyLevel:
    """Map a composite risk score onto an autonomy level.

    Boundary behaviour is deliberately conservative: a score sitting exactly on
    a threshold gets the *more* supervised of the two options. Only scores
    strictly below ``low`` run without a human, and a score exactly at ``high``
    still gets a confirmation rather than being waved through.

    Args:
        score: The scored action.
        thresholds: Mapping with ``low`` and ``high`` keys. Defaults to
            :data:`DEFAULT_THRESHOLDS` (low=0.3, high=0.7).

    Returns:
        ``"autonomous"``, ``"confirm"``, or ``"full_review"``.

    Raises:
        ValueError: If ``thresholds`` is missing a key or ``low`` exceeds ``high``.
    """
    thresholds = DEFAULT_THRESHOLDS if thresholds is None else thresholds

    missing = {"low", "high"} - thresholds.keys()
    if missing:
        raise ValueError(f"thresholds missing required key(s): {sorted(missing)}")

    low, high = thresholds["low"], thresholds["high"]
    if low > high:
        raise ValueError(f"threshold low ({low}) must not exceed high ({high})")

    if score.composite_score < low:
        return "autonomous"
    if score.composite_score > high:
        return "full_review"
    return "confirm"


def describe_routing(
    score: RiskScore,
    decision: AutonomyLevel,
    thresholds: dict[str, float] | None = None,
) -> str:
    """One-line, presenter-friendly summary of why an action was routed as it was."""
    thresholds = DEFAULT_THRESHOLDS if thresholds is None else thresholds
    reasons = {
        "autonomous": "below the low threshold, so it executes without a human",
        "confirm": "between the thresholds, so it needs a one-click confirmation",
        "full_review": "above the high threshold, so it is blocked pending human review",
    }
    return (
        f"composite risk {score.composite_score:.4f} is {reasons[decision]} "
        f"(low={thresholds['low']}, high={thresholds['high']}) -> {decision}"
    )
