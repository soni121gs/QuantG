"""QuantG OptionSelector v2.

Selects the best Upstox option contract from ATM / ITM1 / OTM1 candidates
using a lightweight quality score.  Called by strategy_runner after the
existing _resolve_option_for_strategy produces a resolved contract.

Design contract:
- Inputs: underlying, direction (CE/PE), preference (ATM/ITM1/OTM1/AUTO),
          mode (paper/live), signal confidence, current spot price, plus the
          resolved_contract dict that came back from _resolve_option_for_strategy.
- Output: a flat dict with ok, selected_contract, quality_score, reason_code,
          selected_strike_mode, quote_age_sec, spread_pct, is_simulated.

Hard blocks (return ok=False immediately):
  - MCX / CRUDE / NATURALGAS underlying
  - missing instrument_key (no "|" separator)
  - missing / zero LTP
  - stale quote in live mode (age > LIVE_STALE_SEC)
  - simulated contract in live mode

Threshold:
  - live minimum score: 70
  - paper minimum score: 50

Strike math (all referenced to ATM):
  ATM  -> nearest strike to spot
  ITM1 for CE -> ATM - 1 interval (one strike below spot = in-the-money for CE)
  ITM1 for PE -> ATM + 1 interval (one strike above spot = in-the-money for PE)
  OTM1 for CE -> ATM + 1 interval (one strike above spot = out-of-the-money for CE)
  OTM1 for PE -> ATM - 1 interval (one strike below spot = out-of-the-money for PE)
"""
from __future__ import annotations

import logging
import sys
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Sibling import: options_helper lives one directory above core/
# ---------------------------------------------------------------------------
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import options_helper  # noqa: E402 – imported after path fix

logger = logging.getLogger("quantg.option_selector_v2")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLOCKED_UNDERLYINGS: frozenset[str] = frozenset({
    "CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
    "MCX", "CRUDE", "NATGAS",
})

LIVE_STALE_SEC: float = float(os.environ.get("QUANTG_OPTION_STALE_SEC", "30"))
LIVE_MIN_SCORE: int = int(os.environ.get("QUANTG_OPTION_MIN_SCORE_LIVE", "70"))
PAPER_MIN_SCORE: int = int(os.environ.get("QUANTG_OPTION_MIN_SCORE_PAPER", "50"))

VALID_PREFERENCES: frozenset[str] = frozenset({"ATM", "ITM1", "OTM1", "AUTO"})
VALID_DIRECTIONS: frozenset[str] = frozenset({"CE", "PE"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _float(v: Any, default: float = 0.0) -> float:
    try:
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default


def _parse_dt(v: Any) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        try:
            ts = float(v)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc
        ) if "+" not in str(v) and "Z" not in str(v) else datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        )
    except Exception:
        return None


def _quote_age_sec(contract: Dict[str, Any]) -> Optional[float]:
    ts = (
        contract.get("quote_timestamp")
        or contract.get("received_at")
        or contract.get("timestamp")
    )
    parsed = _parse_dt(ts)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _spread_pct(contract: Dict[str, Any]) -> float:
    bid = _float(
        contract.get("bid") or contract.get("bid_price") or contract.get("best_bid_price")
    )
    ask = _float(
        contract.get("ask") or contract.get("ask_price") or contract.get("best_ask_price")
    )
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        return ((ask - bid) / max(mid, 0.01)) * 100.0
    return 100.0  # unknown spread → worst case


def _target_strike(
    atm: int,
    interval: int,
    direction: str,
    strike_mode: str,
) -> int:
    """Return the target strike for the given strike_mode and direction."""
    if strike_mode == "ATM":
        return atm
    if strike_mode == "ITM1":
        # ITM for CE = lower strike; ITM for PE = higher strike
        return atm - interval if direction == "CE" else atm + interval
    if strike_mode == "OTM1":
        # OTM for CE = higher strike; OTM for PE = lower strike
        return atm + interval if direction == "CE" else atm - interval
    return atm  # fallback


# ---------------------------------------------------------------------------
# Quality scoring (simple 0-100)
# ---------------------------------------------------------------------------

