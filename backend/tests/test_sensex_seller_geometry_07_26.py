"""Regression guards for the SENSEX seller re-cut (2026-07-26).

SENSEX expires THURSDAY (verified from our own fills: 2026-07-09, -16, -23), so a
Monday entry faces 3 DTE — not the `target_dte_days: 2` the config decoratively
claims. `target_dte_days` is read by NO selection code (§21.5); the strategy takes
whatever expiry the chain offers. So the invariant has to be checked at the REAL
worst-case DTE the week presents, which is what these tests do.

At width 6 the feasible set was EMPTY at 3 DTE: no take-profit satisfied the cost
floor and reachability at the same time. Widening the wing is the only lever that
helps both, because it raises the credit without touching reachability.
"""
import pytest

from core.spread_builder import (
    credit_cost_floor,
    tp_reachability,
    SPREAD_MIN_TP_REACHABILITY,
    SPREAD_COST_FLOOR_MULT,
)

SENSEX_LOT = 20
MONDAY_DTE = 3          # Mon -> Thu expiry
DEPLOYED_TP = 0.50
DEPLOYED_HOLD = 330     # minutes
SQUARE_OFF_MIN = 15 * 60 + 40   # NSE F&O close from 2026-08-03 (was 15:30)
ENTRY_OPEN_MIN = 9 * 60 + 45

# REAL SENSEX fills, entered 2026-07-20 (3 DTE, expiry 2026-07-23), width 800.
# (short premium, long premium) -> net credit.
REAL_W800_3DTE = [(276.64, 107.43), (314.04, 127.51), (330.04, 143.02)]
# The same day at width 600, for the contrast that forced the re-cut.
REAL_W600_3DTE = [(288.38, 144.46)]


def _floor(short_px, long_px, width):
    credit = short_px - long_px
    return credit_cost_floor(credit, width, lot_size=SENSEX_LOT,
                             tp_frac=DEPLOYED_TP, leg_premium_sum=short_px + long_px)


@pytest.mark.parametrize("short_px,long_px", REAL_W800_3DTE)
def test_width_800_clears_the_cost_floor_on_real_3dte_fills(short_px, long_px):
    res = _floor(short_px, long_px, 800.0)
    assert res["passed"], f"cost multiple {res['cost_multiple']} < {SPREAD_COST_FLOOR_MULT}"
    assert res["cost_multiple"] >= SPREAD_COST_FLOOR_MULT


@pytest.mark.parametrize("short_px,long_px", REAL_W600_3DTE)
def test_width_600_failed_the_cost_floor_which_is_why_the_wing_widened(short_px, long_px):
    """Pins the defect, so a revert to width 6 cannot pass silently."""
    assert not _floor(short_px, long_px, 600.0)["passed"]


def test_deployed_geometry_clears_reachability_at_the_real_monday_dte():
    ratio = tp_reachability(DEPLOYED_TP, MONDAY_DTE, DEPLOYED_HOLD)["ratio"]
    assert ratio >= SPREAD_MIN_TP_REACHABILITY, (
        f"theta supplies only {ratio:.3f} of the TP target at {MONDAY_DTE} DTE")


def test_the_old_300_minute_hold_did_not_and_qg_o4_tp_060_never_could():
    """Both prior settings are pinned as failures.

    tp 0.60 is the trap the 07-22 cost-floor patch created: it cleared the floor by
    raising the target beyond anything theta could reach, so QG-O4 could never build
    on a Monday at ANY hold the session allows.
    """
    assert tp_reachability(DEPLOYED_TP, MONDAY_DTE, 300)["ratio"] < SPREAD_MIN_TP_REACHABILITY
    max_possible_hold = SQUARE_OFF_MIN - ENTRY_OPEN_MIN     # 345, entering at the open
    assert tp_reachability(0.60, MONDAY_DTE, max_possible_hold)["ratio"] < SPREAD_MIN_TP_REACHABILITY


def test_monday_entry_window_is_bounded_by_the_square_off_not_by_time_exit():
    """Documents the real constraint: past ~10:20 the hold left before the
    square-off is shorter than reachability needs, so raising time_exit further buys
    nothing. If the SENSEX sellers look idle after that, that is the law, not a bug.

    The bound moved when the session lengthened (2026-08-03): a 385-minute day
    contains MORE decay-minutes per DTE, so a fixed hold captures a smaller
    FRACTION of the total decay and the reachability law tightens slightly. The
    extra 10 minutes of session do not extend the feasible entry window."""
    feasible = [
        m for m in range(ENTRY_OPEN_MIN, 15 * 60 + 1, 5)
        if tp_reachability(DEPLOYED_TP, MONDAY_DTE,
                           min(DEPLOYED_HOLD, SQUARE_OFF_MIN - m))["ratio"]
        >= SPREAD_MIN_TP_REACHABILITY
    ]
    assert feasible, "SENSEX must be buildable somewhere in the Monday session"
    assert feasible[0] == ENTRY_OPEN_MIN            # 09:45
    assert feasible[-1] == 10 * 60 + 20             # 10:20
    # A much larger time_exit cannot extend it — the square-off binds.
    wider = [
        m for m in range(ENTRY_OPEN_MIN, 15 * 60 + 1, 5)
        if tp_reachability(DEPLOYED_TP, MONDAY_DTE,
                           min(600, SQUARE_OFF_MIN - m))["ratio"]
        >= SPREAD_MIN_TP_REACHABILITY
    ]
    assert wider == feasible


def test_capital_cap_admits_at_least_one_lot_at_the_wider_wing():
    """§21.4: `lots_for_risk` floor-divides, so a cap left at the width-6 level would
    size to ZERO lots and stand down silently — indistinguishable from a veto."""
    from core.spread_builder import lots_for_risk

    worst_credit = min(s - l for s, l in REAL_W800_3DTE)
    max_loss_per_unit = 800.0 - worst_credit
    assert lots_for_risk(max_loss_per_unit, SENSEX_LOT, 13000.0) >= 1
    assert lots_for_risk(max_loss_per_unit, SENSEX_LOT, 10500.0) == 0    # the old cap
