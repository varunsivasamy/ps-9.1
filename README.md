# PS-9.1 — Graduated Autonomy Engine

A risk-routing layer for AI agents. Today an agent action is either fully
autonomous or fully gated by a human — there is no middle. PS-9.1 asks the agent
to reason about the risk of every action it proposes, across four dimensions
(reversibility, data scope, regulatory category, and its own confidence), and to
state a risk band. That band routes the action: **autonomous** for low risk,
**confirm** — preview then one-click approval — for medium, and **full review**
by a human for high.

Two things make this more than a classifier:

- **The judgement is the model's, not a formula's.** Earlier revisions applied
  fixed weights to the four dimensions. A weighted sum cannot tell the
  difference between deleting 200 rows of marketing preferences and deleting 200
  rows of medical records — they score identically — so the call now sits with
  the model that can read the request. The model writes down its reasoning per
  dimension, and that reasoning is what the audit trail shows.
- **The actions are real.** Low-risk actions execute immediately against a
  99,457-row retail transaction database; medium and high-risk ones execute only
  once a human approves, and never if they are rejected. Every mutation
  snapshots the data first, so "reversible" is a property of the system rather
  than a label the model typed.

Every routing decision is persisted with the model's full reasoning, so any
decision the system made can be explained after the fact.

---

## Architecture

```
                          +------------------------------+
   user request  -------> |  POST /actions/propose       |
                          |  (FastAPI, src/.../main.py)  |
                          +---------------+--------------+
                                          |
                          +---------------v--------------+
                          | agent_actions.propose_action |
                          | Anthropic API + tool use     |
                          | -> AgentAction (+confidence) |
                          +---------------+--------------+
                                          |
                          +---------------v--------------+
                          | the agent reasons across     |
                          |   reversibility              |
                          |   data_scope                 |
                          |   regulatory_category        |
                          |   confidence                 |
                          | -> risk_band + its reasoning |
                          +---------------+--------------+
                                          |
                          +---------------v--------------+
                          | risk_scorer.route_action     |
                          |   low     -> autonomous      |
                          |   medium  -> confirm         |
                          |   high    -> full_review     |
                          +---+-----------+----------+---+
                              |           |          |
                  +-----------v--+ +------v-----+ +--v-------------+
                  | execute now  | | confirmation| | human review  |
                  | auto_executed| | queue       | | queue         |
                  +-----------+--+ +------+-----+ +--+-------------+
                              |           |          |
                              |    executed only once a human approves
                              |           |          |
                          +---v-----------v----------v---+
                          | executor -> data_store       |
                          |   customer_shopping_data.csv |
                          |   + pre-write snapshot       |
                          +---------------+--------------+
                                          |
                          +---v-----------v----------v---+
                          | audit_store (DynamoDB)       |
                          |   <prefix>-audit-log         |
                          |   PK session_id / SK timestamp|
                          |   full risk breakdown per row|
                          +---------------+--------------+
                                          |
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
  risk_scorer.py     # band -> autonomy level routing, zero AWS dependency
  agent_actions.py   # Anthropic call; agent proposes an action + judges its risk
  calibration.py     # adaptive learning: nudges routing from human history
  data_store.py      # the transaction CSV: filters, writes, snapshots, rollback
  executor.py        # runs an already-authorised action against the store
  audit_store.py     # DynamoDB persistence for the audit log
  confirmation.py    # confirmation / review queues; the only doors to executor
  main.py            # FastAPI app
  lambda_handler.py  # Mangum adapter for AWS Lambda
data/
  customer_shopping_data.csv   # 99,457 retail transactions -- the live database
tests/               # pytest suite
tests/fixtures/      # 300-row sample of the above, used by the suite
infra/               # AWS SAM template + config
scripts/             # deploy helpers
docs/                # demo script
frontend/            # React + TypeScript console (Vite)
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
# -> http://127.0.0.1:8000/docs
```

---

## Front end

