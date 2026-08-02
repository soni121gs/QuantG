"""QuantG central configuration.

Single source of truth for all tunable constants. Values fall back to
environment variables so they can be overridden in docker-compose.yml
without a code change.

Usage:
    from config import MARKET, RISK, OPTION_QUALITY, PAPER_TRADING, STRATEGY_DEFAULTS, MONITOR
"""
import os

import session_times as _session_times


class MARKET:
    # Lot sizes and strike intervals are NOT here. They live in
    # `core/market_domains.py` and are read via
    #     resolve_domain_by_underlying(underlying).get_lot_size(underlying)
    #     resolve_domain_by_underlying(underlying).get_strike_interval(underlying)
    # which is what CLAUDE.md §6 mandates and what every real consumer already
    # calls. Duplicates lived here until 2026-08-03 with no readers at all, and
    # SENSEX had drifted to 10 against the verified Jan-2026 value of 20 — a
    # wrong number sitting in a file titled "single source of truth" is worse
    # than no number, because the next reader trusts it. Do not re-add them.

    # Market hours IST (24h). Canonical values live in session_times.py —
    # NSE derivatives close moved 15:30 -> 15:40 on 2026-08-03 and cash
    # continuous trading now ends 15:15 (Closing Auction Session 15:15-15:35).
    MARKET_OPEN_HOUR = _session_times.OPEN_MINUTE // 60
    MARKET_OPEN_MINUTE = _session_times.OPEN_MINUTE % 60
    MARKET_CLOSE_HOUR = _session_times.NSE_FO_CLOSE_MINUTE // 60
    MARKET_CLOSE_MINUTE = _session_times.NSE_FO_CLOSE_MINUTE % 60
    # Intraday squareoff for single-leg/equity — must land BEFORE the 15:15 cash
    # auction, so it leads the continuous-cash close, not the derivatives close.
    SQUAREOFF_HOUR = _session_times.EQUITY_SQUAREOFF_MINUTE // 60
    SQUAREOFF_MINUTE = _session_times.EQUITY_SQUAREOFF_MINUTE % 60


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
    SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.05"))          # 5 bps (0.05%) — equity base / floor

    # Slippage model: a simulated MARKET order crosses the book, so the realistic
    # cost is ~half the bid-ask spread (used when live bid/ask or spread_pct is
    # available). These are the fallbacks when the book is unknown, plus a hard
    # cap so a garbage quote can't model an absurd fill.
    OPTION_SLIPPAGE_PCT = float(os.getenv("PAPER_OPTION_SLIPPAGE_PCT", "0.30"))  # 30 bps fallback for options
    SLIPPAGE_MAX_PCT = float(os.getenv("PAPER_SLIPPAGE_MAX_PCT", "5.0"))         # cap at 5% of price

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
    TRAIL_TRIGGER_PCT = 3.5
    TRAIL_STEP_PCT = 2.0
    EXIT_MODE = "tp_sl"
    TARGET_R_MULTIPLE = 2.0


class MONITOR:
    # Position monitor polling intervals (seconds)
    POLL_IN_HOURS = int(os.getenv("POSITION_MONITOR_POLL_SECONDS", "5"))
    OPTION_LTP_STALE_EXIT_SECONDS = int(os.getenv("OPTION_LTP_STALE_EXIT_SECONDS", "300"))
    POLL_OUT_HOURS = 30

    # Signal manager lock TTL (seconds)
    SIGNAL_LOCK_TTL = 90

    # Signal manager tick interval (seconds)
    SIGNAL_TICK = int(os.getenv("SIGNAL_MANAGER_TICK_SECONDS", "5"))

    # Live candle max age before signal is considered stale (seconds)
    LIVE_CANDLE_MAX_AGE = int(os.getenv("STRATEGY_LIVE_CANDLE_MAX_AGE_SEC", "1200"))

    # Broker reconciliation interval (seconds)
    RECONCILIATION_INTERVAL = 180
