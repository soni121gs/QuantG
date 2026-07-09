"""Tests for EM-1: the pure edge-sizer core (core/edge_sizer.py)."""
import math

from core.edge_sizer import (
    RollingStats,
    rolling_stats_from_pnls,
    payoff_ratio,
    kelly_conviction,
    vol_target_base_lots,
    day_governor_mult,
    edge_size,
    vol_scaled_exit_frac,
    EDGE_KELLY_REF,
)


# ── rolling_stats_from_pnls ──────────────────────────────────────────────────────

def test_rolling_stats_empty():
    s = rolling_stats_from_pnls([])
    assert s.n == 0 and s.win_rate == 0.0 and s.expectancy == 0.0


def test_rolling_stats_mixed():
    # 3 wins (+100 avg), 2 losses (-50 avg) → W=0.6, E = 0.6*100 - 0.4*50 = 40
    s = rolling_stats_from_pnls([100, 100, 100, -50, -50])
    assert s.n == 5
    assert s.win_rate == 0.6
    assert s.avg_win == 100.0
    assert s.avg_loss == 50.0
    assert s.expectancy == 40.0


def test_rolling_stats_breakeven_counts_in_n_only():
    s = rolling_stats_from_pnls([100, 0, -100])
    assert s.n == 3
    assert s.win_rate == round(1 / 3, 4)   # only the +100 is a win
    assert s.avg_win == 100.0 and s.avg_loss == 100.0


def test_rolling_stats_all_wins_no_losses():
    s = rolling_stats_from_pnls([10, 20, 30])
    assert s.win_rate == 1.0 and s.avg_loss == 0.0
    assert payoff_ratio(s.avg_win, s.avg_loss) is None  # unknown, not infinite


# ── payoff_ratio ─────────────────────────────────────────────────────────────────

def test_payoff_ratio():
    assert payoff_ratio(200, 100) == 2.0
    assert payoff_ratio(50, 0) is None
    assert payoff_ratio(50, -1) is None


# ── kelly_conviction ─────────────────────────────────────────────────────────────

def test_kelly_conviction_no_edge_is_zero():
    # W=0.4, b=1 → f = 0.4 - 0.6 = -0.2 < 0 → 0
    assert kelly_conviction(0.4, 1.0) == 0.0


def test_kelly_conviction_saturates_at_one():
    # strong edge: W=0.9, b=3 → f = 0.9 - 0.1/3 ≈ 0.867 >> ref → clamps to 1.0
    assert kelly_conviction(0.9, 3.0) == 1.0


def test_kelly_conviction_linear_midrange():
    # choose f exactly = ref/2 → conviction 0.5. Solve W - (1-W)/b = ref/2 with b=1:
    # 2W - 1 = ref/2 → W = (1 + ref/2)/2
    b = 1.0
    target_f = EDGE_KELLY_REF / 2
    W = (1 + target_f) / 2
    assert abs(kelly_conviction(W, b) - 0.5) < 1e-6


def test_kelly_conviction_unknown_payoff_is_zero():
    assert kelly_conviction(0.8, None) == 0.0


# ── vol_target_base_lots ─────────────────────────────────────────────────────────

def test_vol_target_base_lots():
    # 0.4% of 500k = 2000 risk budget; per-lot max loss 2455 → ~0.81 lots
    lots = vol_target_base_lots(500_000, 2455, risk_pct=0.004)
    assert abs(lots - (2000 / 2455)) < 1e-6


def test_vol_target_base_lots_bad_inputs_zero():
    assert vol_target_base_lots(0, 100) == 0.0
    assert vol_target_base_lots(100_000, 0) == 0.0
    assert vol_target_base_lots(100_000, 100, risk_pct=0) == 0.0


# ── day_governor_mult ────────────────────────────────────────────────────────────

def test_day_governor_green_scales_up():
    # +budget → 1 + 1 = 2 → clamps to max 1.5
    assert day_governor_mult(10_000, 10_000) == 1.5


def test_day_governor_red_scales_down():
    # -half budget → 1 - 0.5 = 0.5
    assert day_governor_mult(-5_000, 10_000) == 0.5


def test_day_governor_clamps_min():
    # deep red → clamps to min 0.25
    assert day_governor_mult(-50_000, 10_000) == 0.25


