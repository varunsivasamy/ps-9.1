"""Agent action layer -- the agent proposes an action and grades its own risk.

This is where the LLM connection is organic rather than bolted on. Instead of
inferring risk from the outside, we ask the model to pick a tool *and*, in the
same structured tool call, classify what it is about to do: is it reversible, how
many records does it touch, is the data regulated, and how confident is it that
this is the right action at all? Those four values feed straight into
:func:`autonomy_engine.risk_scorer.build_assessment`, and the band it states
is what routes the action.

Getting the self-assessment as part of the tool input (rather than as a second
call, or parsed out of prose) means one round trip and no free-text parsing.

The model
---------
Groq, via its OpenAI-compatible chat completions API. There is one provider and
one code path: an earlier revision carried a second Anthropic implementation
alongside this one, which meant every change to the planning loop, the schemas,
or the retry policy had to be made twice and kept in sync by hand.

Three things the agent can do here, in increasing order of commitment:

    count_matching_rows    ask how many rows a filter matches. Changes nothing.
    request_clarification  say the request cannot be safely interpreted, and ask
                           the user a question. Proposes nothing, executes nothing.
    <the five actions>     commit to an action, with a risk self-assessment.

The middle one matters as much as the gating does. Without it, a request the
agent genuinely cannot pin down ("clean up the bad records") still produces a
*guess* -- an action with a filter nobody asked for, banded high and parked in a
review queue for a human to reject. Asking is the better answer, and it is
available before any of the supervision machinery has to engage.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Final

import groq
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from autonomy_engine import data_store
from autonomy_engine.data_store import FIELD_VALUES
from autonomy_engine.data_store import FIELDS as CUSTOMER_FIELDS
from autonomy_engine.risk_scorer import (
    RegulatoryCategory,
    Reversibility,
    RiskBand,
    RiskFactors,
)

# Load .env for local development. In Lambda the variables come from the
# function's environment and there is no .env file -- this is a no-op there.
load_dotenv()

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

#: Model used to propose actions. Overridable so a deployment can pin a
#: different model -- or move to whatever Groq is serving next -- without a code
#: change.
MODEL: Final[str] = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

#: Ceiling on response tokens. Generous relative to a single small tool call so
#: a verbose assessment can't truncate mid-call.
MAX_TOKENS: Final[int] = 8192

#: Zero temperature: the same request should route the same way twice. Risk
#: banding that varies run to run is not something a reviewer can trust.
TEMPERATURE: Final[float] = 0.0

#: The plan calls for exactly one retry, so the SDK's own retry loop is disabled
#: (see ``_client``) and retries are handled here instead.
MAX_ATTEMPTS: Final[int] = 2
RETRY_DELAY_SECONDS: Final[float] = 1.0

_FIELD_VALUE_HELP: Final[str] = "\n".join(
    f"    {field:<15} {' | '.join(values)}" for field, values in FIELD_VALUES.items()
)

#: Columns whose real row counts are worth showing the agent: the ones requests
#: most often filter on, and whose blast radius cannot be guessed from the text.
_CARDINALITY_FIELDS: Final[tuple[str, ...]] = (
    "category",
    "shopping_mall",
    "payment_method",
)


def _cardinality_help() -> str:
    """Real per-value row counts, so scope is a lookup rather than a guess.

    Reads the live data so it stays true after a delete changes the counts.
    Degrades to a note rather than raising: an agent without cardinalities is
    worse at estimating, but an engine that will not start because a CSV moved
    is worse still. Preflight and the blast-radius floor both still apply, so
    this is an accuracy feature and not a safety control.
    """
    try:
        total = len(data_store.load_rows())
    except Exception:  # noqa: BLE001 - prompt building must never be fatal
        logger.warning("could not read data for prompt cardinalities", exc_info=True)
        return "    (row counts unavailable -- estimate conservatively)"

    lines = [f"    TOTAL ROWS: {total:,}"]
    for field in _CARDINALITY_FIELDS:
        try:
            counts = data_store.distribution(field)
        except Exception:  # noqa: BLE001
            continue
        rendered = ", ".join(f"{value}={count:,}" for value, count in counts.items())
        lines.append(f"    {field}: {rendered}")
    return "\n".join(lines)


SYSTEM_PROMPT: Final[str] = f"""\
You are an AI agent operating inside a graduated autonomy engine. You have access \
to tools that operate on a real retail transaction database of roughly 99,000 \
rows. Choose exactly one tool call that fulfils the user's request.

Each row is one invoice, with these fields:
    {", ".join(CUSTOMER_FIELDS)}

`invoice_no` is the primary key -- one row per invoice. These columns only ever \
hold these values, so match them exactly:
{_FIELD_VALUE_HELP}

This is the actual size of the table and of each value in it. Work out how many \
rows your filter will touch from these numbers -- they are measured facts, not \
hints:
{{CARDINALITIES}}

Tools that select rows take a `filter`: a list of {{field, operator, value}} \
criteria, ANDed together. This filter is executed literally against the database, \
so it must express the request precisely. Write dates as ISO `YYYY-MM-DD`. Use \
`before`/`after` only on invoice_date, and `greater_than`/`less_than` only on \
age, quantity, or price.

