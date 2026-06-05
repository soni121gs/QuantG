from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.strategy_leaderboard import build_strategy_leaderboard


NOW = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)


def test_leaderboard_uses_only_closed_trades_with_known_strategy_links():
    strategies = [
        {"id": "s1", "name": "Alpha"},
        {"id": "s2", "name": "Bravo"},
    ]
    trades = [
        {"strategy_id": "s1", "realised_pnl": 1000, "closed_at": "2026-06-01T10:00:00+00:00"},
        {"strategy_id": "s1", "realised_pnl": -250, "closed_at": "2026-06-02T10:00:00+00:00"},
        {"strategy_id": "", "realised_pnl": 9999, "closed_at": "2026-06-02T10:00:00+00:00"},
        {"strategy_id": "deleted", "realised_pnl": 9999, "closed_at": "2026-06-02T10:00:00+00:00"},
        {"strategy_id": "s2", "realised_pnl": 100, "status": "OPEN"},
    ]

    result = build_strategy_leaderboard(strategies, trades, [], now=NOW)
    alpha = next(row for row in result["leaderboard"] if row["strategy_id"] == "s1")
    bravo = next(row for row in result["leaderboard"] if row["strategy_id"] == "s2")

    assert alpha["lifetime"]["total_trades"] == 2
    assert alpha["lifetime"]["win_rate"] == 50.0
    assert alpha["lifetime"]["net_pnl"] == 750
    assert alpha["lifetime"]["profit_factor"] == 4.0
    assert alpha["lifetime"]["average_win"] == 1000
    assert alpha["lifetime"]["average_loss"] == 250
    assert alpha["lifetime"]["max_drawdown"] == 250
    assert bravo["lifetime"]["total_trades"] == 0
    assert result["data_quality"]["excluded_missing_strategy"] == 1
    assert result["data_quality"]["excluded_unknown_strategy"] == 1
    assert result["data_quality"]["excluded_missing_close_time"] == 1


def test_ranking_health_and_allocation_recommendations_are_read_only():
    strategies = [
        {"id": "a", "name": "Trend A"},
        {"id": "b", "name": "Mean B"},
        {"id": "c", "name": "Weak C"},
    ]
    trades = [
        {"strategy_id": "a", "realised_pnl": 700, "closed_at": "2026-05-20T10:00:00+00:00"},
        {"strategy_id": "a", "realised_pnl": 600, "closed_at": "2026-05-25T10:00:00+00:00"},
        {"strategy_id": "a", "realised_pnl": -100, "closed_at": "2026-06-01T10:00:00+00:00"},
        {"strategy_id": "b", "realised_pnl": 350, "closed_at": "2026-05-28T10:00:00+00:00"},
        {"strategy_id": "b", "realised_pnl": -100, "closed_at": "2026-06-01T10:00:00+00:00"},
        {"strategy_id": "c", "realised_pnl": -500, "closed_at": "2026-06-02T10:00:00+00:00"},
    ]

    result = build_strategy_leaderboard(strategies, trades, [], now=NOW)

    assert result["best_strategy"]["strategy_id"] == "a"
    assert result["worst_strategy"]["strategy_id"] == "c"
    assert result["leaderboard"][0]["strategy_id"] == "a"
    assert next(row for row in result["leaderboard"] if row["strategy_id"] == "c")["recommendation"] == "PAUSE_REVIEW"
    assert sum(row["recommended_percent"] for row in result["capital_allocation"]) == 100
    assert result["capital_allocation"][0]["strategy_id"] == "a"
    assert result["basis"] == "closed_realised_trade_history_only"


def test_option_trade_journal_rows_are_counted_when_strategy_is_known():
    result = build_strategy_leaderboard(
        [{"id": "opt", "name": "Option Buyer"}],
        [],
        [{"strategy_id": "opt", "pnl": 125.5, "exit_time": "2026-06-02T09:30:00Z"}],
        now=NOW,
    )

    row = result["leaderboard"][0]
    assert row["lifetime"]["total_trades"] == 1
    assert row["lifetime"]["net_pnl"] == 125.5
    assert result["data_quality"]["sources"]["option_trade_journal"] == 1


def test_leaderboard_excludes_manual_recovery_tool():
    result = build_strategy_leaderboard(
        [
            {"id": "manual_recovery", "name": "MANUAL_RECOVERY"},
            {"id": "real", "name": "Real Strategy"},
        ],
        [],
        [
            {"strategy_id": "manual_recovery", "pnl": 0, "exit_time": "2026-06-02T09:30:00Z"},
            {"strategy_id": "real", "pnl": 100, "exit_time": "2026-06-02T09:31:00Z"},
        ],
        now=NOW,
    )

    assert [row["strategy_id"] for row in result["leaderboard"]] == ["real"]
    assert result["best_strategy"]["strategy_id"] == "real"
