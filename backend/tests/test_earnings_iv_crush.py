from datetime import date, timedelta

from core.earnings_iv_crush import build_event_signals, cost_floor_gate, grade_symbol


class _Store:
    def __init__(self):
        self.days = []
        cur = date(2025, 1, 1)
        while cur <= date(2025, 3, 31):
            if cur.weekday() < 5:
                self.days.append(cur.isoformat())
            cur += timedelta(days=1)

    def trading_days(self, start=None, end=None):
        return [d for d in self.days if (not start or d >= start) and (not end or d <= end)]

    def expiries(self, underlying, day):
        if day == "2025-01-09":
            return ["2025-01-16"]
        if day == "2025-01-15":
            return ["2025-01-16"]
        return ["2025-02-27"]

    def underlying_daily(self, underlying, start=None, end=None):
        out = []
        for i, day in enumerate(self.trading_days(start, end)):
            out.append({
                "date": f"{day} 11:00",
                "open": 100 + i * 0.2,
                "high": 102 + i * 0.2,
                "low": 98 + i * 0.2,
                "close": 100 + i * 0.2,
                "volume": 1000,
            })
        return out

    def option_chain(self, underlying, day):
        spot = self.underlying_daily(underlying, day, day)[0]["close"]
        expiry = self.expiries(underlying, day)[0]
        chain = {}
        for strike in range(50, 151, 5):
            dist = abs(strike - spot)
            chain[float(strike)] = {
                "CE": {"close": max(0.5, 10 - dist * 0.3), "settle": "", "underlying_price": spot, "lot_size": 100},
                "PE": {"close": max(0.5, 10 - dist * 0.3), "settle": "", "underlying_price": spot, "lot_size": 100},
            }
        return {expiry: chain}

    def leg_settle(self, underlying, day, expiry, strike, opt_type):
        return self.option_chain(underlying, day).get(expiry, {}).get(float(strike), {}).get(opt_type, {}).get("close")

    def leg_lot_size(self, underlying, day, expiry, strike, opt_type):
        return 100


def _events(symbol, start, end):
    return [
        {"symbol": symbol, "date": "2025-01-10", "event_type": "earnings"},
        {"symbol": symbol, "date": "2025-01-16", "event_type": "earnings"},
        {"symbol": symbol, "date": "2025-02-20", "event_type": "earnings"},
    ]


def test_selector_uses_t_minus_1_and_skips_expiry_week_and_expiry_day():
    out = build_event_signals(symbol="RELIANCE", store=_Store(), event_loader=_events)
    assert [s["date"][:10] for s in out["signals"]] == ["2025-02-19"]
    assert out["signals"][0]["planned_exit_date"] == "2025-02-21"
    assert out["audit"]["rejected"]["event_in_expiry_week"] == 1
    assert out["audit"]["rejected"]["event_is_expiry"] == 1


def test_grade_symbol_reports_research_only_paper_gates():
    out = grade_symbol("RELIANCE", store=_Store(), event_loader=_events)
    assert out["status"] == "ready"
    assert out["selector"]["accepted"] == 1
    assert out["paper_gate"]["sample_gate"]["required_n"] == 300
    assert out["paper_gate"]["eligible_for_paper"] is False
    assert out["trades"][0]["exit_reason"] == "TIME_EXIT"


def test_cost_floor_requires_three_times_friction():
    fail = cost_floor_gate({"expectancy": 900}, structure="iron_condor")
    ok = cost_floor_gate({"expectancy": 1000}, structure="iron_condor")
    assert fail["passed"] is False
    assert ok["passed"] is True
