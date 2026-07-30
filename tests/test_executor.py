"""Tests for the execution layer.

The executor deliberately performs no authorisation checks of its own -- that
lives in :mod:`confirmation`. What is tested here is that once an action is
allowed to run, it runs correctly, reports what it did, and turns data-level
problems into recorded failures rather than crashes.
"""

import pytest

from autonomy_engine import data_store, executor
from autonomy_engine.executor import ExecutionError, execute
from tests.conftest import SEED_ROW_COUNT


def where(field, operator, value):
    return {"filter": [{"field": field, "operator": operator, "value": value}]}


def first_invoice():
    return data_store.load_rows()[0][data_store.ID_FIELD]


# --------------------------------------------------------------------------
# Row queries
# --------------------------------------------------------------------------


def test_query_returns_matching_rows():
    expected = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Books")]
    )
    result = execute("query_transactions", where("category", "equals", "Books"), record_id="rec-1")

    assert result.status == "success"
    assert result.affected_count == expected
    assert all(row["category"] == "Books" for row in result.rows)


def test_query_changes_nothing():
    execute("query_transactions", {"filter": []}, record_id="rec-1")
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


def test_query_is_capped_and_says_so():
    """A read is low risk to perform, but an unbounded result set is still a way
    to pull the whole table in one call. The cap is reported, not silent."""
    result = execute("query_transactions", {"filter": []}, record_id="rec-1")

    assert result.affected_count == SEED_ROW_COUNT
    assert len(result.rows) == executor.QUERY_ROW_LIMIT
    assert result.truncated is True
    assert str(executor.QUERY_ROW_LIMIT) in result.detail


def test_small_query_is_not_marked_truncated():
    result = execute(
        "query_transactions", where("invoice_no", "equals", first_invoice()), record_id="rec-1"
    )
    assert result.truncated is False
    assert len(result.rows) == 1


# --------------------------------------------------------------------------
# Aggregate queries
# --------------------------------------------------------------------------


def test_summarize_reports_totals_without_returning_rows():
    """The reason this tool exists: answering "how much" with one number rather
    than thousands of rows."""
    result = execute(
        "summarize_transactions",
        {**where("category", "equals", "Clothing"), "group_by": ""},
        record_id="rec-1",
    )

    assert result.status == "success"
    assert result.rows == []
    assert result.summary["transactions"] > 0
    assert result.summary["total_revenue"] > 0
    assert "revenue" in result.detail


def test_summarize_groups_when_asked():
    result = execute(
        "summarize_transactions",
        {"filter": [], "group_by": "shopping_mall"},
        record_id="rec-1",
    )
    assert len(result.summary["groups"]) > 1
    assert "shopping_mall group(s)" in result.detail


def test_summarize_treats_empty_group_by_as_no_grouping():
    """strict mode requires the property on every call, so "" is how the model
    says "no breakdown". It must not be read as a column name."""
    result = execute(
        "summarize_transactions", {"filter": [], "group_by": ""}, record_id="rec-1"
    )
    assert result.status == "success"
    assert "groups" not in result.summary


def test_summarize_changes_nothing():
    execute("summarize_transactions", {"filter": [], "group_by": "category"}, record_id="rec-1")
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def test_update_applies_and_reports_the_change():
    invoice = first_invoice()
    before = data_store.load_rows()[0]["payment_method"]

    result = execute(
        "update_transaction",
        {"invoice_no": invoice, "field": "payment_method", "new_value": "Cash"},
        record_id="rec-1",
    )

    assert result.status == "success"
    assert result.affected_count == 1
    assert before in result.detail or "Cash" in result.detail
    assert data_store.load_rows()[0]["payment_method"] == "Cash"


def test_delete_transaction_removes_exactly_one_row():
    """The user's headline case: remove one particular item."""
    invoice = first_invoice()

    result = execute("delete_transaction", {"invoice_no": invoice}, record_id="rec-1")

    assert result.status == "success"
    assert result.affected_count == 1
    assert len(data_store.load_rows()) == SEED_ROW_COUNT - 1
    assert invoice in result.detail


def test_delete_transaction_echoes_the_deleted_row():
    invoice = first_invoice()
    result = execute("delete_transaction", {"invoice_no": invoice}, record_id="rec-1")
    assert result.rows[0]["invoice_no"] == invoice


def test_bulk_delete_removes_rows_and_reports_the_remainder():
    result = execute(
        "bulk_delete_transactions", where("category", "equals", "Clothing"), record_id="rec-1"
    )
    assert result.status == "success"
    assert result.affected_count > 1
    assert len(data_store.load_rows()) == SEED_ROW_COUNT - result.affected_count


