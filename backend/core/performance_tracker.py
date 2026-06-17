"""QuantG Performance Tracker.

Aggregates statistics from backtesting, paper trading, and live execution.
Calculates 1d, 7d, and 30d P&L, draws down win-rate statistics, and issues status promotions.
"""
from typing import Dict, Any, List
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("quantg.performance_tracker")

class PerformanceTracker:
    def __init__(self, db):
        self.db = db

    async def compile_strategy_scorecard(self, strategy_id: str, user_id: str) -> Dict[str, Any]:
        """Gathers and calculates cross-mode scorecard data for a given strategy."""
        now = datetime.now(timezone.utc)
        ist_offset = timedelta(hours=5, minutes=30)
        today_start = (now + ist_offset).replace(hour=0, minute=0, second=0, microsecond=0) - ist_offset
        seven_days_ago = now - timedelta(days=7)
        thirty_days_ago = now - timedelta(days=30)

        # 1. Fetch Backtest Run Metrics
        backtest = await self.db.backtest_runs.find_one(
            {"strategy_id": strategy_id},
            sort=[("created_at", -1)]
        )
        bt_pnl = backtest.get("pnl", 0.0) if backtest else 0.0
        bt_win_rate = backtest.get("win_rate", 0.0) if backtest else 0.0
        bt_score = backtest.get("strategy_score", 0.0) if backtest else 0.0
        bt_drawdown = backtest.get("max_drawdown_pct", 0.0) if backtest else 0.0

        # 2. Fetch Paper Trading Metrics from closed trade records
        paper_trades = await self.db.trades.find({
            "strategy_id": strategy_id,
            "user_id": user_id,
            "mode": "paper",
        }).to_list(length=1000)

        paper_trades_count = len(paper_trades)
        paper_pnl = sum(
            float(t.get("net_pnl") or t.get("realized_pnl") or 0)
            for t in paper_trades
        )

        paper_wins = sum(
            1 for t in paper_trades
            if float(t.get("net_pnl") or t.get("realized_pnl") or 0) > 0
        )
        paper_win_rate = round(paper_wins / max(1, paper_trades_count), 2)

        # 3. Calculate time-bracket P&Ls from the canonical ledger (db.trade_fills).
        # Single source of truth: same collection + realized_pnl field + CLOSE/REDUCE
        # filter as portfolio_ledger.get_strategy_pnl_today and the execution-snapshot
        # summary. Previously this summed strategy_positions.realized_pnl, which the
        # legacy paper fill engine (_apply_paper_fill_to_position) never writes to —
        # so legacy-filled trades were silently missing from today/7d/30d here while
        # appearing elsewhere. Reading trade_fills fixes that divergence.
        # NOTE: spread_lifecycle does not yet write trade_fills (Stage 4) — credit
        # spread P&L is excluded until that lands; the spread engine is disabled.
        closing_fills = await self.db.trade_fills.find({
            "strategy_id": strategy_id,
            "user_id": user_id,
            "action": {"$in": ["CLOSE", "REDUCE"]},
            "created_at": {"$gte": thirty_days_ago.isoformat()},
        }, {"_id": 0, "realized_pnl": 1, "created_at": 1}).to_list(length=5000)

        today_pnl = 0.0
        seven_day_pnl = 0.0
        thirty_day_pnl = 0.0

        for fill in closing_fills:
            fpnl = float(fill.get("realized_pnl") or 0.0)
            ts_str = fill.get("created_at")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            # Cumulative windows (today ⊂ 7d ⊂ 30d), matching the prior semantics.
            if ts >= thirty_days_ago:
                thirty_day_pnl += fpnl
            if ts >= seven_days_ago:
                seven_day_pnl += fpnl
            if ts >= today_start:
                today_pnl += fpnl

        # 4. Status recommendation model
        status_recommendation = "WATCH"
        if paper_win_rate >= 0.55 and paper_trades_count >= 5:
            status_recommendation = "PROMOTE"
        elif paper_win_rate < 0.40 and paper_trades_count >= 5:
            status_recommendation = "PAUSE"
            
        scorecard = {
            "strategy_id": strategy_id,
            "user_id": user_id,
            "today_pnl": round(today_pnl, 2),
            "seven_day_pnl": round(seven_day_pnl, 2),
            "thirty_day_pnl": round(thirty_day_pnl, 2),
            "paper_pnl": round(paper_pnl, 2),
            "paper_win_rate": paper_win_rate,
            "paper_trades_count": paper_trades_count,
            "backtest_pnl": round(bt_pnl, 2),
            "backtest_win_rate": bt_win_rate,
            "backtest_drawdown": bt_drawdown,
            "backtest_score": bt_score,
            "status_recommendation": status_recommendation,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        # Update strategy performance collection
        await self.db.strategy_performance.update_one(
            {"strategy_id": strategy_id, "user_id": user_id},
            {"$set": scorecard},
            upsert=True
        )
        
        return scorecard

    async def rebuild_leaderboard(self, user_id: str) -> List[Dict[str, Any]]:
        """Rebuilds the complete strategy leaderboard ranking scores."""
        strategies = await self.db.strategies.find({"user_id": user_id}).to_list(length=200)
        leaderboard = []
        for strat in strategies:
            scorecard = await self.compile_strategy_scorecard(strat["id"], user_id)
            leaderboard.append({
                "strategy_id": strat["id"],
                "name": strat["name"],
                "pnl": scorecard["today_pnl"],
                "return_7d": scorecard["seven_day_pnl"],
                "return_30d": scorecard["thirty_day_pnl"],
                "score": scorecard["backtest_score"],
                "win_rate": scorecard["paper_win_rate"],
                "status_recommendation": scorecard["status_recommendation"]
            })
            
        # Sort leaderboard by P&L
        leaderboard.sort(key=lambda s: s["pnl"], reverse=True)
        return leaderboard
