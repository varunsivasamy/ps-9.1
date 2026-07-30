"""CSV-backed transaction store -- the thing the agent's actions actually touch.

Up to Phase 4 the engine scored and routed actions but never performed them: an
approved deletion flipped an audit status and nothing was deleted. That made the
demo hard to believe, because "irreversible" was a label the model typed rather
than a property of the system.

This module is the other half. ``data/customer_shopping_data.csv`` holds ~99,000
real retail transactions, and the tools in :mod:`autonomy_engine.agent_actions`
map onto real reads and writes against it. A CSV rather than a database on
purpose: a human can open the file, approve a deletion, and watch the rows go.

Why the scale matters
---------------------
At 99k rows the gap between "delete invoice I138884" and "delete every Clothing
transaction" is one row versus thirty-four thousand. That gap is the entire
point of a graduated autonomy engine, and it is only visible on a dataset big
enough for a careless filter to be catastrophic. It also means reads are capped
and aggregation is a first-class operation -- returning 34,487 rows to answer
"how much did Clothing make?" would be useless.

Dates
-----
The source data is day-first ``DD/MM/YYYY`` with inconsistent zero-padding
(``5/8/2022`` and ``16/05/2021`` both appear). Filters are written in
unambiguous ISO ``YYYY-MM-DD`` and parsed day-first on the way in, so the model
never has to guess which component is the month -- the one thing about this
dataset most likely to silently select the wrong rows.

Snapshots
---------
Every mutating operation copies the file to ``data/snapshots/<tag>.csv`` before
touching it, where ``tag`` is the audit record id of the action. That gives the
risk model's ``reversibility`` dimension teeth: a "reversible" update really can
be rolled back with :func:`restore_snapshot`. The copy is ~7 MB, which is cheap
next to the cost of an unrecoverable bulk delete.

Writes go through a temp file and :func:`os.replace`, so an interrupted write
leaves the original intact rather than a half-written CSV.
"""

from __future__ import annotations

import csv
import os
import shutil
from collections import Counter
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

#: Columns of the transaction CSV, in file order. Also the set of fields a
#: filter or an update may name -- anything else is rejected rather than
#: silently matching nothing.
FIELDS: Final[tuple[str, ...]] = (
    "invoice_no",
    "customer_id",
    "gender",
    "age",
    "category",
    "quantity",
    "price",
    "payment_method",
    "invoice_date",
    "shopping_mall",
)

#: The primary key: one row per invoice. Never writable by an update.
ID_FIELD: Final[str] = "invoice_no"

#: Fields holding day-first ``DD/MM/YYYY`` dates, so ``before``/``after`` mean
#: chronology rather than string ordering.
DATE_FIELDS: Final[frozenset[str]] = frozenset({"invoice_date"})

#: Fields compared numerically by ``greater_than``/``less_than``.
NUMERIC_FIELDS: Final[frozenset[str]] = frozenset({"age", "quantity", "price"})

#: Known values for the categorical columns. Handed to the model in its system
#: prompt so it filters on ``"Food & Beverage"`` rather than inventing
#: ``"food"`` and quietly matching nothing.
FIELD_VALUES: Final[dict[str, tuple[str, ...]]] = {
    "gender": ("Male", "Female"),
    "category": (
        "Clothing",
        "Shoes",
        "Books",
        "Cosmetics",
        "Food & Beverage",
        "Toys",
        "Technology",
        "Souvenir",
    ),
    "payment_method": ("Cash", "Credit Card", "Debit Card"),
    "shopping_mall": (
        "Kanyon",
        "Forum Istanbul",
        "Metrocity",
        "Metropol AVM",
        "Istinye Park",
        "Mall of Istanbul",
        "Emaar Square Mall",
        "Cevahir AVM",
        "Viaport Outlet",
        "Zorlu Center",
    ),
}

#: How dates appear in the file. Day-first: ``16/05/2021`` is 16 May.
STORAGE_DATE_FORMAT: Final[str] = "%d/%m/%Y"

#: How many deleted invoice numbers to name in a bulk-delete result. The
#: snapshot holds the full record; this is just enough for a human to
#: spot-check what went, without putting 34,000 ids in an audit row.
DELETED_ID_SAMPLE: Final[int] = 20