An empty filter matches all ~99,000 rows. Bulk deletion with an empty filter is \
refused outright.

Scale is the thing to be careful about. Deleting one named invoice touches one \
row; deleting every Clothing transaction touches over thirty thousand. Before \
you choose a band, think about how many rows your filter really matches -- a \
broad category or mall filter matches thousands, not a handful.

WHEN NOT TO ACT AT ALL

You are not obliged to propose an action. If the request cannot be turned into a \
filter you can defend, call `request_clarification` instead and ask the user what \
they meant. Nothing runs, nothing is queued, and they answer and come back.

Ask when:
- the request names a set you cannot express in these columns ("the bad records", \
  "the duplicates", "the test data") -- these are not fields and you cannot infer them;
- it is ambiguous between two readings that touch very different rows ("last \
  year's Kanyon sales" -- 2021 or 2022?);
- it names a value that is not in the vocabularies above and you would have to \
  guess the mapping ("delete the electronics" -- is that Technology?);
- it refers to something outside this conversation ("the invoice from before").

Do NOT ask when the request is clear enough to act on and the only issue is that \
it is large or destructive. Scale and danger are what the risk band is for -- a \
clear instruction to delete 30,000 rows is a HIGH-band action, not a question. \
Asking there just moves work onto the user and slows down a decision they have \
already made. One question, then commit.

Alongside the tool's own parameters, every tool requires a `self_assessment` object. \
This is not paperwork -- it is what determines whether your action runs \
automatically, pauses for a one-click human confirmation, or is blocked pending \
full human review. YOU make that call, not a formula.

Reason through all four dimensions and write down your reasoning for each:

- reversibility: can this be undone afterwards?
    "reversible"             a read, or a change that can be cleanly rolled back
    "partially_reversible"   a change that can be reverted but may lose history
    "irreversible"           a deletion or external side effect that cannot be undone
- data_scope: how many rows the action affects. A single named invoice is 1.
  For a filter, derive it from the row counts above rather than guessing -- those
  counts are the real table. Do not round to a comfortable figure like 100 or
  30000; work it out. Understating this is the dangerous error, because it is
  what you then use to judge the band.
- regulatory_category: the sensitivity of the data involved.
    "none"                non-sensitive business data
    "internal_sensitive"  internal or personal data, not externally regulated
    "regulated"           data under a regime such as GDPR, HIPAA, or PCI
- confidence: 0.0-1.0, how sure you are that this specific tool call is the
  correct interpretation of the request. Understating your uncertainty is the
  more dangerous error: low confidence should pull a human in.

Then weigh those four together and decide `risk_band` yourself:

    "low"     safe to execute with no human involvement
    "medium"  a human should see a preview and confirm before it runs
    "high"    must be blocked until a human explicitly approves it

The dimensions inform this judgement; they do not mechanically determine it. A \
small, reversible change to regulated data may still be low risk. A large but \
purely read-only query may also be low risk. An irreversible deletion of even a \
few records is rarely low risk. Weigh what is actually at stake if you have \
misunderstood the request, and err toward the more supervised band when unsure.

Also give `severity` (0.0-1.0) consistent with your band -- low is 0.00-0.29, \
medium is 0.30-0.70, high is 0.71-1.00 -- and a one-sentence `rationale` for the \
band. The band is what routes the action; severity is only for display.

Do not inflate or deflate any of this to influence the routing outcome. Report \
what you actually believe.

One rule overrides your own judgement, so do not argue with it: deletion is \
never low risk, at any size. If your action deletes rows, the band is medium at \
the very least.
"""

#: Worked examples. Every failure these target was observed on a live run, not
#: imagined: estimating 30,000 rows for a 4,996-row category, estimating a round
#: 100 for a 1,037-row filter, and banding a single-row deletion as low risk.
FEW_SHOT_EXAMPLES: Final[str] = """\
Worked examples -- reason in this style.

Request: "Which category made the most revenue?"
  -> summarize_transactions, filter [], group_by "category"
     data_scope: every row, so the table total above.
     reversibility reversible -- it changes nothing.
     risk_band LOW. Reading everything is safe.

Request: "Correct the age on invoice I138884 to 29."
  -> update_transaction, invoice_no I138884
     data_scope 1 -- one named invoice is exactly one row.
     reversibility partially_reversible -- the old value is recoverable.
     risk_band LOW. One row, named explicitly, and undoable.

Request: "Delete invoice I317333, it was entered by mistake."
  -> delete_transaction, invoice_no I317333
     data_scope 1, reversibility irreversible.
     risk_band MEDIUM -- not low. It is only one row, but deletion cannot be
     undone by anything you do next, and if the invoice number is wrong the data
     is simply gone. A human should see it first.

Request: "Delete every Souvenir transaction at Kanyon."
  -> bulk_delete_transactions, filter [{category equals Souvenir},
     {shopping_mall equals Kanyon}]
     data_scope: derive it. Souvenir is about 5,000 rows and Kanyon is about a
     fifth of the table, so on the order of 1,000 -- not a round guess of 100.
     reversibility irreversible.
     risk_band HIGH. Irreversible, and a four-figure number of rows.

