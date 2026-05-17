"""
Backtrader-based local backtesting engine for QuantG.
Executes strategies using Backtrader and returns detailed performance metrics.
"""

import io
import sys
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

import backtrader as bt
import pandas as pd
import numpy as np

from safe_exec import safe_run_strategy


class StrategyWrapper(bt.Strategy):
    """Wraps user Python strategy signals for Backtrader execution."""

    def __init__(self, signal_map, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.signal_map = signal_map
        self.trades_log = []
        self.signals_log = []
        self.equity_curve = []

    def next(self):
        current_date = self.datas[0].datetime.date(0).strftime("%Y-%m-%d")
        action = self.signal_map.get(current_date)
        if action == "BUY" and self.position.size == 0:
            self.buy()
            self.signals_log.append({
                "date": current_date,
                "action": "BUY",
                "price": float(self.datas[0].close[0]),
            })
        elif action == "SELL" and self.position.size > 0:
            self.sell()
            self.signals_log.append({
                "date": current_date,
                "action": "SELL",
                "price": float(self.datas[0].close[0]),
            })

        self.equity_curve.append({
            "date": current_date,
            "equity": round(float(self.broker.getvalue()), 2),
        })

    def notify_trade(self, trade):
        if trade.isclosed:
            current_date = self.datas[0].datetime.date(0).strftime("%Y-%m-%d")
            price = float(self.datas[0].close[0])
            self.trades_log.append({
                "entry_date": current_date,
                "exit_date": current_date,
                "entry_price": price,
                "exit_price": price,
                "qty": float(trade.size),
                "pnl": float(getattr(trade, 'pnl', 0)),
                "pnl_pct": float(getattr(trade, 'pnlcomm', 0) / trade.value * 100) if trade.value else 0,
            })


def run_backtest(
    symbol: str,
    python_code: str,
    starting_capital: float = 100000,
    days: int = 60,
    data_source: str = "yfinance",
) -> Dict[str, Any]:
    """
    Run a backtest using Backtrader.

    Args:
        symbol: Stock ticker (e.g., 'AAPL', 'RELIANCE.NS')
        python_code: User's strategy Python code
        starting_capital: Initial portfolio value
        days: Number of historical days to backtest
        data_source: Data source ('yfinance' for free data)

    Returns:
        Dictionary with backtest results (equity_curve, trades, signals, summary)
    """

    # Load price data
    try:
        data_df = _fetch_price_data(symbol, days, data_source)
        if data_df.empty:
            raise ValueError(f"No data available for {symbol}")
    except Exception as e:
        raise ValueError(f"Failed to fetch data for {symbol}: {e}")

    # Compile user strategy and generate buy/sell signals
    try:
        data_records = [
            {
                "date": idx.strftime("%Y-%m-%d"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
            for idx, row in data_df.iterrows()
        ]
        signals = safe_run_strategy(python_code, data_records)
        if not isinstance(signals, list):
            raise ValueError("Strategy must return a list of signal dictionaries")
    except Exception as e:
        raise ValueError(f"Strategy compilation failed: {e}")

    signal_map = {}
    for signal in signals:
        date = signal.get("date")
        if hasattr(date, "strftime"):
            date = date.strftime("%Y-%m-%d")
        else:
            date = str(date)
        action = str(signal.get("action", "")).upper()
        if action in ("BUY", "SELL"):
            signal_map[date] = action

    # Set up Backtrader cerebro engine
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(starting_capital)

    # Add price data
    data = bt.feeds.PandasData(dataname=data_df)
    cerebro.adddata(data)

    # Add strategy
    cerebro.addstrategy(StrategyWrapper, signal_map=signal_map)

    # Run backtest
    results = cerebro.run()
    strat = results[0]

    final_equity = cerebro.broker.getvalue()
    total_pnl = round(final_equity - starting_capital, 2)
    trades = strat.trades_log
    signals = strat.signals_log
    equity_curve = strat.equity_curve

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2)

    return {
        "engine": "backtrader",
        "mode": "equity",
        "symbol": symbol,
        "data_source": data_source,
        "data_live": False,
        "equity_curve": equity_curve,
        "trades": trades,
        "signals": signals,
        "summary": {
            "starting_capital": starting_capital,
            "final_equity": round(final_equity, 2),
            "total_pnl": total_pnl,
            "return_pct": round((total_pnl / starting_capital) * 100, 2),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_trade_pnl": round(np.mean([t["pnl"] for t in trades]) if trades else 0, 2),
            "max_drawdown": _compute_max_drawdown(equity_curve),
        },
    }

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2)

    return {
        "engine": "backtrader",
        "symbol": symbol,
        "starting_capital": starting_capital,
        "final_equity": round(final_equity, 2),
        "total_pnl": total_pnl,
        "return_pct": round((total_pnl / starting_capital) * 100, 2),
        "trades": trades,
        "signals": signals,
        "equity_curve": equity_curve,
        "summary": {
            "trades_count": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_trade_pnl": round(np.mean([t["pnl"] for t in trades]) if trades else 0, 2),
            "max_drawdown": _compute_max_drawdown(equity_curve),
        },
    }


def _fetch_price_data(symbol: str, days: int, data_source: str = "yfinance") -> pd.DataFrame:
    """Fetch OHLCV data from public sources (free)."""
    if data_source == "yfinance":
        try:
            import yfinance
        except ImportError:
            raise ImportError("yfinance not installed. Install with: pip install yfinance")

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        def _download(ticker: str) -> pd.DataFrame:
            return yfinance.download(ticker, start=start_date, end=end_date, progress=False)

        df = _download(symbol)
        if df.empty and "." not in symbol and symbol.isalpha():
            df = _download(f"{symbol}.NS")

        if df.empty:
            raise ValueError(f"No data returned for {symbol}")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0].strip().lower().replace(" ", "_") for col in df.columns]
        else:
            df.columns = [col.strip().lower().replace(" ", "_") for col in df.columns]

        required = ["open", "high", "low", "close", "volume"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"Missing OHLCV columns from yfinance: {missing}")
        return df[required]
    else:
        raise ValueError(f"Unsupported data source: {data_source}")


def _compute_max_drawdown(equity_curve: List[Dict[str, Any]]) -> float:
    """Compute maximum drawdown percentage from equity curve."""
    if not equity_curve:
        return 0.0

    values = [e["equity"] for e in equity_curve]
    max_val = values[0]
    max_dd = 0.0

    for val in values:
        if val > max_val:
            max_val = val
        dd = (max_val - val) / max_val * 100 if max_val != 0 else 0
        max_dd = max(max_dd, dd)

    return round(max_dd, 2)
