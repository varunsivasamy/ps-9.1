"""Risk assessment and autonomy routing.

This module is the heart of PS-9.1 and is deliberately pure: no AWS calls, no
network, no I/O. It takes the risk assessment the model produced alongside its
tool call and turns it into a routing decision.

Who decides the risk
--------------------
The model does. Earlier revisions asked the model only to *classify* four
dimensions and then applied fixed weights (35/25/25/15) and fixed thresholds
(0.3/0.7) to derive a band. That arithmetic is gone. A weighted sum cannot tell
the difference between deleting 200 rows of marketing preferences and deleting
200 rows of medical records -- both score identically on every dimension -- so
the judgement now sits with the model that can actually read the request.

The model reasons across the same four dimensions, writes down its reasoning for
each, and states a band directly:

    reversibility        can this action be undone?
    data_scope           how many records/users does it touch?
    regulatory_category  is the data regulated or internally sensitive?
    confidence           how sure is the model this is the right action?

    => risk_band         "low" | "medium" | "high"

The band maps one-to-one onto an autonomy level. :data:`BAND_SEVERITY_RANGES`
still exists, but only as a labelling convention: it keeps the numeric
``composite_score`` shown in the UI and stored in the audit log consistent with
the band the model chose. The band is authoritative; a severity that contradicts
it is clamped, not obeyed.

Every assessment carries a ``breakdown`` of the model's own words, because a
routing decision nobody can explain is not usable in a regulated environment --
and now the explanation is the reasoning that actually drove the decision rather
than a reconstruction of some arithmetic.
"""

from __future__ import annotations

from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, Field

Reversibility: TypeAlias = Literal["reversible", "partially_reversible", "irreversible"]
RegulatoryCategory: TypeAlias = Literal["none", "internal_sensitive", "regulated"]
AutonomyLevel: TypeAlias = Literal["autonomous", "confirm", "full_review"]
RiskBand: TypeAlias = Literal["low", "medium", "high"]

# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

#: The whole routing table. A band maps to exactly one autonomy level -- there
#: is no threshold arithmetic left to tune.
BAND_TO_LEVEL: Final[dict[str, str]] = {
    "low": "autonomous",
    "medium": "confirm",
    "high": "full_review",
}

#: Severity range each band occupies, as (min, max) inclusive. Presentation
#: only: it keeps the number rendered in the UI consistent with the band, and
#: gives the clamp in :func:`build_assessment` something to clamp to.
BAND_SEVERITY_RANGES: Final[dict[str, tuple[float, float]]] = {
    "low": (0.0, 0.29),
    "medium": (0.30, 0.70),
    "high": (0.71, 1.0),
}

#: Used when the model omits a severity, or states one that contradicts its own
#: band. The midpoint is honest about being a stand-in rather than a measurement.
BAND_FALLBACK_SEVERITY: Final[dict[str, float]] = {
    "low": 0.15,
    "medium": 0.50,
    "high": 0.85,
}

#: Decimal places kept on emitted scores. Keeps floating-point noise out of the
#: audit log.
SCORE_PRECISION: Final[int] = 4

#: Order the four dimensions are presented in, in the breakdown and the UI.
DIMENSIONS: Final[tuple[str, ...]] = (
    "reversibility",
    "data_scope",
    "regulatory_category",
    "confidence",
)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class RiskFactors(BaseModel):
    """The model's classification of a proposed action across four dimensions.

    The ``*_reasoning`` fields are the model's own words explaining each call.
    They are what the audit trail shows a human, so they are required rather
    than optional -- a classification with no stated reason is not reviewable.
    """

    reversibility: Reversibility = Field(
        description="Whether the action can be undone once performed.",
    )
    reversibility_reasoning: str = Field(
        default="",
        description="Why the model classified reversibility that way.",
    )
    data_scope: int = Field(
        ge=0,
        description="Number of records or users the action affects.",
    )
    data_scope_reasoning: str = Field(
        default="",
        description="How the model arrived at that record count.",
    )
    regulatory_category: RegulatoryCategory = Field(
        description="Sensitivity class of the data the action touches.",
    )
    regulatory_reasoning: str = Field(
        default="",
        description="Why the data falls in that sensitivity class.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="The model's self-reported confidence that this is the correct action.",
    )
    confidence_reasoning: str = Field(
        default="",
        description="What the model is or is not sure about.",
    )
    risk_band: RiskBand = Field(
        description="The model's overall risk judgement across all four dimensions.",
    )
    severity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional 0-1 severity. Must agree with risk_band or it is clamped.",
    )
    rationale: str = Field(
        default="",
        description="Why those four dimensions together produce this band.",
    )


