from typing import Dict, Any

HERMES_SKILL_PACK: Dict[str, Dict[str, Any]] = {
    "quantg-live-readiness": {
        "name": "quantg-live-readiness",
        "description": "Checks system readiness for live trading execution.",
        "composes_tools": ["get_live_readiness", "get_feed_status", "get_token_status"],
        "playbook": (
            "Verify all pre-flight checks in live-readiness. Ensure Upstox API keys are saved, "
            "the daily OAuth token is valid, the websocket feed is streaming ticks, and the instrument master is synced. "
            "Report any blocked checks with their hints."
        )
    },
    "quantg-why-no-trade": {
        "name": "quantg-why-no-trade",
        "description": "Diagnoses why the platform hasn't executed trades today.",
        "composes_tools": [
            "get_skipped_signals", "get_market_data_status", "get_active_strategies", 
            "get_logs_errors", "get_today_orders", "get_today_fills", "get_recent_alerts"
        ],
        "playbook": (
            "Check if any strategies are live/running. Verify the feed is connected and streaming. "
            "Scan skipped_signals for filter/rejection reasons. Check logs/errors and recent alerts for system exceptions."
        )
    },
    "quantg-strategy-loss-review": {
        "name": "quantg-strategy-loss-review",
        "description": "Evaluates strategy performance, drawdown streaks, and win-rates.",
        "composes_tools": ["get_strategy_scorecard", "get_daily_report", "get_risk_snapshot"],
        "playbook": (
            "Check realized P&L and drawdown metrics from the scorecard. Spot loss streaks, win-rate degradation, "
            "and suggest adjustments if capital buffers or daily loss limits are close to breach."
        )
    },
    "quantg-feed-token-diagnosis": {
        "name": "quantg-feed-token-diagnosis",
        "description": "Troubleshoots broker auth token status and websocket feed packets.",
        "composes_tools": ["get_upstox_status", "get_market_data_status"],
        "playbook": (
            "Diagnose whether the Upstox session token is expired or whether the websocket is connected but streaming zero ticks. "
            "Differentiate between connection issues and stalled packet data."
        )
    },
    "quantg-eod-report": {
        "name": "quantg-eod-report",
        "description": "Reviews the market session performance and daily P&L summary.",
        "composes_tools": ["get_daily_report", "get_risk_snapshot"],
        "playbook": (
            "Analyze the final realized P&L, unrealized P&L, total trades taken, best/worst strategy, and signal counts. "
            "Provide a clean structured summary of the day's execution."
        )
    },
    "quantg-backtest-review": {
        "name": "quantg-backtest-review",
        "description": "Evaluates option backtest scorecard metrics and expectancy.",
        "composes_tools": ["get_backtest_summary"],
        "playbook": (
            "Analyze Sharpe/Sortino ratios, expectancy, profit factor, win-rate, and drawdowns. "
            "Compare options backtesting metrics under real candle OHLC versus Spot-derived pricing."
        )
    },
    "quantg-vps-deploy-check": {
        "name": "quantg-vps-deploy-check",
        "description": "Checks the container deployment and health metrics.",
        "composes_tools": ["get_live_readiness", "get_logs_errors", "get_recent_alerts"],
        "playbook": (
            "Audit docker containers status, startup flags, and check logs for error stack traces or startup warning messages."
        )
    },
    "quantg-incident-postmortem": {
        "name": "quantg-incident-postmortem",
        "description": "Reconstructs timeline and evidence logs for trading outages.",
        "composes_tools": ["get_recent_alerts", "get_logs_errors", "get_today_fills"],
        "playbook": (
            "Trace order/fill logs, system warnings, and error timelines. Compile a clean markdown table of events "
            "leading up to the outage, outlining root cause and recovery details."
        )
    }
}
