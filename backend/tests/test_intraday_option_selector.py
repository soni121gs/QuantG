"""IMD-06 — no-lookahead intraday option selection (pure logic)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.intraday_option_selector import (  # noqa: E402
    BAD_DIRECTION,
    MISSING_LEG,
    OK,
    select_contract,
)


def _row(price, key="k"):
    return {"close": price, "instrument_key": key, "expired_instrument_key": key + "|exp"}


def _chain(expiry="2025-01-09"):
    strikes = {}
    for s in (24000, 24050, 24100, 24150, 24200, 24250, 24300, 24350, 24400):
        strikes[float(s)] = {"CE": _row(max(1, 24200 - s + 100), f"CE{s}"),
                             "PE": _row(max(1, s - 24200 + 100), f"PE{s}")}
    return {expiry: strikes}


def test_single_leg_atm_call():
    sel = select_contract(underlying="NIFTY", spot=24215, chain=_chain(), direction="CE")
    assert sel.ok and sel.reason == OK and sel.structure == "single_leg"
    assert len(sel.legs) == 1
    assert sel.legs[0].strike == 24200.0 and sel.legs[0].side == "BUY"
    assert sel.lot_size == 65


def test_debit_spread_call_pairs_wing_higher():
    sel = select_contract(underlying="NIFTY", spot=24215, chain=_chain(),
                          direction="CE", structure="debit_spread", spread_width=2)
    assert sel.ok and sel.structure == "debit_spread"
    buy, sell = sel.legs
    assert buy.side == "BUY" and buy.strike == 24200.0
    assert sell.side == "SELL" and sell.strike == 24300.0  # +2 strikes (50 step)


def test_debit_spread_put_pairs_wing_lower():
    sel = select_contract(underlying="NIFTY", spot=24215, chain=_chain(),
                          direction="PE", structure="debit_spread", spread_width=2)
    buy, sell = sel.legs
    assert buy.option_type == "PE" and buy.strike == 24200.0
    assert sell.strike == 24100.0  # -2 strikes


def test_otm_offset_shifts_call_up():
    sel = select_contract(underlying="NIFTY", spot=24215, chain=_chain(),
                          direction="CE", otm_offset_strikes=1)
    assert sel.legs[0].strike == 24250.0


def test_missing_wing_leg_typed():
    chain = _chain()
    # strip the strike a CE width-2 spread from ATM would need
    del chain["2025-01-09"][24300.0]
    sel = select_contract(underlying="NIFTY", spot=24215, chain=chain,
                          direction="CE", structure="debit_spread", spread_width=2)
    assert not sel.ok and sel.reason == MISSING_LEG


def test_bad_direction():
    sel = select_contract(underlying="NIFTY", spot=24215, chain=_chain(), direction="XX")
    assert sel.reason == BAD_DIRECTION


def test_no_lookahead_future_strike_does_not_change_pick():
    """Selection must depend only on the passed snapshot — injecting a strike that
    only appears in a *later* snapshot must not alter the current pick."""
    now = select_contract(underlying="NIFTY", spot=24215, chain=_chain(), direction="CE")
    future_chain = _chain()
    future_chain["2025-01-09"][24210.0] = {"CE": _row(999, "future"), "PE": _row(1, "future")}
    # the current snapshot (without 24210) still resolves ATM to 24200
    assert now.legs[0].strike == 24200.0
    # a snapshot that *did* contain 24210 would pick it — proving the input drives it
    later = select_contract(underlying="NIFTY", spot=24215, chain=future_chain, direction="CE")
    assert later.legs[0].strike == 24210.0
