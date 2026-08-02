"""Single source of truth for exchange session times.

**SEBI/NSE change effective 2026-08-03** (verified against NSE/broker circular
coverage on 2026-08-02, not from model memory — CLAUDE.md §20 law 5):

  * NSE **equity derivatives** normal close moves **15:30 -> 15:40 IST**. Open is
    unchanged at 09:15. Applies to index futures, index options, stock futures and
    stock options. Trade-modification end stays 16:15. The close-price VWAP window
    becomes 15:10-15:40 (was 15:00-15:30).
  * The **cash** segment gains a **Closing Auction Session (CAS) 15:15-15:35** for
    F&O-eligible stocks. CONTINUOUS cash trading therefore ends at **15:15**, and
    the official close is struck by the auction. Non-F&O stocks are unchanged.
  * Phase 2 (a revised morning pre-open framework) is slated for 2026-09-07 and is
    NOT modelled here.

Two consequences drive every default below:

1. **F&O gets 10 minutes LONGER.** Anything that assumed a 15:30 close now stops
   10 minutes early — capture loops, market-hours guards, monitors. Left alone
   this silently truncates the last 10 minutes of every session, which is exactly
   the failure class of §21.7 (a store that is quietly short, with nothing in the
   logs).
2. **Cash gets 15 minutes SHORTER for continuous orders.** QuantG places
   continuous market orders and cannot participate in a call auction, so equity
   must be flat BEFORE 15:15. This direction is the dangerous one: it is a
   tightening, and treating it as a widening would push equity square-off into an
   auction window where continuous orders do not behave normally.

BSE: the exchange-level circulars covered by the sources are NSE's. Whether BSE
mirrors the derivatives extension for SENSEX/BANKEX was NOT verified, so
`BSE_FO` deliberately stays at 15:30 (the conservative, pre-change value) and is
env-overridable via `BSE_FO_CLOSE_IST` once confirmed. Being 10 minutes early on
BSE costs opportunity; being 10 minutes late would place orders into a closed
session.

Everything is env-overridable as `HHMM` (e.g. `NSE_FO_CLOSE_IST=1540`) so the
whole change can be reverted without a deploy.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------- helpers ----

def _hhmm(env_key: str, default_minute: int) -> int:
    """Read an `HHMM` env override into minutes-since-midnight IST."""
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return default_minute
    try:
        digits = raw.replace(":", "")
        hh, mm = int(digits[:-2]), int(digits[-2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh * 60 + mm
    except Exception:
        pass
    return default_minute


def hhmm_str(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


# ------------------------------------------------------------- the times ----

OPEN_MINUTE = _hhmm("MARKET_OPEN_IST", 9 * 60 + 15)          # 09:15 — unchanged

# Derivatives close. NSE extended to 15:40 on 2026-08-03; BSE unverified.
NSE_FO_CLOSE_MINUTE = _hhmm("NSE_FO_CLOSE_IST", 15 * 60 + 40)
BSE_FO_CLOSE_MINUTE = _hhmm("BSE_FO_CLOSE_IST", 15 * 60 + 30)

# Cash: last minute at which a CONTINUOUS order is meaningful. The CAS auction
# runs 15:15-15:35 and settles the official close; we do not participate in it.
EQ_CONTINUOUS_CLOSE_MINUTE = _hhmm("EQ_CONTINUOUS_CLOSE_IST", 15 * 60 + 15)
CAS_START_MINUTE = _hhmm("CAS_START_IST", 15 * 60 + 15)
CAS_END_MINUTE = _hhmm("CAS_END_IST", 15 * 60 + 35)

# The latest close across all segments — use for "is anything still trading?"
# and for scheduling anything that must run strictly AFTER the market.
LAST_CLOSE_MINUTE = max(NSE_FO_CLOSE_MINUTE, BSE_FO_CLOSE_MINUTE, CAS_END_MINUTE)

# Square-off deadlines, expressed as an offset from each segment's own close so
# they track the close instead of drifting into it when the close moves again.
_SPREAD_SQUAREOFF_LEAD = int(os.environ.get("SPREAD_SQUAREOFF_LEAD_MIN", "5") or 5)
_EQ_SQUAREOFF_LEAD = int(os.environ.get("EQ_SQUAREOFF_LEAD_MIN", "5") or 5)

# Spreads: close - 5 min (NSE 15:35). Single-leg/equity must be flat before the
# cash auction opens, so it leads EQ_CONTINUOUS_CLOSE, not the derivatives close.
SPREAD_SQUAREOFF_MINUTE = _hhmm(
    "SPREAD_SQUAREOFF_IST", NSE_FO_CLOSE_MINUTE - _SPREAD_SQUAREOFF_LEAD)
EQUITY_SQUAREOFF_MINUTE = _hhmm(
    "EQUITY_SQUAREOFF_IST", EQ_CONTINUOUS_CLOSE_MINUTE - _EQ_SQUAREOFF_LEAD)

# Anything post-market (capture flush, EOD reports, auto-pause) must start after
# the LAST close, never at the old 15:35 which is now mid-session for NSE F&O.
_POST_CLOSE_LAG = int(os.environ.get("POST_CLOSE_LAG_MIN", "5") or 5)
POST_CLOSE_MINUTE = _hhmm("POST_CLOSE_IST", LAST_CLOSE_MINUTE + _POST_CLOSE_LAG)


# ------------------------------------------------------- segment lookups ----

_FO_SEGMENTS = {"NSE_FO", "BSE_FO"}


def close_minute_for(segment: str) -> int:
    """Close (last tradeable minute) for a segment name like 'NSE_FO'."""
    s = (segment or "").upper()
    if s == "NSE_FO":
        return NSE_FO_CLOSE_MINUTE
    if s == "BSE_FO":
        return BSE_FO_CLOSE_MINUTE
    if s in ("NSE_EQ", "BSE_EQ"):
        return EQ_CONTINUOUS_CLOSE_MINUTE
    return NSE_FO_CLOSE_MINUTE


def segment_window(segment: str) -> Tuple[int, int]:
    return OPEN_MINUTE, close_minute_for(segment)


def session_minutes(segment: str = "NSE_FO") -> int:
    """Tradeable minutes in one session — the divisor behind theta-per-minute and
    the §21.2 reachability law. Was a hardcoded 375 (09:15-15:30); NSE F&O is now
    385. Getting this wrong silently mis-states how much decay a hold can reach."""
    open_m, close_m = segment_window(segment)
    return max(1, close_m - open_m)


def expected_bar_count(segment: str = "NSE_FO") -> int:
    """Number of 1-minute bars stamped 09:15..close-1 for one date."""
    return session_minutes(segment)


def in_session(hour: int, minute: int, segment: str = "NSE_FO") -> bool:
    """Is this IST wall-clock time inside the tradeable session?

    Replaces the hand-rolled `(hour == 9 and minute >= 15) or 10 <= hour < 15 or
    (hour == 15 and minute <= 30)` guards that were copy-pasted across the
    capture/VIX/chain loops. Each copy silently stopped at 15:30 and would have
    dropped the last 10 minutes of every session from 2026-08-03.
    """
    m = int(hour) * 60 + int(minute)
    open_m, close_m = segment_window(segment)
    return open_m <= m <= close_m


def is_cash_auction_window(minute_of_day: int) -> bool:
    """True inside the CAS. Continuous cash orders must not be placed here."""
    return CAS_START_MINUTE <= minute_of_day < CAS_END_MINUTE


def describe() -> Dict[str, str]:
    """Human-readable snapshot — surfaced in readiness/ops so the running config
    is visible rather than inferred."""
    return {
        "open": hhmm_str(OPEN_MINUTE),
        "nse_fo_close": hhmm_str(NSE_FO_CLOSE_MINUTE),
        "bse_fo_close": hhmm_str(BSE_FO_CLOSE_MINUTE),
        "eq_continuous_close": hhmm_str(EQ_CONTINUOUS_CLOSE_MINUTE),
        "cas_window": f"{hhmm_str(CAS_START_MINUTE)}-{hhmm_str(CAS_END_MINUTE)}",
        "spread_squareoff": hhmm_str(SPREAD_SQUAREOFF_MINUTE),
        "equity_squareoff": hhmm_str(EQUITY_SQUAREOFF_MINUTE),
        "post_close_tasks": hhmm_str(POST_CLOSE_MINUTE),
        "nse_fo_session_minutes": str(session_minutes("NSE_FO")),
        "effective": "SEBI/NSE 2026-08-03 derivatives close 15:40 + cash CAS 15:15-15:35",
    }
