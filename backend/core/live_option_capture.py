"""IMD-04 (live wiring): capture OPTION 1-minute bars from the live feed.

Read-only tick listener that rolls option ticks into 1-minute OHLCV/OI bars for
the contracts the book ACTUALLY trades — refs are registered from open positions
/ spread legs, not the whole chain — and flushes them at EOD into the
OptionsMinuteStore, the SAME schema as the imported history (IMD-01/03/05). This
is the forward, legal, self-refreshing option-minute source the intraday
backtester (IMD-07) needs so its out-of-sample sample keeps growing from days the
book trades, without re-hitting the expired-candle API.

Guarantees (mirrors LiveIndexCapture):
  - fully guarded by the feed's listener wrapper — a raise here can never break
    the feed or touch a trading decision,
  - only REGISTERED (tradeable) contracts are captured; unknown option ticks are
    ignored, so the store is never polluted,
  - fail-closed: a missing minute is recorded as a data-quality gap by the
    underlying OptionsMinuteCapture, never fabricated.

The index counterpart (live_index_capture.py) intentionally left option-contract
capture as a follow-up because option tokens need strike/expiry/type resolution;
that identity is now taken from the position/leg docs the spread builder already
produced (they carry instrument_key + strike + option_type + expiry).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from core.options_minute_schema import OptionContractRef
from core.options_minute_store import OptionsMinuteStore
from core.options_minute_capture import OptionsMinuteCapture

logger = logging.getLogger("quantg.live_option_capture")

# Underlyings the intraday OOS judge (IMD-07/09) can actually grade. SENSEX/BANKEX
# have no IMD judge and no expired-resolver support, so capturing them would only
# pad the store with un-gradeable contract-days. NIFTY/BANKNIFTY only.
CAPTURE_UNDERLYINGS = {"NIFTY", "BANKNIFTY"}


class LiveOptionCapture:
    def __init__(self, store: Optional[OptionsMinuteStore] = None):
        self.capture = OptionsMinuteCapture(store or OptionsMinuteStore(), {}, source="upstox")

    # ---- ref registration -------------------------------------------------
    def register_ref(self, ref: OptionContractRef) -> bool:
        """Register one tradeable contract. Idempotent — returns True only when new."""
        if ref.instrument_key in self.capture.refs:
            return False
        self.capture.refs[ref.instrument_key] = ref
        return True

    def _build_ref(self, underlying: str, node: Dict[str, Any]) -> Optional[OptionContractRef]:
        ik = (node or {}).get("instrument_key")
        if not ik or "|" not in str(ik):
            return None
        if underlying not in CAPTURE_UNDERLYINGS:
            return None
        strike = node.get("strike")
        otype = node.get("option_type")
        expiry = node.get("expiry")
        if strike is None or not otype or not expiry:
            return None
        try:
            return OptionContractRef(
                underlying=underlying,
                expiry=str(expiry),
                strike=float(strike),
                option_type=str(otype).upper(),
                instrument_key=str(ik),
                # Forward capture: the live key is its own identity (there is no
                # separate expired-instrument key for a still-live contract).
                expired_instrument_key=str(ik),
            )
        except Exception:
            return None

    def register_from_positions(self, positions: Iterable[Dict[str, Any]]) -> List[str]:
        """Register refs for every tradeable option in these position docs (single-leg
        top-level + spread legs). Returns the instrument keys newly registered so the
        caller can ensure they are WS-subscribed (ticks only flow for subscribed keys)."""
        new_keys: List[str] = []
        for p in positions or []:
            underlying = str(p.get("underlying") or "").upper()
            # single-leg option (top-level identity)
            if p.get("instrument_key") and p.get("option_type") and p.get("strike") is not None:
                ref = self._build_ref(underlying, {
                    "instrument_key": p.get("instrument_key"),
                    "strike": p.get("strike"),
                    "option_type": p.get("option_type"),
                    "expiry": p.get("expiry"),
                })
                if ref and self.register_ref(ref):
                    new_keys.append(ref.instrument_key)
            # spread legs
            for leg in (p.get("legs") or []):
                ref = self._build_ref(underlying, leg or {})
                if ref and self.register_ref(ref):
                    new_keys.append(ref.instrument_key)
        if new_keys:
            logger.info("LiveOptionCapture registered %d new option refs (total %d)",
                        len(new_keys), len(self.capture.refs))
        return new_keys

    # ---- feed listener ----------------------------------------------------
    def on_tick(self, instrument_key: str, tick: Dict[str, Any]) -> None:
        """V3 feed tick listener. Signature matches add_tick_listener(fn(key, tick))."""
        if instrument_key not in self.capture.refs:
            return  # only capture registered, tradeable contracts
        ts = tick.get("received_at") or tick.get("timestamp")
        ltp = tick.get("ltp")
        if ts is None or ltp in (None, ""):
            return
        self.capture.on_tick(
            instrument_key, ts, float(ltp),
            cum_volume=tick.get("vtt"), oi=tick.get("oi"),
        )

    # ---- lifecycle --------------------------------------------------------
    def flush_day(self, date: str) -> Dict[str, Any]:
        res = self.capture.flush_day(date)
        logger.info("LiveOptionCapture flushed option minutes for %s: %s", date, res)
        return res

    def health(self) -> Dict[str, Any]:
        return self.capture.health()
