"""Tests for the CSV transaction store.

Every test runs against the 300-row throwaway copy provided by the autouse
``isolated_transaction_data`` fixture, so the real 99k-row file is never touched.

Expectations are derived from the fixture rather than hard-coded wherever the
derivation is not just a restatement of the implementation -- a test asserting
``Clothing == 112`` breaks if the sample is ever regenerated, and teaches
nothing when it does.
"""

import csv
from datetime import datetime

import pytest

from autonomy_engine import data_store
from autonomy_engine.data_store import (
    Criterion,
    DataStoreError,
    RecordNotFoundError,
    SnapshotNotFoundError,
    UnboundedDeleteError,
    UnknownFieldError,
)
from tests.conftest import SEED_ROW_COUNT


def crit(field, operator, value):
    return Criterion(field=field, operator=operator, value=value)


@pytest.fixture
def raw_rows(isolated_transaction_data):
    """The fixture CSV read independently of the module under test."""
    with open(isolated_transaction_data, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def first_invoice():
    return data_store.load_rows()[0][data_store.ID_FIELD]


# --------------------------------------------------------------------------
# Reading and filtering
# --------------------------------------------------------------------------


def test_loads_the_seed_data():
    rows = data_store.load_rows()
    assert len(rows) == SEED_ROW_COUNT
    assert set(rows[0]) == set(data_store.FIELDS)


def test_empty_filter_selects_everything():
    assert len(data_store.select([])) == SEED_ROW_COUNT


def test_invoice_numbers_are_unique():
    """The store treats invoice_no as a primary key; the data must agree."""
    rows = data_store.load_rows()
    ids = [r[data_store.ID_FIELD] for r in rows]
    assert len(set(ids)) == len(ids)


def test_criteria_are_anded(raw_rows):
    expected = sum(
        1
        for r in raw_rows
        if r["category"] == "Clothing" and r["shopping_mall"] == "Kanyon"
    )
    got = data_store.select(
        [
            crit("category", "equals", "Clothing"),
            crit("shopping_mall", "equals", "Kanyon"),
        ]
    )
    assert len(got) == expected
    assert expected > 0, "fixture should contain at least one Clothing row at Kanyon"


def test_string_matching_is_case_insensitive():
    """The model writes "clothing" or "Clothing" interchangeably. A filter that
    silently matched nothing on casing would be scored as a safe no-op when it
    was really a bug."""
    assert data_store.select([crit("category", "equals", "CLOTHING")]) == (
        data_store.select([crit("category", "equals", "Clothing")])
    )


def test_multi_word_category_matches_exactly(raw_rows):
    """'Food & Beverage' is the kind of value a model mangles into 'food'."""
    expected = sum(1 for r in raw_rows if r["category"] == "Food & Beverage")
    assert len(data_store.select([crit("category", "equals", "Food & Beverage")])) == expected


# --------------------------------------------------------------------------
# Dates
#
# The source file is day-first DD/MM/YYYY with inconsistent padding. Getting
# this wrong silently selects the wrong rows, which is the worst failure mode
# available here, so it gets more tests than anything else.
# --------------------------------------------------------------------------


def test_stored_dates_are_read_day_first(raw_rows):
    """16/05/2021 is 16 May, not 5 April. Compared against a July boundary,
    a month-first misreading would put it on the wrong side."""
    expected = sum(
        1
        for r in raw_rows
        if datetime.strptime(r["invoice_date"], "%d/%m/%Y").date()
        < datetime(2022, 1, 1).date()
    )
    got = data_store.select([crit("invoice_date", "before", "2022-01-01")])
    assert len(got) == expected


def test_before_and_after_partition_the_data(raw_rows):
    """Every row is either before or after a boundary it does not fall on."""
    boundary = "2022-01-01"
    before = len(data_store.select([crit("invoice_date", "before", boundary)]))
    after = len(data_store.select([crit("invoice_date", "after", boundary)]))
    on_boundary = sum(1 for r in raw_rows if r["invoice_date"] == "01/01/2022")
    assert before + after + on_boundary == SEED_ROW_COUNT


def test_filter_dates_accept_iso_form():
    assert data_store.select([crit("invoice_date", "after", "2021-06-15")])


def test_filter_dates_also_accept_the_files_own_format():
    """A model that echoes the file's DD/MM/YYYY back at us should not be
    punished for it; ISO is tried first so there is never a guess."""
    iso = data_store.select([crit("invoice_date", "before", "2022-01-01")])
    day_first = data_store.select([crit("invoice_date", "before", "01/01/2022")])
    assert len(iso) == len(day_first)


def test_ambiguous_iso_date_is_never_read_day_first():
    """2021-05-08 must mean 8 May, not 5 August -- ISO wins outright."""
    may = data_store.select([crit("invoice_date", "before", "2021-05-08")])
    august = data_store.select([crit("invoice_date", "before", "2021-08-05")])
    assert len(may) < len(august)


def test_malformed_filter_date_is_rejected():
    with pytest.raises(DataStoreError, match="not a date"):
        data_store.select([crit("invoice_date", "before", "last tuesday")])


# --------------------------------------------------------------------------
# Numerics
# --------------------------------------------------------------------------


def test_numeric_comparison_is_not_lexicographic(raw_rows):
    """String ordering would put '900' above '3000.85'. This is the test that
    catches the columns being compared as text."""
    expected = sum(1 for r in raw_rows if float(r["price"]) > 3000)
    got = data_store.select([crit("price", "greater_than", "3000")])
    assert len(got) == expected
    assert all(float(r["price"]) > 3000 for r in got)


def test_age_is_numeric(raw_rows):
    expected = sum(1 for r in raw_rows if int(r["age"]) < 25)
    assert len(data_store.select([crit("age", "less_than", "25")])) == expected


def test_not_equals_is_the_complement_of_equals():
    equal = len(data_store.select([crit("payment_method", "equals", "Cash")]))
    unequal = len(data_store.select([crit("payment_method", "not_equals", "Cash")]))
    assert equal + unequal == SEED_ROW_COUNT


def test_query_respects_a_limit():
    assert len(data_store.query([], limit=5)) == 5


def test_count_matching_agrees_with_select():
    criteria = [crit("gender", "equals", "Female")]
    assert data_store.count_matching(criteria) == len(data_store.select(criteria))


# --------------------------------------------------------------------------
# Rejected filters
# --------------------------------------------------------------------------


def test_unknown_field_is_rejected():
    """Rejected rather than matching nothing: a typo that quietly returns zero
    rows looks identical to a correct filter with no results."""
    with pytest.raises(UnknownFieldError):
        data_store.select([crit("store_name", "equals", "Kanyon")])


def test_date_operators_are_rejected_on_non_date_fields():
    with pytest.raises(DataStoreError, match="not valid on field"):
        data_store.select([crit("category", "before", "2022-01-01")])


def test_numeric_operators_are_rejected_on_non_numeric_fields():
    with pytest.raises(DataStoreError, match="not valid on field"):
        data_store.select([crit("shopping_mall", "greater_than", "5")])


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def test_summarize_totals_match_a_hand_calculation(raw_rows):
    books = [r for r in raw_rows if r["category"] == "Books"]
    expected_revenue = round(sum(float(r["price"]) * int(r["quantity"]) for r in books), 2)

    totals = data_store.summarize([crit("category", "equals", "Books")])

    assert totals["transactions"] == len(books)
    assert totals["total_quantity"] == sum(int(r["quantity"]) for r in books)
    assert totals["total_revenue"] == pytest.approx(expected_revenue)


def test_summarize_groups_add_up_to_the_whole():
    totals = data_store.summarize([], group_by="category")
    assert sum(g["transactions"] for g in totals["groups"].values()) == SEED_ROW_COUNT
    assert sum(g["total_revenue"] for g in totals["groups"].values()) == pytest.approx(
        totals["total_revenue"], rel=1e-6
    )


def test_summarize_orders_groups_by_revenue():
    """The first group is the answer to "which sold most", so ordering is part
    of the contract rather than incidental."""
    groups = data_store.summarize([], group_by="category")["groups"]
    revenues = [g["total_revenue"] for g in groups.values()]
    assert revenues == sorted(revenues, reverse=True)


def test_summarize_without_grouping_omits_groups():
    assert "groups" not in data_store.summarize([])


def test_summarize_rejects_an_unknown_group_by():
    with pytest.raises(UnknownFieldError):
        data_store.summarize([], group_by="store_name")


def test_summarize_reads_only():
    data_store.summarize([], group_by="shopping_mall")
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


# --------------------------------------------------------------------------
# Updates
# --------------------------------------------------------------------------


def test_update_writes_the_new_value_and_reports_the_old():
    invoice = first_invoice()
    before = data_store.select([crit("invoice_no", "equals", invoice)])[0]["payment_method"]

    outcome = data_store.update_record(
        invoice, "payment_method", "Cash", snapshot_tag="rec-1"
    )

    assert outcome["old_value"] == before
    assert outcome["new_value"] == "Cash"
    assert data_store.select([crit("invoice_no", "equals", invoice)])[0][
        "payment_method"
    ] == "Cash"


def test_update_touches_exactly_one_row():
    before = data_store.load_rows()
    data_store.update_record(first_invoice(), "age", "99", snapshot_tag="rec-1")
    after = data_store.load_rows()

    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(changed) == 1


def test_update_rejects_an_unknown_invoice():
    with pytest.raises(RecordNotFoundError):
        data_store.update_record("I000000", "age", "30", snapshot_tag="rec-1")


def test_primary_key_is_not_writable():
    with pytest.raises(UnknownFieldError, match="primary key"):
        data_store.update_record(first_invoice(), "invoice_no", "I999999", snapshot_tag="rec-1")


# --------------------------------------------------------------------------
# Single-row delete
# --------------------------------------------------------------------------


def test_delete_record_removes_exactly_one_row():
    invoice = first_invoice()
    outcome = data_store.delete_record(invoice, snapshot_tag="rec-1")

    assert outcome["deleted_count"] == 1
    assert outcome["deleted_ids"] == [invoice]
    assert outcome["remaining"] == SEED_ROW_COUNT - 1
    assert data_store.select([crit("invoice_no", "equals", invoice)]) == []


def test_delete_record_returns_what_it_destroyed():
    """The audit record should show what was deleted, not merely that something was."""
    invoice = first_invoice()
    outcome = data_store.delete_record(invoice, snapshot_tag="rec-1")
    assert outcome["deleted_row"][data_store.ID_FIELD] == invoice
    assert set(outcome["deleted_row"]) == set(data_store.FIELDS)


def test_delete_record_rejects_an_unknown_invoice():
    with pytest.raises(RecordNotFoundError):
        data_store.delete_record("I000000", snapshot_tag="rec-1")
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


def test_deleting_one_row_leaves_identical_siblings_alone():
    """Two transactions can share every value but invoice_no. Deleting one must
    not take the other with it."""
    rows = data_store.load_rows()
    twin = dict(rows[0])
    twin["invoice_no"] = "I-TWIN-1"
    data_store._write_rows([*rows, twin])

    data_store.delete_record("I-TWIN-1", snapshot_tag="rec-1")

    assert len(data_store.load_rows()) == SEED_ROW_COUNT
    assert data_store.select([crit("invoice_no", "equals", rows[0]["invoice_no"])])


# --------------------------------------------------------------------------
# Bulk delete
# --------------------------------------------------------------------------


def test_bulk_delete_removes_only_matching_rows():
    criteria = [crit("category", "equals", "Clothing")]
    expected = data_store.count_matching(criteria)

    outcome = data_store.delete_matching(criteria, snapshot_tag="rec-1")

    assert outcome["deleted_count"] == expected
    assert outcome["remaining"] == SEED_ROW_COUNT - expected
    assert data_store.count_matching(criteria) == 0


def test_bulk_delete_with_no_criteria_is_refused():
    """An empty filter is almost always a model that failed to express its
    filter, not a genuine request to wipe the table."""
    with pytest.raises(UnboundedDeleteError):
        data_store.delete_matching([], snapshot_tag="rec-1")
    assert len(data_store.load_rows()) == SEED_ROW_COUNT


def test_deleting_everything_is_possible_but_must_be_explicit():
    outcome = data_store.delete_matching([], snapshot_tag="rec-1", allow_unbounded=True)
    assert outcome["deleted_count"] == SEED_ROW_COUNT
    assert outcome["remaining"] == 0


def test_bulk_delete_caps_the_ids_it_names():
    """A 34,000-entry list of invoice numbers in an audit record helps nobody."""
    outcome = data_store.delete_matching(
        [crit("category", "equals", "Clothing")], snapshot_tag="rec-1"
    )
    assert len(outcome["deleted_ids"]) <= data_store.DELETED_ID_SAMPLE
    assert outcome["deleted_ids_truncated"] is (
        outcome["deleted_count"] > data_store.DELETED_ID_SAMPLE
    )


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------


def test_update_is_reversible_via_its_snapshot():
    """This is what makes the model's "reversible" classification a property of
    the system rather than a label it typed."""
    invoice = first_invoice()
    original = data_store.select([crit("invoice_no", "equals", invoice)])[0]["age"]

    data_store.update_record(invoice, "age", "99", snapshot_tag="rec-1")
    data_store.restore_snapshot("rec-1")

    assert data_store.select([crit("invoice_no", "equals", invoice)])[0]["age"] == original


def test_bulk_delete_is_recoverable_by_an_operator():
    """The agent is told deletion is irreversible, and it is -- from the agent's
    side. The snapshot is the operator's safety net, not a promise to the model."""
    data_store.delete_matching([crit("category", "equals", "Clothing")], snapshot_tag="rec-9")
    assert len(data_store.load_rows()) < SEED_ROW_COUNT

    assert data_store.restore_snapshot("rec-9") == SEED_ROW_COUNT


def test_single_delete_is_recoverable_too():
    data_store.delete_record(first_invoice(), snapshot_tag="rec-2")
    assert data_store.restore_snapshot("rec-2") == SEED_ROW_COUNT


def test_snapshot_is_taken_before_the_write_not_after():
    invoice = first_invoice()
    data_store.update_record(invoice, "payment_method", "Cash", snapshot_tag="rec-1")

    snapshot = data_store.snapshot_dir() / "rec-1.csv"
    with snapshot.open(newline="", encoding="utf-8") as handle:
        snapshotted = {r["invoice_no"]: r for r in csv.DictReader(handle)}
    assert len(snapshotted) == SEED_ROW_COUNT
    assert invoice in snapshotted


def test_restoring_an_unknown_snapshot_is_an_error():
    with pytest.raises(SnapshotNotFoundError):
        data_store.restore_snapshot("never-happened")


def test_missing_data_file_is_loud(monkeypatch, tmp_path):
    """A missing CSV is a deployment problem, not an empty result set."""
    monkeypatch.setenv("CUSTOMER_DATA_PATH", str(tmp_path / "absent.csv"))
    with pytest.raises(DataStoreError, match="not found"):
        data_store.load_rows()