Request: "Remove all Technology purchases."
  -> bulk_delete_transactions, filter [{category equals Technology}]
     data_scope: read Technology straight off the counts above. Do not invent a
     figure like 30000 -- the number is given to you.
     risk_band HIGH. Irreversible deletion of a whole category.
     Do NOT ask a clarifying question here. The request is unambiguous; it is
     simply large. That is what HIGH is for -- a human will see it and decide.

Request: "Clean up the bad records."
  -> request_clarification. No filter can be written from this. "Bad" is not a
     column and not a value in any column, and the rows meant could be almost
     anything. Guessing risks destroying the wrong thousands of rows, and a
     guess dressed up as a low-confidence proposal is still a guess.
     question: "Which rows count as 'bad'? I can filter on quantity, price,
     invoice_date, category, payment_method, shopping_mall, age or gender --
     for example quantity of 0, or a particular date range."

Request: "Delete the electronics from last year."
  -> request_clarification. Two separate gaps: there is no "electronics"
     category (the closest is Technology, but that is my inference, not their
     word), and "last year" is ambiguous in a table spanning 2021-2023.
     Either guess changes which thousands of rows are destroyed.
"""


def build_system_prompt() -> str:
    """The system prompt with live row counts spliced in, plus worked examples.

    Built per call rather than frozen at import, because the counts have to
    describe the table as it is now. An agent reasoning about a category it
    deleted five minutes ago would otherwise be working from a table that no
    longer exists.
    """
    return (
        SYSTEM_PROMPT.replace("{CARDINALITIES}", _cardinality_help())
        + "\n"
        + FEW_SHOT_EXAMPLES
    )

# --------------------------------------------------------------------------
# Tool schemas
#
# Five tools spanning the risk spectrum: two reads (row-level and aggregate), a
# single-record write, a single-record delete, and a filtered bulk delete.
#
# The two deletes are separate tools rather than one tool with an optional
# filter, because "remove invoice I138884" and "remove every Clothing row" are
# one row versus thirty-four thousand. Forcing the agent to pick between them
# puts that distinction in the tool name, where the audit log and the human
# reviewing the preview can both see it, instead of burying it in a filter
# argument nobody reads closely.
#
# Every tool carries the same `self_assessment` block so the risk assessor gets
# the same inputs regardless of which tool the agent picks.
# --------------------------------------------------------------------------

_SELF_ASSESSMENT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "Your reasoned risk judgement. See the system prompt for how to fill this in.",
    "properties": {
        "reversibility": {
            "type": "string",
            "enum": ["reversible", "partially_reversible", "irreversible"],
        },
        "reversibility_reasoning": {"type": "string", "description": "Why, one sentence."},
        "data_scope": {"type": "integer", "description": "Rows affected. Count it, do not guess."},
        "data_scope_reasoning": {"type": "string", "description": "How you got that number."},
        "regulatory_category": {
            "type": "string",
            "enum": ["none", "internal_sensitive", "regulated"],
        },
        "regulatory_reasoning": {"type": "string", "description": "Why that class."},
        "confidence": {"type": "number", "description": "0.0-1.0 that this is the right action."},
        "confidence_reasoning": {"type": "string", "description": "What you are unsure about."},
        "risk_band": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Routes the action: low runs alone, medium confirms, high blocks.",
        },
        "severity": {"type": "number", "description": "0.0-1.0, consistent with risk_band."},
        "rationale": {"type": "string", "description": "One sentence for the band."},
    },
    "required": [
        "reversibility",
        "reversibility_reasoning",
        "data_scope",
        "data_scope_reasoning",
        "regulatory_category",
        "regulatory_reasoning",
        "confidence",
        "confidence_reasoning",
        "risk_band",
        "severity",
        "rationale",
    ],
    "additionalProperties": False,
}

#: Machine-executable filter. Built from the live column list in data_store so
#: the enum the model sees can never drift from the CSV's actual schema.
_FILTER_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "description": "Criteria ANDed together. Empty matches every row.",
    "items": {
        "type": "object",
        "properties": {
            "field": {"type": "string", "enum": list(CUSTOMER_FIELDS)},
            "operator": {
                "type": "string",
                "enum": [
                    "equals",
                    "not_equals",
                    "contains",
                    "before",
                    "after",
                    "greater_than",
                    "less_than",
                ],
            },
            "value": {"type": "string", "description": "Dates as YYYY-MM-DD."},
        },
        "required": ["field", "operator", "value"],
        "additionalProperties": False,
    },
}

TOOL_SCHEMAS: Final[list[dict[str, Any]]] = [
    {
        "name": "query_transactions",
        "description": (
            "Read-only lookup of individual transaction rows. Returns data but "
            "changes nothing. Use when the user wants to see specific invoices. "
            "Results are capped, so prefer summarize_transactions for questions "
            "about totals, revenue, or counts across many rows."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_description": {
                    "type": "string",
                    "description": "Which rows to retrieve, in plain language, for the human preview.",
                },
                "filter": _FILTER_SCHEMA,
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["filter_description", "filter", "self_assessment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "summarize_transactions",
        "description": (
            "Aggregate transactions into counts, total quantity, and total "
            "revenue, optionally broken down by a column. Read-only. Use this "
            "for 'how much', 'how many', 'which category sold most' style "
            "questions -- it answers them in one number instead of returning "
            "thousands of rows."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_description": {
                    "type": "string",
                    "description": "Which rows to aggregate over, in plain language.",
                },
                "filter": _FILTER_SCHEMA,
                "group_by": {
                    "type": "string",
                    "enum": ["", *CUSTOMER_FIELDS],
                    "description": (
                        "Column to break the totals down by, or an empty string "
                        "for one overall total."
                    ),
                },
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["filter_description", "filter", "group_by", "self_assessment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_transaction",
        "description": (
            "Write a new value to one field on one transaction, identified by "
            "invoice_no. Affects exactly one row. The prior value is recoverable "
            "from a snapshot but the change is live immediately. Use for "
            "correcting a single invoice's details."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_no": {
                    "type": "string",
                    "description": "Identifier of the single transaction to update, e.g. I138884.",
                },
                "field": {
                    "type": "string",
                    "enum": [f for f in CUSTOMER_FIELDS if f != "invoice_no"],
                    "description": "Name of the field being changed. invoice_no is not writable.",
                },
                "new_value": {
                    "type": "string",
                    "description": "The value to write to that field.",
                },
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["invoice_no", "field", "new_value", "self_assessment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_transaction",
        "description": (
            "Permanently delete ONE transaction, identified by invoice_no. "
            "Affects exactly one row. Destructive and not recoverable by the "
            "agent. Use this whenever the user names a specific invoice to "
            "remove -- prefer it over bulk_delete_transactions, which is for "
            "filters that may match many rows."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_no": {
                    "type": "string",
                    "description": "Identifier of the single transaction to delete, e.g. I138884.",
                },
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["invoice_no", "self_assessment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bulk_delete_transactions",
        "description": (
            "Permanently delete EVERY transaction matching a filter. Destructive "
            "and NOT recoverable -- deleted rows cannot be restored by the agent. "
            "A broad filter here can remove tens of thousands of rows in one "
            "call. Use only when the request is explicitly a bulk deletion; for "
            "a single named invoice use delete_transaction instead."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_description": {
                    "type": "string",
                    "description": "Which rows to delete, in plain language, for the human preview.",
                },
                "filter": _FILTER_SCHEMA,
                "self_assessment": _SELF_ASSESSMENT_SCHEMA,
            },
            "required": ["filter_description", "filter", "self_assessment"],
            "additionalProperties": False,
        },
    },
]

#: Name of the lookup tool. Not an action: it proposes nothing, changes nothing,
#: and is never routed or audited as an action in its own right.
COUNT_TOOL_NAME: Final[str] = "count_matching_rows"

#: A tool for finding out, rather than guessing.
#:
#: Live evaluation showed the agent estimating 495 rows where the true answer was
#: 1,013, and 15,109 where it was 6,674 -- in the second case it had the category
#: total in front of it and simply ignored the second half of its own filter.
#: Putting the real counts in the prompt did not fix that, because the failure is
#: arithmetic over a conjunction, not missing information.
#:
#: So the agent no longer has to do that arithmetic. It can run the filter and be
#: told. Note there is deliberately no `self_assessment` here: this is a lookup
#: taken *before* the agent has an action to assess, and demanding a risk
#: judgement to ask a question would defeat the point.
PLANNING_TOOL: Final[dict[str, Any]] = {
    "name": COUNT_TOOL_NAME,
    "description": (
        "Read-only lookup: returns how many rows match a filter. Performs no "
        "action and changes nothing. Call this BEFORE proposing any filtered or "
        "destructive action, so your data_scope is the real number and your "
        "risk_band is judged on fact rather than an estimate. Cheap -- prefer it "
        "over guessing, especially when your filter has more than one criterion."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"filter": _FILTER_SCHEMA},
        "required": ["filter"],
        "additionalProperties": False,
    },
}

#: Name of the clarification tool. Like the lookup tool this is not an action:
#: it proposes nothing, executes nothing, and is never routed or audited.
CLARIFY_TOOL_NAME: Final[str] = "request_clarification"

#: The agent's way of saying "I cannot safely interpret this" without having to
#: invent an action to say it with.
#:
#: Before this existed, a request like "clean up the bad records" had exactly one
#: exit: propose *something*, mark confidence low, and let the high band park it
#: in the review queue. That works -- nothing is destroyed -- but it is the wrong
#: shape. It puts a filter nobody asked for in front of a reviewer and makes them
#: reject it, when the honest answer was a question. Supervision is for actions
#: the agent means; it should not be doing duty as a place to put guesses.
#:
#: Deliberately no `self_assessment`: there is no action here to assess. And no
#: severity or band -- asking a question carries no risk, so routing it through
#: the risk machinery would be inventing a number to describe nothing.
CLARIFICATION_TOOL: Final[dict[str, Any]] = {
    "name": CLARIFY_TOOL_NAME,
    "description": (
        "Ask the user a question instead of proposing an action. Use this when "
        "the request cannot be turned into a filter you can defend -- an "
        "undefined set of rows ('the bad records'), an ambiguous date, or a "
        "value that is not in the column vocabularies. Performs nothing and "
        "changes nothing. Do NOT use it merely because an action is large or "
        "destructive: a clear instruction to delete thousands of rows is a "
        "high-risk action, not a question."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "What you need to know, asked directly. One question, "
                    "answerable in a sentence."
                ),
            },
            "why": {
                "type": "string",
                "description": (
                    "Why you cannot proceed without it, and what you would risk "
                    "getting wrong if you guessed."
                ),
            },
            "options": {
                "type": "array",
                "description": (
                    "Concrete answers the user can pick from, if you can offer "
                    "any. Empty array if the question is genuinely open."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["question", "why", "options"],
        "additionalProperties": False,
    },
}

#: How many lookups the agent may make before it must commit to an action.
#: Bounded because this sits on the request path of a 30s Lambda, and an agent
#: that wants a fourth count is not converging on anything.
MAX_PLANNING_TURNS: Final[int] = 4

#: Maps each tool to the action_type recorded in the audit log. Kept separate
#: from the schemas so the audit vocabulary can evolve independently of the
#: tool names the model sees. Note that the two deletes get distinct types: an
#: auditor filtering for "how often did we bulk-delete" must not have to
#: reconstruct that from the parameters.
TOOL_ACTION_TYPES: Final[dict[str, str]] = {
    "query_transactions": "read",
    "summarize_transactions": "aggregate_read",
    "update_transaction": "single_record_write",
    "delete_transaction": "single_record_delete",
    "bulk_delete_transactions": "bulk_delete",
}



# --------------------------------------------------------------------------
# Models and errors
# --------------------------------------------------------------------------


class AgentActionError(RuntimeError):
    """Raised when the agent could not produce a usable action proposal.

    Deliberately loud: a silent failure here would mean an unscored action, and
    an unscored action must never reach an execution path.
    """


class AgentAction(BaseModel):
    """A single action the agent proposes to take, with its own risk assessment."""

    action_type: str = Field(description="Audit vocabulary for this kind of action.")
    description: str = Field(description="Human-readable summary of the proposed action.")
    tool_name: str = Field(description="Which tool the agent chose.")
    parameters: dict[str, Any] = Field(description="Arguments for that tool call.")

    # Everything below is the agent's own judgement: the four classifications,
    # its written reasoning for each, and the band it concluded. The band is what
    # actually routes the action -- see risk_scorer.route_action.
    reversibility: Reversibility
    reversibility_reasoning: str = ""
    data_scope: int = Field(ge=0)
    data_scope_reasoning: str = ""
    regulatory_category: RegulatoryCategory
    regulatory_reasoning: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_reasoning: str = ""
    risk_band: RiskBand
    severity: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str = ""

    def to_risk_factors(self) -> RiskFactors:
        """Project this action onto what the risk assessor needs.

        A straight projection: nothing is recomputed or second-guessed here, so
        what the audit trail shows is exactly what the model reported.
        """
        return RiskFactors(
            reversibility=self.reversibility,
            reversibility_reasoning=self.reversibility_reasoning,
            data_scope=self.data_scope,
            data_scope_reasoning=self.data_scope_reasoning,
            regulatory_category=self.regulatory_category,
            regulatory_reasoning=self.regulatory_reasoning,
            confidence=self.confidence,
            confidence_reasoning=self.confidence_reasoning,
            risk_band=self.risk_band,
            severity=self.severity,
            rationale=self.rationale,
        )


class ClarificationRequest(BaseModel):
    """The agent declining to guess, and asking the user instead.

    Not an action and not a risk assessment: nothing is proposed, so there is
    nothing to band, queue, execute or roll back. :func:`propose_action` returns
    this *instead of* an :class:`AgentAction`, and every caller has to handle
    both -- which is the point. A union that cannot be ignored is how this stays
    a real third outcome rather than a variety of failure.
    """

    question: str = Field(description="What the agent needs to know.")
    why: str = Field(description="Why it cannot proceed without an answer.")
    options: list[str] = Field(
        default_factory=list,
        description="Concrete answers to offer, if the agent could suggest any.",
    )


#: What the agent can come back with. Callers must handle both arms.
ProposalOutcome = AgentAction | ClarificationRequest


# --------------------------------------------------------------------------
# Groq client
# --------------------------------------------------------------------------


def _client() -> groq.Groq:
    """Build a Groq client, failing loudly if no key is configured.

    ``max_retries=0`` disables the SDK's own retry loop so that
    :data:`MAX_ATTEMPTS` here is the whole retry story -- otherwise the two
    layers would multiply and a rate-limited call could stall well past the
    Lambda timeout.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise AgentActionError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add a key, "
            "or set the variable in the Lambda environment."
        )
    return groq.Groq(api_key=api_key, max_retries=0)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def _run_count(arguments: dict[str, Any]) -> str:
    """Execute a count_matching_rows lookup and render the answer for the model.

    Filter errors come back as the tool result rather than as an exception, so a
    malformed filter becomes something the agent can see and correct on its next
    turn instead of a failed request. That is the main practical benefit of
    letting it ask: a bad filter surfaces before it is attached to a deletion.
    """
    try:
        criteria = [
            data_store.Criterion.model_validate(item)
            for item in (arguments.get("filter") or [])
        ]
        matched = data_store.count_matching(criteria)
        total = len(data_store.load_rows())
    except Exception as exc:  # noqa: BLE001 - hand the problem back to the model
        return f"That filter could not be run: {exc}. Fix it and try again."

    if not criteria:
        return f"An empty filter matches every row: {total:,} of {total:,}."
    share = (matched / total * 100) if total else 0.0
    return (
        f"That filter matches {matched:,} of {total:,} rows ({share:.1f}%). "
        "Use this number as your data_scope."
    )


