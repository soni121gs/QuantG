"""QuantG central configuration.

Single source of truth for all tunable constants. Values fall back to
environment variables so they can be overridden in docker-compose.yml
without a code change.

Usage:
    from config import MARKET, RISK, OPTION_QUALITY, PAPER_TRADING, STRATEGY_DEFAULTS, MONITOR
"""
import os


class MARKET:
    NIFTY_LOT_SIZE = 65          # shares per lot
    BANKNIFTY_LOT_SIZE = 30      # shares per lot
    SENSEX_LOT_SIZE = 10         # shares per lot
    NIFTY_STRIKE_INTERVAL = 50   # points between strikes
    BANKNIFTY_STRIKE_INTERVAL = 100
    SENSEX_STRIKE_INTERVAL = 100

    # Market hours IST (24h)
    MARKET_OPEN_HOUR = 9
    MARKET_OPEN_MINUTE = 15
    MARKET_CLOSE_HOUR = 15
    MARKET_CLOSE_MINUTE = 30
    SQUAREOFF_HOUR = 15          # intraday squareoff trigger hour
    SQUAREOFF_MINUTE = 10        # intraday squareoff trigger minute


class RISK:
    # Per-strategy defaults (overridden by visual_config.risk fields)
    DEFAULT_COOLDOWN_MINUTES = 15
    DEFAULT_MAX_TRADES_DAY = 3
    DEFAULT_DAILY_LOSS_LIMIT = float(os.getenv("DEFAULT_DAILY_LOSS_LIMIT", "750"))   # INR

    # Account-level kill switch defaults
    DEFAULT_MAX_DAILY_LOSS = float(os.getenv("DEFAULT_MAX_DAILY_LOSS", "5000"))      # INR
    DEFAULT_MAX_POSITION_SIZE = float(os.getenv("DEFAULT_MAX_POSITION_SIZE", "0"))   # 0 = no cap
    DEFAULT_PER_STRATEGY_CAPITAL = float(os.getenv("DEFAULT_PER_STRATEGY_CAPITAL", "0"))  # 0 = no cap

    # Delta exposure cap (units = delta × lots)
    DELTA_CAP = float(os.getenv("DELTA_CAP", "500"))

    # ATM delta proxy used when no Greeks feed is available
    ATM_DELTA_PROXY = 0.5

    # Exit circuit breaker: position enters CIRCUIT_BREAKER after this many genuine failures
    EXIT_CIRCUIT_BREAKER_THRESHOLD = 3

    # Positions stuck in EXITING longer than this are auto-reverted to OPEN (seconds)
    EXITING_REVERT_TIMEOUT_SECONDS = 300


class OPTION_QUALITY:
    # Minimum quality scores — matches option_selector_v2 thresholds
    LIVE_MIN_SCORE = 70
    PAPER_MIN_SCORE = 50

    # Quote staleness — age beyond this blocks live orders
    LIVE_STALE_SECONDS = float(os.getenv("QUANTG_OPTION_STALE_SEC", "30"))

    # Spread thresholds used in quality scoring
    SPREAD_EXCELLENT_PCT = 1.0   # score = 20
    SPREAD_GOOD_PCT = 3.0        # score = 15
    SPREAD_ACCEPTABLE_PCT = 6.0  # score = 8
    SPREAD_BLOCK_PCT = 100.0     # unknown spread sentinel

    # Minimum daily volume to consider a contract liquid
    MIN_VOLUME = int(os.getenv("OPTION_MIN_VOLUME", "500"))


class PAPER_TRADING:
    STARTING_BALANCE = int(os.getenv("PAPER_STARTING_BALANCE", "500000"))  # INR
    SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.05"))          # 5 bps (0.05%)

    # Partial fill: strikes with volume below this threshold get a partial fill
    # (50–99% of requested qty). Set to 0 to disable partial fill simulation.
    PARTIAL_FILL_VOLUME_THRESHOLD = int(os.getenv("PAPER_PARTIAL_FILL_VOLUME", "500"))

    # Quote staleness: paper fills whose option_contract quote is older than this
    # are rejected with STALE_QUOTE. Generous default (60s) vs live (30s).
    QUOTE_STALE_SECONDS = int(os.getenv("PAPER_QUOTE_STALE_SEC", "60"))


class STRATEGY_DEFAULTS:
    # Default risk block for newly created strategies (mirrors server.py DEFAULT_STRATEGY_RISK)
    COOLDOWN_MINUTES = 15
    MAX_TRADES_DAY = 3
    DAILY_LOSS_LIMIT = 750.0     # INR
    TARGET_PCT = 2.0             # % take-profit
    STOPLOSS_PCT = 1.0           # % stop-loss
    TRAILING_SL_ENABLED = False
    TRAIL_TRIGGER_PCT = 1.5
    TRAIL_STEP_PCT = 0.5
    EXIT_MODE = "tp_sl"
    TARGET_R_MULTIPLE = 2.0


class MONITOR:
    # Position monitor polling intervals (seconds)
    POLL_IN_HOURS = int(os.getenv("POSITION_MONITOR_POLL_SECONDS", "5"))
    POLL_OUT_HOURS = 30

    # Signal manager lock TTL (seconds)
    SIGNAL_LOCK_TTL = 90

    # Signal manager tick interval (seconds)
    SIGNAL_TICK = int(os.getenv("SIGNAL_MANAGER_TICK_SECONDS", "5"))

    # Live candle max age before signal is considered stale (seconds)
    LIVE_CANDLE_MAX_AGE = int(os.getenv("STRATEGY_LIVE_CANDLE_MAX_AGE_SEC", "1200"))

    # Broker reconciliation interval (seconds)
    RECONCILIATION_INTERVAL = 180
