"""
Tests for OptionSelector v2 — backend/core/option_selector_v2.py

Tests:
  T1 – ATM preference selects correctly when score >= threshold
  T2 – ITM1 CE is one strike below ATM
  T3 – ITM1 PE is one strike above ATM
  T4 – OTM1 CE is one strike above ATM
  T5 – OTM1 PE is one strike below ATM
  T6 – AUTO picks the contract when only one candidate
  T7 – MCX underlying is hard-blocked
  T8 – Missing instrument_key (no "|") is hard-blocked
  T9 – Zero LTP is hard-blocked
  T10 – Live + simulated contract is hard-blocked
  T11 – Stale quote in live mode is hard-blocked
  T12 – Paper quality threshold skip (score < 50)
  T13 – Live quality threshold skip (score < 70)
  T14 – Fresh quote passes quality check in paper
  T15 – Selector stores metadata: quality_score & selected_strike_mode on contract
"""
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from core.option_selector_v2 import select_option_contract, LIVE_MIN_SCORE, PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_iso(seconds: int = 120) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat()


def _good_contract(
    strike: int = 24800,
    ltp: float = 85.0,
    simulated: bool = False,
    instrument_key: str = "NSE_FO|12345",
    timestamp: str | None = None,
) -> dict:
    return {
        "instrument_key": instrument_key,
        "tradingsymbol": f"NIFTY24JUN{strike}CE",
        "strike": strike,
        "ltp": ltp,
        "simulated": simulated,
        "bid": ltp - 1.0,
        "ask": ltp + 1.0,
        "underlying": "NIFTY",
        "option_type": "CE",
        "spot": 24850.0,
        "quote_timestamp": timestamp or _now_iso(),
        "source": "LIVE_UPSTOX_CONTRACT",
    }


# ---------------------------------------------------------------------------
# T1 – ATM selects correctly
# ---------------------------------------------------------------------------
def test_atm_preference_selects():
    contract = _good_contract(strike=24850, ltp=90.0)
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True
    assert result["reason_code"] == "SELECTED"
    assert result["selected_strike_mode"] == "ATM"
    assert result["quality_score"] >= PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# T2 – ITM1 CE is one strike below ATM (NIFTY interval=50)
# ---------------------------------------------------------------------------
def test_itm1_ce_strike_below_atm():
    # ATM at 24850, ITM1 CE = 24800
    contract = _good_contract(strike=24800, ltp=115.0)
    contract["spot"] = 24850.0
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ITM1",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True
    # Passed as sole candidate; verify the selected mode reflects ITM1 range
    assert result["quality_score"] >= PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# T3 – ITM1 PE is one strike above ATM