def _quality_score(
    contract: Dict[str, Any],
    *,
    mode: str,
    preference: str,
    strike_mode: str,
    spot: float,
) -> Dict[str, Any]:
    """
    Score a single candidate contract 0-100.
    Returns a dict: score, hard_block, block_reason, warnings.
    """
    underlying = str(contract.get("underlying") or "").upper()
    instrument_key = str(contract.get("instrument_key") or contract.get("upstox_instrument_token") or "")
    is_simulated = bool(contract.get("simulated") or str(instrument_key).startswith("PAPER_"))
    is_live = mode == "live"
    ltp = _float(contract.get("ltp"))
    age = _quote_age_sec(contract)
    spread = _spread_pct(contract)

    # ---------- Hard blocks ----------
    if underlying in BLOCKED_UNDERLYINGS or "MCX" in underlying:
        return {"score": 0, "hard_block": True, "block_reason": "MCX_BLOCKED", "warnings": ["MCX_BLOCKED"]}

    if not instrument_key or "|" not in instrument_key:
        return {"score": 0, "hard_block": True, "block_reason": "MISSING_INSTRUMENT_KEY", "warnings": ["MISSING_INSTRUMENT_KEY"]}

    if ltp <= 0:
        return {"score": 0, "hard_block": True, "block_reason": "MISSING_LTP", "warnings": ["MISSING_LTP"]}

    if is_live and is_simulated:
        return {"score": 0, "hard_block": True, "block_reason": "LIVE_SIMULATED_BLOCKED", "warnings": ["LIVE_SIMULATED_BLOCKED"]}

    if is_live and (age is None or age > LIVE_STALE_SEC):
        age_str = f"{age:.1f}s" if age is not None else "unknown"
        return {"score": 0, "hard_block": True, "block_reason": "STALE_QUOTE_LIVE", "warnings": [f"STALE_QUOTE_LIVE age={age_str}"]}

    # ---------- Scoring components ----------
    warnings: List[str] = []

    # 1. Instrument key validity (0 or 25)
    key_score = 25 if "|" in instrument_key and not is_simulated else 10

    # 2. LTP quality (0-20)
    if 1.0 <= ltp <= 1000.0:
        ltp_score = 20
    elif ltp > 0:
        ltp_score = 10
        warnings.append("LTP_EDGE")
    else:
        ltp_score = 0

    # 3. Quote freshness (0-20)
    if age is None:
        freshness_score = 0 if is_live else 10
        if is_live:
            warnings.append("QUOTE_AGE_UNKNOWN")
    elif age <= 5:
        freshness_score = 20
    elif age <= 15:
        freshness_score = 15
    elif age <= LIVE_STALE_SEC:
        freshness_score = 8
    else:
        freshness_score = 0  # stale in paper is allowed but scored low
        warnings.append("QUOTE_STALE")

    # 4. Spread quality (0-20)
    if spread <= 1.0:
        spread_score = 20
    elif spread <= 3.0:
        spread_score = 15
    elif spread <= 6.0:
        spread_score = 8
    elif spread < 100.0:
        spread_score = 2
        warnings.append("WIDE_SPREAD")
    else:
        spread_score = 0  # unknown spread

    # 5. Preference match (0-10)
    pref_score = 10 if preference == "AUTO" or strike_mode == preference else 5

    # 6. Simulated penalty (paper only — not hard blocked)
    sim_penalty = -5 if is_simulated else 0

    score = max(0, min(100, key_score + ltp_score + freshness_score + spread_score + pref_score + sim_penalty))

    return {
        "score": score,
        "hard_block": False,
        "block_reason": None,
        "warnings": warnings,
        "components": {
            "key_score": key_score,
            "ltp_score": ltp_score,
            "freshness_score": freshness_score,
            "spread_score": spread_score,
            "pref_score": pref_score,
            "sim_penalty": sim_penalty,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_option_contract(
    *,
    underlying: str,
    direction: str,
    preference: str,
    mode: str,
    signal_confidence: float,
    spot: float,
    resolved_contract: Dict[str, Any],
    candidate_contracts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Select the best option contract for a given signal.

    Parameters
    ----------
    underlying : str
        Index name, e.g. "NIFTY".
    direction : str
        "CE" or "PE".
    preference : str
        "ATM", "ITM1", "OTM1", or "AUTO".
    mode : str
        "paper" or "live".
    signal_confidence : float
        0-100 from the strategy signal.
    spot : float
        Current spot price of the underlying index.
    resolved_contract : dict
        The contract dict returned by _resolve_option_for_strategy.
        Must contain instrument_key, ltp, simulated, etc.
    candidate_contracts : dict, optional
        Keyed by strike_mode ("ATM", "ITM1", "OTM1") → resolved contract dict.
        When not provided, only the single resolved_contract is scored under the
        preference / strike_mode derived from spot.

    Returns
    -------
    dict with keys:
        ok, selected_contract, quality_score, reason_code,
        selected_strike_mode, quote_age_sec, spread_pct, is_simulated
    """
    underlying = str(underlying or "").upper()
    direction = str(direction or "CE").upper()
    preference = str(preference or "ATM").upper()
    mode = str(mode or "paper").lower()

    if direction not in VALID_DIRECTIONS:
        direction = "CE"
    if preference not in VALID_PREFERENCES:
        preference = "ATM"

    # --- MCX hard block at selector level ---
    if underlying in BLOCKED_UNDERLYINGS or "MCX" in underlying:
        return _blocked("MCX_BLOCKED", "MCX/commodity underlying blocked")

    interval = options_helper.STRIKE_INTERVALS.get(underlying, 50)
    atm = options_helper.round_to_strike(spot, interval) if spot > 0 else 0

    # Build candidate map.
    # When candidate_contracts is provided (ATM/ITM1/OTM1 keyed dict), use it directly.
    # When only a single resolved_contract is available (the common path from strategy_runner),
    # make it available under ALL strike modes so that the preference loop always finds a
    # contract to score — we just record the preference as the strike_mode label.
    if candidate_contracts:
        candidates = {k.upper(): v for k, v in candidate_contracts.items()}
        single_contract_mode = False
    else:
        # Guess the true strike_mode label for informational purposes only.
        if spot > 0 and atm > 0:
            contract_strike = int(_float(resolved_contract.get("strike"), default=float(atm)))
            if contract_strike == atm:
                strike_mode_guess = "ATM"
            elif (direction == "CE" and contract_strike < atm) or (direction == "PE" and contract_strike > atm):
                strike_mode_guess = "ITM1"
            else:
                strike_mode_guess = "OTM1"
        else:
            strike_mode_guess = "ATM"
        # Register under all possible mode keys so preference lookup never misses.
        candidates = {
            "ATM": resolved_contract,
            "ITM1": resolved_contract,
            "OTM1": resolved_contract,
            strike_mode_guess: resolved_contract,  # redundant but explicit
        }
        single_contract_mode = True

    if preference == "AUTO":
        modes_to_try = ["ATM", "ITM1", "OTM1"]
    else:
        modes_to_try = [preference]

    best_result: Optional[Dict[str, Any]] = None
    best_score = -1
    best_mode = None
    best_contract: Dict[str, Any] = {}
    last_block_reason: Optional[str] = None

    for strike_mode in modes_to_try:
        contract = candidates.get(strike_mode)
        if contract is None:
            continue

        result = _quality_score(
            contract,
            mode=mode,
            preference=preference,
            strike_mode=strike_mode,
            spot=spot,
        )

        if result["hard_block"]:
            last_block_reason = result["block_reason"]
            # For AUTO with multiple real candidates, continue to try the next candidate.
            # For a specific preference (or single contract), return the specific block reason.
            if preference != "AUTO" or single_contract_mode:
                return _blocked(result["block_reason"], f"Hard block on {strike_mode}: {result['block_reason']}")
            continue

        score = result["score"]
        if score > best_score:
            best_score = score
            best_result = result
            best_mode = strike_mode
            best_contract = contract

    if best_result is None or best_mode is None:
        # All candidates hard-blocked; return the last seen block reason if available.
        return _blocked(
            last_block_reason or "ALL_CANDIDATES_BLOCKED",
            "All candidate contracts failed quality checks",
        )


    min_score = LIVE_MIN_SCORE if mode == "live" else PAPER_MIN_SCORE
    if best_score < min_score:
        logger.warning(
            "option_selector_v2: score=%d < threshold=%d underlying=%s direction=%s mode=%s strike_mode=%s",
            best_score, min_score, underlying, direction, mode, best_mode,
        )
        return _blocked(
            "SKIPPED_OPTION_QUALITY_LOW",
            f"Quality score {best_score} below threshold {min_score} for {underlying} {direction} {best_mode}",
        )

    age = _quote_age_sec(best_contract)
    spread = _spread_pct(best_contract)
    is_sim = bool(best_contract.get("simulated") or str(best_contract.get("instrument_key", "")).startswith("PAPER_"))

    logger.info(
        "option_selector_v2: selected %s %s %s score=%d age=%s spread=%.1f%% sim=%s mode=%s",
        underlying, direction, best_mode, best_score,
        f"{age:.1f}s" if age is not None else "?",
        spread if spread < 100 else -1,
        is_sim, mode,
    )

    return {
        "ok": True,
        "selected_contract": best_contract,
        "quality_score": best_score,
        "reason_code": "SELECTED",
        "selected_strike_mode": best_mode,
        "quote_age_sec": round(age, 2) if age is not None else None,
        "spread_pct": round(spread, 2) if spread < 100.0 else None,
        "is_simulated": is_sim,
        "warnings": best_result.get("warnings", []),
        "components": best_result.get("components", {}),
    }


def _blocked(reason_code: str, reason: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "selected_contract": None,
        "quality_score": 0,
        "reason_code": reason_code,
        "selected_strike_mode": None,
        "quote_age_sec": None,
        "spread_pct": None,
        "is_simulated": False,
    }
