"""IA-1 unit tests for core/dealer_positioning (GEX / dealer positioning)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import dealer_positioning as dp


def test_bs_call_put_parity():
    S, K, T, sig = 100.0, 100.0, 30 / 365, 0.2
    c = dp.bs_price(S, K, T, sig, True)
    p = dp.bs_price(S, K, T, sig, False)
    # put-call parity with r=0: C - P == S - K
    assert abs((c - p) - (S - K)) < 1e-6


def test_implied_vol_roundtrip():
    S, K, T = 100.0, 105.0, 20 / 365
    price = dp.bs_price(S, K, T, 0.25, True)
    iv = dp.implied_vol(price, S, K, T, True)
    assert iv is not None and abs(iv - 0.25) < 1e-3


def test_gamma_peaks_atm():
    S, T, sig = 100.0, 20 / 365, 0.2
    g_atm = dp.bs_gamma(S, 100.0, T, sig)
    g_otm = dp.bs_gamma(S, 130.0, T, sig)
    assert g_atm > g_otm > 0


def _row(oi, prem, spot=100.0, lot=1):
    return {"oi": oi, "close": prem, "settle": prem,
            "underlying_price": spot, "lot_size": lot}


def _chain(spot, T, sig):
    """Build a synthetic one-expiry chain around spot."""
    out = {}
    for K in [80, 90, 100, 110, 120]:
        cp = dp.bs_price(spot, K, T, sig, True)
        pp = dp.bs_price(spot, K, T, sig, False)
        out[float(K)] = {"CE": _row(1000, cp, spot), "PE": _row(1000, pp, spot)}
    return out


def test_positive_gex_when_calls_dominate():
    # heavy CALL OI relative to puts -> net positive GEX (dealers long gamma)
    T = 20 / 365
    ch = _chain(100.0, T, 0.2)
    for K in ch:
        ch[K]["CE"]["oi"] = 5000
        ch[K]["PE"]["oi"] = 100
    st = dp.dealer_state_from_chain(ch, 100.0, "2025-01-01", "2025-01-21", underlying="NIFTY")
    assert st.net_gex > 0
    assert st.gex_state == "POSITIVE"


def test_negative_gex_when_puts_dominate():
    T = 20 / 365
    ch = _chain(100.0, T, 0.2)
    for K in ch:
        ch[K]["CE"]["oi"] = 100
        ch[K]["PE"]["oi"] = 5000
    st = dp.dealer_state_from_chain(ch, 100.0, "2025-01-01", "2025-01-21", underlying="NIFTY")
    assert st.net_gex < 0
    assert st.gex_state == "NEGATIVE"


def test_oi_walls_identified():
    T = 20 / 365
    ch = _chain(100.0, T, 0.2)
    ch[120.0]["CE"]["oi"] = 99999     # max call OI -> call wall
    ch[80.0]["PE"]["oi"] = 99999      # max put OI -> put wall
    st = dp.dealer_state_from_chain(ch, 100.0, "2025-01-01", "2025-01-21")
    assert st.call_wall == 120.0
    assert st.put_wall == 80.0


def test_unknown_on_empty_chain():
    st = dp.dealer_state_from_chain({}, 100.0, "2025-01-01", "2025-01-21")
    assert st.gex_state == "UNKNOWN"