Operator: TypeAlias = Literal[
    "equals",
    "not_equals",
    "contains",
    "before",
    "after",
    "greater_than",
    "less_than",
]

#: Operators that only make sense on certain column types, and the fields they
#: are therefore restricted to.
_TYPED_OPERATORS: Final[dict[str, frozenset[str]]] = {
    "before": DATE_FIELDS,
    "after": DATE_FIELDS,
    "greater_than": NUMERIC_FIELDS,
    "less_than": NUMERIC_FIELDS,
}


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DataStoreError(RuntimeError):
    """Base class for transaction-store failures."""


class UnknownFieldError(DataStoreError):
    """A filter or update named a column that does not exist."""


class RecordNotFoundError(DataStoreError):
    """No transaction row matches the given invoice number."""


class UnboundedDeleteError(DataStoreError):
    """A bulk delete arrived with no filter criteria.

    Raised rather than deleting everything. An empty criteria list is almost
    always a model that failed to express its filter, not a genuine request to
    wipe 99,000 transactions, and that failure mode is not one worth being
    relaxed about.
    """


class SnapshotNotFoundError(DataStoreError):
    """No snapshot exists for the given tag."""


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


class Criterion(BaseModel):
    """One field/operator/value test. Multiple criteria are ANDed together."""

    field: str = Field(description="Column to test. Must be one of FIELDS.")
    operator: Operator = Field(description="How to compare the column to value.")
    value: str = Field(description="Value to compare against, as a string.")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

_DEFAULT_DATA_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "data" / "customer_shopping_data.csv"
)

# When running on Lambda the CSV lives in S3.
# Set CUSTOMER_DATA_S3_URI=s3://bucket/key.csv to enable.
_S3_URI: str | None = os.getenv("CUSTOMER_DATA_S3_URI") or None


def _s3_bucket_key() -> tuple[str, str] | None:
    """Parse CUSTOMER_DATA_S3_URI into (bucket, key). Returns None if not set."""
    if not _S3_URI:
        return None
    # s3://bucket/path/to/file.csv
    without_scheme = _S3_URI.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


def data_path() -> Path:
    """Location of the transaction CSV for local use. Override with CUSTOMER_DATA_PATH."""
    override = os.getenv("CUSTOMER_DATA_PATH")
    return Path(override) if override else _DEFAULT_DATA_PATH


def snapshot_dir() -> Path:
    """Directory holding pre-write snapshots. Override with CUSTOMER_SNAPSHOT_DIR."""
    override = os.getenv("CUSTOMER_SNAPSHOT_DIR")
    return Path(override) if override else Path("/tmp/snapshots")


# --------------------------------------------------------------------------
# File I/O  (local + S3)
# --------------------------------------------------------------------------

_S3_LOCAL_CACHE: Path = Path("/tmp/customer_shopping_data.csv")


def load_rows() -> list[dict[str, str]]:
    """Read every transaction row — from S3 on Lambda, from disk locally."""
    s3_loc = _s3_bucket_key()
    if s3_loc:
        return _load_rows_s3(*s3_loc)
    path = data_path()
    if not path.exists():
        raise DataStoreError(
            f"transaction data file not found at {path}. "
            "Set CUSTOMER_DATA_PATH or CUSTOMER_DATA_S3_URI."
        )
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_rows_s3(bucket: str, key: str) -> list[dict[str, str]]:
    """Download from S3 to /tmp cache, then read. Re-downloads if missing."""
    import io
    import boto3 as _boto3
    if not _S3_LOCAL_CACHE.exists():
        s3 = _boto3.client("s3")
        buf = io.BytesIO()
        s3.download_fileobj(bucket, key, buf)
        buf.seek(0)
        _S3_LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _S3_LOCAL_CACHE.write_bytes(buf.read())
    with _S3_LOCAL_CACHE.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_rows(rows: list[dict[str, str]]) -> None:
    """Overwrite the CSV atomically — writes to /tmp then syncs to S3 on Lambda."""
    s3_loc = _s3_bucket_key()
    if s3_loc:
        _write_rows_s3(rows, *s3_loc)
        return
    path = data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".csv.tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def _write_rows_s3(rows: list[dict[str, str]], bucket: str, key: str) -> None:
    """Write rows back to S3 and update the local /tmp cache."""
    import io
    import boto3 as _boto3
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(FIELDS))
    writer.writeheader()
    writer.writerows(rows)
    body = buf.getvalue().encode("utf-8")
    _boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body)
    # Invalidate local cache so next load_rows re-reads fresh data
    if _S3_LOCAL_CACHE.exists():
        _S3_LOCAL_CACHE.unlink()


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------