def propose_action(user_request: str, tool_context: dict[str, Any]) -> ProposalOutcome:
    """Ask the model what to do, letting it check the data or ask a question first.

    Not a single call but a short loop with three ways out:

    - it calls :data:`PLANNING_TOOL` to ask how many rows a filter really
      matches, gets a real answer, and goes round again -- so it reasons from a
      measured number rather than an estimate the engine has to correct after
      the fact;
    - it calls :data:`CLARIFICATION_TOOL`, and a :class:`ClarificationRequest`
      comes back for the user to answer. Nothing is proposed or executed;
    - it commits to an action, and an :class:`AgentAction` comes back carrying
      its risk self-assessment.

    The loop is bounded by :data:`MAX_PLANNING_TURNS`, so an agent that keeps
    counting without ever deciding fails loudly rather than hanging the request.

    Args:
        user_request: What the user asked for, in natural language.
        tool_context: Extra context handed to the agent. ``tools`` may override
            the action schemas offered (defaults to :data:`TOOL_SCHEMAS`); any
            other keys are passed to the model as situational context, e.g.
            ``{"tenant": "acme", "environment": "production"}``.

    Returns:
        An :class:`AgentAction` with the agent's own risk judgement, or a
        :class:`ClarificationRequest` if it could not safely interpret the
        request.

    Raises:
        AgentActionError: If the API is unreachable after one retry, if the model
            returned no usable tool call, or if it never decided within the turn
            budget.
    """
    action_tools = tool_context.get("tools", TOOL_SCHEMAS)
    situational = {k: v for k, v in tool_context.items() if k != "tools"}

    prompt = user_request
    if situational:
        context_lines = "\n".join(f"- {k}: {v}" for k, v in situational.items())
        prompt = f"Context for this request:\n{context_lines}\n\nRequest: {user_request}"

    # Both non-action tools are offered alongside the actions, so checking first
    # and asking first are the agent's decisions rather than fixed pre-steps. A
    # single named invoice needs neither; a two-criterion filter over 99k rows
    # needs the first, and "clean up the bad records" needs the second.
    tools = [PLANNING_TOOL, CLARIFICATION_TOOL, *action_tools]

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for turn in range(MAX_PLANNING_TURNS):
        message = _call_with_one_retry(messages, tools)

        if not message.tool_calls:
            raise AgentActionError(
                "The model returned no tool call, so there is no action to score. "
                f"text={(message.content or '')[:200]!r}"
            )

        call = message.tool_calls[0]
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError as exc:
            raise AgentActionError(
                f"Tool call {call.function.name!r} returned unparseable JSON "
                f"arguments: {exc}"
            ) from exc

        if call.function.name == CLARIFY_TOOL_NAME:
            return _build_clarification(arguments)

        if call.function.name != COUNT_TOOL_NAME:
            return _build_action(call.function.name, arguments)

        result = _run_count(arguments)
        logger.info(
            "agent looked up true row count",
            extra={"turn": turn + 1, "result": result},
        )
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    raise AgentActionError(
        f"The model made {MAX_PLANNING_TURNS} lookups without proposing an action."
    )


