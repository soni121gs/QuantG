"""Unit tests for the keep/kill verdict (WR-41) — pure, broker-free, no DB.

The north-star is risk-adjusted edge (expectancy / Sharpe / profit-factor), NOT
win rate, and a KILL is never issued on a thin sample (the "don't kill on 1-2
days" rule).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.metrics import compute_metrics
from core.strategy_scorecard import keep_kill_verdict, KILL_MIN_TRADES


def _verdict(pnls):
    return keep_kill_verdict(compute_metrics(pnls))["verdict"]


def test_thin_sample_is_watch_not_kill():
    # 4 losing trades — negative, but too few to judge.
    assert _verdict([-100.0, -100.0, -100.0, -100.0]) == "WATCH"


def test_negative_edge_large_sample_is_kill():
    pnls = [-100.0] * KILL_MIN_TRADES  # clearly negative over a sufficient sample
    assert _verdict(pnls) == "KILL"


def test_negative_edge_below_kill_threshold_is_watch():
    pnls = [-100.0] * (KILL_MIN_TRADES - 1)
    assert _verdict(pnls) == "WATCH"


def test_low_winrate_high_payoff_can_keep():
    # 30% win rate but big payoff (trend-follower archetype): +900 x3, -100 x7.
    pnls = [900.0, 900.0, 900.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0]
    v = keep_kill_verdict(compute_metrics(pnls))
    assert v["verdict"] in ("KEEP", "WATCH")  # positive expectancy → never KILL
    assert compute_metrics(pnls)["win_rate"] < 0.5


def test_high_winrate_with_fat_loss_is_not_keep():
    # 90% win rate but one fat loss erases everything: +50 x9, -600 x1 → negative.
    pnls = [50.0] * 9 + [-600.0]
    v = keep_kill_verdict(compute_metrics(pnls))
    assert v["verdict"] != "KEEP"
