from core.iv_surface import implied_vol, richness_zscore, surface_for_day


class _Store:
    def __init__(self):
        self.days = [f"2025-01-{d:02d}" for d in range(2, 21)]

    def trading_days(self, start=None, end=None):
        return [d for d in self.days if (not start or d >= start) and (not end or d <= end)]

    def option_chain(self, underlying, day):
        spot = 100.0
        idx = int(day[-2:])
        base = 9.0 + idx * 0.1
        rows = {}
        for strike in (90.0, 100.0, 110.0):
            rows[strike] = {
                "CE": {
                    "close": base if strike == 100.0 else max(1.0, base - abs(strike - spot) * 0.3),
                    "settle": "",
                    "underlying_price": spot,
                    "oi": 1000,
                    "volume": 10,
                },
                "PE": {
                    "close": base if strike == 100.0 else max(1.0, base - abs(strike - spot) * 0.25),
                    "settle": "",
                    "underlying_price": spot,
                    "oi": 900,
                    "volume": 8,
                },
            }
        return {"2025-02-27": rows}


def test_implied_vol_solver_returns_reasonable_value():
    iv = implied_vol(5.0, 100.0, 100.0, 30 / 365, "CE")
    assert iv is not None
    assert 0.3 < iv < 0.6


def test_surface_for_day_builds_points_and_summary():
    surface = surface_for_day(_Store(), "NIFTY", "2025-01-20")
    assert surface["available"] is True
    assert surface["spot"] == 100.0
    assert surface["summary"]["point_count"] >= 4
    assert surface["summary"]["near_expiry"] == "2025-02-27"


def test_richness_zscore_needs_history_and_labels_state():
    result = richness_zscore(_Store(), "NIFTY", "2025-01-20", lookback_days=18)
    assert result["available"] is True
    assert result["richness"]["available"] is True
    assert result["richness"]["sample_n"] >= 10
    assert result["richness"]["state"] in {"cheap", "neutral", "rich"}
