import type { RiskScorePayload, RoutingDecision } from "../types";

const LABELS: Record<RoutingDecision, string> = {
  autonomous:  "Autonomous — low risk",
  confirm:     "Confirmation needed — medium risk",
  full_review: "Full review required — high risk",
};

export function routingLabel(decision: RoutingDecision): string {
  return LABELS[decision];
}

interface RiskBreakdownProps {
  score: RiskScorePayload;
}

const DIMENSION_ORDER = [
  "reversibility",
  "data_scope",
  "regulatory_category",
  "confidence",
  "composite",
  "blast_radius",
];

const DIMENSION_LABELS: Record<string, string> = {
  reversibility:       "Reversibility",
  data_scope:          "Data scope",
  regulatory_category: "Regulatory",
  confidence:          "Confidence",
  composite:           "Overall judgement",
  blast_radius:        "⚠ Blast radius override",
};

export function RiskBreakdown({ score }: RiskBreakdownProps) {
  const entries = DIMENSION_ORDER.filter((k) => k in score.breakdown);

  return (
    <div className="risk-breakdown">
      <div className="risk-breakdown__score">
        <span className="risk-breakdown__band" data-band={score.risk_band}>
          {score.risk_band}
        </span>
        <span className="risk-breakdown__score-value">
          {score.composite_score.toFixed(2)}
        </span>
        <span className="risk-breakdown__score-label">risk score</span>
        {score.actual_rows != null && (
          <span className="risk-breakdown__score-label">
            {score.actual_rows.toLocaleString()} rows measured
          </span>
        )}
      </div>

      {score.rationale && (
        <p className="risk-breakdown__rationale">{score.rationale}</p>
      )}

      {score.severity_was_clamped && (
        <p className="risk-breakdown__notice">
          Model severity contradicted its own band — band was kept.
        </p>
      )}

      {score.escalated_by_floor && (
        <p className="risk-breakdown__notice">
          Routing was escalated by the blast-radius floor.
        </p>
      )}

      <dl className="risk-breakdown__list">
        {entries.map((key) => (
          <div key={key} className="risk-breakdown__row">
            <dt>{DIMENSION_LABELS[key] ?? key}</dt>
            <dd>{score.breakdown[key]}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
