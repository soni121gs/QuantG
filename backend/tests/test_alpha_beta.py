"""P5-M4 — alpha/beta separation regression core."""
import os
import random
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.alpha_beta import (  # noqa: E402
    ols, classify_alpha_beta, short_vol_benchmark, align_series,
)


def test_ols_recovers_known_coefficients():
    random.seed(1)
    n = 400
    x1 = [random.gauss(0, 1) for _ in range(n)]
    x2 = [random.gauss(0, 1) for _ in range(n)]
    # y = 0.3 + 1.5*x1 - 0.5*x2 + small noise
    y = [0.3 + 1.5 * a - 0.5 * b + random.gauss(0, 0.05) for a, b in zip(x1, x2)]
    reg = ols(y, {"short_vol": x1, "market": x2})
    assert reg is not None
    assert abs(reg.alpha - 0.3) < 0.05
    assert abs(reg.betas["short_vol"] - 1.5) < 0.05
    assert abs(reg.betas["market"] - (-0.5)) < 0.05
    assert reg.r_squared > 0.98
    assert reg.alpha_t is not None and abs(reg.alpha_t) > 2  # 0.3 intercept is significant


def test_classify_replicable_short_vol():
    random.seed(2)
    n = 300
    sv = [random.gauss(0, 1) for _ in range(n)]
    mkt = [random.gauss(0, 1) for _ in range(n)]
    # pure short-vol beta, ~zero alpha
    y = [1.0 * s + random.gauss(0, 0.1) for s in sv]
    reg = ols(y, {"short_vol": sv, "market": mkt})
    assert classify_alpha_beta(reg) == "REPLICABLE_SHORT_VOL_BETA"


def test_classify_has_alpha():
    random.seed(3)
    n = 300
    sv = [random.gauss(0, 1) for _ in range(n)]
    mkt = [random.gauss(0, 1) for _ in range(n)]
    y = [0.5 + 0.2 * s + random.gauss(0, 0.05) for s in sv]  # strong, significant intercept
    reg = ols(y, {"short_vol": sv, "market": mkt})
    assert classify_alpha_beta(reg) == "HAS_ALPHA"


def test_ols_rejects_thin_sample():
    assert ols([1.0, 2.0], {"x": [1.0, 2.0]}) is None


def test_align_series_inner_joins_on_date():
    a = [{"date": "2025-01-01", "ret": 0.1}, {"date": "2025-01-02", "ret": 0.2}]
    b = [{"date": "2025-01-02", "ret": 0.3}, {"date": "2025-01-03", "ret": 0.4}]
    dates, cols = align_series(a, b)
    assert dates == ["2025-01-02"]
    assert cols == [[0.2], [0.3]]


class _StraddleStore:
    _days = [f"2025-01-{d:02d}" for d in range(1, 11)]

    def trading_days(self, start=None, end=None):
        return list(self._days)

    def underlying_daily(self, underlying, start=None, end=None):
        return [{"date": f"{d} 11:00", "close": 20000.0} for d in self._days]

    def option_chain(self, underlying, day):
        return {"2025-01-30": {20000.0: {"CE": {}, "PE": {}}}}

    def leg_settle(self, underlying, day, expiry, strike, opt_type):
        # premium decays 100 -> 90 -> ... deterministically per day index
        idx = self._days.index(day)
        return max(1.0, 100.0 - 5.0 * idx)


def test_short_vol_benchmark_is_positive_when_premium_decays():
    b = short_vol_benchmark(_StraddleStore(), "2025-01-01", "2025-01-10", hold_days=1)
    assert b and all(r["ret"] > 0 for r in b)   # selling into decay makes money
