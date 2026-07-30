"""Adaptive threshold calibration.

The bonus dimension. When a class of action gets confirmed by humans over and
over without modification, the engine should stop pestering them for it: the
signal is that this particular action_type is safer than the model thinks. And
when a class of action gets rejected over and over, the engine should stop
letting it through so easily.

What it does *not* do
---------------------
Two invariants make this safe to leave running unattended:

1. It cannot lower supervision below what the blast-radius floor demands.
   Calibration runs *before* the floor in ``main.py``; a bulk delete that
   calibration nudges to ``low`` still gets escalated back to ``full_review`` by
   the floor because scope is a fact and does not care about history.
2. A single lucky streak cannot flip a band. The signal count must exceed
   :data:`MIN_SIGNALS_FOR_SHIFT` before any shift applies, and even then the
   band moves at most one step per call -- an action_type with 100 net confirms
   moves the same distance as one with 10.

Storage
-------
A JSON file keyed by ``action_type``. Path from ``CALIBRATION_PATH``, defaulting
to ``data/action_type_calibration.json``. On Lambda where the local filesystem
is read-only outside ``/tmp``, point it at ``/tmp/calibration.json`` -- it will
not survive a cold start but a demo does not need it to. A production
deployment would swap this file for a DynamoDB item and keep the same
interface.

Threading
---------
Every operation reads the file, mutates, writes back. Concurrent writers from
two Lambda invocations would race and lose signals. That is acceptable here --
a lost signal in either direction is one out of hundreds and calibration
converges regardless -- but a production version would want an atomic
DynamoDB ``UpdateExpression`` instead.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Final, TypeAlias

from autonomy_engine.risk_scorer import LEVEL_ORDER, AutonomyLevel

logger = logging.getLogger(__name__)

Signal: TypeAlias = int  # +1 for confirm, -1 for reject


#: Net signals (confirms minus rejects) required before calibration adjusts the
#: band at all. Set high enough that a small handful of reviews cannot flip the
#: routing on a novel action type. Ten is the number the brief suggests.
MIN_SIGNALS_FOR_SHIFT: Final[int] = 10

#: The three keys stored per action_type. ``band_offset`` is derived from the
#: two counters at write time, not computed on read, so a human inspecting the
#: file can see the signed value directly instead of doing subtraction.
_FIELDS: Final[tuple[str, ...]] = (
    "confirms_without_modification",
    "rejects_or_modifications",
    "band_offset",
)


# --------------------------------------------------------------------------
# Path / IO
# --------------------------------------------------------------------------


def _path() -> Path:
    """Where the calibration JSON lives.

    Resolved every call rather than cached because tests monkeypatch the
    environment per case, and a cached path would leak between them.
    """
    default = Path(__file__).resolve().parents[2] / "data" / "action_type_calibration.json"
    return Path(os.getenv("CALIBRATION_PATH", str(default)))


def _load() -> dict[str, dict[str, float]]:
    """Read the calibration table off disk.

    A missing file is not an error: it just means no signals have been recorded
    yet. Corruption *is* an error -- an unreadable calibration file is safer to
    fail loudly on than to silently reset, because a reset would erase real
    signal.
    """
    path = _path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.error("calibration file at %s is not valid JSON", path)
        raise
    if not isinstance(data, dict):
        raise ValueError(f"calibration file {path} must contain a JSON object")
    return data


def _save(table: dict[str, dict[str, float]]) -> None:
    """Atomically write the calibration table back to disk."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(table, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Recording signals
# --------------------------------------------------------------------------