def take_snapshot(tag: str) -> str:
    """Copy the current CSV aside before a mutation, and return the copy's path.

    Args:
        tag: Identifier for the snapshot. Callers pass the audit record id, so
            the snapshot and the audit entry that authorised the change share a
            name and either one can find the other.
    """
    destination = snapshot_dir() / f"{tag}.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(data_path(), destination)
    return str(destination)


def restore_snapshot(tag: str) -> int:
    """Roll the CSV back to the snapshot taken before action ``tag``.

    Returns:
        The number of rows in the restored file.

    Raises:
        SnapshotNotFoundError: If no snapshot was taken under that tag.
    """
    source = snapshot_dir() / f"{tag}.csv"
    if not source.exists():
        raise SnapshotNotFoundError(f"no snapshot for {tag!r} at {source}")
    shutil.copy2(source, data_path())
    return len(load_rows())


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def _validate_field(field: str) -> None:
    if field not in FIELDS:
        raise UnknownFieldError(
            f"{field!r} is not a transaction field; expected one of {list(FIELDS)}"
        )


def _parse_stored_date(value: str, *, context: str) -> date:
    """Parse a date as it appears in the file: day-first ``DD/MM/YYYY``."""
    try:
        return datetime.strptime(value.strip(), STORAGE_DATE_FORMAT).date()
    except ValueError as exc:
        raise DataStoreError(
            f"{context}: stored date {value!r} is not in {STORAGE_DATE_FORMAT} form"
        ) from exc


def _parse_filter_date(value: str, *, context: str) -> date:
    """Parse a date supplied in a filter.

    ISO ``YYYY-MM-DD`` is the documented form and is tried first because it is
    unambiguous. Day-first is accepted as a fallback for a model that echoed the
    file's own format back at us -- rejecting that would be pedantry, but
    guessing between ``05/08`` and ``08/05`` would not be, hence ISO first.
    """
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, STORAGE_DATE_FORMAT).date()
    except ValueError as exc:
        raise DataStoreError(
            f"{context}: {value!r} is not a date; use ISO form, e.g. 2022-08-05"
        ) from exc


def _parse_number(value: str, *, context: str) -> float:
    try:
        return float(value.strip() or 0)
    except ValueError as exc:
        raise DataStoreError(f"{context}: {value!r} is not a number") from exc


def _matches(row: dict[str, str], criterion: Criterion) -> bool:
    """Test one row against one criterion.

    String comparisons are case-insensitive: the model writes ``"clothing"`` or
    ``"Clothing"`` interchangeably, and a filter that silently matched nothing
    because of casing would be scored as a safe no-op when it was really a bug.
    """
    _validate_field(criterion.field)

    allowed_fields = _TYPED_OPERATORS.get(criterion.operator)
    if allowed_fields is not None and criterion.field not in allowed_fields:
        raise DataStoreError(
            f"operator {criterion.operator!r} is not valid on field "
            f"{criterion.field!r}; it applies to {sorted(allowed_fields)}"
        )

    cell = row.get(criterion.field, "")
    wanted = criterion.value
    context = f"filter on {criterion.field}"

    if criterion.operator == "equals":
        return cell.strip().casefold() == wanted.strip().casefold()
    if criterion.operator == "not_equals":
        return cell.strip().casefold() != wanted.strip().casefold()
    if criterion.operator == "contains":
        return wanted.strip().casefold() in cell.casefold()
    if criterion.operator == "before":
        return _parse_stored_date(cell, context=context) < _parse_filter_date(
            wanted, context=context
        )
    if criterion.operator == "after":
        return _parse_stored_date(cell, context=context) > _parse_filter_date(
            wanted, context=context
        )
    if criterion.operator == "greater_than":
        return _parse_number(cell, context=context) > _parse_number(wanted, context=context)
    if criterion.operator == "less_than":
        return _parse_number(cell, context=context) < _parse_number(wanted, context=context)

    raise DataStoreError(f"unknown operator: {criterion.operator!r}")  # pragma: no cover