def test_day_governor_flat_is_one():
    assert day_governor_mult(0, 10_000) == 1.0


def test_day_governor_zero_budget_is_neutral():
    assert day_governor_mult(1234, 0) == 1.0


def test_day_governor_profit_ratchet():
    # peak +10k, now +2k (gave back 80% > 50% giveback) → clamp to min despite +ve pnl
    m = day_governor_mult(2_000, 10_000, peak_day_pnl=10_000)
    assert m == 0.25


def test_day_governor_no_ratchet_when_holding_gains():
    # peak +10k, now +8k (gave back only 20% < 50%) → normal scaling, not ratcheted
    m = day_governor_mult(8_000, 10_000, peak_day_pnl=10_000)
    assert m > 0.25


# ── edge_size (combined) ─────────────────────────────────────────────────────────

def _good_stats():
    # W=0.7, avg_win 150, avg_loss 100 → E = 0.7*150 - 0.3*100 = 75
    return rolling_stats_from_pnls([150] * 7 + [-100] * 3)


def test_edge_size_cold_start_uses_neutral_conviction():
    stats = rolling_stats_from_pnls([100, -50])  # n=2 < min
    d = edge_size(stats=stats, payoff_b=2.0, equity=500_000, per_lot_max_loss=2455,
                  day_pnl=0, daily_risk_budget=10_000, min_trades=10)
    assert d.conviction == 0.5
    assert "cold-start" in d.reason


def test_edge_size_negative_expectancy_stands_down():
    # 2 wins +50, 8 losses -100 → E strongly negative
    stats = rolling_stats_from_pnls([50, 50] + [-100] * 8)
    d = edge_size(stats=stats, payoff_b=payoff_ratio(stats.avg_win, stats.avg_loss),
                  equity=500_000, per_lot_max_loss=2455, day_pnl=0, daily_risk_budget=10_000)
    assert d.conviction == 0.0
    assert d.lots == 0
    assert "stand down" in d.reason


def test_edge_size_good_edge_sizes_up_and_respects_day_governor():
    stats = _good_stats()
    b = payoff_ratio(stats.avg_win, stats.avg_loss)
    green = edge_size(stats=stats, payoff_b=b, equity=1_000_000, per_lot_max_loss=2455,
                      day_pnl=10_000, daily_risk_budget=10_000)   # day mult 1.5
    red = edge_size(stats=stats, payoff_b=b, equity=1_000_000, per_lot_max_loss=2455,
                    day_pnl=-5_000, daily_risk_budget=10_000)      # day mult 0.5
    assert green.conviction > 0
    assert green.day_mult == 1.5 and red.day_mult == 0.5
    # a green day sizes strictly larger than the same edge on a red day
    assert green.lots >= red.lots
    assert green.edge_score > red.edge_score


def test_edge_size_floor_lots_respected():
    stats = rolling_stats_from_pnls([50, 50] + [-100] * 8)  # dead edge
    d = edge_size(stats=stats, payoff_b=0.5, equity=500_000, per_lot_max_loss=2455,
                  day_pnl=0, daily_risk_budget=10_000, floor_lots=1)
    assert d.lots == 1  # never below the floor


def test_edge_size_never_raises_on_bad_inputs():
    d = edge_size(stats=RollingStats(0, 0, 0, 0, 0), payoff_b=None, equity=0,
                  per_lot_max_loss=0, day_pnl=0, daily_risk_budget=0)
    assert d.lots == 0


# ── vol_scaled_exit_frac ─────────────────────────────────────────────────────────

def test_vol_scaled_exit_widens_stop_on_high_vol():
    out = vol_scaled_exit_frac(0.5, 2.0, vol_ratio=1.4)
    assert out["tp_frac"] == 0.5
    assert out["sl_mult"] == round(2.0 * 1.4, 4)


def test_vol_scaled_exit_clamps():
    hi = vol_scaled_exit_frac(0.5, 2.0, vol_ratio=5.0)   # clamps to 1.5
    lo = vol_scaled_exit_frac(0.5, 2.0, vol_ratio=0.1)   # clamps to 0.7
    assert hi["sl_mult"] == round(2.0 * 1.5, 4)
    assert lo["sl_mult"] == round(2.0 * 0.7, 4)