def _build_clarification(payload: dict[str, Any]) -> ClarificationRequest:
    """Validate a request_clarification tool call.

    A question with no text is not a question. Raising here rather than
    returning an empty one is deliberate: a blank clarification would stall the
    user with nothing to answer, which is a worse failure than a loud error.
    """
    question = str(payload.get("question", "")).strip()
    if not question:
        raise AgentActionError(
            "The model asked for clarification but supplied no question."
        )

    options = payload.get("options") or []
    if not isinstance(options, list):
        options = []

    clarification = ClarificationRequest(
        question=question,
        why=str(payload.get("why", "")).strip(),
        options=[str(o) for o in options if str(o).strip()],
    )
    logger.info(
        "agent asked for clarification instead of proposing an action",
        extra={"question": clarification.question},
    )
    return clarification


REASSESS_SYSTEM_PROMPT: Final[str] = """\
You are re-judging the risk of an action you already proposed, now that the \
database has been consulted and the true number of affected rows is known.

Your earlier estimate was a guess made from the request text. The number you are \
given now is a fact measured from the data. Re-reason all four dimensions using \
that fact and state the band again.

Do not defend your earlier answer. If the true count is far larger than you \
assumed, the correct response is a higher band -- that is the entire reason you \
are being asked again. If the true count confirms your estimate, keeping the \
same band is equally correct.

Reversibility, sensitivity and your confidence in the interpretation have not \
changed unless the new count reveals you misread the request. Fill in every \
field of the self_assessment, including reasoning for each dimension.
"""

