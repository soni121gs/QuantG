"""QuantG Market Clock.

Provides the single source of truth for market schedules, holidays, and operational statuses.
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from core.market_domains import DomainType

IST_OFFSET = timedelta(hours=5, minutes=30)
NSE_OPEN_MINUTE = 9 * 60 + 15   # 9:15 AM — actual NSE market open
NSE_CLOSE_MINUTE = 15 * 60 + 30  # 3:30 PM — actual NSE market close

# Strategy runner window — broader than market hours to cover pre-market setup
RUNNER_OPEN_MINUTE = 9 * 60        # 9:00 AM IST — runner activates strategies
RUNNER_CLOSE_MINUTE = 15 * 60 + 35 # 3:35 PM IST — runner pauses strategies

# Keep strategy evaluation aligned with real exchange order hours. Pre-market
# readiness can run separately, but scans should not advance outside 09:15-15:30.
RUNNER_OPEN_MINUTE = NSE_OPEN_MINUTE
RUNNER_CLOSE_MINUTE = NSE_CLOSE_MINUTE

MARKET_HOLIDAYS_IST = {
    item.strip()
    for item in (os.environ.get("MARKET_HOLIDAYS_IST") or os.environ.get("MARKET_HOLIDAYS") or "").split(",")
    if item.strip()
}

SEGMENT_WINDOWS = {
    DomainType.NSE_FO: (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "NSE F&O"),
    DomainType.BSE_FO: (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "BSE F&O"),
    # Cash equity trades the same 9:15–15:30 IST session as F&O. Without these
    # entries every NSE_EQ/BSE_EQ strategy (the UPSTOX <STOCK> cash strategies)
    # resolves to "Unsupported segment domain" and the runner skips it every tick.
    DomainType.NSE_EQ: (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "NSE Equity"),
    DomainType.BSE_EQ: (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "BSE Equity"),
}

def get_ist_now(now_utc: Optional[datetime] = None) -> datetime:
    utc = now_utc or datetime.now(timezone.utc)
    return utc + IST_OFFSET

def is_holiday(day_str: str) -> bool:
    return day_str in MARKET_HOLIDAYS_IST

def _ist_iso_for_minute(day: Any, minute: int) -> str:
    return f"{day.isoformat()}T{minute // 60:02d}:{minute % 60:02d}:00+05:30"

def get_next_open(domain: DomainType, now_utc: Optional[datetime] = None) -> Optional[str]:
    window = SEGMENT_WINDOWS.get(domain)
    if not window:
        return None
    open_minute, close_minute, _ = window
    ist_now = get_ist_now(now_utc)
    today_minutes = ist_now.hour * 60 + ist_now.minute
    
    for offset in range(0, 14):
        day = ist_now.date() + timedelta(days=offset)
        if day.weekday() >= 5 or is_holiday(day.isoformat()):
            continue
        if offset == 0:
            if today_minutes < open_minute:
                return _ist_iso_for_minute(day, open_minute)
            if today_minutes <= close_minute:
                # currently open or exactly at close, return it or next day's open
                return _ist_iso_for_minute(day, open_minute)
            continue
        return _ist_iso_for_minute(day, open_minute)
    return None

def get_segment_status(domain: DomainType, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    utc = now_utc or datetime.now(timezone.utc)
    ist_now = get_ist_now(utc)
    day = ist_now.date()
    minutes = ist_now.hour * 60 + ist_now.minute
    
    window = SEGMENT_WINDOWS.get(domain)
    if not window:
        return {
            "segment": domain.value,
            "status": "CLOSED",
            "open": False,
            "reason": "Unsupported segment domain",
            "next_open": None,
            "next_close": None,
        }
        
    open_minute, close_minute, label = window
    holiday = is_holiday(day.isoformat())
    is_weekday = day.weekday() < 5
    open_now = is_weekday and not holiday and (open_minute <= minutes <= close_minute)
    
    if open_now:
        reason = f"{label} market is open"
    elif not is_weekday:
        reason = f"{label} market is closed for weekend"
    elif holiday:
        reason = f"{label} market is closed for holiday ({day.isoformat()})"
    elif minutes < open_minute:
        reason = f"{label} market opens at {open_minute // 60:02d}:{open_minute % 60:02d} IST"
    else:
        reason = f"{label} market closed after {close_minute // 60:02d}:{close_minute % 60:02d} IST"
        
    return {
        "segment": domain.value,
        "status": "OPEN" if open_now else "CLOSED",
        "open": open_now,
        "reason": reason,
        "open_time": f"{open_minute // 60:02d}:{open_minute % 60:02d}",
        "close_time": f"{close_minute // 60:02d}:{close_minute % 60:02d}",
        "next_open": get_next_open(domain, utc) if not open_now else None,
        "next_close": _ist_iso_for_minute(day, close_minute) if open_now else None,
    }

def is_trading_session_active(now_utc: Optional[datetime] = None) -> bool:
    """Return True if within the strategy runner window (9:00 AM – 3:35 PM IST, Mon–Fri, non-holiday)."""
    ist_now = get_ist_now(now_utc)
    day = ist_now.date()
    if day.weekday() >= 5 or is_holiday(day.isoformat()):
        return False
    minutes = ist_now.hour * 60 + ist_now.minute
    return RUNNER_OPEN_MINUTE <= minutes <= RUNNER_CLOSE_MINUTE


def get_market_clock_snapshot(now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    utc = now_utc or datetime.now(timezone.utc)
    ist_now = get_ist_now(utc)
    
    nse = get_segment_status(DomainType.NSE_FO, utc)
    bse = get_segment_status(DomainType.BSE_FO, utc)
    
    open_segments = []
    if nse["open"]:
        open_segments.append("NSE")
    if bse["open"]:
        open_segments.append("BSE")
    if not open_segments:
        global_status = "CLOSED"
    elif len(open_segments) == 2:
        global_status = "ALL OPEN"
    else:
        global_status = " & ".join(open_segments) + " OPEN"
        
    any_open = len(open_segments) > 0
    
    return {
        "global_status": global_status,
        "current_ist_time": ist_now.isoformat(),
        "timezone": "Asia/Kolkata",
        "reason": "At least one segment is open" if any_open else "All segments are closed",
        "segments": {
            "NSE_FO": nse,
            "BSE_FO": bse,
        },
        "NSE_FO": nse,
        "BSE_FO": bse,
    }
