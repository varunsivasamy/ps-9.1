import type { RiskScorePayload, RoutingDecision } from "../types";

const LABELS: Record<RoutingDecision, string> = {
  autonomous: "Autonomous — low risk",
  confirm: "Needs confirmation — medium risk",
  full_review: "Needs full review — high risk",
};

export function routingLabel(decision: RoutingDecision): string {
  return LABELS[decision];
}

interface RiskBreakdownProps {
  score: RiskScorePayload;
}

const DIMENSION_ORDER = ["reversibility", "data_scope", "regulatory_category", "confidence", "composite"];
const DIMENSION_LABELS: Record<string, string> = {
  reversibility: "Reversibility",
  data_scope: "Data scope",
  regulatory_category: "Regulatory category",
  confidence: "Model confidence",
  composite: "Overall judgement",
};

export function RiskBreakdown({ score }: RiskBreakdownProps) {
  const entries = DIMENSION_ORDER.filter((key) => key in score.breakdown);

  return (
    <div className="risk-breakdown">
      {/*
        The band leads and the number follows it, because the band is what
        actually routed the action -- the score is a consistent-looking
        rendering of that judgement, not the thing that made it.
      */}
      <div className="risk-breakdown__score">
        <span className="risk-breakdown__band" data-band={score.risk_band}>
          {score.risk_band}
        </span>
        <span className="risk-breakdown__score-value">{score.composite_score.toFixed(2)}</span>
        <span className="risk-breakdown__score-label">agent's risk judgement</span>
      </div>

      {score.rationale && <p className="risk-breakdown__rationale">{score.rationale}</p>}

      {score.severity_was_clamped && (
        <p className="risk-breakdown__notice">
          The model's severity number disagreed with the band it chose; the band was kept.
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