#: One-tool schema for the re-judgement turn: the same self-assessment block,
#: with no action parameters, because the action itself is not up for revision.
#: Only the judgement of it is.
REASSESS_TOOL: Final[dict[str, Any]] = {
    "name": "revised_assessment",
    "description": "Your re-judged risk assessment, given the true affected-row count.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"self_assessment": _SELF_ASSESSMENT_SCHEMA},
        "required": ["self_assessment"],
        "additionalProperties": False,
    },
}


def reassess_action(action: AgentAction, actual_rows: int) -> AgentAction:
    """Ask the model to re-judge an action's risk against the true row count.

    The model's first band was reasoned from an estimate it made while reading
    the request. When that estimate turns out to be materially wrong, the band
    is an answer to a question nobody asked -- so rather than override it with
    arithmetic, we hand the model the fact and let it judge again. The judgement
    stays the model's; only its premise is corrected.

    Args:
        action: The action as originally proposed.
        actual_rows: True affected-row count from :func:`executor.preflight`.

    Returns:
        A copy of ``action`` carrying the revised assessment. On any failure the
        original is returned unchanged -- a failed re-judgement must not be able
        to *lower* supervision, and the blast-radius floor still applies on top.
    """
    prompt = (
        f"The action you proposed was:\n"
        f"  tool: {action.tool_name}\n"
        f"  parameters: {json.dumps(action.parameters, default=str)}\n"
        f"  your description: {action.description}\n\n"
        f"You estimated it would affect {action.data_scope} row(s) and judged it "
        f"{action.risk_band.upper()} risk because: {action.rationale}\n\n"
        f"The database has now been queried. This action really affects "
        f"{actual_rows:,} row(s).\n\n"
        f"Re-judge it."
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    try:
        message = _call_with_one_retry(
            messages, [REASSESS_TOOL], system=REASSESS_SYSTEM_PROMPT
        )
        if not message.tool_calls:
            raise AgentActionError("re-judgement returned no tool call")
        payload = json.loads(message.tool_calls[0].function.arguments)

        assessment = payload.get("self_assessment")
        if not isinstance(assessment, dict):
            raise AgentActionError("re-judgement returned no self_assessment")

        revised = action.model_copy(
            update={
                "reversibility": assessment["reversibility"],
                "reversibility_reasoning": assessment.get("reversibility_reasoning", ""),
                # The whole point: data_scope becomes the measured truth, not a
                # second guess at it.
                "data_scope": actual_rows,
                "data_scope_reasoning": assessment.get("data_scope_reasoning", ""),
                "regulatory_category": assessment["regulatory_category"],
                "regulatory_reasoning": assessment.get("regulatory_reasoning", ""),
                "confidence": assessment["confidence"],
                "confidence_reasoning": assessment.get("confidence_reasoning", ""),
                "risk_band": assessment["risk_band"],
                "severity": assessment.get("severity"),
                "rationale": assessment.get("rationale", ""),
            }
        )
        logger.info(
            "action re-judged against true scope",
            extra={
                "tool_name": action.tool_name,
                "claimed_scope": action.data_scope,
                "actual_rows": actual_rows,
                "band_before": action.risk_band,
                "band_after": revised.risk_band,
            },
        )
        return revised

    except (AgentActionError, KeyError, ValueError, json.JSONDecodeError) as exc:
        # Deliberately non-fatal. The original band stands and the blast-radius
        # floor still runs on top of it, so a failed re-judgement can only ever
        # leave supervision where it was -- never below it.
        logger.warning(
            "re-judgement failed, keeping the original assessment: %s", exc
        )
        return action


def _call_with_one_retry(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]], system: str | None = None
) -> Any:
    """Call Groq's chat completions API, retrying once on transient failures.

    Takes the whole message list rather than a single prompt, because the
    planning loop needs to carry lookup results back into the next turn. Returns
    the raw assistant message so the loop can read its tool calls and thread them
    into the following turn.

    Retries only what is worth retrying: connection failures, rate limits, and
    5xx. A 400 or 401 means the request or the key is wrong and will fail
    identically the second time, so it is surfaced immediately.

    The system prompt is prepended here rather than by the caller, keeping
    message assembly in one place.
    """
    client = _client()
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                messages=[
                    {"role": "system", "content": system or build_system_prompt()},
                    *messages,
                ],
                tools=_to_openai_tools(tools),
                # A tool call every turn: a lookup, a question, or a committed
                # action. Never free prose, which there would be nothing to do with.
                tool_choice="required",
            )
            return response.choices[0].message
        except (
            groq.APIConnectionError,
            groq.RateLimitError,
            groq.InternalServerError,
        ) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "propose_action attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    MAX_ATTEMPTS,
                    type(exc).__name__,
                    RETRY_DELAY_SECONDS,
                )
                time.sleep(RETRY_DELAY_SECONDS)
        except groq.APIStatusError as exc:
            # Not retryable: bad request, bad key, missing model.
            raise AgentActionError(
                f"Groq API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc

    raise AgentActionError(
        f"Groq API unreachable after {MAX_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _build_action(tool_name: str, payload: dict[str, Any]) -> AgentAction:
    """Turn a {tool_name, payload} pair into a validated AgentAction.

    The one place a raw tool call becomes an action, so the validation rules
    (self-assessment present, fields well-formed, band stated) are applied in a
    single place rather than per call site.
    """
    payload = dict(payload)
    assessment = payload.pop("self_assessment", None)
    if not isinstance(assessment, dict):
        raise AgentActionError(
            f"Tool call {tool_name!r} is missing its self_assessment block; "
            "the action cannot be risk-scored."
        )

    try:
        return AgentAction(
            action_type=TOOL_ACTION_TYPES.get(tool_name, tool_name),
            description=_describe(tool_name, payload),
            tool_name=tool_name,
            parameters=payload,
            reversibility=assessment["reversibility"],
            reversibility_reasoning=assessment.get("reversibility_reasoning", ""),
            data_scope=assessment["data_scope"],
            data_scope_reasoning=assessment.get("data_scope_reasoning", ""),
            regulatory_category=assessment["regulatory_category"],
            regulatory_reasoning=assessment.get("regulatory_reasoning", ""),
            confidence=assessment["confidence"],
            confidence_reasoning=assessment.get("confidence_reasoning", ""),
            # The band is required: it is the routing decision. Without it there
            # is nothing to route on, so this raises rather than defaulting --
            # a missing band must never quietly become "low".
            risk_band=assessment["risk_band"],
            severity=assessment.get("severity"),
            rationale=assessment.get("rationale", ""),
        )
    except (KeyError, ValueError) as exc:
        raise AgentActionError(
            f"Tool call {tool_name!r} returned an unusable self_assessment "
            f"({assessment!r}): {exc}"
        ) from exc


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the engine's tool schemas into OpenAI function-calling format.

    The schemas above are kept in a provider-neutral shape (``input_schema``,
    ``strict``) and converted at the wire, so the tool definitions read as
    descriptions of *this system's* capabilities rather than as one vendor's
    payload format.

    ``strict`` is deliberately not forwarded. It is an internal contract -- it
    marks a schema as closed, which the test suite enforces (every property
    required, ``additionalProperties`` false) -- and not every Groq-hosted model
    accepts the wire-level flag. The closed schema is what does the work; the
    flag would only be the model's promise to honour it.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _format_filter(criteria: Any) -> str:
    """Render structured criteria as readable text, e.g. ``country = SE AND ...``.

    The human approving a deletion needs to see the filter that will actually
    run, not only the model's prose summary of it -- those are the two things
    most worth catching a mismatch between.
    """
    if not isinstance(criteria, list) or not criteria:
        return "no filter (matches every record)"
    symbols = {
        "equals": "=",
        "not_equals": "!=",
        "contains": "contains",
        "before": "before",
        "after": "after",
        "greater_than": ">",
        "less_than": "<",
    }
    parts = []
    for c in criteria:
        if not isinstance(c, dict):
            continue
        op = symbols.get(c.get("operator", ""), c.get("operator", "?"))
        parts.append(f"{c.get('field')} {op} {c.get('value')!r}")
    return " AND ".join(parts) if parts else "no filter (matches every record)"


def _describe(tool_name: str, parameters: dict[str, Any]) -> str:
    """Render a preview line a human can approve or reject without reading JSON."""
    if tool_name == "query_transactions":
        return (
            f"Read transactions where {_format_filter(parameters.get('filter'))} "
            f"({parameters.get('filter_description')})"
        )
    if tool_name == "summarize_transactions":
        grouping = parameters.get("group_by") or ""
        breakdown = f", broken down by {grouping}" if grouping else ""
        return (
            f"Summarise transactions where {_format_filter(parameters.get('filter'))}"
            f"{breakdown} ({parameters.get('filter_description')})"
        )
    if tool_name == "update_transaction":
        return (
            f"Set {parameters.get('field')} to {parameters.get('new_value')!r} "
            f"on invoice {parameters.get('invoice_no')}"
        )
    if tool_name == "delete_transaction":
        return f"PERMANENTLY DELETE invoice {parameters.get('invoice_no')} (1 row)"
    if tool_name == "bulk_delete_transactions":
        return (
            "PERMANENTLY DELETE all transactions where "
            f"{_format_filter(parameters.get('filter'))} "
            f"({parameters.get('filter_description')})"
        )
    return f"{tool_name} with {parameters}"
