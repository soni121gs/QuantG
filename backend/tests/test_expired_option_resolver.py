"""IMD-02 — expired-contract resolver (pure logic, mocked fetchers)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.expired_option_resolver import (  # noqa: E402
    BLOCKED_UNDERLYING,
    EXPIRY_NOT_FOUND,
    FETCH_ERROR,
    OK,
    OPTION_TYPE_INVALID,
    STRIKE_NOT_FOUND,
    UNSUPPORTED_UNDERLYING,
    ExpiredOptionResolver,
    atm_strike,
    select_weekly_expiry,
)


def _contract(strike, opt, expiry="2025-01-09"):
    return {
        "expired_instrument_key": f"NSE_FO|{int(strike)}{opt}|{expiry}",
        "instrument_key": f"NSE_FO|{int(strike)}{opt}",
        "trading_symbol": f"NIFTY {int(strike)} {opt} 09 JAN 25",
        "strike_price": float(strike),
        "instrument_type": opt,
        "lot_size": 75,
        "expiry": expiry,
        "access_token": "SECRET_SHOULD_NOT_PERSIST",
    }


def _chain_fetcher(expiry="2025-01-09"):
    contracts = [_contract(s, o, expiry) for s in (24100, 24200, 24300) for o in ("CE", "PE")]

    calls = {"n": 0}

    def fetch(underlying_key, expiry_date):
        calls["n"] += 1
        return contracts

    fetch.calls = calls
    return fetch


def _resolver(fetch=None, expiries=None):
    fetch = fetch or _chain_fetcher()
    exp = (lambda key: expiries) if expiries is not None else None
    return ExpiredOptionResolver(fetch, exp)


# ---- happy path -------------------------------------------------------------
def test_resolve_exact_strike_ce():
    r = _resolver().resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "CE")
    assert r.ok and r.reason == OK
    assert r.contract.expired_instrument_key == "NSE_FO|24200CE|2025-01-09"
    assert r.contract.option_type == "CE"
    assert r.contract.lot_size == 75


def test_resolve_pe():
    r = _resolver().resolve("NIFTY", "2025-01-06", "2025-01-09", 24100, "PE")
    assert r.ok and r.contract.option_type == "PE"
    assert r.contract.strike == 24100.0


def test_resolved_contract_never_carries_token():
    r = _resolver().resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "CE")
    assert "access_token" not in vars(r.contract)


# ---- ATM rounding -----------------------------------------------------------
def test_atm_rounding_nifty():
    assert atm_strike("NIFTY", 24218) == 24200.0
    assert atm_strike("NIFTY", 24230) == 24250.0


def test_atm_rounding_banknifty():
    assert atm_strike("BANKNIFTY", 51040) == 51000.0
    assert atm_strike("BANKNIFTY", 51060) == 51100.0


def test_resolve_atm_uses_rounded_strike():
    r = _resolver().resolve_atm("NIFTY", "2025-01-06", "2025-01-09", 24215, "CE")
    assert r.ok and r.contract.strike == 24200.0


# ---- weekly expiry selection ------------------------------------------------
def test_select_weekly_expiry_picks_nearest_future():
    expiries = ["2025-01-02", "2025-01-09", "2025-01-16"]
    assert select_weekly_expiry(expiries, "2025-01-06") == "2025-01-09"
    assert select_weekly_expiry(expiries, "2025-01-09") == "2025-01-09"
    assert select_weekly_expiry(expiries, "2025-01-20") is None


def test_available_expiries_sorted():
    r = _resolver(expiries=["2025-01-16", "2025-01-02", "2025-01-09"])
    assert r.available_expiries("NIFTY") == ["2025-01-02", "2025-01-09", "2025-01-16"]


# ---- missing / blocked / invalid --------------------------------------------
def test_missing_strike_returns_typed_reason_not_guess():
    r = _resolver().resolve("NIFTY", "2025-01-06", "2025-01-09", 25000, "CE")
    assert not r.ok and r.reason == STRIKE_NOT_FOUND
    assert r.contract is None


def test_empty_chain_is_expiry_not_found():
    r = ExpiredOptionResolver(lambda k, e: []).resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "CE")
    assert r.reason == EXPIRY_NOT_FOUND


def test_blocked_bse_underlying():
    r = _resolver().resolve("SENSEX", "2025-01-06", "2025-01-09", 80000, "CE")
    assert r.reason == BLOCKED_UNDERLYING


def test_unsupported_underlying():
    r = _resolver().resolve("FINNIFTY", "2025-01-06", "2025-01-09", 23000, "CE")
    assert r.reason == UNSUPPORTED_UNDERLYING


def test_invalid_option_type():
    r = _resolver().resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "XX")
    assert r.reason == OPTION_TYPE_INVALID


def test_fetch_error_is_typed():
    def boom(k, e):
        raise RuntimeError("network down")

    r = ExpiredOptionResolver(boom).resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "CE")
    assert r.reason == FETCH_ERROR and r.contract is None


# ---- caching ----------------------------------------------------------------
def test_repeated_strike_lookups_hit_fetcher_once():
    fetch = _chain_fetcher()
    r = _resolver(fetch=fetch)
    r.resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "CE")
    r.resolve("NIFTY", "2025-01-06", "2025-01-09", 24100, "PE")
    assert fetch.calls["n"] == 1


def test_file_cache_persists_across_instances():
    import shutil

    cache_dir = os.path.join(os.path.dirname(__file__), "_tmp_expired_cache")
    shutil.rmtree(cache_dir, ignore_errors=True)
    try:
        fetch = _chain_fetcher()
        r1 = ExpiredOptionResolver(fetch, cache_dir=cache_dir)
        r1.resolve("NIFTY", "2025-01-06", "2025-01-09", 24200, "CE")

        def fail(k, e):
            raise AssertionError("should have read from disk cache")

        r2 = ExpiredOptionResolver(fail, cache_dir=cache_dir)
        r = r2.resolve("NIFTY", "2025-01-06", "2025-01-09", 24300, "PE")
        assert r.ok and r.contract.strike == 24300.0
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)
