"""Adaptive calibration.

Two directions to prove:

- Repeated confirms of the same action_type eventually lower its routing.
- Repeated rejects of the same action_type eventually raise it.

Plus two safety invariants that make the feature safe to leave running:

- Below the signal threshold, nothing changes.
- The blast-radius floor always beats calibration -- a bulk delete calibrated
  to autonomous still ends up at full_review because scope is a fact.
"""

from __future__ import annotations

from autonomy_engine import calibration, risk_scorer

# Isolation of the calibration path lives in conftest.py — every test gets its
# own file by default. Nothing extra needed here.


# --------------------------------------------------------------------------
# The two directions
# --------------------------------------------------------------------------


def test_repeated_confirms_lower_the_band():
    """Ten net confirms on the same action_type nudges routing one step down."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("single_record_write", positive=True)

    # Model routed this as confirm; history says humans always OK it -> autonomous.
    decision, note = calibration.apply_calibration("confirm", "single_record_write")
    assert decision == "autonomous"
    assert note is not None
    assert "lowered" in note
    assert "single_record_write" in note


def test_repeated_rejects_raise_the_band():
    """Ten net rejects on the same action_type nudges routing one step up."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("bulk_delete", positive=False)

    # Model routed this as confirm; history says humans always refuse -> full_review.
    decision, note = calibration.apply_calibration("confirm", "bulk_delete")
    assert decision == "full_review"
    assert note is not None
    assert "raised" in note


# --------------------------------------------------------------------------
# The threshold: a lucky handful cannot flip routing
# --------------------------------------------------------------------------


def test_below_threshold_makes_no_change():
    """A few confirms do not move the band -- calibration must earn its shift."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT - 1):
        calibration.record_signal("single_record_write", positive=True)

    decision, note = calibration.apply_calibration("confirm", "single_record_write")
    assert decision == "confirm"
    assert note is None


def test_unknown_action_type_makes_no_change():
    """An action_type never seen before is not shifted."""
    decision, note = calibration.apply_calibration("confirm", "brand_new_tool")
    assert decision == "confirm"
    assert note is None


def test_confirms_and_rejects_cancel_out():
    """Ten confirms and ten rejects net to zero -- no shift."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("bulk_delete", positive=True)
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("bulk_delete", positive=False)

    decision, note = calibration.apply_calibration("confirm", "bulk_delete")
    assert decision == "confirm"
    assert note is None


# --------------------------------------------------------------------------
# The extremes: calibration never leaves the routing table
# --------------------------------------------------------------------------


def test_autonomous_cannot_be_lowered_further():
    """Nothing sits below autonomous, so calibration cannot invent a level."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("read", positive=True)

    decision, note = calibration.apply_calibration("autonomous", "read")
    assert decision == "autonomous"
    assert note is None


def test_full_review_cannot_be_raised_further():
    """Nothing sits above full_review either."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("bulk_delete", positive=False)

    decision, note = calibration.apply_calibration("full_review", "bulk_delete")
    assert decision == "full_review"
    assert note is None


# --------------------------------------------------------------------------
# The safety invariant: calibration cannot train the floor away
# --------------------------------------------------------------------------


def test_blast_radius_floor_beats_calibration_relaxation():
    """A bulk delete lowered to autonomous by history still gets re-escalated.

    Calibration is a signal about what history says humans want. The floor is
    a fact about what the action actually touches. Facts win.
    """
    # Calibrate bulk_delete downward as much as possible.
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("bulk_delete", positive=True)

    # After calibration, a bulk delete banded confirm would drop to autonomous.
    decision, _ = calibration.apply_calibration("confirm", "bulk_delete")
    assert decision == "autonomous"

    # The floor then re-escalates because the action actually deletes 5000 rows.
    final, floor_note = risk_scorer.apply_blast_radius_floor(
        decision,
        actual_rows=5000,
        is_mutation=True,
        is_destructive=True,
    )
    assert final == "full_review"
    assert floor_note is not None


# --------------------------------------------------------------------------
# Storage shape: what a human opens the JSON and sees
# --------------------------------------------------------------------------


def test_signals_are_persisted_with_derived_offset():
    """The stored entry is the shape the file spec calls for."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("single_record_write", positive=True)

    table = calibration.snapshot()
    entry = table["single_record_write"]
    assert entry["confirms_without_modification"] == calibration.MIN_SIGNALS_FOR_SHIFT
    assert entry["rejects_or_modifications"] == 0
    assert entry["band_offset"] == -1.0


def test_each_action_type_is_calibrated_independently():
    """Confirms on one action_type do not shift another."""
    for _ in range(calibration.MIN_SIGNALS_FOR_SHIFT):
        calibration.record_signal("single_record_write", positive=True)

    # Bulk_delete has never been seen -- must not be shifted.
    decision, note = calibration.apply_calibration("confirm", "bulk_delete")
    assert decision == "confirm"
    assert note is None
