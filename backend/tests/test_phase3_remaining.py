from datetime import date, timedelta

from core.phase3_remaining import (
    assemble_book,
    grade_participant_oi_overlay,
    grade_pead,
    rejudge_paused_sellers,
)


class _Store:
    def __init__(self):
        self.days = []
        cur = date(2024, 1, 1)
        while cur <= date(2024, 6, 30):
            if cur.weekday() < 5:
                self.days.append(cur.isoformat())
            cur += timedelta(days=1)

    def trading_days(self, start=None, end=None):
        return [d for d in self.days if (not start or d >= start) and (not end or d <= end)]

    def underlying_daily(self, underlying, start=None, end=None):
        out = []
        for i, day in enumerate(self.trading_days(start, end)):
            base = 100 + i * 0.2
            out.append({"date": f"{day} 11:00", "open": base, "high": base + 2,
                        "low": base - 2, "close": base + (1 if i % 7 == 0 else 0),
                        "volume": 1000})
        return out

    def expiries(self, underlying, day):
        return ["2024-02-29", "2024-03-28", "2024-04-25", "2024-05-30", "2024-06-27"]

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


def test_pead_returns_honest_gate(monkeypatch):
    monkeypatch.setattr("core.phase3_remaining.events_for", lambda sym, start, end: [
        {"symbol": sym, "date": "2024-02-01"}, {"symbol": sym, "date": "2024-03-01"}
    ])
    out = grade_pead(["RELIANCE"], store=_Store())
    assert out["task"] == "P3-4"
    assert "eligible_for_paper" in out["paper_gate"]


def test_participant_oi_overlay_returns_wire_gate(monkeypatch):
    monkeypatch.setattr("core.phase3_remaining.participant_oi_days", lambda: ["2024-02-01", "2024-02-02"])
    monkeypatch.setattr("core.phase3_remaining.get_participant_oi", lambda day: {
        "rows": [
            {"participant": "FII", "future_index_long": 100, "future_index_short": 50},
            {"participant": "CLIENT", "future_index_long": 10, "future_index_short": 20},
        ]
    })
    out = grade_participant_oi_overlay(store=_Store())
    assert out["task"] == "P3-5"
    assert "wire_overlay" in out["overlay_gate"]


def test_paused_seller_rejudge_shape():
    out = rejudge_paused_sellers(store=_Store())
    assert out["task"] == "P3-6"
    assert len(out["rows"]) == 5
    assert "founder_action" in out["rows"][0]


def test_book_assembly_is_non_mutating_summary(monkeypatch):
    monkeypatch.setattr("core.phase3_remaining.grade_earnings_iv", lambda *a, **k: {"rows": []})
    monkeypatch.setattr("core.phase3_remaining.grade_delta1", lambda *a, **k: {"rows": []})
    monkeypatch.setattr("core.phase3_remaining.grade_slow_premium", lambda *a, **k: {"rows": []})
    monkeypatch.setattr("core.phase3_remaining.grade_participant_oi_overlay", lambda *a, **k: {"rows": []})
    out = assemble_book(store=_Store())
    assert out["task"] == "P3-7"
    assert out["portfolio"]["paper_book_ready"] is False