class RiskAssessment(BaseModel):
    """A judged action: the band that routes it, plus the reasoning behind it."""

    risk_band: RiskBand
    composite_score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Numeric severity consistent with risk_band. Presentational -- routing "
            "uses the band. Named composite_score for audit-log continuity."
        ),
    )
    severity_was_clamped: bool = Field(
        default=False,
        description="True if the model's severity contradicted its band and was overridden.",
    )
    reversibility: Reversibility
    data_scope: int
    regulatory_category: RegulatoryCategory
    confidence: float
    rationale: str = Field(description="The model's overall justification for the band.")
    breakdown: dict[str, str] = Field(
        description="Per-dimension explanation in the model's own words.",
    )
    escalated_by_floor: bool = Field(
        default=False,
        description="True if the blast-radius floor raised this above the model's band.",
    )
    actual_rows: int | None = Field(
        default=None,
        description="True affected-row count, measured from the data before routing.",
    )

    def with_measured_scope(self, actual_rows: int) -> RiskAssessment:
        """Attach the true affected-row count measured before routing.

        Recorded on every action, not only escalated ones. A reviewer approving
        a deletion needs to know it is 112 rows whether or not the engine had to
        override anything to get the decision in front of them.
        """
        return self.model_copy(update={"actual_rows": actual_rows})

    def with_override(self, note: str) -> RiskAssessment:
        """Record that the blast-radius floor escalated this action.

        The band itself is left alone. What the model judged is a fact about
        the model and stays in the record verbatim; the override sits beside it
        rather than quietly replacing it, so an auditor can see both what the
        agent concluded and why the engine disagreed.
        """
        breakdown = dict(self.breakdown)
        breakdown["blast_radius"] = note
        return self.model_copy(
            update={"breakdown": breakdown, "escalated_by_floor": True}
        )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def _clamp_severity(band: str, severity: float | None) -> tuple[float, bool]:
    """Force the severity number to agree with the band the model chose.

    Returns ``(severity, was_clamped)``. A model that reasons its way to "high"
    and then types 0.2 has contradicted itself; the band is the considered
    judgement and the number is decoration, so the band wins and the override is
    recorded rather than hidden.
    """
    low, high = BAND_SEVERITY_RANGES[band]
    if severity is None:
        return BAND_FALLBACK_SEVERITY[band], False
    if severity < low:
        return low, True
    if severity > high:
        return high, True
    return round(severity, SCORE_PRECISION), False


def build_assessment(factors: RiskFactors) -> RiskAssessment:
    """Turn the model's raw self-assessment into the assessment the engine routes on.

    No risk is computed here. This assembles what the model reported into a
    consistent shape, reconciles the severity number with the band, and builds
    the human-readable breakdown.

    Args:
        factors: The model's four classifications, its reasoning for each, and
            the band it concluded.

    Returns:
        A :class:`RiskAssessment` ready for :func:`route_action`.
    """
    severity, was_clamped = _clamp_severity(factors.risk_band, factors.severity)

    def line(label: str, value: object, reasoning: str) -> str:
        return f"{label}: {value}" + (f" -- {reasoning}" if reasoning else "")

    breakdown = {
        "reversibility": line(
            "reversibility", factors.reversibility, factors.reversibility_reasoning
        ),
        "data_scope": line(
            "records affected", factors.data_scope, factors.data_scope_reasoning
        ),
        "regulatory_category": line(
            "sensitivity", factors.regulatory_category, factors.regulatory_reasoning
        ),
        "confidence": line(
            "model confidence", f"{factors.confidence:.2f}", factors.confidence_reasoning
        ),
        "composite": (
            f"model judged overall risk {factors.risk_band.upper()}"
            + (f" -- {factors.rationale}" if factors.rationale else "")
        ),
    }
    if was_clamped:
        breakdown["composite"] += (
            f" [severity {factors.severity} contradicted the "
            f"{factors.risk_band} band and was clamped to {severity}]"
        )

    return RiskAssessment(
        risk_band=factors.risk_band,
        composite_score=severity,
        severity_was_clamped=was_clamped,
        reversibility=factors.reversibility,
        data_scope=factors.data_scope,
        regulatory_category=factors.regulatory_category,
        confidence=factors.confidence,
        rationale=factors.rationale,
        breakdown=breakdown,
    )


def route_action(assessment: RiskAssessment) -> AutonomyLevel:
    """Map a risk band onto an autonomy level.

    This is the model's judgement alone. Callers handling a real action must
    pass the result through :func:`apply_blast_radius_floor` before acting on
    it -- see that function for why.

    Args:
        assessment: The model's judged assessment.

    Returns:
        ``"autonomous"`` (low), ``"confirm"`` (medium), or ``"full_review"`` (high).
    """
    return BAND_TO_LEVEL[assessment.risk_band]  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Blast-radius floor
