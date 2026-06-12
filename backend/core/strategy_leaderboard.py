"""Strategy leaderboard and capital allocation analytics.

Uses only closed realized trade history. Backtests, open P&L, and generated
placeholders are intentionally excluded from capital recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple


IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class ClosedTrade:
    strategy_id: str
    pnl: float
    closed_at: datetime
    source: str


def parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            if "." in text and "+" in text:
                base, tz = text.split("+", 1)
                head, frac = base.split(".", 1)
                try:
                    dt = datetime.fromisoformat(f"{head}.{frac[:6]}+{tz}")
                except ValueError:
                    return None
            else:
                return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_closed_trades(
    raw_trades: Iterable[Dict[str, Any]],
    raw_option_trades: Iterable[Dict[str, Any]],
    known_strategy_ids: set[str],
) -> Tuple[List[ClosedTrade], Dict[str, Any]]:
    trades: List[ClosedTrade] = []
    quality = {
        "included_closed_trades": 0,
        "excluded_missing_strategy": 0,
        "excluded_unknown_strategy": 0,
        "excluded_missing_close_time": 0,
        "excluded_missing_pnl": 0,
        "sources": {"trades": 0, "option_trade_journal": 0},
    }

    def add_trade(row: Dict[str, Any], *, source: str, pnl_keys: List[str], close_keys: List[str]) -> None:
        sid = str(row.get("strategy_id") or "").strip()
        if not sid:
            quality["excluded_missing_strategy"] += 1
            return
        if sid not in known_strategy_ids:
            quality["excluded_unknown_strategy"] += 1
            return

        pnl_value = None
        for key in pnl_keys:
            if row.get(key) is not None:
                pnl_value = row.get(key)
                break
        if pnl_value is None:
            quality["excluded_missing_pnl"] += 1
            return

        close_value = None
        for key in close_keys:
            if row.get(key):
                close_value = row.get(key)
                break
        closed_at = parse_dt(close_value)
        if not closed_at:
            quality["excluded_missing_close_time"] += 1
            return

        trades.append(ClosedTrade(strategy_id=sid, pnl=_float(pnl_value), closed_at=closed_at, source=source))
        quality["included_closed_trades"] += 1
        quality["sources"][source] += 1

    for row in raw_trades:
        add_trade(
            row,
            source="trades",
            pnl_keys=["net_pnl", "net_realized_pnl", "realized_pnl", "gross_realized_pnl", "pnl"],
            close_keys=["closed_at", "exit_time", "filled_at", "updated_at"],
        )
    for row in raw_option_trades:
        add_trade(
            row,
            source="option_trade_journal",
            pnl_keys=["net_pnl", "net_realized_pnl", "pnl", "realized_pnl"],
            close_keys=["exit_time", "closed_at", "updated_at"],
        )

    return trades, quality


def _period_stats(trades: List[ClosedTrade]) -> Dict[str, Any]:
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = None
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None

    max_drawdown = 0.0
    peak = 0.0
    running = 0.0
    for trade in sorted(trades, key=lambda item: item.closed_at):
        running += trade.pnl
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)

    total = len(pnls)
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round((len(wins) / total) * 100, 2) if total else 0.0,
        "net_pnl": round(sum(pnls), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "average_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "average_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "max_drawdown": round(max_drawdown, 2),
    }


def _risk_adjusted_return(stats: Dict[str, Any]) -> float:
    net = _float(stats.get("net_pnl"))
    drawdown = _float(stats.get("max_drawdown"))
    if net <= 0:
        return round(net / max(drawdown, 1.0), 4)
    return round(net / max(drawdown, 1.0), 4)


def _health_score(lifetime: Dict[str, Any], seven_day: Dict[str, Any], thirty_day: Dict[str, Any]) -> int:
    trades = int(lifetime.get("total_trades") or 0)
    if trades <= 0:
        return 0

    net = _float(lifetime.get("net_pnl"))
    profit_factor = _float(lifetime.get("profit_factor"), 0.0)
    max_drawdown = _float(lifetime.get("max_drawdown"))
    win_rate = _float(lifetime.get("win_rate"))
    recent = _float(seven_day.get("net_pnl"))
    month = _float(thirty_day.get("net_pnl"))

    profitability = 35 if net > 0 else 0
    profitability += min(15, max(0, profit_factor - 1) * 10)
    drawdown_score = 20 if max_drawdown <= 0 else max(0, 20 - min(20, (max_drawdown / max(abs(net), 1.0)) * 20))
    consistency = min(20, max(0, win_rate / 5))
    recent_score = 10 if recent > 0 else 5 if month > 0 else 0
    sample_penalty = 0 if trades >= 10 else (10 - trades)

    return int(max(0, min(100, round(profitability + drawdown_score + consistency + recent_score - sample_penalty))))


def _recommendation(row: Dict[str, Any]) -> str:
    stats = row["lifetime"]
    health = int(row["health_score"])
    if stats["total_trades"] == 0:
        return "NO_DATA"
    if health >= 75 and stats["net_pnl"] > 0 and row["risk_adjusted_return"] > 0:
        return "INCREASE_CAPITAL"
    if health < 35 or stats["net_pnl"] < 0:
        return "PAUSE_REVIEW"
    if health < 55:
        return "WATCH"
    return "MAINTAIN"


def _allocation(leaderboard: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    eligible = [
        row for row in leaderboard
        if row["health_score"] >= 50 and row["lifetime"]["net_pnl"] > 0 and row["lifetime"]["total_trades"] > 0
    ]
    if not eligible:
        return []

    weights = []
    for row in eligible:
        risk_score = max(row["risk_adjusted_return"], 0.1)
        pf = _float(row["lifetime"].get("profit_factor"), 2.0)
        pf_score = min(max(pf, 0.1), 5.0)
        weights.append((row, row["health_score"] * risk_score * pf_score))

    total_weight = sum(weight for _, weight in weights) or 1.0
    raw = [(row, (weight / total_weight) * 100) for row, weight in weights]
    rounded = [(row, max(0, round(percent))) for row, percent in raw]
    drift = 100 - sum(percent for _, percent in rounded)
    if rounded:
        best_idx = max(range(len(rounded)), key=lambda idx: raw[idx][1])
        row, percent = rounded[best_idx]
        rounded[best_idx] = (row, max(0, percent + drift))

    return [
        {
            "strategy_id": row["strategy_id"],
            "strategy_name": row["strategy_name"],
            "recommended_percent": int(percent),
            "reason": row["recommendation"],
            "health_score": row["health_score"],
        }
        for row, percent in rounded
        if percent > 0
    ]


def build_strategy_leaderboard(
    strategies: Iterable[Dict[str, Any]],
    raw_trades: Iterable[Dict[str, Any]],
    raw_option_trades: Iterable[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    strategy_rows = [
        row for row in strategies
        if str(row.get("id") or "") not in {"manual", "manual_recovery"}
        and str(row.get("name") or "").upper() != "MANUAL_RECOVERY"
    ]
    known_ids = {str(row.get("id") or "") for row in strategy_rows if row.get("id")}
    trades, quality = normalize_closed_trades(raw_trades, raw_option_trades, known_ids)

    by_strategy: Dict[str, List[ClosedTrade]] = {sid: [] for sid in known_ids}
    for trade in trades:
        by_strategy.setdefault(trade.strategy_id, []).append(trade)

    seven_cutoff = now_utc - timedelta(days=7)
    thirty_cutoff = now_utc - timedelta(days=30)
    leaderboard: List[Dict[str, Any]] = []

    for strategy in strategy_rows:
        sid = str(strategy.get("id") or "")
        lifetime_trades = by_strategy.get(sid, [])
        seven_trades = [trade for trade in lifetime_trades if trade.closed_at >= seven_cutoff]
        thirty_trades = [trade for trade in lifetime_trades if trade.closed_at >= thirty_cutoff]
        lifetime = _period_stats(lifetime_trades)
        last_7_days = _period_stats(seven_trades)
        last_30_days = _period_stats(thirty_trades)
        risk_adjusted_return = _risk_adjusted_return(lifetime)
        row = {
            "strategy_id": sid,
            "strategy_name": strategy.get("name") or sid,
            "status": strategy.get("status"),
            "mode": strategy.get("mode"),
            "risk_adjusted_return": risk_adjusted_return,
            "health_score": _health_score(lifetime, last_7_days, last_30_days),
            "last_7_days": last_7_days,
            "last_30_days": last_30_days,
            "lifetime": lifetime,
        }
        row["recommendation"] = _recommendation(row)
        leaderboard.append(row)

    leaderboard.sort(
        key=lambda row: (
            row["risk_adjusted_return"],
            row["lifetime"]["profit_factor"] if row["lifetime"]["profit_factor"] is not None else 999.0,
            row["lifetime"]["net_pnl"],
        ),
        reverse=True,
    )
    for idx, row in enumerate(leaderboard, start=1):
        row["rank"] = idx

    traded = [row for row in leaderboard if row["lifetime"]["total_trades"] > 0]
    return {
        "as_of": now_utc.isoformat(),
        "basis": "closed_realized_trade_history_only",
        "leaderboard": leaderboard,
        "health_scores": [
            {
                "strategy_id": row["strategy_id"],
                "strategy_name": row["strategy_name"],
                "health_score": row["health_score"],
                "recommendation": row["recommendation"],
            }
            for row in leaderboard
        ],
        "capital_allocation": _allocation(leaderboard),
        "top_performers": traded[:3],
        "worst_performers": sorted(traded, key=lambda row: row["lifetime"]["net_pnl"])[:3],
        "drawdown_monitor": sorted(traded, key=lambda row: row["lifetime"]["max_drawdown"], reverse=True)[:5],
        "best_strategy": traded[0] if traded else None,
        "worst_strategy": sorted(traded, key=lambda row: row["lifetime"]["net_pnl"])[0] if traded else None,
        "data_quality": quality,
    }
