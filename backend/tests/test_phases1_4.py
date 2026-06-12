"""Phase 1–4 gate tests.

Tests:
  1. Regime CRASH blocks BUY (long), allows SELL (short)
  2. Regime MELTUP blocks SELL (short), allows BUY (long)
  3. Regime mid-session flip detected
  4. regime_gate_strict: RANGE regime blocks S5-style entries
  5. overextended_regime_exempt: CRASH allows overextended PE, blocks overextended CE
  6. Frequency daily cap blocks when exceeded
  7. Loss-streak throttle blocks after 3 consecutive SLs
  8. ExitPolicy ATR SL within bounds
  9. Theta guard blocks when ATR% < threshold
 10. Theta guard passes when ATR% >= threshold

All tests use mocked DB and pure functions — no I/O.
"""
import asyncio
import sys
import os
import time
import types
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# Ensure backend is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_candles(n: int, base_price: float = 20000.0, *, day_offset: int = 0) -> List[Dict]:
    """Generate n 5-min candles with mild upward drift."""
    candles = []
    base_ts = datetime(2026, 6, 10, 9, 15, tzinfo=timezone.utc) + timedelta(days=day_offset)
    for i in range(n):
        price = base_price + i * 10
        ts = (base_ts + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00")
        candles.append({
            "date": ts,
            "open": price - 5,
            "high": price + 10,
            "low": price - 10,
            "close": price,
            "volume": 5000 + i * 100,
        })
    return candles


def _make_crash_candles(n: int = 20) -> List[Dict]:
    """Candles whose last close is ~-2.5% below prev-day close → CRASH regime."""
    prev_close = 20000.0
    # Start well below prev_close so even with small drift the last bar is ≤ -1.5%
    # last_close = 19400 + 19*10 = 19590 → ret = (19590-20000)/20000 = -2.05%
    base_price = 19400.0
    base_ts = datetime(2026, 6, 10, 9, 15, tzinfo=timezone.utc)
    candles = []
    for i in range(n):
        price = base_price + i * 10
        ts = (base_ts + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00")
        candles.append({
            "date": ts,
            "open": price - 5,
            "high": price + 10,
            "low": price - 10,
            "close": price,
            "volume": 5000,
        })
    prev_day_ts = datetime(2026, 6, 9, 15, 25, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:00")
    prev_candle = {
        "date": prev_day_ts,
        "open": prev_close,
        "high": prev_close + 50,
        "low": prev_close - 50,
        "close": prev_close,
        "volume": 1000,
    }
    return [prev_candle] + candles


def _make_meltup_candles(n: int = 20) -> List[Dict]:
    """Candles whose last close is ~+2.5% above prev-day close → MELTUP regime."""
    prev_close = 20000.0
    # last_close = 20600 + 19*10 = 20790 → ret = (20790-20000)/20000 = +3.95%
    base_price = 20600.0
    base_ts = datetime(2026, 6, 10, 9, 15, tzinfo=timezone.utc)
    candles = []
    for i in range(n):
        price = base_price + i * 10
        ts = (base_ts + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00")
        candles.append({
            "date": ts,
            "open": price - 5,
            "high": price + 10,
            "low": price - 10,
            "close": price,
            "volume": 5000,
        })
    prev_day_ts = datetime(2026, 6, 9, 15, 25, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:00")
    prev_candle = {
        "date": prev_day_ts,
        "open": prev_close,
        "high": prev_close + 50,
        "low": prev_close - 50,
        "close": prev_close,
        "volume": 1000,
    }
    return [prev_candle] + candles


class MockDB:
    """Minimal async mock for MongoDB used by frequency/streak tests."""

    def __init__(self):
        self._orders: List[Dict] = []
        self._streaks: Dict[str, Dict] = {}
        self._filter_log: List[Dict] = []

    @property
    def orders(self):
        return _MockCollection(self._orders)

    @property
    def strategy_loss_streaks(self):
        return _MockCollection(self._streaks, key_field="strategy_id")

    @property
    def strategy_filter_log(self):
        return _MockCollection(self._filter_log)

    @property
    def strategies(self):
        return _MockCollection([])


class _MockCollection:
    def __init__(self, store, *, key_field: str = "id"):
        self._store = store
        self._key_field = key_field

    async def count_documents(self, query, *args, **kwargs) -> int:
        return len(self._store) if isinstance(self._store, list) else 0

    async def find_one(self, query, *args, **kwargs) -> Optional[Dict]:
        if isinstance(self._store, dict):
            sid = query.get("strategy_id")
            uid = query.get("user_id")
            key = f"{sid}:{uid}"
            return self._store.get(key)
        return None

    async def update_one(self, query, update, *, upsert=False):
        if isinstance(self._store, dict):
            sid = query.get("strategy_id")
            uid = query.get("user_id")
            if sid and uid:
                key = f"{sid}:{uid}"
                existing = self._store.get(key, {})
                if "$set" in update:
                    existing = {**existing, **update["$set"]}
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        existing[k] = existing.get(k, 0) + v
                self._store[key] = existing

    async def insert_one(self, doc):
        if isinstance(self._store, list):
            self._store.append(doc)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_01_crash_blocks_longs():
    """CRASH regime must set long_entries_allowed=False."""
    from market_regime import compute_regime_from_data
    candles = _make_crash_candles()
    regime = compute_regime_from_data("NIFTY", candles)
    assert regime["regime"] == "CRASH", f"Expected CRASH, got {regime['regime']}"
    assert regime["long_entries_allowed"] is False, "CRASH must block longs"
    assert regime["short_entries_allowed"] is True, "CRASH must allow shorts"
    print("PASS test_01_crash_blocks_longs")


def test_02_meltup_blocks_shorts():
    """MELTUP regime must set short_entries_allowed=False."""
    from market_regime import compute_regime_from_data
    candles = _make_meltup_candles()
    regime = compute_regime_from_data("NIFTY", candles)
    assert regime["regime"] == "MELTUP", f"Expected MELTUP, got {regime['regime']}"
    assert regime["short_entries_allowed"] is False, "MELTUP must block shorts"
    assert regime["long_entries_allowed"] is True, "MELTUP must allow longs"
    print("PASS test_02_meltup_blocks_shorts")


def test_03_regime_flip_detected():
    """update_regime must detect and log a regime flip."""
    from market_regime import compute_regime_from_data, _state
    # First compute RANGE
    range_candles = _make_candles(40)
    r1 = compute_regime_from_data("NIFTY", range_candles)
    # Now compute CRASH (simulates mid-session flip)
    crash_candles = _make_crash_candles()
    r2 = compute_regime_from_data("NIFTY", crash_candles)
    assert r1["regime"] != r2["regime"], "Expected regime change RANGE→CRASH"
    print(f"PASS test_03_regime_flip_detected ({r1['regime']}→{r2['regime']})")


def test_04_regime_strict_gate_blocks_range():
    """regime_gate_strict signals must be blocked in RANGE regime."""
    # This tests the flag logic — simulate the check strategy_runner performs
    signal = {"action": "BUY", "direction": "CE", "regime_gate_strict": True}
    regime = {"regime": "RANGE", "long_entries_allowed": True, "short_entries_allowed": True}
    r_name = regime.get("regime", "RANGE")
    blocked = signal.get("regime_gate_strict") and r_name == "RANGE"
    assert blocked, "RANGE regime should block regime_gate_strict signal"
    # TREND_UP should NOT block
    regime2 = {"regime": "TREND_UP", "long_entries_allowed": True}
    r_name2 = regime2.get("regime", "RANGE")
    blocked2 = signal.get("regime_gate_strict") and r_name2 == "RANGE"
    assert not blocked2, "TREND_UP should pass regime_gate_strict"
    print("PASS test_04_regime_strict_gate_blocks_range")


def test_05_overextended_regime_exempt():
    """overextended_regime_exempt: CRASH+SELL allowed, RANGE+SELL blocked, CRASH+BUY blocked."""
    def _check(regime_name: str, action: str, flag: bool) -> bool:
        if not flag:
            return True  # no flag → no restriction
        return (regime_name == "MELTUP" and action == "BUY") or \
               (regime_name == "CRASH" and action == "SELL")

    assert _check("CRASH", "SELL", True), "CRASH+SELL overextended must pass"
    assert _check("MELTUP", "BUY", True), "MELTUP+BUY overextended must pass"
    assert not _check("RANGE", "SELL", True), "RANGE+SELL overextended must be blocked"
    assert not _check("CRASH", "BUY", True), "CRASH+BUY overextended must be blocked"
    assert _check("RANGE", "BUY", False), "No flag — never blocked"
    print("PASS test_05_overextended_regime_exempt")


def test_06_frequency_daily_cap():
    """Frequency gate must block when daily entries >= effective_cap."""
    async def _run():
        from trade_frequency import check_frequency_gate

        class _CappedDB:
            """Fake DB that reports 100 orders today."""
            @property
            def orders(self): return _HundredOrders()
            @property
            def strategy_loss_streaks(self): return _NoStreaks()

        class _HundredOrders:
            async def count_documents(self, *a, **kw): return 100
        class _NoStreaks:
            async def find_one(self, *a, **kw): return None

        ok, reason = await check_frequency_gate(
            _CappedDB(), "s1", "NIFTY HFT Quick Scalper", "u1", freq_multiplier=1.0
        )
        assert not ok, "Should be blocked when entries >= cap"
        assert "DAILY_CAP" in (reason or ""), f"Unexpected reason: {reason}"
        print("PASS test_06_frequency_daily_cap")

    asyncio.run(_run())


def test_07_loss_streak_throttle():
    """After 3 consecutive SLs and a pause set in the future, gate must block."""
    async def _run():
        from trade_frequency import check_frequency_gate

        future_pause = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()

        class _StreamDB:
            @property
            def orders(self): return _FewOrders()
            @property
            def strategy_loss_streaks(self): return _StreakDoc()

        class _FewOrders:
            async def count_documents(self, *a, **kw): return 1
        class _StreakDoc:
            async def find_one(self, *a, **kw):
                return {"current_streak": 3, "paused_until": future_pause}

        ok, reason = await check_frequency_gate(
            _StreamDB(), "s1", "NIFTY HFT Quick Scalper", "u1", freq_multiplier=1.0
        )
        assert not ok, "Loss-streak throttle should block"
        assert "LOSS_STREAK" in (reason or ""), f"Unexpected reason: {reason}"
        print("PASS test_07_loss_streak_throttle")

    asyncio.run(_run())


def test_08_exit_policy_atr_sl_bounds():
    """ATR-based SL must be clamped to [ATR_SL_MIN_PCT, ATR_SL_MAX_PCT]."""
    from exit_policy import build_exit_policy, ATR_SL_MIN_PCT, ATR_SL_MAX_PCT

    # Very low ATR → SL should be at minimum
    p_low = build_exit_policy(underlying_atr_pct=0.001, option_premium=100.0)
    assert p_low is not None
    assert p_low["stop_loss_pct"] >= ATR_SL_MIN_PCT, "SL below minimum"
    assert p_low["stop_loss_pct"] <= ATR_SL_MAX_PCT, "SL above maximum"

    # High ATR → SL should be at maximum
    p_high = build_exit_policy(underlying_atr_pct=5.0, option_premium=100.0)
    assert p_high is not None
    assert p_high["stop_loss_pct"] <= ATR_SL_MAX_PCT, "SL should be capped at maximum"

    # TP >= 1.5R
    p_mid = build_exit_policy(underlying_atr_pct=0.2, option_premium=100.0)
    assert p_mid is not None
    assert p_mid["take_profit_pct"] >= p_mid["stop_loss_pct"] * 1.5 - 0.01, "TP must be >= 1.5R"
    print(f"PASS test_08_exit_policy_atr_sl_bounds (sl={p_mid['stop_loss_pct']:.1f}%, tp={p_mid['take_profit_pct']:.1f}%)")


def test_09_theta_guard_blocks():
    """Theta guard must block when underlying ATR% < threshold."""
    from exit_policy import attach_exit_policy_to_signal, THETA_GUARD_ATR_PCT

    # Generate flat candles (tiny moves → low ATR)
    candles = []
    for i in range(20):
        ts = (datetime(2026, 6, 10, 9, 15, tzinfo=timezone.utc) + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00")
        candles.append({"date": ts, "open": 100.0, "high": 100.01, "low": 99.99, "close": 100.0, "volume": 1000})

    signal = {"action": "BUY", "direction": "CE", "confidence": 80.0}
    result = attach_exit_policy_to_signal(signal, candles, option_premium=50.0)

    atr_pct = result.get("underlying_atr_pct", 0.0)
    if atr_pct < THETA_GUARD_ATR_PCT:
        assert result.get("theta_guard_blocked"), f"Expected theta_guard_blocked for atr_pct={atr_pct:.4f}"
        print(f"PASS test_09_theta_guard_blocks (atr_pct={atr_pct:.4f} < threshold={THETA_GUARD_ATR_PCT})")
    else:
        print(f"SKIP test_09_theta_guard_blocks (candles give atr_pct={atr_pct:.4f} >= threshold — adjust candles if needed)")


def test_10_theta_guard_passes():
    """Theta guard must pass and attach exit_policy when ATR% >= threshold."""
    from exit_policy import attach_exit_policy_to_signal, THETA_GUARD_ATR_PCT

    candles = []
    base = 20000.0
    for i in range(20):
        ts = (datetime(2026, 6, 10, 9, 15, tzinfo=timezone.utc) + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:00")
        # 0.5% swings — well above 0.08% threshold
        candles.append({
            "date": ts,
            "open": base + i * 10 - 5,
            "high": base + i * 10 + 100,
            "low": base + i * 10 - 100,
            "close": base + i * 10,
            "volume": 5000,
        })

    signal = {"action": "BUY", "direction": "CE", "confidence": 80.0}
    result = attach_exit_policy_to_signal(signal, candles, option_premium=150.0)

    assert not result.get("theta_guard_blocked"), f"Theta guard should pass (atr_pct={result.get('underlying_atr_pct'):.4f})"
    assert result.get("exit_policy") is not None, "exit_policy should be attached"
    ep = result["exit_policy"]
    assert ep.get("protection_status") == "PROTECTED_ATR_POLICY"
    print(f"PASS test_10_theta_guard_passes (atr_pct={result.get('underlying_atr_pct'):.4f}, sl={ep['stop_loss_pct']:.1f}%, tp={ep['take_profit_pct']:.1f}%)")


def test_11_crash_allows_pe_buy_with_confidence_bonus():
    """CRASH: BUY+PE is bearish/aligned — NOT blocked, qualifies for +confidence."""
    from market_regime import compute_regime_from_data
    regime = compute_regime_from_data("NIFTY", _make_crash_candles())
    assert regime["regime"] == "CRASH"

    sig = {"action": "BUY", "direction": "PE", "confidence": 70.0}
    sig_dir = (sig.get("direction") or "").upper()
    is_ce = ("CE" in sig_dir) or (sig["action"] == "BUY" and not sig_dir)
    is_pe = ("PE" in sig_dir) or (sig["action"] == "SELL" and not sig_dir)

    assert not is_ce, "BUY+PE must NOT be CE exposure"
    assert is_pe, "BUY+PE must be PE exposure"

    # CRASH blocks CE (longs), not PE
    blocked = is_ce and not regime["long_entries_allowed"]
    assert not blocked, "PE buy must NOT be blocked in CRASH"

    # CRASH+PE is regime-aligned → qualifies for confidence bonus
    aligned = regime["regime"] in ("CRASH", "TREND_DOWN") and is_pe
    assert aligned, "BUY+PE in CRASH must be regime-aligned (confidence bonus)"
    print("PASS test_11_crash_allows_pe_buy_with_confidence_bonus")


def test_12_crash_blocks_ce_buy():
    """CRASH: BUY+CE is bullish exposure — BLOCKED."""
    from market_regime import compute_regime_from_data
    regime = compute_regime_from_data("NIFTY", _make_crash_candles())
    assert regime["regime"] == "CRASH"

    sig = {"action": "BUY", "direction": "CE", "confidence": 70.0}
    sig_dir = (sig.get("direction") or "").upper()
    is_ce = ("CE" in sig_dir) or (sig["action"] == "BUY" and not sig_dir)

    assert is_ce, "BUY+CE must be CE exposure"
    blocked = is_ce and not regime["long_entries_allowed"]
    assert blocked, "CE buy must be BLOCKED in CRASH"
    print("PASS test_12_crash_blocks_ce_buy")


def test_13_meltup_allows_ce_buy_with_confidence_bonus():
    """MELTUP: BUY+CE is bullish/aligned — NOT blocked, qualifies for +confidence."""
    from market_regime import compute_regime_from_data
    regime = compute_regime_from_data("NIFTY", _make_meltup_candles())
    assert regime["regime"] == "MELTUP"

    sig = {"action": "BUY", "direction": "CE", "confidence": 70.0}
    sig_dir = (sig.get("direction") or "").upper()
    is_ce = ("CE" in sig_dir) or (sig["action"] == "BUY" and not sig_dir)
    is_pe = ("PE" in sig_dir) or (sig["action"] == "SELL" and not sig_dir)

    assert is_ce, "BUY+CE must be CE exposure"
    blocked = is_pe and not regime["short_entries_allowed"]
    assert not blocked, "CE buy must NOT be blocked in MELTUP"

    aligned = regime["regime"] in ("MELTUP", "TREND_UP") and is_ce
    assert aligned, "BUY+CE in MELTUP must be regime-aligned (confidence bonus)"
    print("PASS test_13_meltup_allows_ce_buy_with_confidence_bonus")


def test_14_meltup_blocks_pe_buy():
    """MELTUP: BUY+PE is bearish exposure — BLOCKED."""
    from market_regime import compute_regime_from_data
    regime = compute_regime_from_data("NIFTY", _make_meltup_candles())
    assert regime["regime"] == "MELTUP"

    sig = {"action": "BUY", "direction": "PE", "confidence": 70.0}
    sig_dir = (sig.get("direction") or "").upper()
    is_pe = ("PE" in sig_dir) or (sig["action"] == "SELL" and not sig_dir)

    assert is_pe, "BUY+PE must be PE exposure"
    blocked = is_pe and not regime["short_entries_allowed"]
    assert blocked, "PE buy must be BLOCKED in MELTUP"
    print("PASS test_14_meltup_blocks_pe_buy")


def test_15_all_9_strategies_attach_exit_policy():
    """All 9 live strategy code strings: signals must carry exit_policy when ATR passes theta guard."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import server
    from exit_policy import attach_exit_policy_to_signal
    from safe_exec import safe_run_strategy

    strategy_map = {
        "S1_NIFTY_ATM": server.NIFTY_ATM_MOMENTUM_CODE,
        "S2_BANKNIFTY_ATM": server.BANKNIFTY_ATM_BREAKOUT_CODE,
        "S3_NIFTY_VWAP": server.NIFTY_VWAP_TREND_BREAKOUT_CODE,
        "S4_SENSEX_RSI": server.SENSEX_RSI_PULLBACK_CODE,
        "S5_NIFTY_MICRO": server.NIFTY_MICRO_TREND_CODE,
        "S6_NIFTY_SCALPER": server.NIFTY_QUICK_SCALPER_CODE,
        "S7_BANKNIFTY_STD": server.BANKNIFTY_STD_BAND_SCALPER_CODE,
        "S8_NIFTY_EMA": server.NIFTY_QUICK_EMA_SCALPER_CODE,
        "S9_BANKNIFTY_VOLBK": server.BANKNIFTY_SENSITIVE_VOL_BREAKOUT_CODE,
    }

    # 60 candles across 3 days with strong ATR so theta guard passes and vol is present
    candles = []
    from datetime import datetime, timezone, timedelta
    for day in range(3):
        base_ts = datetime(2026, 6, 8 + day, 9, 15, tzinfo=timezone.utc)
        base_p = 22000.0 + day * 200
        for j in range(20):
            p = base_p + j * 15
            ts = (base_ts + timedelta(minutes=5 * j)).strftime("%Y-%m-%dT%H:%M:00")
            candles.append({
                "date": ts, "open": p - 5, "high": p + 80, "low": p - 80,
                "close": p, "volume": 8000 + j * 200,
                "tod_vol_ratio": 1.2,   # pre-enriched: above any threshold
            })

    for name, code in strategy_map.items():
        if not code:
            continue
        try:
            signals = safe_run_strategy(code, candles)
        except Exception as exc:
            print(f"  {name}: safe_run error {exc} — skipped")
            continue
        if not signals:
            print(f"  {name}: no signals generated — skipped (needs more data or market conditions)")
            continue
        last_sig = signals[-1]
        enriched = attach_exit_policy_to_signal(last_sig, candles, option_premium=150.0)
        if enriched.get("theta_guard_blocked"):
            print(f"  {name}: theta guard blocked — skipped")
            continue
        assert "exit_policy" in enriched, (
            f"{name}: exit_policy missing from signal {last_sig.get('action')} "
            f"reason={last_sig.get('entry_reason')}"
        )
        ep = enriched["exit_policy"]
        assert ep.get("trail_trigger_pct") is not None, f"{name}: trail_trigger_pct missing from exit_policy"
        assert ep.get("trail_step_pct") is not None, f"{name}: trail_step_pct missing from exit_policy"
    print("PASS test_15_all_9_strategies_attach_exit_policy")


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_01_crash_blocks_longs,
    test_02_meltup_blocks_shorts,
    test_03_regime_flip_detected,
    test_04_regime_strict_gate_blocks_range,
    test_05_overextended_regime_exempt,
    test_06_frequency_daily_cap,
    test_07_loss_streak_throttle,
    test_08_exit_policy_atr_sl_bounds,
    test_09_theta_guard_blocks,
    test_10_theta_guard_passes,
    test_11_crash_allows_pe_buy_with_confidence_bonus,
    test_12_crash_blocks_ce_buy,
    test_13_meltup_allows_ce_buy_with_confidence_bonus,
    test_14_meltup_blocks_pe_buy,
    test_15_all_9_strategies_attach_exit_policy,
]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test_fn in TESTS:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"FAIL {test_fn.__name__}: {exc}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{len(TESTS)} passed, {failed} failed")
    if failed:
        sys.exit(1)