def record_signal(action_type: str, *, positive: bool) -> dict[str, float]:
    """Record one human decision as a signal on ``action_type``.

    Args:
        action_type: The audit vocabulary for the action, e.g. ``bulk_delete``.
            This is the key calibration groups by -- specific enough that
            different tools are learned separately, not so specific that no two
            actions ever share a bucket.
        positive: ``True`` for a confirm/approve; ``False`` for a reject.
            "Modification" is not modelled here because the current API has no
            modify-then-approve path; a reject is the only negative signal
            available.

    Returns:
        The updated entry for this action_type, useful for logging.
    """
    table = _load()
    entry = table.get(action_type) or {k: 0.0 for k in _FIELDS}

    if positive:
        entry["confirms_without_modification"] = _int(entry.get("confirms_without_modification")) + 1
    else:
        entry["rejects_or_modifications"] = _int(entry.get("rejects_or_modifications")) + 1

    entry["band_offset"] = _compute_offset(
        confirms=_int(entry["confirms_without_modification"]),
        rejects=_int(entry["rejects_or_modifications"]),
    )

    table[action_type] = entry
    _save(table)

    logger.info(
        "calibration signal recorded",
        extra={
            "action_type": action_type,
            "positive": positive,
            "confirms": entry["confirms_without_modification"],
            "rejects": entry["rejects_or_modifications"],
            "band_offset": entry["band_offset"],
        },
    )
    return entry


def _int(value: object) -> int:
    """Coerce a stored counter to int. Tolerates the JSON module returning floats."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _compute_offset(*, confirms: int, rejects: int) -> float:
    """Derive the signed offset from the two counters.

    Negative offset means "shift toward autonomous", positive means "shift
    toward full_review". Zero until the net signal crosses
    :data:`MIN_SIGNALS_FOR_SHIFT`; one step in the indicated direction
    thereafter. The magnitude does not keep growing with more signals -- a
    hundred confirms should not run away with the routing.
    """
    net = confirms - rejects
    if net >= MIN_SIGNALS_FOR_SHIFT:
        return -1.0
    if net <= -MIN_SIGNALS_FOR_SHIFT:
        return 1.0
    return 0.0


# --------------------------------------------------------------------------
# Applying calibration to a routing decision
# --------------------------------------------------------------------------


def apply_calibration(
    decision: AutonomyLevel,
    action_type: str,
) -> tuple[AutonomyLevel, str | None]:
    """Nudge a routing decision by the calibration learned for this action_type.

    A negative offset shifts one level toward autonomous; a positive offset
    shifts one level toward full_review. The shift is capped to one step -- the
    engine learns, but slowly, and always audibly.

    Args:
        decision: The model's routed decision.
        action_type: Audit vocabulary key. If unknown, no shift is applied.

    Returns:
        ``(new_decision, note)`` where ``note`` is ``None`` if calibration made
        no change, and otherwise a human-readable explanation for the audit
        trail. An adjustment nobody can see after the fact would be worse than
        no adjustment at all.
    """
    entry = _load().get(action_type)
    if not entry:
        return decision, None

    offset = float(entry.get("band_offset", 0.0))
    if offset == 0.0:
        return decision, None

    idx = LEVEL_ORDER.index(decision)
    if offset < 0 and idx > 0:
        new_idx = idx - 1
    elif offset > 0 and idx < len(LEVEL_ORDER) - 1:
        new_idx = idx + 1
    else:
        # Already at the extreme in the shift direction; nothing to do.
        return decision, None

    new_decision: AutonomyLevel = LEVEL_ORDER[new_idx]  # type: ignore[assignment]
    confirms = _int(entry.get("confirms_without_modification"))
    rejects = _int(entry.get("rejects_or_modifications"))
    direction = "lowered" if offset < 0 else "raised"
    note = (
        f"calibration {direction} {decision} -> {new_decision}: "
        f"{action_type} has {confirms} confirms and {rejects} rejects "
        f"(net {confirms - rejects}, threshold {MIN_SIGNALS_FOR_SHIFT})"
    )
    return new_decision, note


# --------------------------------------------------------------------------
# Introspection (for the API/UI, and for tests)
# --------------------------------------------------------------------------


def snapshot() -> dict[str, dict[str, float]]:
    """Full calibration table, for debugging and the /calibration endpoint."""
    return _load()


def reset() -> None:
    """Delete the calibration file. Tests use this; production should not."""
    path = _path()
    if path.exists():
        path.unlink()