def test_bulk_delete_names_a_sample_of_what_it_removed():
    result = execute(
        "bulk_delete_transactions", where("category", "equals", "Clothing"), record_id="rec-1"
    )
    assert "Sample:" in result.detail


def test_mutations_report_a_snapshot_path_that_works():
    """The audit record has to carry the path an operator would use to roll the
    change back, so an unusable one is a real defect."""
    result = execute("delete_transaction", {"invoice_no": first_invoice()}, record_id="rec-1")
    assert result.snapshot
    assert executor.rollback("rec-1") == SEED_ROW_COUNT


def test_reads_take_no_snapshot():
    for tool, params in [
        ("query_transactions", {"filter": []}),
        ("summarize_transactions", {"filter": [], "group_by": ""}),
    ]:
        assert execute(tool, params, record_id="rec-1").snapshot is None


# --------------------------------------------------------------------------
# Scope checking
# --------------------------------------------------------------------------


def test_accurate_estimate_is_confirmed():
    expected = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Books")]
    )
    result = execute(
        "query_transactions",
        where("category", "equals", "Books"),
        record_id="rec-1",
        claimed_scope=expected,
    )
    assert "accurate" in result.scope_check


def test_wildly_wrong_estimate_is_flagged():
    """The model's data_scope fed its own banding, so a badly wrong count means
    the band was reasoned from a false premise. Reported, not acted on."""
    result = execute(
        "bulk_delete_transactions",
        where("category", "equals", "Clothing"),
        record_id="rec-1",
        claimed_scope=1,
    )
    assert "MISMATCH" in result.scope_check


def test_close_estimate_is_not_flagged():
    """Being off by a couple of rows is good estimating, not a red flag."""
    expected = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Books")]
    )
    result = execute(
        "query_transactions",
        where("category", "equals", "Books"),
        record_id="rec-1",
        claimed_scope=expected + 1,
    )
    assert "MISMATCH" not in result.scope_check


def test_scope_check_is_omitted_when_nothing_was_claimed():
    result = execute("query_transactions", {"filter": []}, record_id="rec-1")
    assert result.scope_check is None


def test_bulk_delete_scope_is_measured_against_what_matched():
    """Counting after the delete would always report zero."""
    expected = data_store.count_matching(
        [data_store.Criterion(field="category", operator="equals", value="Clothing")]
    )
    result = execute(
        "bulk_delete_transactions",
        where("category", "equals", "Clothing"),
        record_id="rec-1",
        claimed_scope=expected,
    )
    assert f"matched {expected}" in result.scope_check


def test_single_delete_understating_scope_is_impossible_to_hide():
    """delete_transaction always affects one row, so a claim of 500 is checkable."""
    result = execute(
        "delete_transaction",
        {"invoice_no": first_invoice()},
        record_id="rec-1",
        claimed_scope=500,
    )
    assert "MISMATCH" in result.scope_check


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parameters",
    [
        {"filter": [{"field": "store_name", "operator": "equals", "value": "x"}]},
        {"filter": [{"field": "invoice_date", "operator": "before", "value": "soon"}]},
        {"filter": [{"field": "category", "operator": "before", "value": "2022-01-01"}]},
        {"filter": "not-a-list"},
        {"filter": [{"field": "category"}]},
    ],
)
def test_bad_filters_become_recorded_failures_not_crashes(parameters):
    """These belong in the audit trail as a failed action, not as an opaque 500."""
    result = execute("query_transactions", parameters, record_id="rec-1")
    assert result.status == "failed"
    assert result.detail


def test_unbounded_bulk_delete_fails_without_touching_the_data():
    result = execute("bulk_delete_transactions", {"filter": []}, record_id="rec-1")
    assert result.status == "failed"
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


def test_update_to_a_missing_invoice_fails_cleanly():
    result = execute(
        "update_transaction",
        {"invoice_no": "I000000", "field": "age", "new_value": "30"},
        record_id="rec-1",
    )
    assert result.status == "failed"
    assert "I000000" in result.detail


def test_delete_of_a_missing_invoice_fails_cleanly():
    result = execute("delete_transaction", {"invoice_no": "I000000"}, record_id="rec-1")
    assert result.status == "failed"
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


def test_unknown_tool_raises_rather_than_failing_quietly():
    """A tool with no handler is a wiring bug on our side. Silently returning
    "failed" would let a schema be added without an implementation."""
    with pytest.raises(ExecutionError):
        execute("refund_transaction", {}, record_id="rec-1")


def test_every_declared_tool_has_a_handler():
    """The guard above only helps if the two lists actually agree."""
    from autonomy_engine.agent_actions import TOOL_SCHEMAS

    assert {t["name"] for t in TOOL_SCHEMAS} == set(executor._HANDLERS)
