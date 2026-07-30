# PS-9.1 — Graduated Autonomy Engine

A risk-routing layer for AI agents. Today an agent action is either fully
autonomous or fully gated by a human — there is no middle. PS-9.1 dynamically
scores every proposed agent action on a risk dimension (reversibility, data
scope, regulatory category, and the model's own self-reported confidence) and
routes it to the appropriate autonomy level: **autonomous** for low risk,
**confirm** — preview then one-click approval — for medium risk, and **full
review** by a human for high risk. Every routing decision is persisted with its
complete risk score breakdown, so any decision the system made can be explained
after the fact.

---

## Architecture

```
                          ┌──────────────────────────────┐
   user request  ───────► │  POST /actions/propose        │
                          │  (FastAPI, src/.../main.py)   │
                          └───────────────┬───────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │  agent_actions.propose_action │
                          │  Anthropic API + tool use     │
                          │  → AgentAction (+ confidence) │
                          └───────────────┬───────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │  risk_scorer.score_action     │
                          │  reversibility     35%        │
                          │  data_scope        25%        │
                          │  regulatory        25%        │
                          │  1 - confidence    15%        │
                          │  → composite_score + breakdown│
                          └───────────────┬───────────────┘
                                          │
                          ┌───────────────▼───────────────┐
                          │  risk_scorer.route_action     │
                          │  score < 0.3   → autonomous   │
                          │  0.3 – 0.7     → confirm      │
                          │  score > 0.7   → full_review  │
                          └───┬───────────┬───────────┬───┘
                              │           │           │
                  ┌───────────▼──┐ ┌──────▼──────┐ ┌──▼─────────────┐
                  │ execute now  │ │ confirmation│ │ human review   │
                  │ auto_executed│ │ queue       │ │ queue          │
                  └───────────┬──┘ └──────┬──────┘ └──┬─────────────┘
                              │           │           │
                          ┌───▼───────────▼───────────▼───┐
                          │  audit_store (DynamoDB)       │
                          │  <prefix>-audit-log           │
                          │  PK session_id / SK timestamp │
                          │  full risk breakdown per row  │
                          └───────────────┬───────────────┘
                                          │
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
# → http://127.0.0.1:8000/docs
```

---

## Risk model

| Dimension | Weight | Scoring |
|---|---|---|
| `reversibility` | 35% | reversible `0.1` · partially_reversible `0.5` · irreversible `0.9` |
| `data_scope` | 25% | 1 record `0.1` · 2–10 `0.3` · 11–100 `0.6` · 100+ `0.9` |
| `regulatory_category` | 25% | none `0.1` · internal_sensitive `0.5` · regulated `0.9` |
| `confidence` | 15% | `1 - confidence` — low model confidence means high risk |

Weights are named constants at the top of `risk_scorer.py` and routing
thresholds (`low=0.3`, `high=0.7`) are configurable per call.

---

Status: Phase 1 complete