`frontend/` is a Vite + React + TypeScript console for the API: send a
request, watch it get routed (autonomous / confirm / full review) with the
full risk breakdown, confirm or approve/reject pending actions, and browse
the live audit trail for a session.

```bash
cd frontend
npm install
cp .env.example .env.local     # VITE_API_BASE_URL defaults to localhost:8000
npm run dev                    # -> http://localhost:5173
```

It talks to the FastAPI backend directly (`/actions/propose`,
`/confirmations/{id}/resolve`, `/reviews/{id}/resolve`, `/audit/{session_id}`,
`/health`), so run `uvicorn autonomy_engine.main:app --reload` alongside it.
The backend's CORS allow-list (`CORS_ALLOWED_ORIGINS` in `.env`) already
includes the Vite dev origin; add your Vercel domain there once deployed.

---

## Risk model

The agent reasons across four dimensions and states a band. There is no
arithmetic between the two — the band it chooses is the routing decision.

| Dimension | What the agent reports |
|---|---|
| `reversibility` | reversible / partially_reversible / irreversible, plus why |
| `data_scope` | how many records its filter will touch, plus how it got there |
| `regulatory_category` | none / internal_sensitive / regulated, naming the regime if one applies |
| `confidence` | `0.0`-`1.0` that this is the right action, plus what it is unsure about |
| **`risk_band`** | **low / medium / high — weighing all four, with a one-line rationale** |

| Band | Routes to | Behaviour |
|---|---|---|
| `low` | `autonomous` | Executes immediately, no human involved |
| `medium` | `confirm` | Queued; executes on one-click confirmation |
| `high` | `full_review` | Blocked; executes only on explicit approval |

The agent also gives a `severity` number for display. If it contradicts the band
(band `high`, severity `0.05`), the band wins and the override is recorded — the
band is the considered judgement, the number is decoration.

## Making the agent right the first time

The floor below is a net. It is better for the agent not to fall into it, so two
things are done to the prompt before any of that engages.

**It can ask.** The agent has a sixth tool, `count_matching_rows`, alongside the
five actions. It is read-only, performs nothing, and answers one question: how
many rows does this filter match? The agent may call it before committing, get a
real number back, and reason from that:

```
agent  -> count_matching_rows [category=Souvenir, shopping_mall=Kanyon]
engine -> "That filter matches 1,037 of 99,457 rows (1.0%). Use this as data_scope."
agent  -> bulk_delete_transactions [same filter], data_scope 1037, risk_band HIGH
```

This is the difference between an agent that estimates and one that knows.
Putting the counts in the prompt (below) was not enough on its own, because the
failure was never missing information — asked about "Cosmetics paid in cash" the
model answered 15,109, essentially the Cosmetics total, having ignored the second
half of its own filter. It was doing arithmetic over a conjunction and getting it
wrong. Now it does not have to: it runs the filter and is told.

A malformed filter comes back as an error message rather than an exception, so
the agent sees the problem and can correct it — before that filter is attached to
a deletion. The loop is capped at `MAX_PLANNING_TURNS` lookups and only exits on
a committed action, so an agent that never decides fails loudly instead of
hanging the request.

Note that `count_matching_rows` carries no `self_assessment`. It is a question,
not an action; requiring a risk judgement in order to ask would defeat the point.
It never appears in the audit vocabulary or the executor's handler table.

**It is given the real cardinalities.** The system prompt also carries the
measured size of the table and of every categorical value in it:

```
TOTAL ROWS: 99,457
category: Clothing=34,487, Cosmetics=15,097, Food & Beverage=14,776, Toys=10,087,
          Shoes=10,034, Souvenir=4,999, Technology=4,996, Books=4,981
shopping_mall: Mall of Istanbul=19,943, Kanyon=19,823, Metrocity=15,011, ...
```

These give it the shape of the table for single-column filters without spending a
lookup. They are read from the live file and cached against its size and mtime,
so a bulk delete that changes them invalidates the cache rather than leaving the
agent reasoning about a table it just shrank.