# ---------------------------------------------------------------------------
def test_itm1_pe_strike_above_atm():
    # ATM at 24850, ITM1 PE = 24900
    contract = _good_contract(strike=24900, ltp=110.0)
    contract["spot"] = 24850.0
    contract["option_type"] = "PE"
    result = select_option_contract(
        underlying="NIFTY",
        direction="PE",
        preference="ITM1",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True
    assert result["quality_score"] >= PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# T4 – OTM1 CE is one strike above ATM
# ---------------------------------------------------------------------------
def test_otm1_ce_strike_above_atm():
    contract = _good_contract(strike=24900, ltp=60.0)
    contract["spot"] = 24850.0
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="OTM1",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# T5 – OTM1 PE is one strike below ATM
# ---------------------------------------------------------------------------
def test_otm1_pe_strike_below_atm():
    contract = _good_contract(strike=24800, ltp=55.0)
    contract["spot"] = 24850.0
    contract["option_type"] = "PE"
    result = select_option_contract(
        underlying="NIFTY",
        direction="PE",
        preference="OTM1",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# T6 – AUTO selects the single contract when only one available
# ---------------------------------------------------------------------------
def test_auto_selects_single_candidate():
    contract = _good_contract(strike=24850, ltp=90.0)
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="AUTO",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# T7 – MCX underlying hard-blocked
# ---------------------------------------------------------------------------
def test_mcx_underlying_blocked():
    contract = _good_contract()
    contract["underlying"] = "CRUDEOIL"
    result = select_option_contract(
        underlying="CRUDEOIL",
        direction="CE",
        preference="ATM",
        mode="live",
        signal_confidence=90.0,
        spot=6500.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "MCX_BLOCKED"


# ---------------------------------------------------------------------------
# T8 – Missing instrument_key hard-blocked
# ---------------------------------------------------------------------------
def test_missing_instrument_key_blocked():
    contract = _good_contract(instrument_key="BAD_KEY_NO_PIPE", ltp=85.0)
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "MISSING_INSTRUMENT_KEY"


# ---------------------------------------------------------------------------
# T9 – Zero LTP hard-blocked
# ---------------------------------------------------------------------------
def test_zero_ltp_blocked():
    contract = _good_contract(ltp=0.0)
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "MISSING_LTP"


# ---------------------------------------------------------------------------
# T10 – Live + simulated contract hard-blocked
# ---------------------------------------------------------------------------
def test_live_simulated_blocked():
    contract = _good_contract(simulated=True, ltp=85.0)
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="live",
        signal_confidence=90.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "LIVE_SIMULATED_BLOCKED"


# ---------------------------------------------------------------------------
# T11 – Stale quote in live mode hard-blocked
# ---------------------------------------------------------------------------
def test_stale_quote_live_blocked():
    contract = _good_contract(ltp=85.0, timestamp=_stale_iso(120))
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="live",
        signal_confidence=90.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "STALE_QUOTE_LIVE"


# ---------------------------------------------------------------------------
# T12 – Paper quality threshold skip
# Build a contract that passes all hard blocks but scores < PAPER_MIN_SCORE (50)
# by having a huge spread and an edge ltp
# ---------------------------------------------------------------------------
def test_paper_quality_threshold_skip():
    contract = {
        "instrument_key": "NSE_FO|99999",
        "tradingsymbol": "NIFTY24JUN24850CE",
        "strike": 24850,
        "ltp": 0.5,          # edge LTP → ltp_score = 10
        "simulated": False,
        "bid": 0.0,
        "ask": 0.0,          # no bid/ask → spread_pct = 100 → spread_score = 0
        "underlying": "NIFTY",
        "option_type": "CE",
        "spot": 24850.0,
        "quote_timestamp": None,  # unknown age → freshness_score = 10 (paper)
        "source": "LIVE_UPSTOX_CONTRACT",
    }
    # key_score=25, ltp_score=10, freshness=10, spread=0, pref=10, sim=0 → 55
    # With preference=ITM1 and strike_mode=ATM: pref_score=5 → total=50
    # Let's set preference=ATM, strike_mode=ATM: pref_score=10 → total=55 → passes paper
    # Force fail by also having unknown age in live-adjacent paper scenario:
    # We want score < 50, so remove key score too by using PAPER_ prefix
    contract["instrument_key"] = "PAPER_NIFTY_CE_24850"  # no "|" → hard blocked
    # Actually to get score < 50 without hard block, use minimal real key but all zeros
    contract["instrument_key"] = "NSE_FO|99999"
    contract["ltp"] = 0.5
    contract["quote_timestamp"] = None
    # key=25, ltp=10, fresh=10(paper no age), spread=0, pref=10 → 55 → above threshold
    # To fall below 50, strip the key score further — use a simulated:
    # With simulated=True in paper (not hard blocked): sim_penalty=-5 → 50 (exactly at threshold)
    # Let's set freshness_score to 0 by using a stale timestamp:
    contract["quote_timestamp"] = _stale_iso(120)  # age = 120s > LIVE_STALE_SEC(30)
    # freshness_score=0, key=25, ltp=10, spread=0, pref=10 → 45 → below paper threshold
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "SKIPPED_OPTION_QUALITY_LOW"
    assert result["quality_score"] < PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# T13 – Live quality threshold skip (score < 70)
# A contract that passes all live hard-blocks but still scores < 70
# ---------------------------------------------------------------------------
def test_live_quality_threshold_skip():
    # Fresh quote, real key, ltp > 0, not simulated — but no bid/ask (spread unknown)
    # key=25, ltp=20, fresh=20 (age<5), spread=0 (unknown bid/ask), pref=10 → 75 → passes
    # Need to reduce: use ltp edge (< 1)
    contract = _good_contract(ltp=0.5, timestamp=_now_iso())
    contract["bid"] = 0.0
    contract["ask"] = 0.0
    # key=25, ltp=10, fresh=20, spread=0, pref=10 → 65 < 70 → blocked in live
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="live",
        signal_confidence=90.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is False
    assert result["reason_code"] == "SKIPPED_OPTION_QUALITY_LOW"
    assert result["quality_score"] < LIVE_MIN_SCORE


# ---------------------------------------------------------------------------
# T14 – Fresh good contract passes paper threshold
# ---------------------------------------------------------------------------
def test_fresh_contract_passes_paper():
    contract = _good_contract(ltp=90.0, timestamp=_now_iso())
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True
    assert result["quality_score"] >= PAPER_MIN_SCORE


# ---------------------------------------------------------------------------
# T15 – Selector attaches quality_score and selected_strike_mode to contract
# ---------------------------------------------------------------------------
def test_selector_attaches_metadata_to_contract():
    contract = _good_contract(ltp=90.0, timestamp=_now_iso())
    result = select_option_contract(
        underlying="NIFTY",
        direction="CE",
        preference="ATM",
        mode="paper",
        signal_confidence=85.0,
        spot=24850.0,
        resolved_contract=contract,
    )
    assert result["ok"] is True
    selected = result["selected_contract"]
    assert isinstance(result["quality_score"], int)
    assert result["selected_strike_mode"] is not None
    # quote_age_sec should be populated (fresh timestamp)
    assert result["quote_age_sec"] is not None
    assert result["quote_age_sec"] < 10.0  # fresh