#
# The model decides the band, and on a correct premise that judgement is the
# right one to route on. But a band is a judgement about *what the action is*,
# and it is made before anyone has checked what the action actually touches. If
# the model believes its filter matches five rows and it really matches fifteen
# thousand, "low" is not a lenient judgement -- it is an answer to a different
# question.
#
# So the true affected-row count, measured from the data by executor.preflight,
# sets a floor on supervision. The floor only ever escalates: it can turn
# autonomous into confirm, never confirm into autonomous. The model can always
# ask for *more* oversight than the floor requires and get it.
#
# This is not the old weighted formula coming back. It does not score anything
# and it cannot lower supervision. It encodes one fact: a change to thousands
# of rows is not something a machine should be able to wave through by
# describing it as small.
# --------------------------------------------------------------------------

#: Most rows a mutation may change while still running with no human at all.
#: One row is deliberately strict -- "change this specific invoice" is the only
#: mutation shape that is genuinely bounded.
AUTONOMOUS_MAX_MUTATION_ROWS: Final[int] = 1

#: Most rows a mutation may change on a one-click confirmation. Above this, a
#: reviewer should be reading it properly rather than clicking through.
CONFIRM_MAX_MUTATION_ROWS: Final[int] = 100

#: Supervision levels, least to most supervised. Used to take a maximum.
LEVEL_ORDER: Final[tuple[str, ...]] = ("autonomous", "confirm", "full_review")


def scope_floor(
    *,
    actual_rows: int,
    is_mutation: bool,
    is_destructive: bool = False,
    resolvable: bool = True,
) -> AutonomyLevel:
    """The least supervision this action may run under, given what it really does.

    Two independent axes, because size and destructiveness are different kinds
    of danger. Updating one row is recoverable by updating it back; deleting one
    row is not recoverable by any subsequent agent action. A pure row-count
    floor rates those identically, and a live run showed exactly what that
    costs: the agent banded "delete invoice I317333" as low risk -- defensibly,
    on its own terms -- and one row was destroyed with no human involved.

    So deletion never runs unattended, at any size, and size escalates from
    there.

    Args:
        actual_rows: True affected-row count, from :func:`executor.preflight`.
        is_mutation: Whether the action changes data. Reads are never escalated
            by size: they destroy nothing and are capped at the executor.
        is_destructive: Whether the action deletes rows outright.
        resolvable: False if the filter could not be resolved. An action whose
            blast radius is *unknown* is treated as maximally risky -- not as
            zero rows, which would look harmless and sail straight through.

    Returns:
        The minimum autonomy level permitted.
    """
    if not resolvable:
        return "full_review"
    if not is_mutation:
        return "autonomous"

    by_size: AutonomyLevel = "autonomous"
    if actual_rows > CONFIRM_MAX_MUTATION_ROWS:
        by_size = "full_review"
    elif actual_rows > AUTONOMOUS_MAX_MUTATION_ROWS:
        by_size = "confirm"

    # Destruction sets its own minimum, then the stricter of the two wins.
    by_kind: AutonomyLevel = "confirm" if is_destructive else "autonomous"

    return max(by_size, by_kind, key=LEVEL_ORDER.index)  # type: ignore[return-value]


def apply_blast_radius_floor(
    decision: AutonomyLevel,
    *,
    actual_rows: int,
    is_mutation: bool,
    is_destructive: bool = False,
    resolvable: bool = True,
) -> tuple[AutonomyLevel, str | None]:
    """Raise a routing decision to meet the floor its true blast radius demands.

    Returns:
        ``(final_decision, note)``. ``note`` is ``None`` when the model's own
        decision already met the floor, and otherwise explains the escalation
        for the audit trail -- an override that nobody can see afterwards is
        not much better than no override at all.
    """
    floor = scope_floor(
        actual_rows=actual_rows,
        is_mutation=is_mutation,
        is_destructive=is_destructive,
        resolvable=resolvable,
    )
    if LEVEL_ORDER.index(floor) <= LEVEL_ORDER.index(decision):
        return decision, None

    noun = "row" if actual_rows == 1 else "rows"
    if not resolvable:
        reason = "its true scope could not be determined"
    elif is_destructive and actual_rows <= AUTONOMOUS_MAX_MUTATION_ROWS:
        reason = f"it permanently deletes {actual_rows:,} {noun}, which is never unattended"
    elif is_destructive:
        reason = f"it permanently deletes {actual_rows:,} {noun}"
    else:
        reason = f"it actually changes {actual_rows:,} {noun}"
    note = (
        f"escalated {decision} -> {floor}: the agent judged this "
        f"{decision}, but {reason}"
    )
    return floor, note


def describe_routing(assessment: RiskAssessment, decision: AutonomyLevel) -> str:
    """One-line, presenter-friendly summary of why an action was routed as it was."""
    reasons = {
        "autonomous": "runs without a human",
        "confirm": "needs a one-click confirmation",
        "full_review": "is blocked pending human review",
    }
    summary = f"model judged this {assessment.risk_band} risk, so it {reasons[decision]}"
    if assessment.rationale:
        summary += f": {assessment.rationale}"
    return f"{summary} -> {decision}"