def select(criteria: list[Criterion]) -> list[dict[str, str]]:
    """Every row satisfying all criteria. Empty criteria selects everything."""
    rows = load_rows()
    return [row for row in rows if all(_matches(row, c) for c in criteria)]


def count_matching(criteria: list[Criterion]) -> int:
    """How many rows a filter would touch. Used to fact-check the agent's
    ``data_scope`` estimate against reality before anything is executed."""
    return len(select(criteria))


def _file_signature() -> tuple[str, int, float]:
    """Cheap identity for the current data file, for cache invalidation."""
    path = data_path()
    try:
        stat = path.stat()
        return (str(path), stat.st_size, stat.st_mtime)
    except OSError:
        return (str(path), -1, -1.0)


@lru_cache(maxsize=8)
def _distribution_cached(field: str, _signature: tuple[str, int, float]) -> dict[str, int]:
    counts: Counter[str] = Counter(row.get(field, "") for row in load_rows())
    return dict(counts.most_common())


def distribution(field: str) -> dict[str, int]:
    """How many rows hold each value of a categorical column, commonest first.

    Exists so the agent can be *told* the cardinalities instead of guessing
    them. "Delete every Souvenir transaction at Kanyon" is a request whose blast
    radius is unknowable from the text alone -- it could be five rows or five
    thousand -- and a model asked to estimate it will guess. Putting the real
    numbers in the system prompt turns that guess into a lookup.

    Cached against the file's size and mtime, so a delete that changes the
    counts invalidates it rather than leaving the agent reasoning from a stale
    picture of a table it just shrank.
    """
    _validate_field(field)
    return _distribution_cached(field, _file_signature())


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def query(criteria: list[Criterion], limit: int | None = None) -> list[dict[str, str]]:
    """Read rows matching a filter. Changes nothing."""
    rows = select(criteria)
    return rows[:limit] if limit is not None else rows


def summarize(criteria: list[Criterion], group_by: str | None = None) -> dict[str, Any]:
    """Aggregate matching rows instead of returning them.

    With ~99k rows, "how much revenue did Cosmetics make at Kanyon?" is a real
    question whose honest answer is one number, not 15,097 rows. Returning rows
    for that would blow past the read cap and answer nothing.

    Args:
        criteria: Filter to aggregate over.
        group_by: Optional column to break the totals down by.

    Returns:
        Overall ``transactions``/``total_quantity``/``total_revenue``, plus a
        ``groups`` breakdown when ``group_by`` is given.
    """
    if group_by is not None:
        _validate_field(group_by)

    rows = select(criteria)
    totals = {
        "transactions": len(rows),
        "total_quantity": sum(_parse_number(r["quantity"], context="quantity") for r in rows),
        "total_revenue": round(
            sum(
                _parse_number(r["price"], context="price")
                * _parse_number(r["quantity"], context="quantity")
                for r in rows
            ),
            2,
        ),
    }

    if group_by is None:
        return totals

    groups: dict[str, dict[str, float]] = {}
    for row in rows:
        bucket = groups.setdefault(
            row[group_by], {"transactions": 0, "total_quantity": 0.0, "total_revenue": 0.0}
        )
        quantity = _parse_number(row["quantity"], context="quantity")
        bucket["transactions"] += 1
        bucket["total_quantity"] += quantity
        bucket["total_revenue"] += _parse_number(row["price"], context="price") * quantity

    for bucket in groups.values():
        bucket["total_revenue"] = round(bucket["total_revenue"], 2)

    totals["groups"] = dict(
        sorted(groups.items(), key=lambda kv: kv[1]["total_revenue"], reverse=True)
    )
    return totals


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def _find_row(rows: list[dict[str, str]], invoice_no: str) -> dict[str, str] | None:
    wanted = invoice_no.strip().casefold()
    return next((r for r in rows if r[ID_FIELD].strip().casefold() == wanted), None)


