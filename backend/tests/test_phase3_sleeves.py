from datetime import date, timedelta

from core.daily_delta1_momentum import build_signals, grade_underlying, vol_target_lots
from core.slow_premium_core import build_richness_signals, grade_slow_premium


class _Store:
    def __init__(self):
        self.days = []
        cur = date(2024, 1, 1)
        while cur <= date(2024, 5, 31):
            if cur.weekday() < 5:
                self.days.append(cur.isoformat())
            cur += timedelta(days=1)

    def trading_days(self, start=None, end=None):
        return [d for d in self.days if (not start or d >= start) and (not end or d <= end)]

    def underlying_daily(self, underlying, start=None, end=None):
        out = []
        for i, day in enumerate(self.trading_days(start, end)):
            base = 100 + i * 0.45
            if i > 70:
                base -= (i - 70) * 0.15
            out.append({
                "date": f"{day} 11:00", "open": base * 1.003, "high": base * 1.01,
                "low": base * 0.99, "close": base, "volume": 1000,
            })
        return out

    def expiries(self, underlying, day):
        return ["2024-02-29", "2024-03-28", "2024-04-25", "2024-05-30"]

    def option_chain(self, underlying, day):
        spot = self.underlying_daily(underlying, day, day)[0]["close"]
        chain = {}
        for exp in self.expiries(underlying, day):
            rows = {}
            for strike in range(50, 181, 5):
                dist = abs(strike - spot)
                rows[float(strike)] = {
                    "CE": {"close": max(0.5, 12 - dist * 0.25), "settle": "", "underlying_price": spot, "lot_size": 100},
                    "PE": {"close": max(0.5, 12 - dist * 0.25), "settle": "", "underlying_price": spot, "lot_size": 100},
                }
            chain[exp] = rows
        return chain

    def leg_settle(self, underlying, day, expiry, strike, opt_type):
        return self.option_chain(underlying, day).get(expiry, {}).get(float(strike), {}).get(opt_type, {}).get("close")

    def leg_lot_size(self, underlying, day, expiry, strike, opt_type):
        return 100


def test_delta1_builds_close_and_overnight_signal_streams():
    candles = _Store().underlying_daily("NIFTY")
    close_signals = build_signals(candles, variant="close_to_close")
    overnight = build_signals(candles, variant="overnight")
    assert close_signals["audit"]["accepted"] > 0
    assert overnight["audit"]["variant"] == "overnight"
    assert all(s["direction"] in {"CE", "PE"} for s in close_signals["signals"])


def test_vol_target_lots_scales_down_high_vol():
    assert vol_target_lots(4.0, target_vol_pct=12.0, max_lots=3) == 3
    assert vol_target_lots(30.0, target_vol_pct=12.0, max_lots=3) == 1


def test_delta1_grade_reports_paper_gate():
    out = grade_underlying("NIFTY", store=_Store())
    assert out["status"] == "ready"
    assert "eligible_for_paper" in out["paper_gate"]
    assert out["paper_gate"]["regime_gate"]["regime_count"] >= 0


def test_slow_premium_richness_signal_gate(monkeypatch):
    def fake_surface(store, underlying, day):
        z = 1.0 if day.endswith(("15", "16", "17")) else -0.2
        return {"available": True, "richness": {"available": True, "zscore": z}}

    monkeypatch.setattr("core.slow_premium_core._richness_for", fake_surface)
    out = build_richness_signals(_Store().underlying_daily("NIFTY"), store=_Store(), underlying="NIFTY", min_z=0.75)
    assert out["audit"]["accepted"] > 0
    assert out["audit"]["rejected"]["not_rich"] > 0


def test_slow_premium_grade_reports_two_candidates(monkeypatch):
    monkeypatch.setattr(
        "core.slow_premium_core._richness_for",
        lambda store, underlying, day: {"available": True, "richness": {"available": True, "zscore": 1.0}},
    )
    out = grade_slow_premium(store=_Store())
    assert out["count"] == 2
    assert all("eligible_for_paper" in row.get("paper_gate", {}) for row in out["rows"] if row["status"] == "ready")