**It is given worked examples.** Six few-shot cases covering each tool, each
showing the reasoning rather than just the answer. Every one targets a failure
actually observed on a live run — estimating 30,000 rows for a 4,996-row
category, estimating a round 100 for a 1,037-row filter, and banding a
single-row deletion as low risk. One example deliberately has no answer
("clean up the bad records") to teach that an unwritable filter means low
confidence and a high band, not a guess.

The prompt also states one rule that overrides the agent's own judgement:
**deletion is never low risk, at any size.**

---

## Why the band alone is not enough

A band is the model's judgement about *what an action is*. It is formed before
anyone has checked what the action actually touches. If the model believes its
filter matches five rows and it really matches fifteen thousand, `low` is not a
lenient judgement — it is a correct answer to the wrong question.

So every proposed action passes through three steps before it is routed.

**1. Measure it.** `executor.preflight()` resolves the filter against the real
data and returns the true affected-row count. It is strictly read-only — it
counts, never writes, and takes no snapshot — so it is safe to run on an action
a human may still reject. A filter that cannot be resolved comes back as
*unknown*, never as "0 rows"; zero looks harmless and would sail through, and
"we don't know what this does" is strictly worse than "this does nothing".

**2. Re-judge it.** If the estimate was materially wrong (off by more than half),
the engine hands the model the true number and asks it to judge again. The
judgement stays the model's; only its premise is corrected. A failed re-judgement
keeps the original band — it can never lower supervision.

**3. Floor it.** The true blast radius sets a *minimum* supervision level:

| What it really does | Lowest level allowed |
|---|---|
| Any read, any size | `autonomous` |
| Edits 1 row | `autonomous` |
| Edits 2–100 rows | `confirm` |
| Edits >100 rows | `full_review` |
| **Deletes anything, at any size** | `confirm` |
| Deletes >100 rows | `full_review` |
| True scope unknown | `full_review` |

The floor **only ever escalates**. It cannot turn `confirm` into `autonomous`,
so a cautious model always gets the supervision it asked for. That invariant is
tested against every combination — if the floor could de-escalate, it would be a
way to launder a cautious band into a permissive one, which is worse than having
no floor at all.

This is not the old weighted formula returning. It scores nothing and it cannot
reduce oversight. It encodes one fact: **a change to thousands of rows is not
something a machine should be able to wave through by describing it as small.**

When the floor overrides the agent, the agent's original band stays in the audit
record verbatim, with the override recorded beside it — so a reviewer sees both
what the agent concluded and why the engine disagreed. The API returns
`escalated_by_floor` and `actual_rows` on every proposal.

### Two things live runs caught

Both of these were found by pointing the real model at the real data, not by
reasoning about it:

- The agent banded *"delete invoice I317333, it was entered by mistake"* as
  **low** risk — defensibly, on its own terms: one row, unambiguous request — and
  the row was destroyed with no human involved. Row count alone rated a delete
  the same as an edit. Deletion is now its own axis, and never runs unattended at
  any size.
- Asked to *"remove all Technology purchases"*, the agent estimated 30,000 rows.
  The filter it wrote was correct and matched exactly the intended 4,996. Without
  preflight, the band would have been reasoned from a number that was off by 6×.

### Also enforced

- **Unbounded delete guard.** A bulk delete arriving with no filter criteria is
  refused outright rather than emptying the table.
- **Read cap.** Row queries return at most 25 rows and say so — a read is safe to
  perform, but an unbounded result set is still a way to pull the table in one
  call.

---

## The data and the tools

`data/customer_shopping_data.csv` holds 99,457 real retail transactions — the
database the agent's tools actually read and write. One row per invoice:

```
invoice_no,customer_id,gender,age,category,quantity,price,payment_method,invoice_date,shopping_mall
I138884,C241288,Female,28,Clothing,5,1500.4,Credit Card,5/8/2022,Kanyon
```

