# PS-9.1 â€” Graduated Autonomy Engine

A risk-routing layer for AI agents. Today an agent action is either fully
autonomous or fully gated by a human â€” there is no middle. PS-9.1 dynamically
scores every proposed agent action on a risk dimension (reversibility, data
scope, regulatory category, and the model's own self-reported confidence) and
routes it to the appropriate autonomy level: **autonomous** for low risk,
**confirm** â€” preview then one-click approval â€” for medium risk, and **full
review** by a human for high risk. Every routing decision is persisted with its
complete risk score breakdown, so any decision the system made can be explained
after the fact.

---

## Architecture

```
                          â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
   user request  â”€â”€â”€â”€â”€â”€â”€â–º â”‚  POST /actions/propose        â”‚
                          â”‚  (FastAPI, src/.../main.py)   â”‚
                          â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                          â”‚
                          â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                          â”‚  agent_actions.propose_action â”‚
                          â”‚  Anthropic API + tool use     â”‚
                          â”‚  â†’ AgentAction (+ confidence) â”‚
                          â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                          â”‚
                          â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                          â”‚  risk_scorer.score_action     â”‚
                          â”‚  reversibility     35%        â”‚
                          â”‚  data_scope        25%        â”‚
                          â”‚  regulatory        25%        â”‚
                          â”‚  1 - confidence    15%        â”‚
                          â”‚  â†’ composite_score + breakdownâ”‚
                          â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                          â”‚
                          â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                          â”‚  risk_scorer.route_action     â”‚
                          â”‚  score < 0.3   â†’ autonomous   â”‚
                          â”‚  0.3 â€“ 0.7     â†’ confirm      â”‚
                          â”‚  score > 0.7   â†’ full_review  â”‚
                          â””â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”˜
                              â”‚           â”‚           â”‚
                  â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â” â”Œâ”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â” â”Œâ”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                  â”‚ execute now  â”‚ â”‚ confirmationâ”‚ â”‚ human review   â”‚
                  â”‚ auto_executedâ”‚ â”‚ queue       â”‚ â”‚ queue          â”‚
                  â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”˜ â””â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”˜ â””â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                              â”‚           â”‚           â”‚
                          â”Œâ”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–¼â”€â”€â”€â”
                          â”‚  audit_store (DynamoDB)       â”‚
                          â”‚  <prefix>-audit-log           â”‚
                          â”‚  PK session_id / SK timestamp â”‚
                          â”‚  full risk breakdown per row  â”‚
                          â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
                                          â”‚
                                  GET /audit/{session_id}
                                  "show the receipts"
```

Deployment target: a single AWS Lambda (FastAPI wrapped in Mangum) behind an
HTTP API, with a PAY_PER_REQUEST DynamoDB table for the audit log. See
`infra/template.yaml`.

---

## Layout

```
src/autonomy_engine/
  risk_scorer.py     # pure scoring + routing logic, zero AWS dependency
  agent_actions.py   # Anthropic call; agent proposes an action + confidence
  audit_store.py     # DynamoDB persistence for the audit log
  confirmation.py    # confirmation / review queue resolution
  main.py            # FastAPI app
  lambda_handler.py  # Mangum adapter for AWS Lambda
tests/               # pytest suite
infra/               # AWS SAM template + config
scripts/             # deploy helpers
docs/                # demo script
```

---

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in ANTHROPIC_API_KEY
```

Run the tests:

```bash
pytest                             # unit tests, no API key or AWS needed
pytest -m integration              # live Anthropic calls, needs ANTHROPIC_API_KEY
```

Run the API locally:

```bash
uvicorn autonomy_engine.main:app --reload
# â†’ http://127.0.0.1:8000/docs
```

---

## Risk model

| Dimension | Weight | Scoring |
|---|---|---|
| `reversibility` | 35% | reversible `0.1` Â· partially_reversible `0.5` Â· irreversible `0.9` |
| `data_scope` | 25% | 1 record `0.1` Â· 2â€“10 `0.3` Â· 11â€“100 `0.6` Â· 100+ `0.9` |
| `regulatory_category` | 25% | none `0.1` Â· internal_sensitive `0.5` Â· regulated `0.9` |
| `confidence` | 15% | `1 - confidence` â€” low model confidence means high risk |

Weights are named constants at the top of `risk_scorer.py` and routing
thresholds (`low=0.3`, `high=0.7`) are configurable per call.

---

Status: Phase 3 complete
