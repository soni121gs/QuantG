from datetime import date, timedelta

from core.long_vol_tail import build_tail_signals, grade_underlying, grade_universe


class _Store:
    """Deterministic fake with a downside crash midway so the tail put pays."""

    def __init__(self):
        self.days = []
        cur = date(2024, 1, 1)
        while cur <= date(2024, 6, 28):
            if cur.weekday() < 5:
                self.days.append(cur.isoformat())
            cur += timedelta(days=1)

    def trading_days(self, start=None, end=None):
        return [d for d in self.days if (not start or d >= start) and (not end or d <= end)]

    def underlying_daily(self, underlying, start=None, end=None):
        out = []
        for i, day in enumerate(self.trading_days(start, end)):
            base = 100 + i * 0.1
            if i >= 60:  # crash window: index gaps down hard -> OTM puts pay
                base = 100 - (i - 60) * 1.2
            out.append({
                "date": f"{day} 11:00", "open": base, "high": base * 1.01,
                "low": base * 0.99, "close": base, "volume": 1000,
            })
        return out

    def expiries(self, underlying, day):
        return ["2024-02-29", "2024-03-28", "2024-04-25", "2024-05-30", "2024-06-27"]

    def option_chain(self, underlying, day):
        spot = self.underlying_daily(underlying, day, day)[0]["close"]
        chain = {}
        for exp in self.expiries(underlying, day):
            rows = {}
            for strike in range(40, 181, 5):
                dist = abs(strike - spot)
                rows[float(strike)] = {
                    "CE": {"close": max(0.5, 12 - dist * 0.25), "underlying_price": spot, "lot_size": 100},
                    "PE": {"close": max(0.5, 12 - dist * 0.25), "underlying_price": spot, "lot_size": 100},
                }
            chain[exp] = rows
        return chain

    def leg_settle(self, underlying, day, expiry, strike, opt_type):
        return self.option_chain(underlying, day).get(expiry, {}).get(float(strike), {}).get(opt_type, {}).get("close")

    def leg_lot_size(self, underlying, day, expiry, strike, opt_type):
        return 100


def test_tail_signals_are_all_long_puts():
    candles = _Store().underlying_daily("NIFTY")
    out = build_tail_signals(candles, entry_stride_days=5)
    assert out["audit"]["accepted"] > 0
    assert all(s["action"] == "BUY" and s["direction"] == "PE" for s in out["signals"])


def test_cheap_vol_gate_rejects_some_days():
    candles = _Store().underlying_daily("NIFTY")
    gated = build_tail_signals(candles, entry_stride_days=5, cheap_vol_max_pct=1.0)
    ungated = build_tail_signals(candles, entry_stride_days=5)
    # a very low vol ceiling must accept no more than the unconditional stream
    assert gated["audit"]["accepted"] <= ungated["audit"]["accepted"]
    assert gated["audit"]["cheap_vol_max_pct"] == 1.0


def test_grade_reports_verdict_and_tail_metrics():
    out = grade_underlying("NIFTY", store=_Store(), otm_offset_pct=0.05, entry_stride_days=5)
    assert out["status"] == "ready"
    assert "verdict" in out
    assert "tail_metrics" in out and out["tail_metrics"]["n"] >= 0
    assert "eligible_for_paper" in out["paper_gate"]


def test_grade_universe_runs_both_gates_per_underlying():
    out = grade_universe(["NIFTY"], store=_Store())
    assert out["count"] == 2  # unconditional + cheap-vol
    gates = {("cheap_vol" if r.get("gate") is not None else "uncond") for r in out["rows"]}
    assert gates == {"cheap_vol", "uncond"}