A CSV on purpose: you can open it, approve a deletion in the UI, and watch the
rows disappear.

Two details of this file that the code has to respect:

- **Dates are day-first `DD/MM/YYYY` with inconsistent padding** (`5/8/2022` and
  `16/05/2021` both appear). Filters are written in ISO `YYYY-MM-DD` and parsed
  day-first on the way in, because reading `05/08` as 5 August instead of 8 May
  would silently select the wrong rows — the worst failure available here.
- **Categorical columns have fixed vocabularies.** The agent is given the valid
  values for `gender`, `category`, `payment_method`, and `shopping_mall` in its
  system prompt, so it filters on `"Food & Beverage"` rather than inventing
  `"food"` and quietly matching nothing.

### Tools

| Tool | Rows touched | Typical band |
|---|---|---|
| `count_matching_rows` | read, returns a count only | n/a — not an action |
| `query_transactions` | read, capped at 25 returned | low |
| `summarize_transactions` | read, aggregates to totals | low |
| `update_transaction` | exactly 1 | medium |
| `delete_transaction` | exactly 1, by `invoice_no` | medium |
| `bulk_delete_transactions` | 1 to ~99,000, by filter | high |

The two deletes are separate tools rather than one tool with an optional filter.
"Remove invoice I138884" and "remove every Clothing row" are one row versus
34,487, and putting that distinction in the *tool name* means the audit log and
the human reading the preview can both see it, instead of it being buried in a
filter argument nobody reads closely.

`summarize_transactions` exists for the same reason the read cap does: answering
"which category made the most revenue?" by returning 34,487 rows would be
useless, so it returns one number per group instead.

### Rollback

Before any mutation the whole file is copied to
`data/snapshots/<audit_record_id>.csv`, so every change in the audit log has a
named rollback point:

```python
from autonomy_engine import executor
executor.rollback("<audit_record_id>")     # restores the pre-action state
```

To reset the data entirely: `git checkout data/customer_shopping_data.csv`.

Tests never touch this file. `conftest.py` hands every test its own throwaway
copy of a 300-row sample (`tests/fixtures/shopping_sample.csv`) through an
autouse fixture — copying 7.2 MB per test across ~210 tests would spend over a
gigabyte of I/O proving things that hold just as well on 300 rows.

---

## Adaptive calibration

Every human decision on a queued action is a signal. If the same `action_type`
gets confirmed by reviewers over and over without modification, the engine
learns to route it more permissively; if it keeps getting rejected, the engine
learns to route it more strictly.

The counters live in a JSON file (`data/action_type_calibration.json` by
default, `CALIBRATION_PATH` to override — point it at `/tmp/…` on Lambda):

```json
{
  "single_record_write": {
    "confirms_without_modification": 12,
    "rejects_or_modifications": 1,
    "band_offset": -1.0
  }
}
```

- Net signal below `MIN_SIGNALS_FOR_SHIFT` (10) → no change. A lucky handful
  cannot flip routing on a novel action type.
- Net ≥ 10 confirms → routing shifts one step toward autonomous.
- Net ≥ 10 rejects → routing shifts one step toward full_review.
- The shift is capped to one level per call — an action_type with 100 net
  confirms moves the same distance as one with 10.

Two invariants make this safe to leave running:

- **Calibration runs before the blast-radius floor.** A bulk delete calibrated
  down to `autonomous` still gets re-escalated to `full_review` by the floor,
  because scope is a fact and does not care about history. Safety cannot be
  trained away.
- **Every shift is recorded in the audit breakdown** with the exact counts
  that produced it, so a reviewer sees the shift came from history rather than
  from the model changing its mind.

Inspect the live table via `GET /calibration`.

---

Status: Phase 4 complete, plus LLM-judged risk banding, preflight scope
grounding with re-judgement, an escalate-only blast-radius floor, a live
execution layer over the 99k-row transaction dataset, and adaptive calibration
learning from human confirm/reject signals.
