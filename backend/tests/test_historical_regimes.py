from core.historical_regimes import aggregate_by_regime, tag_regimes


def test_regimes_use_only_prior_history():
    candles = [{"date": f"2025-01-{i+1:02d}", "open": 100+i, "close": 100+i} for i in range(25)]
    tags = tag_regimes(candles)
    assert len(tags) == 25
    assert tags[-1]["trend"] == "UPTREND"


def test_aggregate_by_regime():
    regimes = {"2025-01-01": {"trend": "RANGE", "vol": "LOW_VOL"}}
    out = aggregate_by_regime([{"date": "2025-01-01", "pnl": 100}], regimes)
    assert out["RANGE|LOW_VOL"]["expectancy"] == 100