def update_record(
    invoice_no: str,
    field: str,
    new_value: str,
    *,
    snapshot_tag: str,
) -> dict[str, Any]:
    """Write one field on one transaction row.

    Args:
        invoice_no: Row to change.
        field: Column to write. ``invoice_no`` itself is not writable.
        new_value: Value to store.
        snapshot_tag: Audit record id; the pre-write snapshot is filed under it.

    Returns:
        ``{"invoice_no", "field", "old_value", "new_value", "snapshot"}`` -- the
        before and after, so the audit entry can show what actually changed
        rather than only what was requested.

    Raises:
        UnknownFieldError: Unknown or non-writable column.
        RecordNotFoundError: No such invoice.
    """
    _validate_field(field)
    if field == ID_FIELD:
        raise UnknownFieldError(f"{ID_FIELD!r} is the primary key and cannot be updated")

    rows = load_rows()
    target = _find_row(rows, invoice_no)
    if target is None:
        raise RecordNotFoundError(f"no transaction with {ID_FIELD}={invoice_no!r}")

    snapshot = take_snapshot(snapshot_tag)
    old_value = target[field]
    target[field] = new_value
    _write_rows(rows)

    return {
        "invoice_no": target[ID_FIELD],
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "snapshot": snapshot,
    }


def delete_record(invoice_no: str, *, snapshot_tag: str) -> dict[str, Any]:
    """Delete exactly one transaction, by invoice number.

    Kept separate from :func:`delete_matching` rather than expressed as a
    one-row filter. Deleting a named invoice and deleting everything matching a
    predicate are different acts with different blast radii, and the engine
    should be able to tell them apart before a human is asked to approve one.

    Raises:
        RecordNotFoundError: No such invoice.
    """
    rows = load_rows()
    target = _find_row(rows, invoice_no)
    if target is None:
        raise RecordNotFoundError(f"no transaction with {ID_FIELD}={invoice_no!r}")

    snapshot = take_snapshot(snapshot_tag)
    remaining = [r for r in rows if r is not target]
    _write_rows(remaining)

    return {
        "deleted_count": 1,
        "deleted_ids": [target[ID_FIELD]],
        "deleted_row": dict(target),
        "remaining": len(remaining),
        "snapshot": snapshot,
    }


def delete_matching(
    criteria: list[Criterion],
    *,
    snapshot_tag: str,
    allow_unbounded: bool = False,
) -> dict[str, Any]:
    """Delete every row matching the filter.

    Args:
        criteria: The filter. Must be non-empty unless ``allow_unbounded``.
        snapshot_tag: Audit record id; the pre-delete snapshot is filed under it.
        allow_unbounded: Permit an empty filter, i.e. delete every transaction.
            Defaults to ``False`` and no caller in the engine passes ``True``.

    Returns:
        ``{"deleted_count", "deleted_ids", "remaining", "snapshot"}``.
        ``deleted_ids`` is capped -- a 34,000-entry list of invoice numbers in
        an audit record helps nobody.

    Raises:
        UnboundedDeleteError: Empty criteria without ``allow_unbounded``.
    """
    if not criteria and not allow_unbounded:
        raise UnboundedDeleteError(
            "refusing to delete with no filter criteria: this would remove every "
            "transaction. Pass allow_unbounded=True only if that is genuinely intended."
        )

    rows = load_rows()
    # Partitioned in one pass by identity rather than by filtering twice on
    # value: two transactions can legitimately hold identical field values, and
    # a value-based `not in` check would delete both when only one matched.
    doomed: list[dict[str, str]] = []
    survivors: list[dict[str, str]] = []
    for row in rows:
        (doomed if all(_matches(row, c) for c in criteria) else survivors).append(row)

    snapshot = take_snapshot(snapshot_tag)
    _write_rows(survivors)

    return {
        "deleted_count": len(doomed),
        "deleted_ids": [row[ID_FIELD] for row in doomed[:DELETED_ID_SAMPLE]],
        "deleted_ids_truncated": len(doomed) > DELETED_ID_SAMPLE,
        "remaining": len(survivors),
        "snapshot": snapshot,
    }
