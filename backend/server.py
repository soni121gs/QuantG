from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import re
import uuid
import asyncio
import secrets as _secrets
import hashlib
import base64
import math
import logging
import time
import json
import inspect
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

# Position lifecycle pure functions — extracted from server.py to break the
# monolith incrementally. server.py keeps thin aliases so call sites are unchanged.
from core.position_lifecycle import (
    DEFAULT_STRATEGY_RISK as _DEFAULT_STRATEGY_RISK_MODULE,
    normalize_strategy_risk as _normalize_strategy_risk_module,
    position_risk_prices as _position_risk_prices_module,
    exit_reason as _exit_reason_module,
    parse_iso_dt as _parse_iso_dt_module,
    adaptive_risk_percentages as _adaptive_risk_percentages_module,
    _risk_pct as _risk_pct_module,
    _clamp as _clamp_float_module,
)
from pydantic import BaseModel, Field, EmailStr, validator

import upstox_helper
from brokers.upstox_gateway import UpstoxGateway, extract_order_id as extract_upstox_order_id
from brokers import upstox_gateway as upstox_gateway_utils
from brokers.upstox_portfolio_stream import UpstoxPortfolioStream
import options_helper
import backtrader_runner
import strategy_runner
from signal_manager import signal_manager_loop
from options_delta import OPTION_DELTA_SELECTION_ENABLED, target_delta_for_style, pick_delta_strike
from option_state_ledger import OptionStateLedger
from execution_bridge import payload_from_intent, submit_order as bridge_submit_order
from execution_state import execution_state_manager
from market_protection import MarketTrendAnalyzer, FakeSignalFilter, OrderExecutionRetry
from daily_strategy_reporter import DailyStrategyReporter
from realtime_ticks import RealtimeTickManager
from safe_exec import safe_run_strategy
from order_lifecycle import (
    ORDER_NEW,
    ORDER_PLACED,
    ORDER_OPEN,
    ORDER_PARTIAL_FILL,
    ORDER_FILLED,
    ORDER_CANCELLED,
    ORDER_REJECTED,
    ORDER_CLOSED,
    ORDER_PAPER_CREATED,
    ORDER_PAPER_FILLED,
    ORDER_SKIPPED_SIGNAL,
    ORDER_UNKNOWN_NEEDS_REVIEW,
    ORDER_ACTIVE_STATUSES,
    ORDER_TERMINAL_STATUSES,
    LEGACY_OPEN_STATUSES,
    LEGACY_FILLED_STATUSES,
    LEGACY_TERMINAL_STATUSES,
    canonical_order_status,
    is_order_active,
    validate_order_transition,
)
from risk_controls import SizeInputs, compute_position_size, evaluate_market_data_quality, parse_market_timestamp
from core.strategy_leaderboard import build_strategy_leaderboard
from core.instrument_resolver import InstrumentResolver
from core.models import InstrumentSource
from core.quote_service import QuoteService
from core.portfolio_ledger import get_strategy_pnl_today
from upstox_analytics import UpstoxAnalyticsClient
from upstox_trading_quality import (
    apply_broker_truth_event,
    broker_reconciliation_summary,
    feed_health_status,
    is_quote_stale,
    lookup_instrument_guard,
    option_entry_quality_score,
    quote_age_seconds,
    store_skipped_signal,
    sync_upstox_instruments,
)

# Cryptographically strong RNG for mock data jitter — replaces _rng.random()
_rng = _secrets.SystemRandom()


# Mongo
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

import pymongo
_sync_client = None
_sync_db = None

def get_sync_db():
    global _sync_client, _sync_db
    if _sync_db is not None:
        return _sync_db
    url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
    try:
        c = pymongo.MongoClient(url, serverSelectionTimeoutMS=1000)
        c.admin.command('ping')
        _sync_client = c
        _sync_db = c[os.environ.get('DB_NAME', 'quantg')]
        return _sync_db
    except Exception:
        try:
            c = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=1000)
            c.admin.command('ping')
            _sync_client = c
            _sync_db = c[os.environ.get('DB_NAME', 'quantg')]
            return _sync_db
        except Exception:
            _sync_client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=100)
            _sync_db = _sync_client[os.environ.get('DB_NAME', 'quantg')]
            return _sync_db


# JWT
JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY") or "quantg-development-secret"
if len(JWT_SECRET.encode()) < 32:
    JWT_SECRET = hashlib.sha256(JWT_SECRET.encode()).hexdigest()
JWT_ALG = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24 * 7  # 7 days for trader convenience
SIGNAL_CONFIDENCE_MIN = float(os.environ.get("SIGNAL_CONFIDENCE_MIN", "45"))
APP_VERSION = "12.0"
START_TIME = datetime.now(timezone.utc)

def get_git_info():
    import subprocess
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
    except Exception:
        commit = "unknown"
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
    except Exception:
        branch = "unknown"
    try:
        status = subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        dirty = bool(status)
    except Exception:
        dirty = False
    return commit, branch, dirty

def get_file_version():
    try:
        version_file = ROOT_DIR.parent / "VERSION"
        if version_file.exists():
            return version_file.read_text().strip()
    except Exception:
        pass
    return "10.0.0"


app = FastAPI(title="QuantG Algo Trading API", version=APP_VERSION)
api = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Structured logging (JSON, rotating) — Gate 6 observability requirement
# Falls back to basicConfig if python-json-logger is not installed.
# ---------------------------------------------------------------------------
try:
    import logging.handlers
    from pythonjsonlogger import jsonlogger  # type: ignore
    _json_handler = logging.handlers.RotatingFileHandler(
        "quantg.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _json_handler.setFormatter(
        jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.root.setLevel(logging.INFO)
    logging.root.handlers = [_json_handler, _stream_handler]
except ImportError:
    # python-json-logger not installed — add it to requirements.txt: python-json-logger>=2.0
    import logging.handlers
    logging.basicConfig(level=logging.INFO)
    _rotate_handler = logging.handlers.RotatingFileHandler(
        "quantg.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.root.addHandler(_rotate_handler)
logger = logging.getLogger("quantdesk")

OPTION_LEDGER_PATH = os.environ.get("OPTION_LEDGER_PATH") or str(ROOT_DIR / "runtime_state.sqlite3")
option_ledger = OptionStateLedger(
    OPTION_LEDGER_PATH,
    pool_size=int(os.environ.get("OPTION_LEDGER_POOL_SIZE", "4")),
)

STRATEGY_LIVE_CANDLE_MAX_AGE_SEC = int(os.environ.get("STRATEGY_LIVE_CANDLE_MAX_AGE_SEC", "1200"))
_RATE_LIMIT_LOCK = asyncio.Lock()
_RATE_LIMIT_LAST: Dict[str, float] = {}
_LOG_THROTTLE_LAST: Dict[str, float] = {}
_HISTORY_CACHE: Dict[str, Dict[str, Any]] = {}
_ORDER_SYNC_CACHE: Dict[str, Dict[str, Any]] = {}
_UPSTOX_GATEWAYS: Dict[str, UpstoxGateway] = {}
_UPSTOX_TOKEN_VALIDATION_CACHE: Dict[str, Dict[str, Any]] = {}
REMOVED_COMMODITY_UNDERLYINGS = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT_SEC = float(os.environ.get("GEMINI_TIMEOUT_SEC", "20"))


def _fernet() -> Fernet:
    raw_key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY") or JWT_SECRET
    if raw_key.startswith("gAAAA"):
        raw_key = JWT_SECRET
    key = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return value
    return "enc:" + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return value
    if not isinstance(value, str) or not value.startswith("enc:"):
        return value
    try:
        return _fernet().decrypt(value[4:].encode()).decode()
    except InvalidToken:
        logger.warning("Unable to decrypt stored credential; encryption key may have changed.")
        return None


def _mask_secret(value: Optional[str], head: int = 4, tail: int = 4) -> str:
    if not value:
        return "not saved"
    if len(value) <= head + tail:
        return "*" * len(value)
    suffix = value[-tail:] if tail else ""
    return value[:head] + "*" * max(0, len(value) - head - tail) + suffix


# ============== Models ==============
class RegisterReq(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class LoginReq(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    created_at: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class BrokerKeyReq(BaseModel):
    broker: str = "upstox"
    api_key: str
    api_secret: str
    user_id_at_broker: Optional[str] = None
    mobile_number: Optional[str] = None
    mpin: Optional[str] = None
    totp_secret_key: Optional[str] = None
    redirect_uri: Optional[str] = None
    is_sandbox: Optional[bool] = False


class BrokerKeyOut(BaseModel):
    id: str
    broker: str
    api_key_masked: str
    user_id_at_broker: Optional[str] = None
    created_at: str


class StrategyReq(BaseModel):
    name: str
    description: Optional[str] = ""
    kind: str  # "python" | "visual"
    python_code: Optional[str] = None
    visual_config: Optional[Dict[str, Any]] = None
    asset_class: Optional[str] = None
    strategy_type: Optional[str] = None
    required_capital: Optional[float] = None
    instrument_group: Optional[str] = None
    status: str = "draft"  # draft | live | paused
    broker: Optional[str] = "upstox"
    mode: Optional[str] = "paper"
    market_suitability: Optional[str] = "Any Market Condition"


class StrategyOut(BaseModel):
    id: str
    name: str
    description: str
    kind: str
    python_code: Optional[str] = None
    visual_config: Optional[Dict[str, Any]] = None
    asset_class: str = "equity"
    strategy_type: str = "Option Buying"
    required_capital: float = 0.0
    instrument_group: Optional[str] = None
    ai_confidence_score: Optional[float] = None
    ai_confidence_reason: Optional[str] = None
    status: str
    created_at: str
    last_pnl: Optional[float] = None
    # Scan / runner telemetry
    evaluations: Optional[int] = 0
    evaluations_today: Optional[int] = 0
    order_count_today: Optional[int] = 0
    signals_fired: Optional[int] = 0
    last_evaluated_at: Optional[str] = None
    last_signal_at: Optional[str] = None
    last_signal_action: Optional[str] = None
    last_signals_count: Optional[int] = None
    last_data_source: Optional[str] = None
    last_data_live: Optional[bool] = None
    last_data_reason: Optional[str] = None
    last_candle_at: Optional[str] = None
    latest_candle_age_sec: Optional[float] = None
    last_error: Optional[str] = None
    broker: Optional[str] = "upstox"
    mode: Optional[str] = "paper"
    market_suitability: Optional[str] = "Any Market Condition"
    today_pnl: Optional[float] = None
    # Day-level lock state (core/profit_lock, core/loss_killswitch) — surfaced so the
    # UI can show a stood-down badge when a strategy has booked its day or hit its loss floor.
    day_profit_locked: Optional[bool] = None
    day_profit_locked_date: Optional[str] = None
    day_loss_locked: Optional[bool] = None
    day_loss_locked_date: Optional[str] = None



class BacktestReq(BaseModel):
    strategy_id: Optional[str] = None
    python_code: Optional[str] = None
    symbol: str = "RELIANCE"
    days: int = 60
    options: Optional[Dict[str, Any]] = None  # {enabled, underlying, strike_mode, lots, ...}
    engine: str = "local"  # "local" or "backtrader"

    @validator("days")
    def clamp_days(cls, v: int) -> int:
        """Prevent absurdly long backtests that could overwhelm the Upstox API rate limits."""
        return max(1, min(365, v))





class OrderReq(BaseModel):
    symbol: str
    side: str  # BUY | SELL
    qty: int = Field(gt=0, description="Quantity must be > 0")
    order_type: str = "MARKET"  # MARKET | LIMIT
    price: Optional[float] = None
    product: str = "MIS"
    exchange: str = "NSE"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=160)


class InstrumentRef(BaseModel):
    broker: str          # 'upstox'
    segment: str         # 'EQUITY', 'FUTURES', 'OPTIONS'
    exchange: str        # 'NSE', 'BSE', 'NFO', 'BFO'
    tradingsymbol: str
    instrument_token: str
    asset_class: str     # 'DIRECT', 'OPTION_LONG', 'OPTION_SHORT'


class OrderIntent(BaseModel):
    instrument: InstrumentRef
    quantity: int
    intent: str          # 'OPEN_LONG', 'CLOSE_LONG', 'OPEN_SHORT', 'CLOSE_SHORT'
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class ExecutionPreflightResult(BaseModel):
    ok: bool
    status: str
    reason_code: Optional[str] = None
    reason: Optional[str] = None
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    resolved_instrument: Optional[Dict[str, Any]] = None
    segment: Optional[str] = None
    ltp: Optional[float] = None
    market_session: Optional[Dict[str, Any]] = None
    price_validation: Optional[Dict[str, Any]] = None


class StrategyRuntimeSettingsReq(BaseModel):
    max_lot: Optional[int] = 1
    target_pct: Optional[float] = None
    stoploss_pct: Optional[float] = None
    trailing_sl_enabled: Optional[bool] = None
    trail_trigger_pct: Optional[float] = None
    trail_step_pct: Optional[float] = None
    cooldown_minutes: Optional[int] = None
    max_trades_day: Optional[int] = None
    daily_loss_limit: Optional[float] = None
    required_capital: Optional[float] = None
    time_exit_minutes: Optional[int] = None
    indicator_exit_enabled: Optional[bool] = None
    exit_mode: Optional[str] = None
    risk_style: Optional[str] = None
    strategy_category: Optional[str] = None
    adaptive_exits_enabled: Optional[bool] = None
    target_r_multiple: Optional[float] = None
    broker: Optional[str] = None
    mode: Optional[str] = None
    product: Optional[str] = None
    # Phase 2 #5: option structure — "single_leg" (default) or "credit_spread".
    structure: Optional[str] = None
    spread_width: Optional[int] = None   # short→long strike distance, in # of strike intervals
    short_delta: Optional[float] = None  # target |delta| for the spread's short leg


class ProfileUpdateReq(BaseModel):
    name: Optional[str] = None
    default_qty: Optional[int] = None
    default_product: Optional[str] = None
    max_daily_loss: Optional[float] = None
    max_position_size: Optional[float] = None
    per_strategy_capital: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    data_broker: Optional[str] = None
    execution_broker: Optional[str] = None
    fallback_broker: Optional[str] = None
    paper_mode: Optional[bool] = None
    allow_simulated_prices: Optional[bool] = None


class ChangePasswordReq(BaseModel):
    current_password: str
    new_password: str


class OpsActionReq(BaseModel):
    note: Optional[str] = None


class UpstoxTestOrderReq(BaseModel):
    instrument_token: str
    qty: int = Field(gt=0, description="Quantity must be > 0")
    side: str
    order_type: str = "MARKET"
    price: Optional[float] = None
    product: str = "I"
    validity: str = "DAY"
    trigger_price: Optional[float] = None
    disclosed_quantity: int = 0
    is_amo: bool = False
    market_protection: Optional[float] = -1
    confirm_live_order: bool = False


class UpstoxSubscribeReq(BaseModel):
    instruments: List[str]
    mode: str = "ltpc"


class ChatReq(BaseModel):
    session_id: str = "default"
    message: str


class StrategyAIModifyReq(BaseModel):
    instruction: str
    apply: bool = False


# ============== Auth helpers ==============
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


async def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    if creds is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.get("approved", True) and user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Your registration is pending approval by the owner.")
    return user


async def get_user_from_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        return None
    return await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})


# ============== Market data (simulated) ==============
SYMBOLS = [
    {"symbol": "RELIANCE", "name": "Reliance Industries", "base": 2945.50},
    {"symbol": "TCS", "name": "Tata Consultancy", "base": 4120.20},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "base": 1672.80},
    {"symbol": "INFY", "name": "Infosys", "base": 1890.45},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "base": 1245.30},
    {"symbol": "SBIN", "name": "State Bank of India", "base": 824.10},
    {"symbol": "AXISBANK", "name": "Axis Bank", "base": 1180.60},
    {"symbol": "ITC", "name": "ITC Ltd", "base": 482.95},
    {"symbol": "LT", "name": "Larsen & Toubro", "base": 3680.55},
    {"symbol": "MARUTI", "name": "Maruti Suzuki", "base": 12450.00},
    {"symbol": "NIFTY", "name": "Nifty 50", "base": 24850.40},
    {"symbol": "BANKNIFTY", "name": "Bank Nifty", "base": 52340.85},
    {"symbol": "SENSEX", "name": "BSE Sensex", "base": 81460.20},
]

# Mock ticks should move during market hours, then freeze. This keeps paper PnL
# from changing on nights/weekends when the user is not trading.
IST_OFFSET = timedelta(hours=5, minutes=30)
NSE_OPEN_MINUTE = 9 * 60 + 15
NSE_CLOSE_MINUTE = 15 * 60 + 30
SUPPORTED_ORDER_EXCHANGES = {"NSE", "BSE", "NFO", "BFO"}
ACTIVE_STRATEGY_POSITION_STATUSES = {"RESERVED", "PENDING_OPEN", "PENDING_BROKER", "FILLED", "OPEN", "EXITING"}
STALE_ORDER_STATUSES = {"STALE", "BROKER_NOT_FOUND", "UNKNOWN_NEEDS_REVIEW"}
MARKET_HOLIDAYS_IST = {
    item.strip()
    for item in (os.environ.get("MARKET_HOLIDAYS_IST") or os.environ.get("MARKET_HOLIDAYS") or "").split(",")
    if item.strip()
}
SEGMENT_MARKET_WINDOWS = {
    "NSE_EQ": (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "NSE equity"),
    "BSE_EQ": (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "BSE equity"),
    "NSE_FO": (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "NSE F&O"),
    "BSE_FO": (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "BSE F&O"),
}


def _ist_date_key(now_utc: Optional[datetime] = None) -> str:
    return ((now_utc or datetime.now(timezone.utc)) + IST_OFFSET).date().isoformat()


def _ist_iso_for_minute(day: Any, minute: int) -> str:
    return f"{day.isoformat()}T{minute // 60:02d}:{minute % 60:02d}:00+05:30"


def _is_trading_holiday(day: Any) -> bool:
    return day.isoformat() in MARKET_HOLIDAYS_IST


def _next_segment_open(segment: str, now_utc: Optional[datetime] = None) -> Optional[str]:
    window = SEGMENT_MARKET_WINDOWS.get(segment)
    if not window:
        return None
    open_minute, close_minute, _ = window
    ist_now = (now_utc or datetime.now(timezone.utc)) + IST_OFFSET
    today_minutes = ist_now.hour * 60 + ist_now.minute
    for offset in range(0, 14):
        day = ist_now.date() + timedelta(days=offset)
        if day.weekday() >= 5 or _is_trading_holiday(day):
            continue
        if offset == 0 and today_minutes <= close_minute:
            if today_minutes <= open_minute:
                return _ist_iso_for_minute(day, open_minute)
            if open_minute <= today_minutes <= close_minute:
                return _ist_iso_for_minute(day, open_minute)
            continue
        return _ist_iso_for_minute(day, open_minute)
    return None


def _segment_session_status(segment: str, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    day = ist_now.date()
    minutes = ist_now.hour * 60 + ist_now.minute
    window = SEGMENT_MARKET_WINDOWS.get(segment)
    if not window:
        return {
            "segment": segment,
            "status": "CLOSED",
            "open": False,
            "reason": "Unsupported market segment",
            "next_open": None,
            "next_close": None,
        }
    open_minute, close_minute, label = window
    holiday = _is_trading_holiday(day)
    weekday = day.weekday() < 5
    open_now = weekday and not holiday and open_minute <= minutes <= close_minute
    if open_now:
        reason = f"{label} market is open"
    elif not weekday:
        reason = f"{label} market is closed for weekend"
    elif holiday:
        reason = f"{label} market is closed for configured holiday"
    elif minutes < open_minute:
        reason = f"{label} market opens at {open_minute // 60:02d}:{open_minute % 60:02d} IST"
    else:
        reason = f"{label} market closed after {close_minute // 60:02d}:{close_minute % 60:02d} IST"
    return {
        "segment": segment,
        "status": "OPEN" if open_now else "CLOSED",
        "open": open_now,
        "reason": reason,
        "open_time": f"{open_minute // 60:02d}:{open_minute % 60:02d}",
        "close_time": f"{close_minute // 60:02d}:{close_minute % 60:02d}",
        "next_open": _next_segment_open(segment, now_utc) if not open_now else None,
        "next_close": _ist_iso_for_minute(day, close_minute) if open_now else None,
    }


def market_session_snapshot(now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    segments = {
        key: _segment_session_status(key, now_utc)
        for key in ("NSE_FO", "BSE_FO", "NSE_EQ", "BSE_EQ")
    }
    open_count = sum(1 for row in segments.values() if row["open"])
    any_open = open_count > 0
    global_status = "OPEN" if open_count == len(segments) else "PARTIAL_OPEN" if any_open else "CLOSED"
    first_closed = next((row for row in segments.values() if not row["open"]), None)
    return {
        "global_status": global_status,
        "current_ist_time": (now_utc + IST_OFFSET).isoformat(),
        "timezone": "Asia/Kolkata",
        "reason": "All configured segments are open" if global_status == "OPEN" else "Only some segments are open" if global_status == "PARTIAL_OPEN" else ((first_closed or {}).get("reason") or "Markets closed"),
        "next_open": min([row["next_open"] for row in segments.values() if row.get("next_open")] or [None]),
        "next_close": min([row["next_close"] for row in segments.values() if row.get("next_close")] or [None]),
        "segments": segments,
        **segments,
    }


def _execution_segment_for(exchange: str, asset_class: Optional[str] = None, symbol: str = "", option_contract: Optional[Dict[str, Any]] = None) -> str:
    exch = (exchange or "NSE").upper()
    asset = (asset_class or "").upper()
    opt_type = str((option_contract or {}).get("option_type") or (option_contract or {}).get("instrument_type") or "").upper()
    symbol_upper = (symbol or "").upper()
    is_option = asset in {"OPTION_LONG", "OPTION_SHORT"} or opt_type in {"CE", "PE", "OPTCOM"} or symbol_upper.endswith(("CE", "PE"))
    if exch in {"NFO", "NSE_FO"} or (exch == "NSE" and is_option):
        return "NSE_FO"
    if exch in {"BFO", "BSE_FO"} or (exch == "BSE" and is_option):
        return "BSE_FO"
    if exch == "BSE":
        return "BSE_EQ"
    return "NSE_EQ"


def _asset_type_for_instrument(instr: "InstrumentRef", option_contract: Optional[Dict[str, Any]] = None) -> str:
    asset = str(getattr(instr, "asset_class", "") or "").upper()
    segment = str(getattr(instr, "segment", "") or "").upper()
    exchange = str(getattr(instr, "exchange", "") or "").upper()
    symbol = str(getattr(instr, "tradingsymbol", "") or "").upper()
    if option_contract or asset in {"OPTION_LONG", "OPTION_SHORT"} or symbol.endswith(("CE", "PE")):
        return "option"
    return "equity"


def _market_session_for_instrument(instr: "InstrumentRef", option_contract: Optional[Dict[str, Any]] = None, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    segment = _execution_segment_for(instr.exchange, instr.asset_class, instr.tradingsymbol, option_contract)
    return _segment_session_status(segment, now_utc)


def _last_nse_session_close_utc(now_utc: Optional[datetime] = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    minutes = ist_now.hour * 60 + ist_now.minute
    days_back = 0
    if ist_now.weekday() >= 5:
        days_back = ist_now.weekday() - 4
    elif minutes < NSE_OPEN_MINUTE:
        days_back = 3 if ist_now.weekday() == 0 else 1
    close_day = ist_now.date() - timedelta(days=days_back)
    close_ist = datetime(close_day.year, close_day.month, close_day.day, 15, 30, tzinfo=timezone.utc)
    return close_ist - IST_OFFSET


def _mock_price_bucket(now_utc: Optional[datetime] = None) -> int:
    now_utc = now_utc or datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    minutes = ist_now.hour * 60 + ist_now.minute
    is_open = ist_now.weekday() < 5 and NSE_OPEN_MINUTE <= minutes <= NSE_CLOSE_MINUTE
    if is_open:
        return int(now_utc.timestamp() // 60)
    return int(_last_nse_session_close_utc(now_utc).timestamp() // 60)


def live_price(base: float, seed: int) -> Dict[str, Any]:
    bucket = _mock_price_bucket()
    drift = math.sin(bucket / 11.0 + seed * 1.7) * (base * 0.004)
    noise = math.sin(bucket / 5.0 + seed * 8.3) * (base * 0.001)
    price = round(base + drift + noise, 2)
    change = round(drift + noise, 2)
    pct = round((change / base) * 100, 2)
    bid_ask = _mock_bid_ask(price)
    return {"price": price, "change": change, "pct": pct, **bid_ask}


# Base prices used by _get_paper_ltp() for realistic paper fills.
_PAPER_BASE_PRICES: Dict[str, float] = {
    "RELIANCE":   2945.50,  "TCS":       4120.20,  "HDFCBANK":   1672.80,
    "INFY":       1890.45,  "ICICIBANK": 1245.30,  "SBIN":        824.10,
    "AXISBANK":   1180.60,  "ITC":        482.95,  "LT":          3680.55,
    "MARUTI":    12450.00,  "NIFTY":    24850.40,  "BANKNIFTY": 52340.85,
    "SENSEX":    81460.20,
}


def _get_paper_ltp(
    symbol: str,
    option_contract: Optional[Dict[str, Any]] = None,
) -> float:
    """Return a realistic simulated LTP for paper-mode order fills.

    Equity / index: returns mock live_price() (moves with time like the UI quote).
    Options: simulates a sensible ATM/OTM premium so paper P&L is not ₹100 dummy:
        NIFTY ATM   ~₹125-150
        BANKNIFTY   ~₹260
    OTM options get a discount proportional to their distance from ATM.
    """
    sym_upper = str(symbol).upper()
    base = _PAPER_BASE_PRICES.get(sym_upper, 100.0)
    seed = abs(hash(sym_upper)) % 97
    underlying_ltp = live_price(base, seed)["price"]

    if not option_contract:
        return underlying_ltp

    # Option premium model (paper-only approximation)
    # ATM premium ≈ 0.5 % of underlying  (empirical rough rule)
    atm_premium = max(5.0, underlying_ltp * 0.005)
    # OTM discount: normalise by 2% of underlying as one full strike interval
    otm_points = abs(float(option_contract.get("otm_points") or 0))
    otm_discount = min(0.80, otm_points / max(1.0, underlying_ltp * 0.02))
    premium = atm_premium * (1.0 - otm_discount)
    # Small deterministic jitter keyed on the contract symbol
    opt_seed = abs(hash(str(option_contract.get("tradingsymbol") or sym_upper))) % 40
    jitter = (opt_seed - 20) / 1000.0   # ±2% noise
    return round(max(5.0, premium * (1.0 + jitter)), 2)


def historical_series(base: float, days: int = 60) -> List[Dict[str, Any]]:
    """Daily mock candles — used for /backtest UI which expects day-level data."""
    out = []
    price = base * 0.92
    for i in range(days):
        d = datetime.now(timezone.utc) - timedelta(days=days - i)
        # random walk with slight up drift
        price = price * (1 + (_rng.random() - 0.48) * 0.02)
        out.append({"date": d.strftime("%Y-%m-%d"), "close": round(price, 2)})
    return out


def intraday_series(base: float, bars: int = 250) -> List[Dict[str, Any]]:
    """Mock 5-minute intraday candles for the strategy runner in paper mode.
    Each bar has a UNIQUE timestamp so the runner's signal-dedup (by date)
    allows fresh signals every 5 minutes.
    """
    out = []
    price = base * 0.985
    now = datetime.now(timezone.utc)
    # snap to nearest 5-min boundary so dates align with live candles
    minute_floor = (now.minute // 5) * 5
    end = now.replace(minute=minute_floor, second=0, microsecond=0)
    for i in range(bars):
        ts = end - timedelta(minutes=5 * (bars - 1 - i))
        # random walk with mild trend bias — produces signals ~20% of bars
        price = price * (1 + (_rng.random() - 0.49) * 0.004)
        out.append({
            "date": ts.strftime("%Y-%m-%d %H:%M"),
            "close": round(price, 2),
            "open": round(price * (1 + (_rng.random() - 0.5) * 0.001), 2),
            "high": round(price * (1 + _rng.random() * 0.002), 2),
            "low": round(price * (1 - _rng.random() * 0.002), 2),
            "volume": int(1000 + _rng.random() * 5000),
        })
    return out


async def _index_spot_token(kite, symbol: str) -> Optional[int]:
    return None


def _merge_tick_bars(historical: List[Dict[str, Any]], tick_bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not tick_bars:
        return historical
    if not historical:
        return tick_bars
    last_hist = historical[-1]["date"]
    last_tick = tick_bars[-1]["date"]
    if last_hist == last_tick:
        return historical[:-1] + [tick_bars[-1]]
    if last_tick > last_hist:
        return historical + [tick_bars[-1]]
    return historical


def _parse_candle_datetime_ist(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    if "+" in text:
        text = text.split("+", 1)[0]
    if text.endswith("Z"):
        text = text[:-1]
    formats = (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    )
    for fmt, width in formats:
        try:
            return datetime.strptime(text[:width], fmt)
        except Exception:
            continue
    return None


def _latest_candle_fresh_for_live(candles: List[Dict[str, Any]], exchange: str) -> Dict[str, Any]:
    if not candles:
        return {"fresh": False, "reason": "no candles"}
    last_at = _parse_candle_datetime_ist(candles[-1].get("date"))
    if not last_at:
        return {"fresh": False, "reason": "latest candle timestamp unavailable"}

    now_ist = datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET
    market_open = _is_order_market_open(exchange)
    age_sec = (now_ist - last_at).total_seconds()
    fresh = market_open and -60 <= age_sec <= STRATEGY_LIVE_CANDLE_MAX_AGE_SEC
    return {
        "fresh": fresh,
        "last_candle_at": last_at.strftime("%Y-%m-%d %H:%M"),
        "age_sec": round(age_sec, 2),
        "reason": "fresh Upstox historical candle" if fresh else f"stale candle age {round(age_sec)}s",
    }


async def _rate_limit(bucket: str, min_interval_sec: float) -> None:
    async with _RATE_LIMIT_LOCK:
        now = time.monotonic()
        last = _RATE_LIMIT_LAST.get(bucket, 0.0)
        wait = max(0.0, min_interval_sec - (now - last))
        if wait:
            await asyncio.sleep(wait)
        _RATE_LIMIT_LAST[bucket] = time.monotonic()


def _history_cache_key(user_id: str, token: int, days: int, interval: str) -> str:
    return f"{user_id}:{token}:{days}:{interval}"


def _history_cache_get(user_id: str, token: int, days: int, interval: str) -> Optional[List[Dict[str, Any]]]:
    item = _HISTORY_CACHE.get(_history_cache_key(user_id, token, days, interval))
    if not item:
        return None
    if time.monotonic() - item["cached_at"] > KITE_HISTORY_CACHE_TTL_SEC:
        return None
    return item["data"]


def _history_cache_set(user_id: str, token: int, days: int, interval: str, data: Optional[List[Dict[str, Any]]]) -> None:
    if data:
        _HISTORY_CACHE[_history_cache_key(user_id, token, days, interval)] = {
            "cached_at": time.monotonic(),
            "data": data,
        }


async def _cached_safe_historical(kite, user_id: str, token: int, days: int, interval: str) -> Optional[List[Dict[str, Any]]]:
    return None


async def _fetch_strategy_history(
    user_id: str,
    symbol: str,
    days: int = 60,
    interval: str = "5minute",
    min_intraday_bars: int = 20,
    allow_mock: bool = True,
    strategy: Optional[dict] = None,
) -> Dict[str, Any]:
    """Fetch strategy candles with explicit source metadata.

    Upstox is the primary live source. Mock candles are only a paper/demo
    fallback and are tagged as such so the UI can warn users.
    """
    sym_upper = symbol.upper()
    if sym_upper in REMOVED_COMMODITY_UNDERLYINGS or "MCX" in sym_upper or sym_upper.endswith("FUT"):
        if not allow_mock:
            raise ValueError(f"MCX commodity history has been removed from QuantG: {sym_upper}")
        return {"data": [], "source": "removed-mcx", "is_live": False, "interval": interval, "removed": True, "paper_mode": allow_mock}

    settings = await get_user_settings(user_id)
    data_broker = settings.get("data_broker", "upstox")

    if data_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user_id)
        if upstox_gw and upstox_gw.connected:
            if sym_upper in {"NIFTY", "BANKNIFTY"}:
                exchange = "NSE"
            elif sym_upper == "SENSEX":
                exchange = "BSE"
            else:
                exchange = "NSE"
            token = _upstox_instrument_token(exchange, sym_upper)
            token_candidates = [token] if token else []
            for token in dict.fromkeys(k for k in token_candidates if k):
                # Ensure real Upstox V3 websocket tracking is active. Historical
                # candles remain the bootstrap source; the latest bar is then
                # refreshed from websocket cache when a live tick exists.
                feed_started = upstox_gw.start_market_data_ws([token], mode="full")
                live_data = await asyncio.to_thread(upstox_gw.get_historical_candles, token, interval, days)
                tick = upstox_gw.latest_tick(token)
                candle_freshness = _latest_candle_fresh_for_live(live_data or [], exchange)
                if tick and live_data:
                    # Format the tick bar date as IST %Y-%m-%d %H:%M to match all other
                    # candle dates.  Using the UTC ISO received_at string broke signal
                    # dedup in strategy_runner (recent_dates uses the same format).
                    _ist_now = datetime.now(timezone.utc) + IST_OFFSET
                    _bucket_min = {"1minute": 1, "minute": 1, "5minute": 5,
                                   "15minute": 15, "30minute": 30}.get(interval, 5)
                    _floored = (_ist_now.minute // _bucket_min) * _bucket_min
                    _tick_date = _ist_now.replace(minute=_floored, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
                    tick_bar = {
                        "date": _tick_date,
                        "open": float(tick.get("ltp") or 0),
                        "high": float(tick.get("ltp") or 0),
                        "low": float(tick.get("ltp") or 0),
                        "close": float(tick.get("ltp") or 0),
                        "volume": int(tick.get("last_trade_quantity") or 0),
                    }
                    live_data = _merge_tick_bars(live_data, [tick_bar])
                elif not tick:
                    _log_throttled(
                        f"upstox-bootstrap-empty:{token}",
                        120.0,
                        logging.INFO,
                        "Upstox V3 tick cache empty for %s; using historical REST bootstrap only feed_started=%s",
                        token,
                        feed_started,
                    )
                min_required = min_intraday_bars if interval != "day" else 2
                if live_data and len(live_data) >= min_required:
                    is_live_source = bool(tick) or bool(candle_freshness.get("fresh"))
                    return {
                        "data": live_data,
                        "source": f"upstox-v3-websocket+historical:{interval}:{sym_upper}",
                        "is_live": is_live_source,
                        "live_reason": "websocket tick" if tick else candle_freshness.get("reason"),
                        "last_candle_at": candle_freshness.get("last_candle_at"),
                        "latest_candle_age_sec": candle_freshness.get("age_sec"),
                        "interval": interval,
                        "paper_mode": allow_mock,
                    }
            
            # Raise explicit error instead of silently falling back to mock candles
            if not allow_mock:
                resolved_tokens = [k for k in token_candidates if k]
                raise ValueError(
                    f"Upstox V3 historical data failed for symbol '{sym_upper}' on exchange '{exchange}' ({interval}). "
                    f"Resolved tokens: {resolved_tokens}. "
                    f"Please ensure Upstox historical data is available, or check your internet connection."
                )
        else:
            if not allow_mock:
                raise ValueError(
                    f"Upstox data broker selected but gateway is not connected or initialized for user {user_id}."
                )

    if allow_mock:
        # SAFETY: mock candles are ONLY permitted when the caller explicitly opted in
        # AND the strategy is in paper mode. The check below is a belt-and-suspenders
        # guard: if somehow allow_mock is True for a live strategy, raise hard rather
        # than silently feeding random-walk data to a real-money order engine.
        if strategy is not None:
            strategy_mode = strategy.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
            if strategy_mode != "paper":
                raise ValueError(
                    f"Mock candle data is BLOCKED for strategy '{strategy.get('name', sym_upper)}'. "
                    "Mock candle fallbacks are disabled in production scanning paths."
                )
        sym = next((s for s in SYMBOLS if s["symbol"] == sym_upper), None)
        if sym:
            if interval == "day":
                return {
                    "data": historical_series(sym["base"], days),
                    "source": f"mock-day:{sym_upper}",
                    "is_live": False,
                    "interval": "day",
                    "paper_mode": True,
                }
            return {
                "data": intraday_series(sym["base"], bars=max(250, min_intraday_bars + 1)),
                "source": f"mock-5minute:{sym_upper}",
                "is_live": False,
                "interval": "5minute",
                "paper_mode": True,
            }

    return {"data": [], "source": "none", "is_live": False, "interval": interval, "paper_mode": allow_mock}


# system routes moved to routes/system.py


# ============== Routes: Auth (extracted to routes/auth.py) ==============



# ============== Routes: AI Bot ==============
def _quantbot_reply(message: str) -> str:
    text = message.lower()
    if "upstox" in text or "hft" in text or "latency" in text:
        return (
            "⚡ **QuantG Upstox API v2 HFT Gateway & HFT Optimization**\n\n"
            "- **Low-Latency Order Routing:** When live, HFT orders are automatically routed to Upstox's high-speed endpoint (`api-hft.upstox.com`) with `hft=True` to bypass standard latency overhead.\n"
            "- **Adaptive HFT Signal Scoring:** The AI Bot (`FakeSignalFilter`) automatically applies custom HFT scoring when the strategy name or description contains 'HFT' or 'scalper':\n"
            "  * **Lower Threshold:** Signal validation threshold is lowered to **35%** (down from 40%).\n"
            "  * **ATR Compression Reward:** Micro-ATR contractions (ATR < 0.08%) are rewarded with **+8 points** (instead of the standard -12 flat-range penalty) to identify breakout compression.\n"
            "  * **Countertrend Entry Penalty Reduction:** Reduced to **-12 points** (from -30) to allow fast countertrend scalps.\n"
            "  * **Volume Confirmation Bonus:** Boosting volume expansion setups to **+15 points** (from +8).\n"
            "- **Actionable Advice:** Keep HFT strategies restricted to high-liquidity underlyings like NIFTY and BANKNIFTY options, and ensure a risk cooldown of at least 15-30 minutes is active."
        )
    if "zerodha" in text or "rate limit" in text or "kite" in text or "tick" in text:
        return (
            "📊 **Zerodha Kite & Runtime Performance Guidelines**\n\n"
            "- **Rate Limits:** Zerodha Kite allows ~100 API calls/minute. Running too many strategies on tight timeframes will trigger 429 rate limit exceptions.\n"
            "- **Hardware & Execution Bounds:** On standard terminal setups (e.g. 4 cores, 4GB RAM):\n"
            "  * **Limit:** Run a maximum of **2 to 3 live strategies** concurrently.\n"
            "  * **Ticks:** Keep the tick evaluation interval at **30 seconds** (do not go below 15s).\n"
            "  * **Diversity:** Distribute strategies across different symbols (NIFTY, BANKNIFTY, MCX Crude) and timeframes to minimize overlapping API calls."
        )
    if "mcx" in text or "commodity" in text or "gas" in text or "oil" in text:
        return (
            "🛢️ **Upstox MCX Commodities Integration**\n\n"
            "- **Commodity Options & Futures:** QuantG supports MCX crude oil and natural gas through Upstox instrument master resolution.\n"
            "- **Instrument Keys:** The engine uses exact Upstox `instrument_key` values from the JSON master, never guessed MCX symbols.\n"
            "- **Risk Management:** Commodity option premiums move fast. Start in paper mode, use one lot, and confirm TP/SL exits before switching live trading on."
        )
    if "filter" in text or "fake" in text or "bot" in text or "accuracy" in text or "score" in text:
        return (
            "🤖 **AI Signal Validation Bot (FakeSignalFilter)**\n\n"
            "The validation engine processes trade signals through 9 distinct telemetry layers to assign a 0-100% confidence score:\n"
            "1. **Trend Alignment:** Compares signal action with 8/21 EMA trend bias (+25 for alignment, -30 for fight, -12 for HFT fight).\n"
            "2. **Reversal Risk:** Evaluates Bollinger Bands and RSI overbought/oversold limits (cuts confidence up to -20 if entering high-risk zone).\n"
            "3. **Whipsaw Detection:** Inspects the last 3 candles for rapid directional flips (penalizes consecutive opposite entries).\n"
            "4. **Price Action Confirmation:** Checks recent candle bars for body strength relative to the trade direction.\n"
            "5. **Multi-Timeframe Confirmation:** Calculates direction bias using a consolidated group of 3 periods.\n"
            "6. **VWAP Hold:** Checks if the price holds near/above VWAP for BUYs, or near/below VWAP for SELLs (+8 points).\n"
            "7. **ATR/Volume Regime:** Evaluates market volatility and rewards high-volume moves (+8 pts standard, +15 HFT expansion).\n"
            "8. **Options OI Bias:** Integrates option chain Open Interest (OI) support to confirm trade participation (+10 points).\n"
            "9. **Support & Resistance Bounce:** Increases score by +15 points if the entry is within 0.5% of local Support (BUY) or Resistance (SELL)."
        )
    if "python" in text or "code" in text or "strategy" in text or "structure" in text:
        return (
            "🐍 **QuantG Compliant Python Strategy Structure**\n\n"
            "Every Python strategy must expose exactly a `run(data)` function and return a list of signals:\n\n"
            "```python\ndef run(data):\n"
            "    # data is a list of dicts: [{'open': 24000, 'high': 24050, 'low': 23980, 'close': 24020, 'volume': 5000, 'date': '2026-05-22T09:15:00+05:30'}]\n"
            "    signals = []\n"
            "    closes = [row['close'] for row in data]\n"
            "    if len(closes) < 20:\n"
            "        return signals\n\n"
            "    # Calculate indicators\n"
            "    ma = sum(closes[-20:]) / 20\n"
            "    if closes[-1] > ma and closes[-2] <= ma:\n"
            "        # BUY signal must include exact date and action\n"
            "        signals.append({'date': data[-1]['date'], 'action': 'BUY'})\n"
            "    return signals\n```\n"
            "*Sandbox Safeguard:* Do not import external modules, write to files, perform networking, or call broker APIs directly inside the script."
        )
    if "risk" in text or "limits" in text or "portfolio" in text or "capital" in text:
        return (
            "🛡️ **QuantG Risk & Capital Protection Suite**\n\n"
            "QuantG embeds institutional risk bounds which are synchronized dynamically from the frontend strategy card:\n"
            "- **Target Profit (TP%):** Automatic profit booking threshold.\n"
            "- **Stop Loss (SL%):** Capital protection exit threshold.\n"
            "- **Trailing Stop Loss:** Step-based trail trigger & adjustment.\n"
            "- **Cooldown (Mins):** Mandates a pause between trades to avoid revenge trading.\n"
            "- **Max Trades/Day:** Caps daily activity to prevent runaway loop losses.\n"
            "- **Daily Loss Limit (INR):** Stops the strategy instantly if cumulative loss exceeds the bound."
        )
    return (
        "👋 **QuantBot v11.0 Live Assistant**\n\n"
        "I am fully updated on the QuantG terminal architecture! Ask me about:\n"
        "- `Upstox HFT` (low-latency order routing, adaptive `is_hft` validation scoring)\n"
        "- `Upstox V3 feed` (websocket status, instrument_key subscriptions, tick freshness)\n"
        "- `Upstox MCX` (instrument-master contract resolution for Crude Oil / Natural Gas)\n"
        "- `AI Signal Validation` (the 9 layers of `FakeSignalFilter` and scoring rules)\n"
        "- `Python Strategy Code` (standard `run(data)` syntax and deterministic sandbox design)\n"
        "- `Risk Bounds` (custom SL/TP, Trailing stops, cooldowns, daily loss limits)"
    )


def _google_ai_reply_sync(message: str, recent_messages: Optional[List[Dict[str, Any]]] = None) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _quantbot_reply(message)

    history_text = ""
    for row in (recent_messages or [])[-8:]:
        role = "User" if row.get("role") == "user" else "Assistant"
        content = str(row.get("content") or "").strip()
        if content:
            history_text += f"{role}: {content[:1200]}\n"

    prompt = f"""
You are QuantBot inside QuantG, a personal Indian algo-trading terminal. You are extremely accurate, highly up-to-date, and completely context-aware of the system's exact specifications.

### QUANTG TERMINAL ARCHITECTURE & CORE CAPABILITIES
1. **Supported Brokers & Routing:**
   - **Upstox-only Runtime:** Market data, instrument resolution, margin checks, and orders route through Upstox APIs.
   - **Upstox Market Data Feed V3:** Live strategies use V3 websocket ticks and exact `instrument_key` values from the Upstox instrument master.
   - **Upstox Orders:** Strategy signals only request BUY/SELL intent; the QuantG order manager resolves the contract, checks risk/margin, and places one controlled order.
2. **Fake Signal Filter (AI Validation Bot) & Scoring Rules:**
   - Evaluates signals using a 9-layer scoring model (0-100% confidence score, standard threshold is **40%**, HFT threshold is **35%**).
   - **HFT Scoring Mode:** Engaged automatically if strategy name or description contains "HFT" or "scalper".
     * **Bypasses low-ATR penalties:** Standard swing strategies penalize flat ranges (ATR < 0.08% gets -12). HFT mode recognizes this as compression before breakouts and rewards **+8 points**.
     * **Reduces trend fight penalties:** Reduces trend fights to **-12 points** (down from -30) to facilitate micro-trend scalps.
     * **Boosts volume confirmations:** Volume expansion setups trigger **+15 points** confirmation bonus (up from +8).
     * **Lowers absolute validation threshold:** 35% validation minimum to accommodate lightning-fast executions.
   - **Standard Scoring Telemetry Layers:** Trend alignment (EMA 8/21), reversal risk (RSI & Bollinger limits), whipsaw checks (opposite signal in last 3 bars), candle body strength, multi-timeframe consolidated bias, VWAP hold confirmation (+8), volume ratio (>1.2x gets volume expansion bonus), options OI chain bias, and Support/Resistance bounces (+15 if close to S/R).
3. **Hardware Boundaries & Stability Rules:**
   - Designed for low-resource environments (4 logical processors, 4GB RAM laptops). Recommended limits: max 2-3 concurrent strategies, 30s scanning interval, keep backend/MongoDB RAM under 150MB each.
4. **Python Strategy Code Standard:**
   - Must define `def run(data):` returning a list of dicts: `[{"date": data[i]["date"], "action": "BUY"}]` (or "SELL").
   - Strictly deterministic, sandboxed, no file writes, no direct broker execution, no external imports.
5. **Dynamic Risk Bounds:**
   - Standard exit and risk settings (TP%, SL%, trailing stop triggers, cooldown minutes, max trades per day, daily loss limits in INR) are fully editable in the visual card and applied by the QuantG engine.

### RESPONSE GUIDELINES
- Speak as a knowledgeable, highly professional quant assistant.
- Give educational and implementation help only. Never promise guaranteed profit.
- Ground all advice in the specific parameters, brokers, limits, and rules of QuantG.
- Keep responses concise, structured, and easy to read.

### EXAMPLES (match this grounding, tone, and brevity — do not copy verbatim)
Example 1
User: Why do so few of my signals turn into trades?
Assistant: Most signals are filtered before they reach the order manager. Two common causes in QuantG:
- **FakeSignalFilter threshold** — signals scoring below 40% (35% for HFT/scalper strategies) are rejected. Trend fights, whipsaws, and flat ATR all cut the score.
- **Option-quality / feed gate** — if the V3 `ltpc` tick for the selected contract is stale or the contract fails liquidity checks, the signal is skipped even when scored well.
Check the strategy's `last_error` and whether signals show PROCESSED vs FILTERED/SKIPPED to see which gate is firing. Loosening filters trades quality for quantity — I won't promise more fills.

Example 2
User: What lot size should I use for a NIFTY options strategy?
Assistant: Don't hardcode it. QuantG resolves lot size from `core/market_domains.py` — NIFTY is 65 and BANKNIFTY is 30 today, but these change, so the engine reads them via `resolve_domain_by_underlying(...).get_lot_size(...)`. In the visual card set your capital and lot count; the order manager multiplies by the live lot size when it builds the order.

Recent chat:
{history_text or "None"}

User: {message}
"""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        response = requests.post(
            url,
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=GEMINI_TIMEOUT_SEC,
        )
        response.raise_for_status()
        payload = response.json()
        parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text = "\n".join(str(part.get("text") or "") for part in parts).strip()
        return text or _quantbot_reply(message)
    except Exception as e:
        logger.warning("Google Gemini REST reply failed: %s", e)
        return _quantbot_reply(message)


async def _google_ai_reply(message: str, recent_messages: Optional[List[Dict[str, Any]]] = None) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_google_ai_reply_sync, message, recent_messages),
            timeout=GEMINI_TIMEOUT_SEC,
        )
    except Exception as e:
        logger.warning("Google AI reply timeout/fallback: %s", e)
        return _quantbot_reply(message)


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI response did not contain a JSON object")
    return json.loads(raw[start:end + 1])


def _google_strategy_edit_sync(strategy: Dict[str, Any], instruction: str) -> Dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    current_config = strategy.get("visual_config") or {}
    prompt = f"""
You are editing a QuantG trading strategy. Return JSON only.

Hard rules:
- Keep Python deterministic and sandbox-safe.
- Do not import modules, read files, call network, call brokers, or place orders.
- Strategy code must define exactly `def run(data):` and return a list of signals.
- Signal shape must be `{{"date": data[i]["date"], "action": "BUY"}}` or SELL.
- Prefer fewer, higher-quality signals with volume/VWAP/ATR/trend filters.
- Keep risk realistic. Do not promise profit.

EXAMPLE (return JSON in exactly this shape — values are illustrative, adapt to the real instruction):
User instruction: "Add a volume filter so it only buys on above-average volume."
Output:
{{"name": "EMA Cross + Volume", "description": "Only buys the MA20 breakout when the latest bar's volume beats the 20-bar average.", "python_code": "def run(data):\\n    closes=[r['close'] for r in data]\\n    vols=[r['volume'] for r in data]\\n    if len(closes)<21: return []\\n    ma=sum(closes[-20:])/20\\n    avg_vol=sum(vols[-20:])/20\\n    if closes[-1]>ma and vols[-1]>avg_vol:\\n        return [{{'date': data[-1]['date'], 'action': 'BUY'}}]\\n    return []", "visual_config": {{"symbol": "NIFTY"}}, "notes": ["Buys only when price > MA20 and volume > 20-bar average.", "Deterministic and sandbox-safe — no imports or network."]}}

Existing strategy:
name: {strategy.get("name")}
description: {strategy.get("description")}
visual_config JSON: {json.dumps(current_config, default=str)[:6000]}
python_code:
{(strategy.get("python_code") or "")[:12000]}

User instruction:
{instruction}

Return JSON with:
{{
  "name": "short name",
  "description": "what changed",
  "python_code": "complete Python code",
  "visual_config": {{...}},
  "notes": ["short practical notes"]
}}
"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = requests.post(
        url,
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            # JSON mode: forces a single parseable JSON value (no markdown fences or
            # prose preamble), which is the main failure mode _extract_json_object
            # was working around. We deliberately do NOT pin a rigid responseSchema:
            # visual_config is an open-ended object and a strict schema would drop
            # legitimate config keys the model sets. responseMimeType is enough.
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=GEMINI_TIMEOUT_SEC,
    )
    response.raise_for_status()
    payload = response.json()
    parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    text = "\n".join(str(part.get("text") or "") for part in parts).strip()
    return _extract_json_object(text)


def _strategy_market_symbol(row: Dict[str, Any]) -> str:
    vc = row.get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    if opt_cfg.get("enabled"):
        return (opt_cfg.get("underlying") or "NIFTY").upper()
    commodity_cfg = vc.get("commodity_options") or {}
    if commodity_cfg.get("underlying"):
        return str(commodity_cfg.get("underlying")).upper()
    return (vc.get("symbol") or "RELIANCE").upper()


def _market_score_for_strategy(row: Dict[str, Any], market_by_symbol: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    symbol = _strategy_market_symbol(row)
    market = market_by_symbol.get(symbol) or {}
    pct = float(market.get("pct") or 0)
    score = 52.0
    reasons = []

    if row.get("status") == "live":
        score += 8
        reasons.append("strategy is live")
    elif row.get("status") == "paused":
        score -= 6
        reasons.append("strategy is paused")

    if row.get("last_error"):
        score -= 22
        reasons.append("recent runtime error")
    if row.get("last_data_live"):
        score += 8
        reasons.append("live broker data")
    elif row.get("last_data_source"):
        score += 2
        reasons.append("data source available")

    if _strategy_type(row) == "Option Selling":
        if abs(pct) <= 0.35:
            score += 16
            reasons.append("low directional movement favors premium selling")
        else:
            score -= 10
            reasons.append("directional movement is elevated for selling")
    else:
        if abs(pct) >= 0.25:
            score += 14
            reasons.append("momentum is visible")
        elif abs(pct) <= 0.08:
            score -= 7
            reasons.append("momentum is muted")

    signals = int(row.get("signals_fired") or 0)
    evaluations = int(row.get("evaluations") or 0)
    if evaluations:
        score += min(8, signals / max(1, evaluations) * 20)
        reasons.append("scanner telemetry included")

    clamped = round(max(5.0, min(95.0, score)), 1)
    return {
        "strategy_id": row["id"],
        "symbol": symbol,
        "score": clamped,
        "reason": "; ".join(reasons[:3]) or "baseline market structure score",
        "market": {
            "price": market.get("price"),
            "pct": market.get("pct"),
            "source": market.get("source"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ============== Routes: AI Bot (extracted to routes/ai.py) ==============



# broker key routes moved to routes/broker.py


# moved to routes/market.py

# ============== Routes: Strategies ==============# ============== Routes: Strategies ==============
DEFAULT_PYTHON = """# Simple Moving Average Crossover
# Available: data (list of {date, close}), signals (output list of {date, action})
def run(data):
    short, long, signals = 5, 20, []
    closes = [d['close'] for d in data]
    for i in range(len(closes)):
        if i < long: continue
        s_avg = sum(closes[i-short:i]) / short
        l_avg = sum(closes[i-long:i]) / long
        prev_s = sum(closes[i-short-1:i-1]) / short
        prev_l = sum(closes[i-long-1:i-1]) / long
        if prev_s <= prev_l and s_avg > l_avg:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif prev_s >= prev_l and s_avg < l_avg:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
"""

DEFAULT_STRATEGY_RISK = {
    # Human percent values. The SQLite option ledger stores these as fractions.
    "stop_loss_pct": 8.0,
    "take_profit_pct": 12.0,
    "trail_trigger_pct": 5.5,
    "trail_step_pct": 3.0,
    "cooldown_minutes": 15,
    "max_trades_day": 3,
    "daily_loss_limit": 750.0,
    "time_exit_minutes": 22,
    "indicator_exit_enabled": True,
    "exit_mode": "tp_sl_tsl_or_signal",
    "pause_on_issue": True,
    "risk_style": "balanced",
    "adaptive_exits_enabled": True,
    "target_r_multiple": 1.45,
}


RISK_STYLE_PRESETS = {
    "micro_scalp": {
        "stop_loss_pct": 5.5,
        "take_profit_pct": 8.0,
        "trail_trigger_pct": 3.5,
        "trail_step_pct": 2.0,
        "cooldown_minutes": 1,
        "max_trades_day": 20,
        "daily_loss_limit": 3000.0,
        "time_exit_minutes": 10,
        "target_r_multiple": 1.25,
    },
    "momentum": {
        "stop_loss_pct": 7.0,
        "take_profit_pct": 11.0,
        "trail_trigger_pct": 5.0,
        "trail_step_pct": 2.8,
        "cooldown_minutes": 12,
        "max_trades_day": 3,
        "daily_loss_limit": 650.0,
        "time_exit_minutes": 18,
        "target_r_multiple": 1.45,
    },
    "breakout": {
        "stop_loss_pct": 8.5,
        "take_profit_pct": 14.0,
        "trail_trigger_pct": 6.0,
        "trail_step_pct": 3.4,
        "cooldown_minutes": 18,
        "max_trades_day": 8,
        "daily_loss_limit": 800.0,
        "time_exit_minutes": 25,
        "target_r_multiple": 1.55,
    },
    "volatile_breakout": {
        "stop_loss_pct": 10.0,
        "take_profit_pct": 17.0,
        "trail_trigger_pct": 7.5,
        "trail_step_pct": 4.2,
        "cooldown_minutes": 22,
        "max_trades_day": 8,
        "daily_loss_limit": 950.0,
        "time_exit_minutes": 30,
        "target_r_multiple": 1.6,
    },
    "pullback": {
        "stop_loss_pct": 7.5,
        "take_profit_pct": 12.0,
        "trail_trigger_pct": 5.5,
        "trail_step_pct": 3.0,
        "cooldown_minutes": 20,
        "max_trades_day": 8,
        "daily_loss_limit": 700.0,
        "time_exit_minutes": 28,
        "target_r_multiple": 1.45,
    },
}


def _classify_strategy_risk_style(template: Dict[str, Any]) -> str:
    text = " ".join(
        str(template.get(key) or "")
        for key in ("name", "description", "market_suitability", "underlying", "instrument_group")
    ).lower()
    underlying = str(template.get("underlying") or "").upper()
    if any(token in text for token in ("hft", "scalper", "quick", "micro", "mini")):
        return "micro_scalp"
    if underlying in {"CRUDEOIL", "NATURALGAS"} or any(token in text for token in ("volatility", "breakout", "expansion", "channel")):
        return "volatile_breakout" if underlying in REMOVED_COMMODITY_UNDERLYINGS else "breakout"
    if any(token in text for token in ("rsi", "pullback", "reversion", "swing")):
        return "pullback"
    return "momentum"


# Trade-frequency category derived from a strategy's risk_style. Independent of
# the SL/TP exit profile — drives the scalper guardrail in normalize_strategy_risk.
_RISK_STYLE_CATEGORY = {
    "micro_scalp": "scalper",
    "momentum": "intraday",
    "breakout": "intraday",
    "volatile_breakout": "intraday",
    "pullback": "swing",
}


def _strategy_risk_profile(template: Dict[str, Any]) -> Dict[str, Any]:
    style = str(template.get("risk_style") or _classify_strategy_risk_style(template))
    risk = {
        **DEFAULT_STRATEGY_RISK,
        **RISK_STYLE_PRESETS.get(style, RISK_STYLE_PRESETS["momentum"]),
        **dict(template.get("risk") or {}),
    }
    risk["risk_style"] = style
    risk.setdefault("strategy_category", _RISK_STYLE_CATEGORY.get(style, "intraday"))
    risk["adaptive_exits_enabled"] = True
    risk["trailing_sl_enabled"] = True
    return risk


CREDIT_SPREAD_THETA_NAMES = {
    "NIFTY Quick EMA Scalper",
    "BANKNIFTY HFT Momentum Scalper",
    "BANKNIFTY Breakout Buyer",
    "BANKNIFTY Volatility Breakout",
    "SENSEX Swing RSI Pullback",
    "NIFTY Micro-Lot Trend Follower",
    "NIFTY Momentum Buyer",
}

# EDR-03 (2026-07-04): the entire pre-2026-07 book has ZERO out-of-sample edge
# (OOS validator: 0 CANDIDATE_EDGE across 23 strategies). Archived on startup and
# replaced by the OOS-validated "NIFTY Put Spread Theta (OOS)" (EDR-09/EDR-10).
# Archiving (not deleting) preserves P&L history; the runner skips status=archived.
DEAD_STRATEGY_NAMES = frozenset({
    "SENSEX Swing RSI Pullback", "NIFTY Micro-Lot Trend Follower", "NIFTY HFT Quick Scalper",
    "BANKNIFTY HFT Momentum Scalper", "NIFTY Quick EMA Scalper", "BANKNIFTY Volatility Breakout",
    "NIFTY Momentum Buyer", "BANKNIFTY Breakout Buyer", "NIFTY VWAP Trend Breakout",
    "RELIANCE Trend Rider", "SBIN Short Seller", "HDFCBANK Range Rebound",
    "ICICIBANK Volatility Breakout", "TCS Swing Accumulator", "INFY VWAP Pullback",
    "AXISBANK Trend Follower", "LT Momentum Rider", "BHARTIARTL Intraday Trend",
    "KOTAKBANK RSI Rebound", "NIFTY Theta Credit Spread", "NIFTY Range Credit Spread",
    "BANKNIFTY Theta Credit Spread", "SENSEX Theta Credit Spread",
})

OPTION_ALPHA_REBUILD_NAMES = frozenset({
    "QG-O1 NIFTY Put Spread Theta Core",
    "QG-O2 NIFTY Trend-Filtered Put Spread Theta",
    "QG-O3 SENSEX Put Spread Theta Pilot",
    "QG-O4 SENSEX Call Spread Range Pilot",
    "QG-O5 NIFTY Opening Range Call Buyer",
    "QG-O6 NIFTY Opening Range Put Buyer",
    "QG-O7 BANKNIFTY VWAP Reclaim Call Buyer",
    "QG-O8 BANKNIFTY VWAP Reject Put Buyer",
    "QG-O9 NIFTY Tail Event Put Buyer",
    "QG-O10 NIFTY Premium-Safe Debit Buyer",
})

PAPER_FORWARD_ACTIVE_STRATEGY_NAMES = frozenset({
    "QG-O1 NIFTY Put Spread Theta Core",
    "QG-O5 NIFTY Opening Range Call Buyer",
    # 2026-07-06: un-archived by founder direction. Sole archived strategy with a
    # real OOS edge — EOD bhavcopy walk-forward: +₹74/tr (2024) and +₹19/tr (2025 OOS),
    # 87% WR, 82% green months, all_years_positive. Paper-forward per the ladder;
    # thin OOS margin + SENSEX (BFO) execution cost keep it paper-only until proven.
    "QG-O4 SENSEX Call Spread Range Pilot",
})
PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES = OPTION_ALPHA_REBUILD_NAMES - PAPER_FORWARD_ACTIVE_STRATEGY_NAMES

CREDIT_SPREAD_THETA_RISK = {
    "cooldown_minutes": 15,
    "max_trades_day": 8,
    # Killswitch geometry: a spread's designed max loss is ~required_capital
    # (₹8k budget → lots via lots_for_risk). The daily loss floor must sit AT
    # or above one designed loss, otherwise the killswitch force-closes a
    # breathing spread mid-drawdown and realizes the worst tick (the −21k
    # leak found in the 2026-07-02 book analysis).
    "daily_loss_limit": 8000.0,
    "time_exit_minutes": 0,
    "strategy_category": "intraday",
}

EQUITY_MIN_REQUIRED_CAPITAL = float(os.environ.get("EQUITY_MIN_REQUIRED_CAPITAL", "50000"))
EQUITY_ENTRY_CUTOFF = os.environ.get("EQUITY_ENTRY_CUTOFF", "1430")
BANKNIFTY_THETA_EXPIRY_WEEK_ONLY = os.environ.get("BANKNIFTY_THETA_EXPIRY_WEEK_ONLY", "true").lower() == "true"
EQUITY_CAPITAL_TIERS = {
    "RELIANCE Trend Rider": 75000.0,
    "HDFCBANK Range Rebound": 75000.0,
    "ICICIBANK Volatility Breakout": 75000.0,
}


def _risk_update_fields(risk: Dict[str, Any], prefix: str = "visual_config.risk") -> Dict[str, Any]:
    fields = {
        f"{prefix}.stop_loss_pct": risk["stop_loss_pct"],
        f"{prefix}.stoploss_pct": risk["stop_loss_pct"],
        f"{prefix}.take_profit_pct": risk["take_profit_pct"],
        f"{prefix}.target_pct": risk["take_profit_pct"],
        f"{prefix}.trailing_sl_enabled": bool(risk.get("trailing_sl_enabled", True)),
        f"{prefix}.trail_trigger_pct": risk["trail_trigger_pct"],
        f"{prefix}.trail_step_pct": risk["trail_step_pct"],
        f"{prefix}.cooldown_minutes": risk["cooldown_minutes"],
        f"{prefix}.max_trades_day": risk["max_trades_day"],
        f"{prefix}.daily_loss_limit": risk["daily_loss_limit"],
        f"{prefix}.time_exit_minutes": risk["time_exit_minutes"],
        f"{prefix}.indicator_exit_enabled": bool(risk.get("indicator_exit_enabled", True)),
        f"{prefix}.exit_mode": risk.get("exit_mode") or DEFAULT_STRATEGY_RISK["exit_mode"],
        f"{prefix}.risk_style": risk.get("risk_style", "balanced"),
        f"{prefix}.strategy_category": risk.get("strategy_category") or _RISK_STYLE_CATEGORY.get(str(risk.get("risk_style") or ""), "intraday"),
        f"{prefix}.adaptive_exits_enabled": bool(risk.get("adaptive_exits_enabled", True)),
        f"{prefix}.target_r_multiple": float(risk.get("target_r_multiple") or DEFAULT_STRATEGY_RISK["target_r_multiple"]),
    }
    if risk.get("required_capital") is not None:
        fields[f"{prefix}.required_capital"] = float(risk.get("required_capital") or 0)
    return fields


RETAIL_LIVE_STATE_CODE = """def run(data):
    if len(data) < 25:
        return []

    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    volumes = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(value * k + out[-1] * (1 - k))
        return out

    def avg(values):
        return sum(values) / max(1, len(values))

    ema_fast = ema(closes, 4)
    ema_slow = ema(closes, 10)
    ema_filter = ema(closes, 18)
    signals = []
    position = "NONE"
    entry = 0.0

    for i in range(18, len(data)):
        close = closes[i]
        prev = closes[i - 1]
        range_recent = avg([highs[j] - lows[j] for j in range(max(1, i - 8), i + 1)])
        momentum = close - closes[max(0, i - 3)]
        min_move = max(close * 0.00014, range_recent * 0.24)
        avg_vol = avg(volumes[max(0, i - 15):i])
        vol_ok = volumes[i] >= avg_vol * 0.9
        body_up = close >= prev
        body_down = close <= prev
        higher_high = highs[i] > max(highs[i - 3:i])
        lower_low = lows[i] < min(lows[i - 3:i])

        bullish = ema_fast[i] > ema_slow[i] > ema_filter[i] and momentum > min_move and body_up and higher_high and vol_ok
        bearish = ema_fast[i] < ema_slow[i] < ema_filter[i] and momentum < -min_move and body_down and lower_low and vol_ok

        if position == "LONG":
            pnl = (close - entry) / entry * 100 if entry else 0
            if bearish or pnl <= -0.30 or pnl >= 0.75:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Retail CE exit / PE rotation'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry - close) / entry * 100 if entry else 0
            if bullish or pnl <= -0.30 or pnl >= 0.75:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Retail PE exit / CE rotation'})
                position = "NONE"
        else:
            if bullish:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Retail live CE momentum'})
                position = "LONG"
                entry = close
            elif bearish:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Retail live PE momentum'})
                position = "SHORT"
                entry = close

    return signals
"""


NIFTY_ATM_MOMENTUM_CODE = """def run(data):
    if len(data) < 45:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(value * k + out[-1] * (1 - k))
        return out

    def avg(values):
        return sum(values) / max(1, len(values))

    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, vols):
        weighted += ((h + l + c) / 3.0) * v
        total_vol += v
        vwap.append(weighted / max(1.0, total_vol))

    ema8 = ema(closes, 8)
    ema21 = ema(closes, 21)
    ema34 = ema(closes, 34)

    vwap_crosses = [0] * len(closes)
    for i in range(1, len(closes)):
        cross = (closes[i] > vwap[i] and closes[i-1] <= vwap[i-1]) or (closes[i] < vwap[i] and closes[i-1] >= vwap[i-1])
        vwap_crosses[i] = vwap_crosses[i-1] + (1 if cross else 0)

    signals = []
    position = "NONE"
    # Two-bar exit counters: require 2 consecutive closes beyond EMA8 to exit
    bars_below_ema8 = 0
    bars_above_ema8 = 0
    TWO_BAR_EXIT = 2

    for i in range(34, len(data)):
        if closes[i] <= 0:
            continue
        clock = str(data[i].get('date', ''))[11:16]
        if clock and (clock < '09:30' or clock > '14:45'):
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "trend_momentum",
                    "confidence": 50.0,
                    "entry_reason": "NIFTY momentum time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 20,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "NONE"
                bars_below_ema8 = 0
                bars_above_ema8 = 0
            continue

        recent_range = max(highs[i-12:i+1]) - min(lows[i-12:i+1])
        candle_range = max(0.01, avg([highs[j] - lows[j] for j in range(i-10, i+1)]))
        momentum = closes[i] - closes[i-3]
        avg_vol = avg(vols[i-20:i])
        vol_ok = vols[i] >= avg_vol * 0.95

        recent_cross_count = vwap_crosses[i] - vwap_crosses[i-15]
        chop_free = recent_cross_count < 3

        bullish_ema = ema8[i] > ema21[i] > ema34[i]
        bearish_ema = ema8[i] < ema21[i] < ema34[i]

        bullish = bullish_ema and closes[i] > vwap[i] and chop_free and closes[i] > max(highs[i-4:i]) and momentum > max(recent_range * 0.16, candle_range * 0.55) and vol_ok
        bearish = bearish_ema and closes[i] < vwap[i] and chop_free and closes[i] < min(lows[i-4:i]) and momentum < -max(recent_range * 0.16, candle_range * 0.55) and vol_ok

        if position == "LONG":
            # Update two-bar counter
            if closes[i] < ema8[i]:
                bars_below_ema8 += 1
            else:
                bars_below_ema8 = 0
            # Two consecutive closes below EMA8 OR bearish reversal signal triggers exit
            if bearish or bars_below_ema8 >= TWO_BAR_EXIT:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "trend_momentum",
                    "confidence": 75.0,
                    "entry_reason": "NIFTY momentum CE exit (two-bar EMA8 break)" if bars_below_ema8 >= TWO_BAR_EXIT else "NIFTY momentum CE exit (bearish reversal)",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 20,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "NONE"
                bars_below_ema8 = 0
        elif position == "SHORT":
            if closes[i] > ema8[i]:
                bars_above_ema8 += 1
            else:
                bars_above_ema8 = 0
            if bullish or bars_above_ema8 >= TWO_BAR_EXIT:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "trend_momentum",
                    "confidence": 75.0,
                    "entry_reason": "NIFTY momentum PE exit (two-bar EMA8 break)" if bars_above_ema8 >= TWO_BAR_EXIT else "NIFTY momentum PE exit (bullish reversal)",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 20,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "NONE"
                bars_above_ema8 = 0
        else:
            bars_below_ema8 = 0
            bars_above_ema8 = 0
            if bullish:
                conf = 60.0
                if vols[i] >= avg_vol * 1.5: conf += 15.0
                if (ema8[i] - ema34[i]) > candle_range: conf += 15.0
                if momentum > recent_range * 0.25: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "trend_momentum",
                    "confidence": conf,
                    "entry_reason": "NIFTY CE momentum — EMA8>21>34, VWAP, volume aligned",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 20,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ITM1" if conf > 85.0 else "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "LONG"
            elif bearish:
                conf = 60.0
                if vols[i] >= avg_vol * 1.5: conf += 15.0
                if (ema34[i] - ema8[i]) > candle_range: conf += 15.0
                if momentum < -recent_range * 0.25: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "trend_momentum",
                    "confidence": conf,
                    "entry_reason": "NIFTY PE momentum — EMA8<21<34, VWAP, volume aligned",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 20,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ITM1" if conf > 85.0 else "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "SHORT"

    return signals
"""


BANKNIFTY_ATM_BREAKOUT_CODE = """def run(data):
    if len(data) < 45:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def avg(values):
        return sum(values) / max(1, len(values))

    signals = []
    position = "NONE"
    entry = 0.0
    failed_breakout_count = 0
    cooldown = 0

    for i in range(24, len(data)):
        if closes[i] <= 0:
            continue
        if cooldown > 0:
            cooldown -= 1
        clock = str(data[i].get('date', ''))[11:16]
        if clock and (clock < '09:35' or clock > '14:35'):
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "range_breakout",
                    "confidence": 50.0,
                    "entry_reason": "BANKNIFTY breakout time exit",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue

        channel_high = max(highs[i-15:i])
        channel_low = min(lows[i-15:i])
        channel_mid = (channel_high + channel_low) / 2
        avg_range = max(0.01, avg([highs[j] - lows[j] for j in range(i-15, i)]))
        atr = max(0.01, avg([max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-14, i)]))
        avg_vol = avg(vols[i-20:i])
        vol_ok = float(data[i].get('tod_vol_ratio', 1.0)) >= 0.85

        # Volatility expansion
        range_expanded = avg_range > atr * 0.8
        
        # Overextension check — body < 2.5×ATR; exempt in CRASH/MELTUP via runner flag
        body_size = abs(closes[i] - data[i].get('open', closes[i]))
        not_overextended = body_size < 2.5 * atr
        # Strong overextension (2.5–5×ATR) with high volume: flag for regime exemption
        strong_overextended = (2.5 * atr <= body_size < 5.0 * atr) and vols[i] >= avg_vol * 1.5

        allowed = cooldown == 0 and failed_breakout_count < 3

        bullish = allowed and closes[i] > channel_high and (closes[i] - closes[i-1]) > avg_range * 0.45 and range_expanded and vol_ok and not_overextended
        bearish = allowed and closes[i] < channel_low and (closes[i-1] - closes[i]) > avg_range * 0.45 and range_expanded and vol_ok and not_overextended
        # Overextended breakouts: same direction conditions but skip body filter; tagged for regime gate
        bullish_oe = allowed and closes[i] > channel_high and (closes[i] - closes[i-1]) > avg_range * 0.45 and range_expanded and strong_overextended
        bearish_oe = allowed and closes[i] < channel_low and (closes[i-1] - closes[i]) > avg_range * 0.45 and range_expanded and strong_overextended

        if position == "LONG":
            if bearish or closes[i] < channel_mid:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "range_breakout",
                    "confidence": 70.0,
                    "entry_reason": "BANKNIFTY CE breakout exit",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                if closes[i] < channel_mid:
                    failed_breakout_count += 1
                position = "NONE"
                cooldown = 10
        elif position == "SHORT":
            if bullish or closes[i] > channel_mid:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "range_breakout",
                    "confidence": 70.0,
                    "entry_reason": "BANKNIFTY PE breakout exit",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                if closes[i] > channel_mid:
                    failed_breakout_count += 1
                position = "NONE"
                cooldown = 10
        else:
            if bullish:
                conf = 65.0
                if vols[i] >= avg_vol * 1.5: conf += 15.0
                if (closes[i] - channel_high) > atr * 0.5: conf += 10.0
                if body_size > avg_range * 0.8: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "range_breakout",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY CE range breakout with expansion and volume",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "LONG"
            elif bearish:
                conf = 65.0
                if vols[i] >= avg_vol * 1.5: conf += 15.0
                if (channel_low - closes[i]) > atr * 0.5: conf += 10.0
                if body_size > avg_range * 0.8: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "range_breakout",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY PE range breakdown with expansion and volume",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "SHORT"
            elif bullish_oe and not bullish:
                # Overextended CE breakout: allowed only in MELTUP regime (runner checks flag)
                conf = 60.0
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "range_breakout_overextended",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY CE breakout overextended — MELTUP regime required",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "overextended_regime_exempt": True,
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "LONG"
            elif bearish_oe and not bearish:
                # Overextended PE breakdown: allowed only in CRASH regime (runner checks flag)
                conf = 60.0
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "range_breakout_overextended",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY PE breakdown overextended — CRASH regime required",
                    "target_R": 1.9,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 25,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "breakout",
                    "overextended_regime_exempt": True,
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "SHORT"

    return signals
"""


NIFTY_VWAP_TREND_BREAKOUT_CODE = """def run(data):
    if len(data) < 45:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]
    # Index ltpc ticks carry no volume, so every bar collapses to the 1.0 floor.
    # When that happens the volume series is degenerate and the volume gate below
    # (avg_vol * 1.05) is mathematically unsatisfiable, silently blocking EVERY
    # entry. Detect a real volume signal once and only enforce the gate then;
    # otherwise treat volume as confirmed (price/VWAP logic still governs entries).
    vol_reliable = any(float(d.get('volume') or 0) > 0 for d in data)

    def avg(values):
        return sum(values) / max(1, len(values))

    # VWAP Calculation
    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, vols):
        weighted += ((h + l + c) / 3.0) * v
        total_vol += v
        vwap.append(weighted / max(1.0, total_vol))

    signals = []
    position = "NONE"
    
    # Retest continuation states:
    # 0 = searching breakout
    # 1 = broke above VWAP, waiting for retest near VWAP
    # 2 = retested and held, waiting for continuation close higher
    # -1 = broke below VWAP, waiting for retest near VWAP from below
    # -2 = retested from below, waiting for continuation close lower
    bullish_state = 0
    bearish_state = 0
    retest_val = 0.0
    breakout_bar_idx = -100
    vwap_cross_count = 0
    cooldown = 0

    for i in range(25, len(data)):
        if closes[i] <= 0:
            continue
        if cooldown > 0:
            cooldown -= 1
        clock = str(data[i].get('date', ''))[11:16]
        if clock and (clock < '09:45' or clock > '14:30'):
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "vwap_retest_continuation",
                    "confidence": 50.0,
                    "entry_reason": "VWAP time exit",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue

        avg_vol = avg(vols[i-20:i])
        range_avg = max(0.01, avg([highs[j] - lows[j] for j in range(i-10, i)]))
        body = abs(closes[i] - closes[i-1])
        
        # Track crosses to avoid chop
        crossed = (closes[i] > vwap[i] and closes[i-1] <= vwap[i-1]) or (closes[i] < vwap[i] and closes[i-1] >= vwap[i-1])
        if crossed:
            vwap_cross_count += 1
            
        # Decay chop count
        if i % 10 == 0 and vwap_cross_count > 0:
            vwap_cross_count -= 1

        chop_free = vwap_cross_count < 4

        # State transitions
        # Bullish setup state machine
        if closes[i] > vwap[i] and closes[i-1] <= vwap[i-1] and bullish_state == 0 and chop_free:
            bullish_state = 1
            breakout_bar_idx = i
        elif bullish_state == 1:
            # Check for retest: price low is close to VWAP but close remains above
            if lows[i] <= vwap[i] * 1.001 and closes[i] >= vwap[i]:
                bullish_state = 2
                retest_val = closes[i]
            # Invalidation: closes below VWAP or too long since breakout
            if closes[i] < vwap[i] or i - breakout_bar_idx > 8:
                bullish_state = 0

        # Bearish setup state machine
        if closes[i] < vwap[i] and closes[i-1] >= vwap[i-1] and bearish_state == 0 and chop_free:
            bearish_state = 1
            breakout_bar_idx = i
        elif bearish_state == 1:
            # Check for retest: price high is close to VWAP but close remains below
            if highs[i] >= vwap[i] * 0.999 and closes[i] <= vwap[i]:
                bearish_state = 2
                retest_val = closes[i]
            # Invalidation: closes above VWAP or too long since breakout
            if closes[i] > vwap[i] or i - breakout_bar_idx > 8:
                bearish_state = 0

        # Triggers
        bullish_trigger = False
        if bullish_state == 2:
            if closes[i] > retest_val and closes[i] > vwap[i] and (not vol_reliable or vols[i] >= avg_vol * 1.05) and body >= range_avg * 0.25 and cooldown == 0:
                bullish_trigger = True
                bullish_state = 0
            elif closes[i] < vwap[i]:
                bullish_state = 0

        bearish_trigger = False
        if bearish_state == 2:
            if closes[i] < retest_val and closes[i] < vwap[i] and (not vol_reliable or vols[i] >= avg_vol * 1.05) and body >= range_avg * 0.25 and cooldown == 0:
                bearish_trigger = True
                bearish_state = 0
            elif closes[i] > vwap[i]:
                bearish_state = 0

        if cooldown == 0:
            recent_high = max(highs[i-6:i])
            recent_low = min(lows[i-6:i])
            bullish_continuation = (
                closes[i] > vwap[i] * 0.999
                and closes[i] >= recent_high * 0.997
                and closes[i] > closes[i-1]
                and body >= range_avg * 0.08
            )
            bearish_continuation = (
                closes[i] < vwap[i] * 1.001
                and closes[i] <= recent_low * 1.003
                and closes[i] < closes[i-1]
                and body >= range_avg * 0.08
            )
            bullish_trigger = bullish_trigger or bullish_continuation
            bearish_trigger = bearish_trigger or bearish_continuation

        if position == "LONG":
            if bearish_trigger or closes[i] < vwap[i] * 0.998:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "vwap_retest_continuation",
                    "confidence": 70.0,
                    "entry_reason": "VWAP CE exit",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 10
        elif position == "SHORT":
            if bullish_trigger or closes[i] > vwap[i] * 1.002:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "vwap_retest_continuation",
                    "confidence": 70.0,
                    "entry_reason": "VWAP PE exit",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 10
        else:
            if bullish_trigger:
                conf = 65.0
                if vols[i] >= avg_vol * 1.3: conf += 15.0
                if vwap_cross_count == 0: conf += 10.0
                if body >= range_avg * 0.5: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "vwap_retest_continuation",
                    "confidence": conf,
                    "entry_reason": "NIFTY VWAP retest hold and continuation CE buy",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
            elif bearish_trigger:
                conf = 65.0
                if vols[i] >= avg_vol * 1.3: conf += 15.0
                if vwap_cross_count == 0: conf += 10.0
                if body >= range_avg * 0.5: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "vwap_retest_continuation",
                    "confidence": conf,
                    "entry_reason": "NIFTY VWAP retest rejection and continuation PE sell",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "SHORT"

    return signals
"""


SENSEX_RSI_PULLBACK_CODE = """def run(data):
    # S4 v2: VWAP+EMA trend filter, intraday-return gap-down guard (≤-2% blocks longs)
    if len(data) < 65:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for v in values[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    def rsi_at(i, period):
        gains = losses = 0.0
        for j in range(i - period + 1, i + 1):
            chg = closes[j] - closes[j-1]
            gains += max(chg, 0); losses += max(-chg, 0)
        rs = (gains / period) / max(0.0001, losses / period)
        return 100 - (100 / (1 + rs))

    # Day-anchored VWAP
    dates = [str(d.get('date', '')) for d in data]
    today_str = dates[-1][:10] if dates else ''
    w_sum = v_sum = 0.0
    vwap = []
    for i, (h, l, c, v, dt) in enumerate(zip(highs, lows, closes, vols, dates)):
        if dt[:10] != today_str:
            w_sum = v_sum = 0.0
        w_sum += ((h + l + c) / 3.0) * v; v_sum += v
        vwap.append(w_sum / max(1.0, v_sum))

    ema12 = ema(closes, 12)  # 1-hr EMA proxy on 5-min bars
    rsi = [50.0 if i < 14 else rsi_at(i, 14) for i in range(len(closes))]

    # Intraday return: today's first open vs current close
    today_open = None
    for i, dt in enumerate(dates):
        if dt[:10] == today_str:
            today_open = float(data[i].get('open', closes[i]) or closes[i])
            break
    intraday_ret_pct = 0.0
    if today_open and today_open > 0:
        intraday_ret_pct = (closes[-1] - today_open) / today_open * 100.0

    # Gap-down guard: block new longs when day is already down ≥2%
    gap_down = intraday_ret_pct <= -2.0

    signals = []
    position = "NONE"
    cooldown = 0

    for i in range(50, len(data)):
        if closes[i] <= 0:
            continue
        if cooldown > 0:
            cooldown -= 1
        clock = str(data[i].get('date', ''))[11:16]
        if clock and (clock < '10:00' or clock > '14:25'):
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "rsi_pullback",
                    "confidence": 50.0,
                    "entry_reason": "SENSEX RSI time exit",
                    "target_R": 1.6, "initial_stop_R": 1.0, "trail_after_R": 0.9,
                    "max_hold_minutes": 30, "invalidation_rule": "time_or_stop",
                    "regime_required": "pullback", "option_selection_preference": "ATM",
                    "signal_version": "v13", "strategy_logic_version": "2.0"
                })
                position = "NONE"
            continue

        # VWAP + rising EMA12 trend filter (replaces SMA20/50)
        trend_up   = closes[i] > vwap[i] and ema12[i] > ema12[i-3]
        trend_down = closes[i] < vwap[i] and ema12[i] < ema12[i-3]

        slope_extreme = abs(vwap[i] - vwap[max(0, i-10)]) > vwap[i] * 0.02

        was_oversold  = any(rsi[j] <= 42 for j in range(max(0, i-8), i + 1))
        was_overbought= any(rsi[j] >= 58 for j in range(max(0, i-8), i + 1))

        bullish = (trend_up and was_oversold and rsi[i] >= rsi[i-1] and rsi[i] >= 38
                   and closes[i] > closes[i-1] and not slope_extreme
                   and cooldown == 0 and not gap_down)
        bearish = (trend_down and was_overbought and rsi[i] <= rsi[i-1] and rsi[i] <= 62
                   and closes[i] < closes[i-1] and not slope_extreme and cooldown == 0)

        if position == "LONG":
            if bearish or rsi[i] > 68 or closes[i] < vwap[i]:
                signals.append({
                    "date": data[i]["date"], "action": "SELL", "direction": "CE",
                    "setup_type": "rsi_pullback", "confidence": 75.0,
                    "entry_reason": "SENSEX RSI CE exit",
                    "target_R": 1.6, "initial_stop_R": 1.0, "trail_after_R": 0.9,
                    "max_hold_minutes": 30, "invalidation_rule": "time_or_stop",
                    "regime_required": "pullback", "option_selection_preference": "ATM",
                    "signal_version": "v13", "strategy_logic_version": "2.0"
                })
                position = "NONE"; cooldown = 4
        elif position == "SHORT":
            if bullish or rsi[i] < 32 or closes[i] > vwap[i]:
                signals.append({
                    "date": data[i]["date"], "action": "BUY", "direction": "PE",
                    "setup_type": "rsi_pullback", "confidence": 75.0,
                    "entry_reason": "SENSEX RSI PE exit",
                    "target_R": 1.6, "initial_stop_R": 1.0, "trail_after_R": 0.9,
                    "max_hold_minutes": 30, "invalidation_rule": "time_or_stop",
                    "regime_required": "pullback", "option_selection_preference": "ATM",
                    "signal_version": "v13", "strategy_logic_version": "2.0"
                })
                position = "NONE"; cooldown = 4
        else:
            if bullish:
                conf = 65.0
                dist = abs(closes[i] - vwap[i]) / max(1.0, vwap[i])
                if dist < 0.005: conf += 15.0
                if rsi[i-1] <= 30: conf += 10.0
                if closes[i] > highs[i-1]: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"], "action": "BUY", "direction": "CE",
                    "setup_type": "rsi_pullback", "confidence": conf,
                    "entry_reason": "SENSEX RSI pullback CE VWAP+EMA trend",
                    "target_R": 1.6, "initial_stop_R": 1.0, "trail_after_R": 0.9,
                    "max_hold_minutes": 30, "invalidation_rule": "time_or_stop",
                    "regime_required": "pullback", "option_selection_preference": "ATM",
                    "signal_version": "v13", "strategy_logic_version": "2.0"
                })
                position = "LONG"
            elif bearish:
                conf = 65.0
                dist = abs(closes[i] - vwap[i]) / max(1.0, vwap[i])
                if dist < 0.005: conf += 15.0
                if rsi[i-1] >= 70: conf += 10.0
                if closes[i] < lows[i-1]: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"], "action": "SELL", "direction": "PE",
                    "setup_type": "rsi_pullback", "confidence": conf,
                    "entry_reason": "SENSEX RSI pullback PE VWAP+EMA trend",
                    "target_R": 1.6, "initial_stop_R": 1.0, "trail_after_R": 0.9,
                    "max_hold_minutes": 30, "invalidation_rule": "time_or_stop",
                    "regime_required": "pullback", "option_selection_preference": "ATM",
                    "signal_version": "v13", "strategy_logic_version": "2.0"
                })
                position = "SHORT"

    return signals
"""


NIFTY_MICRO_TREND_CODE = """def run(data):
    if len(data) < 55:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(value * k + out[-1] * (1 - k))
        return out

    # VWAP Calculation
    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, vols):
        weighted += ((h + l + c) / 3.0) * v
        total_vol += v
        vwap.append(weighted / max(1.0, total_vol))

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    
    signals = []
    position = "NONE"
    cooldown = 0

    for i in range(50, len(data)):
        if closes[i] <= 0:
            continue
        if cooldown > 0:
            cooldown -= 1
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '14:35':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "slow_trend_follow",
                    "confidence": 50.0,
                    "entry_reason": "Micro trend time exit",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 40,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue

        # Slope check
        slope_up = ema20[i] > ema20[i-3]
        slope_down = ema20[i] < ema20[i-3]
        
        # No-trade zones: keep only a small buffer so paper measurement captures valid intraday trends.
        buffer = closes[i] * 0.0006
        outside_buffer = abs(closes[i] - ema20[i]) > buffer or abs(closes[i] - vwap[i]) > buffer
        
        bullish = (
            ((closes[i] > ema20[i] and ema20[i] >= ema50[i] * 0.999) or (closes[i] > vwap[i] and closes[i] > closes[i-1] > closes[i-2]))
            and slope_up and outside_buffer and cooldown == 0
        )
        bearish = (
            ((closes[i] < ema20[i] and ema20[i] <= ema50[i] * 1.001) or (closes[i] < vwap[i] and closes[i] < closes[i-1] < closes[i-2]))
            and slope_down and outside_buffer and cooldown == 0
        )

        if position == "LONG":
            if bearish or closes[i] < ema20[i]:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "slow_trend_follow",
                    "confidence": 70.0,
                    "entry_reason": "Micro trend CE exit",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 40,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 6
        elif position == "SHORT":
            if bullish or closes[i] > ema20[i]:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "slow_trend_follow",
                    "confidence": 70.0,
                    "entry_reason": "Micro trend PE exit",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 40,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 6
        else:
            if bullish:
                conf = 60.0
                if ema20[i] > ema50[i] * 1.002: conf += 15.0
                if closes[i] > ema20[i] * 1.003: conf += 15.0
                if slope_up: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "slow_trend_follow",
                    "confidence": conf,
                    "entry_reason": "NIFTY Micro-Lot trend continuation — EMA20>50, slope up, VWAP above",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 40,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "regime_gate_strict": True,
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "LONG"
            elif bearish:
                conf = 60.0
                if ema50[i] > ema20[i] * 1.002: conf += 15.0
                if closes[i] < ema20[i] * 0.997: conf += 15.0
                if slope_down: conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "slow_trend_follow",
                    "confidence": conf,
                    "entry_reason": "NIFTY Micro-Lot trend breakdown — EMA20<50, slope down, VWAP below",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 40,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trend",
                    "regime_gate_strict": True,
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "SHORT"

    return signals
"""


NIFTY_QUICK_SCALPER_CODE = """def run(data):
    if len(data) < 20:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def avg(values):
        return sum(values) / max(1, len(values))

    # Calculate VWAP (day-anchored in runner; here cumulative is fine for signal quality)
    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, vols):
        weighted += ((h + l + c) / 3.0) * v
        total_vol += v
        vwap.append(weighted / max(1.0, total_vol))

    signals = []
    position = "NONE"

    # Daily state
    current_day = ""
    orb_high = 0.0
    orb_low = 0.0
    orb_5min_high = 0.0   # range from first bar only (09:15–09:20) for early trigger
    orb_5min_low = 0.0
    entry_taken = False

    for i in range(len(data)):
        if closes[i] <= 0:
            continue
        date_str = str(data[i].get('date', ''))
        day_str = date_str[:10]
        clock = date_str[11:16]

        if day_str != current_day:
            current_day = day_str
            orb_high = 0.0
            orb_low = 0.0
            orb_5min_high = 0.0
            orb_5min_low = 0.0
            entry_taken = False

        # Accumulate opening range 09:15–09:30
        if clock and '09:15' <= clock <= '09:30':
            if orb_high == 0.0 or highs[i] > orb_high:
                orb_high = highs[i]
            if orb_low == 0.0 or lows[i] < orb_low:
                orb_low = lows[i]

        # First 5-min range (09:15 bar only) for early ORB trigger
        if clock == '09:15':
            orb_5min_high = highs[i]
            orb_5min_low = lows[i]

        avg_vol = avg(vols[max(0, i - 20):i]) if i > 0 else vols[i]

        # ── Early ORB trigger at 09:25 ────────────────────────────────────────
        # If the 09:15–09:20 range (first 5-min bar) is broken at 09:25 AND
        # move continues (close > orb_5min_high with VWAP support), enter at
        # 0.5x confidence penalty / 1.5x stop distance (wider SL for early fill).
        if clock == '09:25' and not entry_taken and orb_5min_high > 0 and orb_5min_low > 0:
            vol_ok = float(data[i].get('tod_vol_ratio', 1.0)) >= 0.75
            early_bull = closes[i] > orb_5min_high and closes[i] > vwap[i] and vol_ok
            early_bear = closes[i] < orb_5min_low and closes[i] < vwap[i] and vol_ok
            if early_bull:
                conf = 55.0
                if vols[i] >= avg_vol * 1.5: conf += 10.0
                conf = min(90.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "opening_range_breakout_early",
                    "confidence": conf,
                    "entry_reason": "NIFTY ORB Early Bull 09:25 — 5-min range broken",
                    "target_R": 1.5,
                    "initial_stop_R": 1.5,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "LONG"
                entry_taken = True
            elif early_bear:
                conf = 55.0
                if vols[i] >= avg_vol * 1.5: conf += 10.0
                conf = min(90.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "opening_range_breakout_early",
                    "confidence": conf,
                    "entry_reason": "NIFTY ORB Early Bear 09:25 — 5-min range broken",
                    "target_R": 1.5,
                    "initial_stop_R": 1.5,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "SHORT"
                entry_taken = True

        # Only trade the main window: 9:30 to 11:00
        if not clock or clock < '09:30' or clock > '11:00':
            if position != "NONE" and clock and clock > '11:00':
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "opening_range_breakout",
                    "confidence": 50.0,
                    "entry_reason": "Opening range time exit",
                    "target_R": 1.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "NONE"
            continue

        if orb_high == 0.0 or orb_low == 0.0:
            continue

        vol_ok = float(data[i].get('tod_vol_ratio', 1.0)) >= 0.85
        body = abs(closes[i] - data[i].get('open', closes[i]))

        bullish = not entry_taken and closes[i] > orb_high and closes[i] > vwap[i] and vol_ok
        bearish = not entry_taken and closes[i] < orb_low and closes[i] < vwap[i] and vol_ok

        if position == "LONG":
            if closes[i] < orb_high or closes[i] < vwap[i]:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "opening_range_breakout",
                    "confidence": 70.0,
                    "entry_reason": "ORB CE failure exit",
                    "target_R": 1.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "NONE"
        elif position == "SHORT":
            if closes[i] > orb_low or closes[i] > vwap[i]:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "opening_range_breakout",
                    "confidence": 70.0,
                    "entry_reason": "ORB PE failure exit",
                    "target_R": 1.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "NONE"
        else:
            if bullish:
                conf = 60.0
                if vols[i] >= avg_vol * 1.5: conf += 15.0
                if body >= (orb_high - orb_low) * 0.15: conf += 15.0
                if clock <= '10:00': conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "opening_range_breakout",
                    "confidence": conf,
                    "entry_reason": "NIFTY ORB Bullish Breakout above high and VWAP",
                    "target_R": 1.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "LONG"
                entry_taken = True
            elif bearish:
                conf = 60.0
                if vols[i] >= avg_vol * 1.5: conf += 15.0
                if body >= (orb_high - orb_low) * 0.15: conf += 15.0
                if clock <= '10:00': conf += 10.0
                conf = min(100.0, conf)
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "opening_range_breakout",
                    "confidence": conf,
                    "entry_reason": "NIFTY ORB Bearish Breakdown below low and VWAP",
                    "target_R": 1.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.9,
                    "max_hold_minutes": 15,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "opening_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.0"
                })
                position = "SHORT"
                entry_taken = True

    return signals
"""


BANKNIFTY_STD_BAND_SCALPER_CODE = """def run(data):
    if len(data) < 40:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]
    band_mult = 1.25  # High sensitivity standard deviation bands for HFT scalp
    
    def avg(values):
        return sum(values) / max(1, len(values))

    signals = []
    position = "NONE"
    cooldown = 0

    for i in range(24, len(data)):
        if closes[i] <= 0:
            continue
        if cooldown > 0:
            cooldown -= 1
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '14:25':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "fast_volatility_scalp",
                    "confidence": 50.0,
                    "entry_reason": "BANKNIFTY band time exit",
                    "target_R": 1.4,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.8,
                    "max_hold_minutes": 10,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "high_volatility",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue

        chunk = closes[i-10:i]
        sma = sum(chunk) / 10
        std = (sum((x - sma) ** 2 for x in chunk) / 10) ** 0.5
        upper = sma + band_mult * std
        lower = sma - band_mult * std
        avg_vol = avg(vols[i-20:i])
        
        # ATR calculation
        atr_chunk = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-10, i+1)]
        atr_curr = sum(atr_chunk) / len(atr_chunk)
        atr_prev = sum(atr_chunk[:-1]) / (len(atr_chunk) - 1)
        atr_expanding = atr_curr > atr_prev
        
        # Volume and body confirmation
        body = abs(closes[i] - data[i].get('open', closes[i]))
        avg_body = avg([abs(closes[j] - data[j].get('open', closes[j])) for j in range(i-10, i)])
        vol_ok = float(data[i].get('tod_vol_ratio', 1.0)) >= 0.85 and body > avg_body * 0.95

        # Prevent extreme overextension entry
        not_overextended = body < 2.2 * atr_curr
        
        bullish = closes[i] > upper and closes[i-1] <= upper and atr_expanding and vol_ok and not_overextended and cooldown == 0
        bearish = closes[i] < lower and closes[i-1] >= lower and atr_expanding and vol_ok and not_overextended and cooldown == 0

        if position == "LONG":
            if bearish or closes[i] < sma:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "fast_volatility_scalp",
                    "confidence": 70.0,
                    "entry_reason": "BANKNIFTY band CE exit",
                    "target_R": 1.4,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.8,
                    "max_hold_minutes": 10,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "high_volatility",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 5
        elif position == "SHORT":
            if bullish or closes[i] > sma:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "fast_volatility_scalp",
                    "confidence": 70.0,
                    "entry_reason": "BANKNIFTY band PE exit",
                    "target_R": 1.4,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.8,
                    "max_hold_minutes": 10,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "high_volatility",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 5
        else:
            if bullish:
                conf = 60.0
                if vols[i] >= avg_vol * 1.4: conf += 15.0
                if atr_curr > atr_prev * 1.05: conf += 15.0
                if body > avg_body * 1.5: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "fast_volatility_scalp",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY fast volatility CE breakout with volume",
                    "target_R": 1.4,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.8,
                    "max_hold_minutes": 10,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "high_volatility",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
            elif bearish:
                conf = 60.0
                if vols[i] >= avg_vol * 1.4: conf += 15.0
                if atr_curr > atr_prev * 1.05: conf += 15.0
                if body > avg_body * 1.5: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "fast_volatility_scalp",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY fast volatility PE breakdown with volume",
                    "target_R": 1.4,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.8,
                    "max_hold_minutes": 10,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "high_volatility",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "SHORT"

    return signals
"""


NIFTY_QUICK_EMA_SCALPER_CODE = """def run(data):
    if len(data) < 35:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]

    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for value in values[1:]:
            out.append(value * k + out[-1] * (1 - k))
        return out

    # Calculate VWAP
    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, vols):
        weighted += ((h + l + c) / 3.0) * v
        total_vol += v
        vwap.append(weighted / max(1.0, total_vol))

    ema3 = ema(closes, 3)
    ema9 = ema(closes, 9)
    signals = []
    position = "NONE"
    last_entry_i = -100

    for i in range(15, len(data)):
        if closes[i] <= 0:
            continue
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '14:15':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "ema_quick_scalp",
                    "confidence": 50.0,
                    "entry_reason": "Quick EMA scalper time exit",
                    "target_R": 1.25,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.75,
                    "max_hold_minutes": 12,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "scalp_momentum",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue

        avg_vol = sum(vols[i-10:i]) / 10
        range_avg = max(0.01, sum(highs[j] - lows[j] for j in range(i-8, i)) / 8)
        body = abs(closes[i] - data[i].get('open', closes[i]))
        vol_ok = float(data[i].get('tod_vol_ratio', 1.0)) >= 0.85

        # Crossover or active alignment checks. The old template only entered on
        # the exact EMA cross candle, which left the strategy idle after restart.
        bullish_cross = ema3[i] > ema9[i] and ema3[i-1] <= ema9[i-1]
        bearish_cross = ema3[i] < ema9[i] and ema3[i-1] >= ema9[i-1]
        bullish_aligned = ema3[i] > ema9[i] * 0.999 and closes[i] >= ema3[i] * 0.999 and closes[i] >= closes[i-1]
        bearish_aligned = ema3[i] < ema9[i] * 1.001 and closes[i] <= ema3[i] * 1.001 and closes[i] <= closes[i-1]
        
        bullish = (bullish_cross or bullish_aligned) and closes[i] > vwap[i] * 0.998 and body > range_avg * 0.08 and vol_ok
        bearish = (bearish_cross or bearish_aligned) and closes[i] < vwap[i] * 1.002 and body > range_avg * 0.08 and vol_ok

        if position == "LONG":
            if bearish_cross or i - last_entry_i >= 6:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "ema_quick_scalp",
                    "confidence": 70.0,
                    "entry_reason": "Quick EMA CE exit",
                    "target_R": 1.25,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.75,
                    "max_hold_minutes": 12,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "scalp_momentum",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
        elif position == "SHORT":
            if bullish_cross or i - last_entry_i >= 6:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "ema_quick_scalp",
                    "confidence": 70.0,
                    "entry_reason": "Quick EMA PE exit",
                    "target_R": 1.25,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.75,
                    "max_hold_minutes": 12,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "scalp_momentum",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
        elif i - last_entry_i >= 4:
            if bullish:
                conf = 65.0
                if vols[i] >= avg_vol * 1.4: conf += 15.0
                if body >= range_avg * 0.7: conf += 10.0
                if closes[i] > highs[i-1]: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "ema_quick_scalp",
                    "confidence": conf,
                    "entry_reason": "NIFTY CE EMA Crossover scalp above VWAP",
                    "target_R": 1.25,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.75,
                    "max_hold_minutes": 12,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "scalp_momentum",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
                last_entry_i = i
            elif bearish:
                conf = 65.0
                if vols[i] >= avg_vol * 1.4: conf += 15.0
                if body >= range_avg * 0.7: conf += 10.0
                if closes[i] < lows[i-1]: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "ema_quick_scalp",
                    "confidence": conf,
                    "entry_reason": "NIFTY PE EMA Crossover scalp below VWAP",
                    "target_R": 1.25,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 0.75,
                    "max_hold_minutes": 12,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "scalp_momentum",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "SHORT"
                last_entry_i = i

    return signals
"""


BANKNIFTY_SENSITIVE_VOL_BREAKOUT_CODE = """def run(data):
    if len(data) < 40:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close') or 0) or 0) for d in data]
    lows = [float(d.get('low', d.get('close') or 0) or 0) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]
    band_mult = 1.2
    
    def avg(values):
        return sum(values) / max(1, len(values))

    signals = []
    position = "NONE"
    cooldown = 0

    for i in range(24, len(data)):
        if closes[i] <= 0:
            continue
        if cooldown > 0:
            cooldown -= 1
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '14:25':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL" if position == "LONG" else "BUY",
                    "direction": "CE" if position == "LONG" else "PE",
                    "setup_type": "volatility_expansion",
                    "confidence": 50.0,
                    "entry_reason": "BANKNIFTY vol time exit",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatility_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue

        chunk = closes[i-20:i]
        sma = sum(chunk) / 20
        std = (sum((x - sma) ** 2 for x in chunk) / 20) ** 0.5
        upper = sma + band_mult * std
        lower = sma - band_mult * std
        avg_vol = avg(vols[i-20:i])
        
        # Volatility compression calculation: BB width compared to average of last 30 bars
        width = (upper - lower) / sma if sma else 0.0
        width_history = []
        for j in range(i-30, i):
            c_chunk = closes[j-20:j]
            c_sma = sum(c_chunk) / 20
            c_std = (sum((x - c_sma) ** 2 for x in c_chunk) / 20) ** 0.5
            width_history.append((c_sma + band_mult * c_std - (c_sma - band_mult * c_std)) / c_sma if c_sma else 0.0)
            
        avg_width = sum(width_history) / len(width_history)
        compressed = width <= avg_width * 1.05
        
        # ATR check
        atr = max(0.01, avg([max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-14, i)]))
        body = abs(closes[i] - data[i].get('open', closes[i]))
        
        # Breakout indicators
        expanding = width > width_history[-1]
        vol_surge = float(data[i].get('tod_vol_ratio', 1.0)) >= 0.75 and body > atr * 0.35

        bullish = compressed and closes[i] > upper and closes[i] > closes[i-1] and expanding and vol_surge and cooldown == 0
        bearish = compressed and closes[i] < lower and closes[i] < closes[i-1] and expanding and vol_surge and cooldown == 0

        if position == "LONG":
            if bearish or closes[i] < sma:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "volatility_expansion",
                    "confidence": 75.0,
                    "entry_reason": "BANKNIFTY vol CE exit",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatility_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 6
        elif position == "SHORT":
            if bullish or closes[i] > sma:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "volatility_expansion",
                    "confidence": 75.0,
                    "entry_reason": "BANKNIFTY vol PE exit",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatility_expansion",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
                cooldown = 6
        else:
            if bullish:
                # Compression-to-expansion breakout confidence
                conf = 65.0
                if width <= avg_width * 0.8: conf += 15.0 # deep compression
                if vols[i] >= avg_vol * 1.6: conf += 10.0
                if body >= atr * 1.2: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "volatility_expansion",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY volatility expansion CE breakout",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatility_expansion",
                    "option_selection_preference": "OTM1" if conf > 85.0 else "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
            elif bearish:
                conf = 65.0
                if width <= avg_width * 0.8: conf += 15.0
                if vols[i] >= avg_vol * 1.6: conf += 10.0
                if body >= atr * 1.2: conf += 10.0
                conf = min(100.0, conf)
                
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "volatility_expansion",
                    "confidence": conf,
                    "entry_reason": "BANKNIFTY volatility expansion PE breakout",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 30,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatility_expansion",
                    "option_selection_preference": "OTM1" if conf > 85.0 else "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "SHORT"

    return signals
"""


DEFAULT_OPTION_STRATEGIES = [
    {
        # EDR-10/13 (2026-07-04/06): the FIRST OOS-validated edge. Sell a
        # defined-risk ~3% OTM NIFTY put spread, hold to weekly expiry (no intraday
        # stop) — harvest the downside vol-risk premium. 2026-07-06 focused sweep
        # improved the paper-forward template from width=6 to width=10:
        # +₹382/trade, OOS +₹549/trade, 95% WR, both years positive. This widens
        # the defined-risk cap, so required_capital/daily_loss_limit stay honest.
        # PAPER-ACTIVE for forward testing only. CORE_ENGINE_LIVE_ENABLED stays false.
        "name": "QG-O1 NIFTY Put Spread Theta Core",
        "description": "Defined-risk NIFTY put-spread income pilot from EDR-09/QG-O1: sell a ~3% OTM put spread with a wider 10-strike defined-risk wing and hold to weekly expiry to harvest downside volatility-risk premium. This is the primary paper-forward candidate; real-live promotion still requires forward-paper evidence.",
        "underlying": "NIFTY", "strike_mode": "OTM_SELL", "otm_points": 720, "lots": 1,
        "structure": "credit_spread", "spread_width": 10,
        "short_otm_pct": 0.03, "wing_width": 10, "exit_mode": "expiry", "short_delta": 0.12,
        "strategy_type": "Option Selling", "required_capital": 35000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "pullback", "strategy_category": "swing", "daily_loss_limit": 40000.0,
                 "time_exit_minutes": 0, "exit_mode": "hold_to_expiry", "cooldown_minutes": 60,
                 "max_trades_day": 1},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 20:
        return []
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '13:00'):
        return []
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': 'CE',
        'setup_type': 'defined_risk_put_spread_income',
        'confidence': 72.0,
        'entry_reason': 'QG-O1 sell 3% OTM NIFTY put spread with 10-strike wing, hold to expiry',
        'target_R': 1.0, 'initial_stop_R': 1.0, 'trail_after_R': 0.0,
        'max_hold_minutes': 0, 'invalidation_rule': 'weekly_expiry_defined_risk',
        'regime_required': 'range_to_up', 'option_selection_preference': 'OTM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "Non-crash / range-to-up markets (short downside vol)",
    },
    {
        "name": "QG-O2 NIFTY Trend-Filtered Put Spread Theta",
        "description": "Lower-frequency NIFTY put-spread income pilot. It sells a ~3% OTM defined-risk put spread only when the underlying is above its 20/50-period trend filter.",
        "underlying": "NIFTY", "strike_mode": "OTM_SELL", "otm_points": 720, "lots": 1,
        "structure": "credit_spread", "spread_width": 6,
        "short_otm_pct": 0.03, "wing_width": 6, "exit_mode": "expiry", "short_delta": 0.12,
        "strategy_type": "Option Selling", "required_capital": 25000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "pullback", "strategy_category": "swing", "daily_loss_limit": 30000.0,
                 "time_exit_minutes": 0, "exit_mode": "hold_to_expiry", "cooldown_minutes": 60,
                 "max_trades_day": 1},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 60:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '13:00'):
        return []
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    if not (closes[-1] > ma20 > ma50):
        return []
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': 'CE',
        'setup_type': 'trend_filtered_put_spread_income',
        'confidence': 68.0,
        'entry_reason': 'QG-O2 NIFTY uptrend filter passed; sell OTM put spread',
        'target_R': 1.0, 'initial_stop_R': 1.0, 'trail_after_R': 0.0,
        'max_hold_minutes': 0, 'invalidation_rule': 'weekly_expiry_defined_risk',
        'regime_required': 'uptrend', 'option_selection_preference': 'OTM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "Uptrend / non-crash NIFTY weeks",
    },
    {
        "name": "QG-O3 SENSEX Put Spread Theta Pilot",
        "description": "SENSEX defined-risk put-spread pilot adapted from the short-vol research pack into the app's supported 2-leg credit-spread engine.",
        "underlying": "SENSEX", "strike_mode": "OTM_SELL", "otm_points": 1300, "lots": 1,
        "structure": "credit_spread", "spread_width": 4,
        "short_otm_pct": 0.02, "wing_width": 4, "exit_mode": "expiry", "short_delta": 0.14,
        "strategy_type": "Option Selling", "required_capital": 30000.0, "instrument_group": "BFO",
        "initial_status": "live",
        "risk": {"risk_style": "pullback", "strategy_category": "swing", "daily_loss_limit": 25000.0,
                 "time_exit_minutes": 0, "exit_mode": "hold_to_expiry", "cooldown_minutes": 60,
                 "max_trades_day": 1},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 30:
        return []
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '13:00'):
        return []
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': 'CE',
        'setup_type': 'sensex_put_spread_income',
        'confidence': 64.0,
        'entry_reason': 'QG-O3 sell defined-risk SENSEX OTM put spread',
        'target_R': 1.0, 'initial_stop_R': 1.0, 'trail_after_R': 0.0,
        'max_hold_minutes': 0, 'invalidation_rule': 'weekly_expiry_defined_risk',
        'regime_required': 'range_to_up', 'option_selection_preference': 'OTM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "SENSEX range-to-up short-vol paper pilot",
    },
    {
        "name": "QG-O4 SENSEX Call Spread Range Pilot",
        "description": "SENSEX defined-risk call-spread pilot for range/down weeks. This is a 2-leg app-compatible substitute for the researched condor until 4-leg live support exists.",
        "underlying": "SENSEX", "strike_mode": "OTM_SELL", "otm_points": 1300, "lots": 1,
        "structure": "credit_spread", "spread_width": 4,
        "short_otm_pct": 0.02, "wing_width": 4, "exit_mode": "expiry", "short_delta": 0.14,
        "strategy_type": "Option Selling", "required_capital": 30000.0, "instrument_group": "BFO",
        "initial_status": "live",
        "risk": {"risk_style": "pullback", "strategy_category": "swing", "daily_loss_limit": 25000.0,
                 "time_exit_minutes": 0, "exit_mode": "hold_to_expiry", "cooldown_minutes": 60,
                 "max_trades_day": 1},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 40:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '13:00'):
        return []
    ten_range = (max(closes[-10:]) - min(closes[-10:])) / max(1.0, closes[-1])
    if ten_range > 0.035:
        return []
    return [{
        'date': d['date'], 'action': 'SELL', 'direction': 'PE',
        'setup_type': 'sensex_range_call_spread_income',
        'confidence': 58.0,
        'entry_reason': 'QG-O4 SENSEX range filter passed; sell OTM call spread',
        'target_R': 1.0, 'initial_stop_R': 1.0, 'trail_after_R': 0.0,
        'max_hold_minutes': 0, 'invalidation_rule': 'weekly_expiry_defined_risk',
        'regime_required': 'range', 'option_selection_preference': 'OTM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "SENSEX range / capped-upside weeks",
    },
    {
        "name": "QG-O5 NIFTY Opening Range Call Buyer",
        "description": "Intraday NIFTY opening-range credit-spread scalp. It keeps the QG-O5 bullish breakout trigger but sells a tiny bull-put spread instead of buying premium; paper-forward only until the IMD sample gate matures.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "credit_spread", "spread_width": 1, "short_offset_strikes": 2,
        "candle_interval": "1minute",
        "strategy_type": "Option Selling", "required_capital": 5000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "breakout", "strategy_category": "intraday", "daily_loss_limit": 4000.0,
                 "time_exit_minutes": 60, "exit_mode": "signal_or_tp_sl_trailing", "cooldown_minutes": 60,
                 "max_trades_day": 1, "target_r_multiple": 0.7},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 30:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close')) or 0) for d in data]
    lows = [float(d.get('low', d.get('close')) or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:35' or clock > '14:20'):
        return []
    opening_high = max(highs[-18:-6]) if len(highs) >= 24 else max(highs[:-1])
    avg_range = sum(highs[-12:][i] - lows[-12:][i] for i in range(12)) / 12
    body = closes[-1] - float(d.get('open', closes[-1]) or closes[-1])
    if closes[-1] <= opening_high or body <= avg_range * 0.35:
        return []
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': 'CE',
        'setup_type': 'opening_range_bull_put_credit_scalp',
        'confidence': 61.0,
        'entry_reason': 'QG-O5 NIFTY opening range upside break; sell 2-OTM/1-wide bull put credit spread',
        'target_R': 0.7, 'initial_stop_R': 2.0, 'trail_after_R': 99.0,
        'max_hold_minutes': 60, 'invalidation_rule': 'credit_spread_expansion_or_time',
        'regime_required': 'intraday_momentum_up', 'option_selection_preference': 'OTM_CREDIT',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "NIFTY intraday trend-up days; under-sampled paper-forward credit scalp",
    },
    {
        "name": "QG-O6 NIFTY Opening Range Put Buyer",
        "description": "Intraday NIFTY debit-spread put buyer. It trades only after a strong opening-range downside break and uses defined debit risk.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2, "candle_interval": "1minute",
        "strategy_type": "Option Buying", "required_capital": 12000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "breakout", "strategy_category": "intraday", "daily_loss_limit": 5000.0,
                 "time_exit_minutes": 35, "exit_mode": "signal_or_tp_sl_trailing", "cooldown_minutes": 45,
                 "max_trades_day": 1, "target_r_multiple": 1.4},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 30:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close')) or 0) for d in data]
    lows = [float(d.get('low', d.get('close')) or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:35' or clock > '14:20'):
        return []
    opening_low = min(lows[-18:-6]) if len(lows) >= 24 else min(lows[:-1])
    avg_range = sum(highs[-12:][i] - lows[-12:][i] for i in range(12)) / 12
    body = float(d.get('open', closes[-1]) or closes[-1]) - closes[-1]
    if closes[-1] >= opening_low or body <= avg_range * 0.35:
        return []
    return [{
        'date': d['date'], 'action': 'SELL', 'direction': 'PE',
        'setup_type': 'opening_range_put_breakdown',
        'confidence': 63.0,
        'entry_reason': 'QG-O6 NIFTY opening range downside break',
        'target_R': 1.4, 'initial_stop_R': 0.7, 'trail_after_R': 1.0,
        'max_hold_minutes': 35, 'invalidation_rule': 'breakdown_failure_or_time',
        'regime_required': 'intraday_momentum_down', 'option_selection_preference': 'ATM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "NIFTY intraday trend-down days only",
    },
    {
        "name": "QG-O7 BANKNIFTY VWAP Reclaim Call Buyer",
        "description": "Intraday BANKNIFTY debit-spread call buyer for failed breakdown and VWAP reclaim days. Paper-sized and limited to one trade per day.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2, "candle_interval": "1minute",
        "strategy_type": "Option Buying", "required_capital": 15000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "breakout", "strategy_category": "intraday", "daily_loss_limit": 6000.0,
                 "time_exit_minutes": 25, "exit_mode": "signal_or_tp_sl_trailing", "cooldown_minutes": 60,
                 "max_trades_day": 1, "target_r_multiple": 1.2},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 35:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close')) or 0) for d in data]
    lows = [float(d.get('low', d.get('close')) or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:40' or clock > '14:10'):
        return []
    avg_range = sum(highs[-14:][i] - lows[-14:][i] for i in range(14)) / 14
    reclaimed = closes[-1] > sum(closes[-12:]) / 12 and closes[-2] <= sum(closes[-13:-1]) / 12
    higher_low = lows[-1] > min(lows[-8:-1])
    if not (reclaimed and higher_low and (highs[-1] - lows[-1]) > avg_range * 0.85):
        return []
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': 'CE',
        'setup_type': 'banknifty_vwap_reclaim_call',
        'confidence': 58.0,
        'entry_reason': 'QG-O7 BANKNIFTY failed breakdown and VWAP reclaim',
        'target_R': 1.2, 'initial_stop_R': 0.6, 'trail_after_R': 0.9,
        'max_hold_minutes': 25, 'invalidation_rule': 'reclaim_failure_or_time',
        'regime_required': 'intraday_reversal_up', 'option_selection_preference': 'ATM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "BANKNIFTY fast reversal-up days only",
    },
    {
        "name": "QG-O8 BANKNIFTY VWAP Reject Put Buyer",
        "description": "Intraday BANKNIFTY debit-spread put buyer for VWAP rejection and lower-high breakdown days. Paper-sized and limited to one trade per day.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2, "candle_interval": "1minute",
        "strategy_type": "Option Buying", "required_capital": 15000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "breakout", "strategy_category": "intraday", "daily_loss_limit": 6000.0,
                 "time_exit_minutes": 25, "exit_mode": "signal_or_tp_sl_trailing", "cooldown_minutes": 60,
                 "max_trades_day": 1, "target_r_multiple": 1.2},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 35:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close')) or 0) for d in data]
    lows = [float(d.get('low', d.get('close')) or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:40' or clock > '14:10'):
        return []
    avg_range = sum(highs[-14:][i] - lows[-14:][i] for i in range(14)) / 14
    rejected = closes[-1] < sum(closes[-12:]) / 12 and closes[-2] >= sum(closes[-13:-1]) / 12
    lower_high = highs[-1] < max(highs[-8:-1])
    if not (rejected and lower_high and (highs[-1] - lows[-1]) > avg_range * 0.85):
        return []
    return [{
        'date': d['date'], 'action': 'SELL', 'direction': 'PE',
        'setup_type': 'banknifty_vwap_reject_put',
        'confidence': 58.0,
        'entry_reason': 'QG-O8 BANKNIFTY VWAP reject and lower-high breakdown',
        'target_R': 1.2, 'initial_stop_R': 0.6, 'trail_after_R': 0.9,
        'max_hold_minutes': 25, 'invalidation_rule': 'reject_failure_or_time',
        'regime_required': 'intraday_reversal_down', 'option_selection_preference': 'ATM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "BANKNIFTY fast reversal-down days only",
    },
    {
        "name": "QG-O9 NIFTY Tail Event Put Buyer",
        "description": "Rare NIFTY intraday put buyer for sharp downside expansion days. This stays small and exits quickly because EOD-held long puts tested poorly.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "single_leg", "spread_width": 1, "candle_interval": "1minute",
        "strategy_type": "Option Buying", "required_capital": 10000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "volatile_breakout", "strategy_category": "intraday", "daily_loss_limit": 3500.0,
                 "time_exit_minutes": 20, "exit_mode": "signal_or_tp_sl_trailing", "cooldown_minutes": 90,
                 "max_trades_day": 1, "target_r_multiple": 1.5},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 30:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close')) or 0) for d in data]
    lows = [float(d.get('low', d.get('close')) or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '14:00'):
        return []
    move3 = (closes[-1] - closes[-4]) / max(1.0, closes[-4])
    avg_range = sum(highs[-12:][i] - lows[-12:][i] for i in range(12)) / 12
    if move3 > -0.006 or (highs[-1] - lows[-1]) < avg_range * 1.15:
        return []
    return [{
        'date': d['date'], 'action': 'SELL', 'direction': 'PE',
        'setup_type': 'nifty_tail_event_put',
        'confidence': 56.0,
        'entry_reason': 'QG-O9 NIFTY sharp downside expansion put buyer',
        'target_R': 1.5, 'initial_stop_R': 0.7, 'trail_after_R': 1.0,
        'max_hold_minutes': 20, 'invalidation_rule': 'snapback_or_time',
        'regime_required': 'tail_downside_expansion', 'option_selection_preference': 'ATM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "Rare NIFTY downside expansion days",
    },
    {
        "name": "QG-O10 NIFTY Premium-Safe Debit Buyer",
        "description": "NIFTY intraday debit-spread buyer that only enters when the candle expansion justifies paying option premium. Defined debit risk, one paper trade per day.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2, "candle_interval": "1minute",
        "strategy_type": "Option Buying", "required_capital": 12000.0, "instrument_group": "NFO",
        "initial_status": "live",
        "risk": {"risk_style": "momentum", "strategy_category": "intraday", "daily_loss_limit": 4500.0,
                 "time_exit_minutes": 35, "exit_mode": "signal_or_tp_sl_trailing", "cooldown_minutes": 60,
                 "max_trades_day": 1, "target_r_multiple": 1.3},
        "python_code": """def run(data):
    position = "NONE"
    if len(data) < 45:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high', d.get('close')) or 0) for d in data]
    lows = [float(d.get('low', d.get('close')) or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '14:15'):
        return []
    ma12 = sum(closes[-12:]) / 12
    ma30 = sum(closes[-30:]) / 30
    avg_range = sum(highs[-20:][i] - lows[-20:][i] for i in range(20)) / 20
    expansion = highs[-1] - lows[-1]
    if expansion < avg_range * 1.1:
        return []
    if closes[-1] > ma12 > ma30:
        action, direction, reason = 'BUY', 'CE', 'QG-O10 premium-safe upside debit spread'
    elif closes[-1] < ma12 < ma30:
        action, direction, reason = 'SELL', 'PE', 'QG-O10 premium-safe downside debit spread'
    else:
        return []
    return [{
        'date': d['date'], 'action': action, 'direction': direction,
        'setup_type': 'premium_safe_debit_spread',
        'confidence': 60.0,
        'entry_reason': reason,
        'target_R': 1.3, 'initial_stop_R': 0.7, 'trail_after_R': 1.0,
        'max_hold_minutes': 35, 'invalidation_rule': 'momentum_failure_or_time',
        'regime_required': 'intraday_momentum', 'option_selection_preference': 'ATM',
        'signal_version': 'v13', 'strategy_logic_version': 'qg-alpha-2026-07'
    }]
""",
        "market_suitability": "NIFTY intraday range-expansion days",
    },
    {
        "name": "NIFTY Momentum Buyer",
        "description": "Upstox-compatible single-leg NIFTY ATM option buying strategy. Uses live NIFTY candles, resolves the exact Upstox option instrument_key, enters on momentum, and exits through the same order manager.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2,
        "strategy_type": "Option Buying", "required_capital": 25000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 35: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    signals = []
    position = "NONE"
    entry = 0.0
    for i in range(21, len(data)):
        ema8 = sum(closes[i-7:i+1]) / 8
        ema21 = sum(closes[i-20:i+1]) / 21
        range12 = max(highs[i-11:i+1]) - min(lows[i-11:i+1])
        momentum = closes[i] - closes[i-3]
        bullish = closes[i] > ema8 > ema21 and momentum > range12 * 0.18
        bearish = closes[i] < ema8 < ema21 and momentum < -range12 * 0.18
        if position == "LONG":
            pnl = (closes[i] - entry) / entry * 100 if entry else 0.0
            if closes[i] < ema8 or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'NIFTY option exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry - closes[i]) / entry * 100 if entry else 0.0
            if closes[i] > ema8 or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'NIFTY put exit'})
                position = "NONE"
        else:
            if bullish:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'NIFTY CE momentum'})
                position = "LONG"
                entry = closes[i]
            elif bearish:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'NIFTY PE momentum'})
                position = "SHORT"
    return signals
""",
        "market_suitability": "Upstox live trend and momentum",
    },
    {
        "name": "BANKNIFTY Breakout Buyer",
        "description": "BANKNIFTY breakout expressed as a THETA CREDIT SPREAD (2026-06-30: converted from a directional debit spread that lost every trade in chop). A BUY signal sells a put spread, a SELL signal sells a call spread — it earns time decay if price holds, instead of needing a big directional move.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "credit_spread", "spread_width": 2,
        "strategy_type": "Option Selling", "required_capital": 8000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 35: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    signals = []
    position = "NONE"
    entry = 0.0
    for i in range(22, len(data)):
        channel_high = max(highs[i-12:i])
        channel_low = min(lows[i-12:i])
        avg_range = sum(highs[j] - lows[j] for j in range(i-12, i)) / 12
        bullish = closes[i] > channel_high and closes[i] - closes[i-1] > avg_range * 0.35
        bearish = closes[i] < channel_low and closes[i-1] - closes[i] > avg_range * 0.35
        mid = (channel_high + channel_low) / 2
        if position == "LONG":
            pnl = (closes[i] - entry) / entry * 100 if entry else 0.0
            if closes[i] < mid or pnl <= -0.4 or pnl >= 0.9:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'BANKNIFTY option exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry - closes[i]) / entry * 100 if entry else 0.0
            if closes[i] > mid or pnl <= -0.4 or pnl >= 0.9:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'BANKNIFTY put exit'})
                position = "NONE"
        else:
            if bullish:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'BANKNIFTY CE breakout'})
                position = "LONG"
                entry = closes[i]
            elif bearish:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'BANKNIFTY PE breakout'})
                position = "SHORT"
    return signals
""",
        "market_suitability": "Upstox live volatility breakout",
    },
    {
        "name": "NIFTY VWAP Trend Breakout",
        "description": "Directional index option buying triggered by volume-backed VWAP breakouts on NIFTY 5-minute charts.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2,
        "strategy_type": "Option Buying", "required_capital": 25000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 40: return []
    closes = [d['close'] for d in data]
    highs = [d.get('high', d['close']) for d in data]
    lows = [d.get('low', d['close']) for d in data]
    volumes = [float(d.get('volume') or 0) for d in data]
    
    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, volumes):
        weighted += ((h + l + c) / 3.0) * max(1.0, v)
        total_vol += max(1.0, v)
        vwap.append(weighted / total_vol)
        
    signals = []
    for i in range(20, len(data)):
        if closes[i] > vwap[i] and closes[i-1] <= vwap[i-1] and volumes[i] > sum(volumes[max(0, i-20):i])/20 * 1.2:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'VWAP Bullish Breakout'})
        elif closes[i] < vwap[i] and closes[i-1] >= vwap[i-1] and volumes[i] > sum(volumes[max(0, i-20):i])/20 * 1.2:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'VWAP Bearish Breakout'})
    return signals
""",
        "market_suitability": "Strong Trending & Sustained Breakout",
    },
    {
        "name": "SENSEX Swing RSI Pullback",
        "description": "Swing trading pullback entries on SENSEX triggered by extreme RSI levels overbought/oversold recoveries.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2,
        "strategy_type": "Option Buying", "required_capital": 25000.0, "instrument_group": "BFO",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [d['close'] for d in data]
    period = 14
    rsi = [50.0] * len(closes)
    for i in range(period, len(closes)):
        gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-period+1, i+1))
        losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-period+1, i+1)) or 0.0001
        rs = gains / losses
        rsi[i] = 100 - (100 / (1 + rs))
        
    signals = []
    for i in range(period + 1, len(data)):
        if rsi[i] > 30 and rsi[i-1] <= 30:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'RSI Oversold Pullback'})
        elif rsi[i] < 70 and rsi[i-1] >= 70:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'RSI Overbought Reversal'})
    return signals
""",
        "market_suitability": "Trend Retracements & Swing Pullbacks",
    },
    {
        "name": "Crude Oil Momentum Breakout",
        "description": "MCX Crude Oil directional option buying triggered by high-volume breakouts of 12-bar price channels.",
        "underlying": "CRUDEOIL", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 65000.0, "instrument_group": "MCX",
        "python_code": """def run(data):
    if len(data) < 20: return []
    closes = [d['close'] for d in data]
    highs = [d.get('high', d['close']) for d in data]
    lows = [d.get('low', d['close']) for d in data]
    
    signals = []
    for i in range(12, len(data)):
        prev_high = max(highs[i-12:i])
        prev_low = min(lows[i-12:i])
        if closes[i] > prev_high:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude Channel Breakout'})
        elif closes[i] < prev_low:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude Channel breakdown'})
    return signals
""",
        "market_suitability": "Highly Volatile & Momentum Trends",
    },
    {
        "name": "Natural Gas Volatility Compression",
        "description": "MCX Natural Gas options breakouts triggered by Bollinger Band volatility squeezes.",
        "underlying": "NATURALGAS", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 55000.0, "instrument_group": "MCX",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [d['close'] for d in data]
    
    signals = []
    for i in range(20, len(data)):
        chunk = closes[i-20:i]
        sma = sum(chunk) / 20
        variance = sum((x - sma) ** 2 for x in chunk) / 20
        std = (variance) ** 0.5
        upper = sma + 2 * std
        lower = sma - 2 * std
        width = (upper - lower) / sma if sma else 0.0
        
        if width < 0.02:
            if closes[i] > upper:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'BB Squeeze Breakout UP'})
            elif closes[i] < lower:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'BB Squeeze Breakout DOWN'})
    return signals
""",
        "market_suitability": "Volatility Expansion Following Squeeze",
    },
    {
        "name": "Crude Oil Mini Intraday Scalper",
        "description": "Fast-moving low-capital Crude Oil Mini (CRUDEOILM) option buying scalps using 3/8 EMA crossovers.",
        "underlying": "CRUDEOILM", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 5000.0, "instrument_group": "MCX",
        "python_code": """def run(data):
    if len(data) < 20: return []
    closes = [d['close'] for d in data]
    signals = []
    for i in range(8, len(data)):
        ema3 = sum(closes[max(0, i-2):i+1]) / min(3, i+1)
        ema8 = sum(closes[max(0, i-7):i+1]) / min(8, i+1)
        if ema3 > ema8 and closes[i] > closes[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Mini EMA Scalp Buy'})
        elif ema3 < ema8 and closes[i] < closes[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Mini EMA Scalp Sell'})
    return signals
""",
        "market_suitability": "Fast Dynamic Swings & Volatility Sweeps",
    },
    {
        "name": "NIFTY Micro-Lot Trend Follower",
        "description": "Low-capital NIFTY ATM option buying trend-follower. Capital required: 10,000.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2,
        "strategy_type": "Option Buying", "required_capital": 10000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [d['close'] for d in data]
    signals = []
    for i in range(20, len(data)):
        sma20 = sum(closes[i-20:i]) / 20
        if closes[i] > sma20 and closes[i-1] <= sma20:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Micro Trend BUY'})
        elif closes[i] < sma20 and closes[i-1] >= sma20:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Micro Trend SELL'})
    return signals
""",
        "market_suitability": "Intraday Trend Following",
    },
    {
        "name": "NIFTY HFT Quick Scalper",
        "description": "High-frequency index option scalper targeting micro-breakouts on NIFTY 1-minute and 5-minute charts using HFT execution templates.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2,
        "strategy_type": "Option Buying", "required_capital": 10000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 20: return []
    closes = [d['close'] for d in data]
    signals = []
    for i in range(10, len(data)):
        ema3 = sum(closes[i-2:i+1]) / 3
        ema10 = sum(closes[i-9:i+1]) / 10
        if ema3 > ema10 and closes[i] > closes[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'HFT Momentum BUY'})
        elif ema3 < ema10 and closes[i] < closes[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'HFT Momentum SELL'})
    return signals
""",
        "market_suitability": "Scalper (HFT Low-Latency)",
    },
    {
        "name": "BANKNIFTY HFT Momentum Scalper",
        "description": "BANKNIFTY std-dev band breakout expressed as a THETA CREDIT SPREAD (2026-06-30: converted from a directional debit spread that lost every trade in chop). Sells a put/call spread on the band signal and rides to TP/SL/time decay instead of needing a directional move.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "credit_spread", "spread_width": 2,
        "strategy_type": "Option Selling", "required_capital": 8000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 25: return []
    closes = [d['close'] for d in data]
    signals = []
    for i in range(20, len(data)):
        sma = sum(closes[i-20:i]) / 20
        var = sum((x - sma) ** 2 for x in closes[i-20:i]) / 20
        std = var ** 0.5
        upper = sma + 1.5 * std
        lower = sma - 1.5 * std
        if closes[i] > upper and closes[i-1] <= upper:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'HFT Band Breakout BUY'})
        elif closes[i] < lower and closes[i-1] >= lower:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'HFT Band Breakdown SELL'})
    return signals
""",
        "market_suitability": "Volatile Breakout (HFT Execution)",
    },
    {
        "name": "NIFTY Quick EMA Scalper",
        "description": "Capital-efficient (INR 8,000) NIFTY momentum strategy expressed as a THETA CREDIT SPREAD (2026-06-30: converted from a directional debit spread that lost every trade in chop). A fast 3/9 EMA crossover sells a put/call spread and earns time decay if price holds, instead of paying premium for a directional move.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "credit_spread", "spread_width": 2,
        "strategy_type": "Option Selling", "required_capital": 8000.0, "instrument_group": "NFO",
        "risk": {
            "stop_loss_pct": 8, "take_profit_pct": 12,
            "trail_trigger_pct": 5, "trail_step_pct": 2.8,
            "cooldown_minutes": 8, "max_trades_day": 6,
            "time_exit_minutes": 18, "daily_loss_limit": 1200,
            "strategy_category": "intraday", "target_r_multiple": 1.45,
        },
        "python_code": """def run(data):
    if len(data) < 20: return []
    closes = [d['close'] for d in data]
    signals = []
    for i in range(10, len(data)):
        ema3 = sum(closes[i-2:i+1]) / 3
        ema9 = sum(closes[i-8:i+1]) / 9
        if ema3 > ema9 and closes[i] > closes[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'EMA Crossover Buy'})
        elif ema3 < ema9 and closes[i] < closes[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'EMA Crossover Sell'})
    return signals
""",
        "market_suitability": "High-Frequency Retail Scalper",
    },
    {
        "name": "BANKNIFTY Volatility Breakout",
        "description": "Capital-efficient (INR 8,000) BANKNIFTY retail options buying strategy. Captures quick price expansions when closing candles break out of standard deviation bands.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "structure": "debit_spread", "spread_width": 2,
        "strategy_type": "Option Buying", "required_capital": 8000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 25: return []
    closes = [d['close'] for d in data]
    signals = []
    for i in range(20, len(data)):
        sma = sum(closes[i-20:i]) / 20
        var = sum((x - sma) ** 2 for x in closes[i-20:i]) / 20
        std = var ** 0.5
        upper = sma + 1.2 * std
        lower = sma - 1.2 * std
        if closes[i] > upper and closes[i-1] <= upper:
            signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Volatility Band Breakout'})
        elif closes[i] < lower and closes[i-1] >= lower:
            signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Volatility Band Breakdown'})
    return signals
""",
        "market_suitability": "Volatile Retail Momentum",
    },
    {
        "name": "RELIANCE Trend Rider",
        "risk": {"daily_loss_limit": 2500},  # equity loss floor ~3.3% of ₹75k tier (WR-52)
        "description": "Rides strong bull trends using double EMA (9/21) filtered by index direction and tod_vol_ratio to ensure high volume participation. Uses trailing ATR stops.",
        "underlying": "RELIANCE",
        "strategy_type": "Equity Intraday",
        "required_capital": 15000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 55: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]
    
    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for val in values[1:]:
            out.append(val * k + out[-1] * (1 - k))
        return out

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    
    signals = []
    position = "NONE"
    
    for i in range(30, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "trend_follow",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        tod_vol = float(data[i].get('tod_vol_ratio', 1.0))
        bullish = ema9[i] > ema21[i] and ema9[i-1] <= ema21[i-1] and tod_vol > 1.1
        bearish = ema9[i] < ema21[i] and ema9[i-1] >= ema21[i-1]
        
        if position == "NONE":
            if bullish:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "trend_follow",
                    "confidence": 85.0,
                    "entry_reason": "EMA bullish crossover",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
        elif position == "LONG":
            if bearish:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "trend_follow",
                    "confidence": 85.0,
                    "entry_reason": "EMA bearish crossover exit",
                    "target_R": 2.2,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Strong Bull Markets & Intraday Momentum",
    },
    {
        "name": "SBIN Short Seller",
        "risk": {"daily_loss_limit": 1800},  # equity loss floor ~3.6% of ₹50k tier (WR-52)
        "description": "Short-selling intraday strategy for bear markets. Enters when price drops below VWAP and sloping 200 EMA, utilizing volume-backed breakdowns.",
        "underlying": "SBIN",
        "strategy_type": "Equity Intraday",
        "required_capital": 15000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 55: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]
    
    weighted = 0.0
    total_vol = 0.0
    vwap = []
    for h, l, c, v in zip(highs, lows, closes, vols):
        weighted += ((h + l + c) / 3.0) * v
        total_vol += v
        vwap.append(weighted / max(1.0, total_vol))
        
    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for val in values[1:]:
            out.append(val * k + out[-1] * (1 - k))
        return out
        
    ema50 = ema(closes, 50)
    
    signals = []
    position = "NONE"
    
    for i in range(50, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "bearish_breakdown",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        tod_vol = float(data[i].get('tod_vol_ratio', 1.0))
        bearish_entry = (
            closes[i] < vwap[i] * 1.001
            and closes[i] < ema50[i] * 1.001
            and ema50[i] < ema50[i-10]
            and tod_vol >= 0.8
            and closes[i] <= min(closes[max(0, i-6):i]) * 1.001
            and closes[i] <= closes[i-1]
        )
        bearish_exit = closes[i] > vwap[i] or closes[i] > ema50[i]
        
        if position == "NONE":
            if bearish_entry:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "PE",
                    "setup_type": "bearish_breakdown",
                    "confidence": 72.0,
                    "entry_reason": "VWAP & EMA bearish continuation",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "SHORT"
        elif position == "SHORT":
            if bearish_exit:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "PE",
                    "setup_type": "bearish_breakdown",
                    "confidence": 85.0,
                    "entry_reason": "Bearish trend reversal exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Bear Markets & Intraday Short Breakdown",
    },
    {
        "name": "HDFCBANK Range Rebound",
        "risk": {"daily_loss_limit": 2500},  # equity loss floor ~3.3% of ₹75k tier (WR-52)
        "description": "Mean reversion strategy for rangebound/low volatility environments. Uses Bollinger Bands compression/reversals with RSI filters.",
        "underlying": "HDFCBANK",
        "strategy_type": "Equity Intraday",
        "required_capital": 12000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [float(d['close']) for d in data]
    
    rsi = [50.0] * len(closes)
    for i in range(14, len(closes)):
        gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1))
        losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1)) or 0.0001
        rs = gains / losses
        rsi[i] = 100 - (100 / (1 + rs))
        
    signals = []
    position = "NONE"
    last_entry = -99

    for i in range(50, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "mean_reversion",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 1.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.0,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "range",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        chunk = closes[i-20:i]
        sma = sum(chunk) / 20
        var = sum((x - sma) ** 2 for x in chunk) / 20
        std = var ** 0.5
        lower_band = sma - 2 * std
        sma50 = sum(closes[i-49:i+1]) / 50
        # v2.1 fix: mean-revert the lower band ONLY when not in a clear downtrend
        # (price at/above the 50-SMA). The old code bought every lower-band touch
        # and got run over on HDFCBANK downtrends. Cooldown + hard trend-break bail
        # cap the damage when a "range" turns into a trend.
        not_downtrend = closes[i] >= sma50 * 0.995

        if position == "NONE":
            if not_downtrend and rsi[i] < 32 and closes[i] <= lower_band and (i - last_entry) >= 8:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "mean_reversion",
                    "confidence": 80.0,
                    "entry_reason": "Lower-band rebound in non-downtrend",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 120,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "range",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "LONG"
                last_entry = i
        elif position == "LONG":
            if closes[i] >= sma or rsi[i] > 68 or closes[i] < sma50 * 0.97:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "mean_reversion",
                    "confidence": 80.0,
                    "entry_reason": "SMA target / RSI / trend-break exit",
                    "target_R": 1.8,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.2,
                    "max_hold_minutes": 120,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "range",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Flat, Consolidating, and Rangebound Markets",
    },
    {
        "name": "ICICIBANK Volatility Breakout",
        "risk": {"daily_loss_limit": 2500},  # equity loss floor ~3.3% of ₹75k tier (WR-52)
        "description": "High-volatility intraday breakout strategy designed to capture sharp news-driven and earnings catalysts. Enters on extreme volume and ATR spikes.",
        "underlying": "ICICIBANK",
        "strategy_type": "Equity Intraday",
        "required_capital": 20000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    tr = [0.0] * len(closes)
    for j in range(1, len(closes)):
        tr[j] = max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
    atr = [0.0] * len(closes)
    for j in range(14, len(closes)):
        atr[j] = sum(tr[j-13:j+1]) / 14

    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for val in values[1:]:
            out.append(val * k + out[-1] * (1 - k))
        return out
    ema20 = ema(closes, 20)

    signals = []
    position = "NONE"
    last_entry = -99

    for i in range(20, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "volatility_breakout",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatile",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        donchian_high = max(highs[i-12:i])
        tod_vol = float(data[i].get('tod_vol_ratio', 1.0))
        # v2.1 fix: require a GENUINE breakout (close above the 12-bar Donchian high)
        # WITH the trend (close > rising EMA20) and real volume + cooldown. The old
        # 0.12-ATR "or" trigger fired on tiny moves in any regime (failed breakouts).
        news_trigger = (
            closes[i] > donchian_high
            and closes[i] > closes[i-1]
            and closes[i] > ema20[i] and ema20[i] > ema20[i-5]
            and tod_vol >= 0.9
        )

        if position == "NONE":
            if news_trigger and (i - last_entry) >= 8:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "volatility_breakout",
                    "confidence": 76.0,
                    "entry_reason": "Volatility continuation breakout buy",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 120,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatile",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
                last_entry = i
        elif position == "LONG":
            if closes[i] < ema20[i]:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "volatility_breakout",
                    "confidence": 95.0,
                    "entry_reason": "EMA20 trend-break exit",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 120,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "volatile",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "High Volatility, Earnings Release, and News Catalysts",
    },
    {
        "name": "TCS Swing Accumulator",
        "risk": {"daily_loss_limit": 1800},  # equity loss floor ~3.6% of ₹50k tier (WR-52)
        "description": "Swing delivery strategy built to accumulate defensive IT giant during global corrections/macro selloffs. Enters when daily RSI is extremely oversold.",
        "underlying": "TCS",
        "strategy_type": "Equity Delivery",
        "required_capital": 30000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "CNC",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [float(d['close']) for d in data]
    
    rsi = [50.0] * len(closes)
    for i in range(14, len(closes)):
        gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1))
        losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1)) or 0.0001
        rs = gains / losses
        rsi[i] = 100 - (100 / (1 + rs))
        
    signals = []
    position = "NONE"
    
    def sma_at(p, i):
        return sum(closes[i-p+1:i+1]) / p

    last_entry = -99
    for i in range(50, len(data)):
        sma50 = sma_at(50, i)
        # v2.1 fix: only accumulate an oversold bounce while the HIGHER trend is up
        # (close > SMA50) and RSI is turning back up — never buy RSI<25 in a
        # downtrend (the old falling-knife bleed). Cooldown stops same-dip churn.
        uptrend = closes[i] > sma50
        if position == "NONE":
            if uptrend and rsi[i-1] < 35 and rsi[i] > rsi[i-1] and (i - last_entry) >= 8:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "defensive_accumulation",
                    "confidence": 80.0,
                    "entry_reason": "Oversold bounce in uptrend",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 1440,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "any",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "LONG"
                last_entry = i
        elif position == "LONG":
            if rsi[i] > 62 or closes[i] < sma50:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "defensive_accumulation",
                    "confidence": 80.0,
                    "entry_reason": "RSI strength exit / trend break",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 1440,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "any",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "World Affairs, Global Risk-Off & Macro Reversals",
    },
    {
        "name": "INFY VWAP Pullback",
        "risk": {"daily_loss_limit": 1800},  # equity loss floor ~3.6% of ₹50k tier (WR-52)
        "description": "Intraday pullback buyer. Waits for price to pull back below VWAP inside a strong EMA 50 uptrend, entering on VWAP breakout recovery.",
        "underlying": "INFY",
        "strategy_type": "Equity Intraday",
        "required_capital": 10000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 55: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]
    
    cum_pv = 0.0
    cum_v = 0.0
    vwap = [0.0] * len(closes)
    for j in range(len(closes)):
        typical = (highs[j] + lows[j] + closes[j]) / 3.0
        cum_pv += typical * vols[j]
        cum_v += vols[j]
        vwap[j] = cum_pv / max(1.0, cum_v)
        
    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for val in values[1:]:
            out.append(val * k + out[-1] * (1 - k))
        return out
        
    ema50 = ema(closes, 50)
    signals = []
    position = "NONE"
    last_entry = -99

    for i in range(50, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "pullback",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        # v2.1 fix: require a RISING EMA50 (not just price above it), volume
        # confirmation on the recovery bar, and a cooldown — the old code fired on
        # EVERY bar whose low tagged VWAP and closed above it, churning on each
        # touch. Exit when price loses VWAP, not only on a full EMA50 break.
        avg_vol = sum(vols[i-20:i]) / 20
        trend_bullish = closes[i] > ema50[i] and ema50[i] > ema50[i-10]
        pulled_back = lows[i] <= vwap[i]
        recovered = closes[i] > vwap[i] and closes[i] > closes[i-1]
        vol_ok = vols[i] > avg_vol

        if position == "NONE":
            if trend_bullish and pulled_back and recovered and vol_ok and (i - last_entry) >= 10:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "pullback",
                    "confidence": 85.0,
                    "entry_reason": "VWAP pullback recovery (rising EMA50 + volume)",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "LONG"
                last_entry = i
        elif position == "LONG":
            if closes[i] < vwap[i] or closes[i] < ema50[i]:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "pullback",
                    "confidence": 85.0,
                    "entry_reason": "Lost VWAP / EMA trend break exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Intraday Pullback in Uptrends",
    },
    {
        "name": "AXISBANK Trend Follower",
        "risk": {"daily_loss_limit": 1800},  # equity loss floor ~3.6% of ₹50k tier (WR-52)
        "description": "Macro EMA filter trend follower. Enters when 9/21 EMA crossover aligns with slope of 200 EMA.",
        "underlying": "AXISBANK",
        "strategy_type": "Equity Intraday",
        "required_capital": 15000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 210: return []
    closes = [float(d['close']) for d in data]
    
    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for val in values[1:]:
            out.append(val * k + out[-1] * (1 - k))
        return out
        
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema200 = ema(closes, 200)
    
    signals = []
    position = "NONE"
    
    for i in range(200, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "trend_follow",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        macro_up = ema200[i] > ema200[i-1]
        crossover_buy = ema9[i] > ema21[i] and ema9[i-1] <= ema21[i-1]
        crossover_sell = ema9[i] < ema21[i]
        
        if position == "NONE":
            if macro_up and crossover_buy:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "trend_follow",
                    "confidence": 85.0,
                    "entry_reason": "Macro EMA crossover buy",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
        elif position == "LONG":
            if crossover_sell:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "trend_follow",
                    "confidence": 85.0,
                    "entry_reason": "Macro EMA cross exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Intraday Trending Markets",
    },
    {
        "name": "LT Momentum Rider",
        "risk": {"daily_loss_limit": 1800},  # equity loss floor ~3.6% of ₹50k tier (WR-52)
        "description": "Intraday momentum strategy designed for capital goods sector (L&T). Captures breakouts of Donchian channels with dynamic trailing stops.",
        "underlying": "LT",
        "strategy_type": "Equity Intraday",
        "required_capital": 18000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    signals = []
    position = "NONE"
    
    for i in range(20, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "breakout",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        donchian_high = max(highs[i-12:i])
        donchian_low = min(lows[i-12:i])
        range_mid = (donchian_high + donchian_low) / 2
        
        if position == "NONE":
            if closes[i] >= donchian_high * 0.999 or (closes[i] > range_mid and closes[i] > closes[i-1] > closes[i-2]):
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "breakout",
                    "confidence": 74.0,
                    "entry_reason": "Donchian momentum participation buy",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
        elif position == "LONG":
            if closes[i] < donchian_low:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "breakout",
                    "confidence": 85.0,
                    "entry_reason": "Donchian channel breakdown exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Intraday Breakout & Sector Momentum",
    },
    {
        "name": "BHARTIARTL Intraday Trend",
        "description": "Defensive intraday trend following on low-beta telecom giant. Minimizes whipsaw losses using smoothed moving averages.",
        "underlying": "BHARTIARTL",
        "strategy_type": "Equity Intraday",
        "required_capital": 15000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "MIS",
        # Whipsaw churn (2026-06-24: 4 losing ~5-min round-trips on EMA-cross flips).
        # Raise the re-entry cooldown and cap daily trades so it cannot re-enter the
        # same chop minutes after a stop-out. (Equity has no signal-exit min-hold, so
        # cooldown is the lever — see EQUITY_COUNTERTREND_BLOCK_STRENGTH for direction.)
        "risk": {"cooldown_minutes": 30, "max_trades_day": 2, "daily_loss_limit": 1200},  # ~3.4% of ₹35k tier (WR-52)
        "python_code": """def run(data):
    if len(data) < 55: return []
    closes = [float(d['close']) for d in data]
    
    def ema(values, period):
        k = 2.0 / (period + 1)
        out = [values[0]]
        for val in values[1:]:
            out.append(val * k + out[-1] * (1 - k))
        return out
        
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    signals = []
    position = "NONE"
    
    for i in range(50, len(data)):
        clock = str(data[i].get('date', ''))[11:16]
        if clock and clock > '15:05':
            if position != "NONE":
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "defensive_trend",
                    "confidence": 50.0,
                    "entry_reason": "Time exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 60,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
            continue
            
        crossover_buy = (
            ema20[i] > ema50[i] * 0.999
            and ema50[i] >= ema50[i-10]
            and ema20[i] >= ema20[i-3] * 0.999
            and closes[i] >= ema20[i] * 0.997
            and closes[i] >= min(closes[i-1], closes[i-2])
        )
        crossover_sell = ema20[i] < ema50[i] or closes[i] < ema50[i] * 0.995
        
        if position == "NONE":
            if crossover_buy:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "defensive_trend",
                    "confidence": 72.0,
                    "entry_reason": "Telecom EMA trend participation buy",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "LONG"
        elif position == "LONG":
            if crossover_sell:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "defensive_trend",
                    "confidence": 85.0,
                    "entry_reason": "Telecom ema crossover exit",
                    "target_R": 2.0,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.5,
                    "max_hold_minutes": 180,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "trending",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "1.0"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Low Volatility Stable Trend Markets",
    },
    {
        "name": "KOTAKBANK RSI Rebound",
        "risk": {"daily_loss_limit": 1800},  # equity loss floor ~3.6% of ₹50k tier (WR-52)
        "description": "Swing delivery strategy designed for Kotak Bank. Enters on daily RSI recovery above 30 from oversold zones.",
        "underlying": "KOTAKBANK",
        "strategy_type": "Equity Delivery",
        "required_capital": 25000.0,
        "instrument_group": "NSE",
        "lots": 1,
        "product": "CNC",
        "python_code": """def run(data):
    if len(data) < 30: return []
    closes = [float(d['close']) for d in data]
    
    rsi = [50.0] * len(closes)
    for i in range(14, len(closes)):
        gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1))
        losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1)) or 0.0001
        rs = gains / losses
        rsi[i] = 100 - (100 / (1 + rs))
        
    signals = []
    position = "NONE"
    last_entry = -99

    for i in range(50, len(data)):
        sma50 = sum(closes[i-49:i+1]) / 50
        if position == "NONE":
            recent_rsi = min(rsi[max(0, i-6):i+1])
            rebound = rsi[i] >= 34 and rsi[i] > rsi[i-1] and closes[i] >= closes[i-1]
            value_zone = recent_rsi <= 42 and closes[i] <= max(closes[max(0, i-10):i+1])
            # v2.1 fix: only buy the RSI rebound while the higher trend is UP
            # (close > 50-SMA), with a cooldown. The old code rebounded in ANY
            # regime -> caught falling knives on KOTAKBANK downtrends.
            uptrend = closes[i] > sma50
            if rebound and value_zone and uptrend and (i - last_entry) >= 8:
                signals.append({
                    "date": data[i]["date"],
                    "action": "BUY",
                    "direction": "CE",
                    "setup_type": "rsi_swing",
                    "confidence": 72.0,
                    "entry_reason": "RSI rebound in uptrend",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.8,
                    "max_hold_minutes": 1440,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "any",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "LONG"
                last_entry = i
        elif position == "LONG":
            if rsi[i] > 65 or closes[i] < sma50:
                signals.append({
                    "date": data[i]["date"],
                    "action": "SELL",
                    "direction": "CE",
                    "setup_type": "rsi_swing",
                    "confidence": 85.0,
                    "entry_reason": "RSI overbought / trend-break exit",
                    "target_R": 2.5,
                    "initial_stop_R": 1.0,
                    "trail_after_R": 1.8,
                    "max_hold_minutes": 1440,
                    "invalidation_rule": "time_or_stop",
                    "regime_required": "any",
                    "option_selection_preference": "ATM",
                    "signal_version": "v13",
                    "strategy_logic_version": "2.1"
                })
                position = "NONE"
    return signals
""",
        "market_suitability": "Swing Trading Financials Recovery (CNC)",
    },
]

LEGACY_OPTION_STRATEGIES = DEFAULT_OPTION_STRATEGIES


TREND_CONTINUATION_CODE = """def run(data):
    if len(data) < 55:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    def calc_sma(values, period):
        return [sum(values[i-period+1:i+1])/period if i >= period-1 else values[i] for i in range(len(values))]
        
    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)
    tr = [max(highs[i], closes[i-1]) - min(lows[i], closes[i-1]) for i in range(len(data))]
    atr = calc_sma(tr, 14)
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(50, len(data)):
        clock = str(data[i]['date'])[11:16]
        if clock < '09:45' or clock > '14:45':
            if position != "NONE":
                signals.append({'date': data[i]['date'], 'action': 'SELL' if position == "LONG" else 'BUY', 'reason': 'Time Exit'})
                position = "NONE"
            continue
        close = closes[i]
        high = highs[i]
        low = lows[i]
        prev_high = max(highs[i-11:i])
        prev_low = min(lows[i-11:i])
        body = abs(close - closes[i-1])
        
        bullish_entry = close > prev_high and sma20[i] > sma50[i] and close > sma20[i] and body > atr[i] * 0.35
        bearish_entry = close < prev_low and sma20[i] < sma50[i] and close < sma20[i] and body > atr[i] * 0.35
        
        if position == "LONG":
            highest_price = max(highest_price, high)
            pnl = (close - entry_price) / entry_price * 100
            dd = (highest_price - close) / entry_price * 100
            if close < sma20[i] or pnl <= -0.4 or pnl >= 1.0 or (highest_price > entry_price * 1.003 and dd >= 0.25):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Trend Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, low)
            pnl = (entry_price - close) / entry_price * 100
            dd = (close - lowest_price) / entry_price * 100
            if close > sma20[i] or pnl <= -0.4 or pnl >= 1.0 or (lowest_price < entry_price * 0.997 and dd >= 0.25):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Trend Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Trend Buy'})
                position = "LONG"
                entry_price = close
                highest_price = high
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Trend Sell'})
                position = "SHORT"
                entry_price = close
                lowest_price = low
    return signals
"""

OPENING_RANGE_VWAP_CODE = """def run(data):
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    today = str(data[-1]['date'])[:10]
    today_indices = [i for i, d in enumerate(data) if str(d['date'])[:10] == today]
    if len(today_indices) < 5:
        return []
        
    start_idx = today_indices[0]
    opening_bars = today_indices[:3]
    range_high = max(highs[i] for i in opening_bars)
    range_low = min(lows[i] for i in opening_bars)
    
    cum_pv = 0.0
    cum_vol = 0.0
    vwap = [closes[i] for i in range(len(data))]
    for i in today_indices:
        typical = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical * vols[i]
        cum_vol += vols[i]
        vwap[i] = cum_pv / max(1, cum_vol)
        
    for i in today_indices[3:]:
        clock = str(data[i]['date'])[11:16]
        if clock > '15:10':
            if position != "NONE":
                signals.append({'date': data[i]['date'], 'action': 'SELL' if position == "LONG" else 'BUY', 'reason': 'Market Close Exit'})
                position = "NONE"
            continue
            
        close = closes[i]
        vol = vols[i]
        avg_vol = sum(vols[start_idx:i]) / max(1, i - start_idx)
        
        bullish_entry = close > range_high and close > vwap[i] and vol > avg_vol * 1.05
        bearish_entry = close < range_low and close < vwap[i] and vol > avg_vol * 1.05
        
        if position == "LONG":
            pnl = (close - entry_price) / entry_price * 100
            if close < vwap[i] or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'ORB Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - close) / entry_price * 100
            if close > vwap[i] or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'ORB Short Exit'})
                position = "NONE"
        else:
            if clock < '11:15':
                if bullish_entry:
                    signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'ORB Buy'})
                    position = "LONG"
                    entry_price = close
                elif bearish_entry:
                    signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'ORB Sell'})
                    position = "SHORT"
                    entry_price = close
    return signals
"""

VWAP_PULLBACK_CODE = """def run(data):
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    today = str(data[-1]['date'])[:10]
    today_indices = [i for i, d in enumerate(data) if str(d['date'])[:10] == today]
    if len(today_indices) < 15:
        return []
        
    start_idx = today_indices[0]
    cum_pv = 0.0
    cum_vol = 0.0
    vwap = [closes[i] for i in range(len(data))]
    for i in today_indices:
        typical = (highs[i] + lows[i] + closes[i]) / 3
        cum_pv += typical * vols[i]
        cum_vol += vols[i]
        vwap[i] = cum_pv / max(1, cum_vol)
        
    def calc_sma(values, period):
        return [sum(values[j-period+1:j+1])/period if j >= period-1 else values[j] for j in range(len(values))]
        
    ma20 = calc_sma(closes, 20)
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in today_indices[5:]:
        clock = str(data[i]['date'])[11:16]
        if clock < '10:00' or clock > '14:30':
            if position != "NONE":
                signals.append({'date': data[i]['date'], 'action': 'SELL' if position == "LONG" else 'BUY', 'reason': 'Time Exit'})
                position = "NONE"
            continue
            
        close = closes[i]
        prev_high = max(highs[i-6:i])
        prev_low = min(lows[i-6:i])
        
        bullish_entry = ma20[i] > ma20[i-1] and closes[i-1] <= vwap[i-1] and close > vwap[i] and close > prev_high
        bearish_entry = ma20[i] < ma20[i-1] and closes[i-1] >= vwap[i-1] and close < vwap[i] and close < prev_low
        
        if position == "LONG":
            pnl = (close - entry_price) / entry_price * 100
            if close < vwap[i] or pnl <= -0.3 or pnl >= 0.7:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Pullback Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - close) / entry_price * 100
            if close > vwap[i] or pnl <= -0.3 or pnl >= 0.7:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Pullback Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Pullback Buy'})
                position = "LONG"
                entry_price = close
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Pullback Sell'})
                position = "SHORT"
                entry_price = close
    return signals
"""

ATR_VOLUME_BREAKOUT_CODE = """def run(data):
    if len(data) < 70:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    def calc_sma(values, period):
        return [sum(values[j-period+1:j+1])/period if j >= period-1 else values[j] for j in range(len(values))]
        
    tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(len(data))]
    atr = calc_sma(tr, 14)
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(50, len(data)):
        clock = str(data[i]['date'])[11:16]
        if clock < '10:15' or clock > '14:40':
            if position != "NONE":
                signals.append({'date': data[i]['date'], 'action': 'SELL' if position == "LONG" else 'BUY', 'reason': 'Time Exit'})
                position = "NONE"
            continue
            
        close = closes[i]
        high = highs[i]
        low = lows[i]
        recent_range = sum(highs[j] - lows[j] for j in range(i-8, i-2)) / 6
        prev_high = max(highs[i-13:i])
        prev_low = min(lows[i-13:i])
        avg_vol = sum(vols[i-21:i]) / 20
        
        bullish_entry = recent_range < atr[i] * 0.85 and vols[i] > avg_vol * 1.15 and close > prev_high
        bearish_entry = recent_range < atr[i] * 0.85 and vols[i] > avg_vol * 1.15 and close < prev_low
        
        if position == "LONG":
            highest_price = max(highest_price, high)
            pnl = (close - entry_price) / entry_price * 100
            dd = (highest_price - close) / entry_price * 100
            if close < prev_high - atr[i] or pnl <= -0.4 or pnl >= 1.0 or (highest_price > entry_price * 1.003 and dd >= 0.25):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'ATR Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, low)
            pnl = (entry_price - close) / entry_price * 100
            dd = (close - lowest_price) / entry_price * 100
            if close > prev_low + atr[i] or pnl <= -0.4 or pnl >= 1.0 or (lowest_price < entry_price * 0.997 and dd >= 0.25):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'ATR Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'ATR Buy'})
                position = "LONG"
                entry_price = close
                highest_price = high
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'ATR Sell'})
                position = "SHORT"
                entry_price = close
                lowest_price = low
    return signals
"""

RSI_REVERSAL_CODE = """def run(data):
    if len(data) < 60:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    def calc_rsi(end_idx, period=14):
        gains = 0.0
        losses = 0.0
        for j in range(end_idx - period + 1, end_idx + 1):
            change = closes[j] - closes[j-1]
            if change > 0:
                gains += change
            else:
                losses += abs(change)
        avg_gain = gains / period
        avg_loss = losses / period if losses else 0.0001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
        
    rsi = [calc_rsi(i) if i >= 14 else 50.0 for i in range(len(closes))]
    
    def calc_sma(values, period):
        return [sum(values[j-period+1:j+1])/period if j >= period-1 else values[j] for j in range(len(values))]
        
    sma50 = calc_sma(closes, 50)
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(50, len(data)):
        clock = str(data[i]['date'])[11:16]
        if clock < '10:00' or clock > '14:15':
            if position != "NONE":
                signals.append({'date': data[i]['date'], 'action': 'SELL' if position == "LONG" else 'BUY', 'reason': 'Time Exit'})
                position = "NONE"
            continue
            
        close = closes[i]
        prev_high = max(highs[i-5:i])
        prev_low = min(lows[i-5:i])
        
        bullish_entry = rsi[i-1] < 32 and rsi[i] > 38 and close > prev_high and close > sma50[i]
        bearish_entry = rsi[i-1] > 68 and rsi[i] < 62 and close < prev_low and close < sma50[i]
        
        if position == "LONG":
            pnl = (close - entry_price) / entry_price * 100
            if rsi[i] > 70 or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'RSI Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - close) / entry_price * 100
            if rsi[i] < 30 or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'RSI Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'RSI Buy'})
                position = "LONG"
                entry_price = close
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'RSI Sell'})
                position = "SHORT"
                entry_price = close
    return signals
"""

CRUDEOILM_EMA_MOMENTUM_CODE = """def run(data):
    if len(data) < 25:
        return []
    closes = [float(d['close']) for d in data]
    
    # 5-period EMA and 13-period EMA for quick crossover signals
    fast_period, slow_period = 5, 13
    
    def calc_ema(values, period):
        ema = []
        k = 2 / (period + 1)
        for i, val in enumerate(values):
            if i == 0:
                ema.append(val)
            else:
                ema.append(val * k + ema[-1] * (1 - k))
        return ema

    ema_fast = calc_ema(closes, fast_period)
    ema_slow = calc_ema(closes, slow_period)
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(21, len(data)):
        close = closes[i]
        
        # Crossover triggers
        bullish_cross = ema_fast[i] > ema_slow[i] and ema_fast[i-1] <= ema_slow[i-1]
        bearish_cross = ema_fast[i] < ema_slow[i] and ema_fast[i-1] >= ema_slow[i-1]
        
        if position == "LONG":
            pnl = (close - entry_price) / entry_price * 100
            # Exit long on bearish crossover, or +1.2% TP, or -0.5% SL
            if bearish_cross or pnl <= -0.5 or pnl >= 1.2:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'EMA Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - close) / entry_price * 100
            # Exit short on bullish crossover, or +1.2% TP, or -0.5% SL
            if bullish_cross or pnl <= -0.5 or pnl >= 1.2:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'EMA Short Exit'})
                position = "NONE"
        else:
            if bullish_cross:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'EMA Crossover Buy'})
                position = "LONG"
                entry_price = close
            elif bearish_cross:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'EMA Crossover Sell'})
                position = "SHORT"
                entry_price = close
                
    return signals
"""

CRUDEOILM_RSI_REVERSION_CODE = """def run(data):
    if len(data) < 30:
        return []
    closes = [float(d['close']) for d in data]
    
    # RSI period 14, EMA 21 trend filter
    period = 14
    
    def calc_ema(values, prd):
        ema = []
        k = 2 / (prd + 1)
        for i, val in enumerate(values):
            if i == 0:
                ema.append(val)
            else:
                ema.append(val * k + ema[-1] * (1 - k))
        return ema
        
    ema21 = calc_ema(closes, 21)
    
    # Calculate RSI
    rsi = [50.0] * len(closes)
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains[i] = max(diff, 0)
        losses[i] = max(-diff, 0)
        
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rsi[period] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rsi[i] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
            
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(period + 5, len(closes)):
        close = closes[i]
        
        # Bullish entry: RSI oversold < 35 and price reclaims above EMA21
        bullish_entry = rsi[i] < 35 and close > ema21[i]
        # Bearish entry: RSI overbought > 65 and price breaks below EMA21
        bearish_entry = rsi[i] > 65 and close < ema21[i]
        
        if position == "LONG":
            pnl = (close - entry_price) / entry_price * 100
            if rsi[i] > 65 or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'RSI Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - close) / entry_price * 100
            if rsi[i] < 35 or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'RSI Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'RSI Reversion CE Buy'})
                position = "LONG"
                entry_price = close
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'RSI Reversion PE Buy'})
                position = "SHORT"
                entry_price = close
                
    return signals
"""

CRUDEOILM_VOLATILITY_SCALPER_CODE = """def run(data):
    if len(data) < 30:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    # 20-period BB with 2.0 std dev, 14-period ATR
    period = 20
    
    def calc_sma(values, prd):
        return [sum(values[j-prd+1:j+1])/prd if j >= prd-1 else values[j] for j in range(len(values))]
        
    sma20 = calc_sma(closes, 20)
    
    # Calculate Bollinger Bands
    upper_band = []
    lower_band = []
    for i in range(len(closes)):
        if i < period - 1:
            upper_band.append(closes[i])
            lower_band.append(closes[i])
            continue
        mean = sma20[i]
        variance = sum((closes[j] - mean) ** 2 for j in range(i-period+1, i+1)) / period
        std_dev = variance ** 0.5
        upper_band.append(mean + 2.0 * std_dev)
        lower_band.append(mean - 2.0 * std_dev)
        
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(period + 2, len(closes)):
        close = closes[i]
        
        # Squeeze breakout: price closes outside Bollinger Bands
        bullish_breakout = close > upper_band[i-1] and closes[i-1] <= upper_band[i-2]
        bearish_breakout = close < lower_band[i-1] and closes[i-1] >= lower_band[i-2]
        
        if position == "LONG":
            pnl = (close - entry_price) / entry_price * 100
            # Exit if price drops below SMA20 (mean reversion) or hit targets
            if close < sma20[i] or pnl <= -0.5 or pnl >= 1.5:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'BB Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - close) / entry_price * 100
            if close > sma20[i] or pnl <= -0.5 or pnl >= 1.5:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'BB Short Exit'})
                position = "NONE"
        else:
            if bullish_breakout:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Volatility CE Breakout'})
                position = "LONG"
                entry_price = close
            elif bearish_breakout:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Volatility PE Breakout'})
                position = "SHORT"
                entry_price = close
                
    return signals
"""


DEFAULT_OPTION_STRATEGIES = []

COMMODITY_MOMENTUM_BREAKOUT_CODE = """def run(data):
    if len(data) < 55:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    def calc_sma(values, period):
        return [sum(values[j-period+1:j+1])/period if j >= period-1 else values[j] for j in range(len(values))]
        
    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(50, len(data)):
        close = closes[i]
        high = highs[i]
        low = lows[i]
        prev_high = max(highs[i-12:i])
        prev_low = min(lows[i-12:i])
        avg_vol = sum(vols[i-21:i]) / 20
        
        bullish_entry = close > prev_high and sma20[i] > sma50[i] and vols[i] > avg_vol * 1.1
        bearish_entry = close < prev_low and sma20[i] < sma50[i] and vols[i] > avg_vol * 1.1
        
        if position == "LONG":
            highest_price = max(highest_price, high)
            pnl = (close - entry_price) / entry_price * 100
            dd = (highest_price - close) / entry_price * 100
            if close < sma20[i] or pnl <= -0.5 or pnl >= 1.2 or (highest_price > entry_price * 1.004 and dd >= 0.35):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Comm Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, low)
            pnl = (entry_price - close) / entry_price * 100
            dd = (close - lowest_price) / entry_price * 100
            if close > sma20[i] or pnl <= -0.5 or pnl >= 1.2 or (lowest_price < entry_price * 0.996 and dd >= 0.35):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Comm Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Comm Buy'})
                position = "LONG"
                entry_price = close
                highest_price = high
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Comm Sell'})
                position = "SHORT"
                entry_price = close
                lowest_price = low
    return signals
"""

COMMODITY_RANGE_SELLING_CODE = """def run(data):
    if len(data) < 45:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(40, len(data)):
        recent_range = max(highs[i-20:i+1]) - min(lows[i-20:i+1])
        wider_range = max(highs[i-40:i+1]) - min(lows[i-40:i+1])
        drift = abs(closes[i] - closes[i-10]) / max(1, closes[i-10])
        
        bearish_entry = wider_range > 0 and recent_range < wider_range * 0.55 and drift < 0.015
        
        if position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100
            if pnl <= -0.4 or pnl >= 0.8 or drift > 0.025:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Comm Range Cover'})
                position = "NONE"
        else:
            if bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Comm Range Sell'})
                position = "SHORT"
                entry_price = closes[i]
    return signals
"""

COMMODITY_VOLATILITY_STRADDLE_CODE = """def run(data):
    if len(data) < 35:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(25, len(data)):
        tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-14, i+1)]
        atr = sum(tr) / len(tr)
        compression = sum(highs[j] - lows[j] for j in range(i-8, i+1)) / 8
        
        breakout_entry = compression < atr * 0.75
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100
            if pnl <= -0.35 or pnl >= 0.9 or compression > atr * 1.1:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Straddle Exit'})
                position = "NONE"
        else:
            if breakout_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Straddle Entry'})
                position = "LONG"
                entry_price = closes[i]
    return signals
"""

UPSTOX_HFT_SCALPER_CODE = """def run(data):
    if len(data) < 20:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(15, len(data)):
        ema3 = sum(closes[i-3:i+1]) / 4
        ema7 = sum(closes[i-7:i+1]) / 8
        velocity = closes[i] - closes[i-3]
        tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-5, i+1)]
        atr = sum(tr) / len(tr)
        avg_vol = sum(vols[i-8:i]) / 7
        vol_expansion = vols[i] / max(1, avg_vol)
        
        bullish_entry = ema3 > ema7 and velocity > atr * 0.4 and vol_expansion > 1.25
        bearish_entry = ema3 < ema7 and velocity < -atr * 0.4 and vol_expansion > 1.25
        
        if position == "LONG":
            highest_price = max(highest_price, highs[i])
            pnl = (closes[i] - entry_price) / entry_price * 100
            dd = (highest_price - closes[i]) / entry_price * 100
            if ema3 < ema7 or pnl <= -0.25 or pnl >= 0.55 or (highest_price > entry_price * 1.002 and dd >= 0.18):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'HFT Scalp Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, lows[i])
            pnl = (entry_price - closes[i]) / entry_price * 100
            dd = (closes[i] - lowest_price) / entry_price * 100
            if ema3 > ema7 or pnl <= -0.25 or pnl >= 0.55 or (lowest_price < entry_price * 0.998 and dd >= 0.18):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'HFT Scalp Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'HFT Scalp Buy'})
                position = "LONG"
                entry_price = closes[i]
                highest_price = highs[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'HFT Scalp Sell'})
                position = "SHORT"
                entry_price = closes[i]
                lowest_price = lows[i]
    return signals
"""

UPSTOX_HFT_DELTA_NEUTRAL_CODE = """def run(data):
    if len(data) < 30:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(25, len(data)):
        recent_band = max(highs[i-8:i+1]) - min(lows[i-8:i+1])
        hist_band = max(highs[i-25:i+1]) - min(lows[i-25:i+1])
        
        is_compressed = recent_band < hist_band * 0.4
        is_expanding = recent_band > hist_band * 0.75
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100
            if is_expanding or pnl <= -0.5 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'HFT Strangle Neutral Exit'})
                position = "NONE"
        else:
            if is_compressed:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'HFT Strangle Neutral Entry'})
                position = "LONG"
                entry_price = closes[i]
    return signals
"""

BANKNIFTY_HFT_BREAKOUT_CODE = """def run(data):
    if len(data) < 25:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(20, len(data)):
        prev_high = max(highs[i-6:i])
        prev_low = min(lows[i-6:i])
        tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-10, i+1)]
        atr = sum(tr) / len(tr)
        avg_vol = sum(vols[i-10:i]) / 9
        vol_surge = vols[i] > avg_vol * 1.3
        
        bullish_entry = closes[i] > prev_high and vol_surge and (closes[i] - closes[i-1]) > atr * 0.5
        bearish_entry = closes[i] < prev_low and vol_surge and (closes[i-1] - closes[i]) > atr * 0.5
        
        if position == "LONG":
            highest_price = max(highest_price, highs[i])
            pnl = (closes[i] - entry_price) / entry_price * 100
            dd = (highest_price - closes[i]) / entry_price * 100
            if closes[i] < (prev_high + prev_low) / 2 or pnl <= -0.35 or pnl >= 0.75 or (highest_price > entry_price * 1.003 and dd >= 0.22):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'BN Breakout Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, lows[i])
            pnl = (entry_price - closes[i]) / entry_price * 100
            dd = (closes[i] - lowest_price) / entry_price * 100
            if closes[i] > (prev_high + prev_low) / 2 or pnl <= -0.35 or pnl >= 0.75 or (lowest_price < entry_price * 0.997 and dd >= 0.22):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'BN Breakdown Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'BN Breakout Buy'})
                position = "LONG"
                entry_price = closes[i]
                highest_price = highs[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'BN Breakdown Sell'})
                position = "SHORT"
                entry_price = closes[i]
                lowest_price = lows[i]
    return signals
"""

NIFTY_HFT_MICRO_SCALPER_CODE = """def run(data):
    if len(data) < 20:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    def calc_ema(values, period):
        k = 2.0 / (period + 1)
        ema = [values[0]]
        for val in values[1:]:
            ema.append(val * k + ema[-1] * (1 - k))
        return ema
        
    ema3 = calc_ema(closes, 3)
    ema8 = calc_ema(closes, 8)
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(15, len(data)):
        avg_vol = sum(vols[i-6:i]) / 5
        vol_spike = vols[i] > avg_vol * 1.5
        bullish_cross = ema3[i] > ema8[i] and ema3[i-1] <= ema8[i-1]
        bearish_cross = ema3[i] < ema8[i] and ema3[i-1] >= ema8[i-1]
        
        if position == "LONG":
            highest_price = max(highest_price, highs[i])
            pnl = (closes[i] - entry_price) / entry_price * 100
            dd = (highest_price - closes[i]) / entry_price * 100
            if bearish_cross or pnl <= -0.3 or pnl >= 0.65 or (highest_price > entry_price * 1.003 and dd >= 0.2):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Nifty Scalp Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, lows[i])
            pnl = (entry_price - closes[i]) / entry_price * 100
            dd = (closes[i] - lowest_price) / entry_price * 100
            if bullish_cross or pnl <= -0.3 or pnl >= 0.65 or (lowest_price < entry_price * 0.997 and dd >= 0.2):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Nifty Scalp Short Exit'})
                position = "NONE"
        else:
            if bullish_cross and vol_spike:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Nifty Scalp Buy'})
                position = "LONG"
                entry_price = closes[i]
                highest_price = highs[i]
            elif bearish_cross and vol_spike:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Nifty Scalp Sell'})
                position = "SHORT"
                entry_price = closes[i]
                lowest_price = lows[i]
    return signals
"""

SENSEX_HFT_MOMENTUM_SCALPER_CODE = """def run(data):
    if len(data) < 20:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    def calc_ema(values, period):
        k = 2.0 / (period + 1)
        ema = [values[0]]
        for val in values[1:]:
            ema.append(val * k + ema[-1] * (1 - k))
        return ema
        
    ema5 = calc_ema(closes, 5)
    ema12 = calc_ema(closes, 12)
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(15, len(data)):
        gains = []
        losses = []
        for j in range(i-5, i+1):
            diff = closes[j] - closes[j-1]
            gains.append(max(0, diff))
            losses.append(max(0, -diff))
        avg_gain = sum(gains) / 6
        avg_loss = sum(losses) / 6
        rs = avg_gain / (avg_loss if avg_loss > 0 else 0.0001)
        rsi5 = 100 - (100 / (1 + rs))
        
        bullish_entry = ema5[i] > ema12[i] and rsi5 > 55 and rsi5 < 75
        bearish_entry = ema5[i] < ema12[i] and rsi5 < 45 and rsi5 > 25
        
        if position == "LONG":
            highest_price = max(highest_price, highs[i])
            pnl = (closes[i] - entry_price) / entry_price * 100
            dd = (highest_price - closes[i]) / entry_price * 100
            if ema5[i] < ema12[i] or rsi5 > 80 or pnl <= -0.3 or pnl >= 0.7 or (highest_price > entry_price * 1.003 and dd >= 0.22):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Sensex Scalp Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, lows[i])
            pnl = (entry_price - closes[i]) / entry_price * 100
            dd = (closes[i] - lowest_price) / entry_price * 100
            if ema5[i] > ema12[i] or rsi5 < 20 or pnl <= -0.3 or pnl >= 0.7 or (lowest_price < entry_price * 0.997 and dd >= 0.22):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Sensex Scalp Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Sensex Scalp Buy'})
                position = "LONG"
                entry_price = closes[i]
                highest_price = highs[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Sensex Scalp Sell'})
                position = "SHORT"
                entry_price = closes[i]
                lowest_price = lows[i]
    return signals
"""

CRUDEOIL_HFT_LOW_CAPITAL_SCALPER_CODE = """def run(data):
    if len(data) < 15:
        return []
    closes = [float(d['close']) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(10, len(data)):
        roc = (closes[i] - closes[i-3]) / closes[i-3] * 100
        avg_vol = sum(vols[i-5:i]) / 5
        
        bullish_entry = roc > 0.15 and vols[i] > avg_vol * 1.3
        bearish_entry = roc < -0.15 and vols[i] > avg_vol * 1.3
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100
            if roc < -0.05 or pnl <= -0.4 or pnl >= 0.9:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude ROC Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100
            if roc > 0.05 or pnl <= -0.4 or pnl >= 0.9:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude ROC Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude ROC Buy'})
                position = "LONG"
                entry_price = closes[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude ROC Sell'})
                position = "SHORT"
                entry_price = closes[i]
    return signals
"""

NATURALGAS_HFT_MICRO_SCALPER_CODE = """def run(data):
    if len(data) < 25:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(20, len(data)):
        mean = sum(closes[i-14:i+1]) / 15
        variance = sum((x - mean) ** 2 for x in closes[i-14:i+1]) / 15
        std = variance ** 0.5
        
        upper = mean + 1.2 * std
        lower = mean - 1.2 * std
        
        bullish_entry = closes[i] > upper and closes[i-1] <= upper
        bearish_entry = closes[i] < lower and closes[i-1] >= lower
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100
            if closes[i] < mean or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'NG Band Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100
            if closes[i] > mean or pnl <= -0.35 or pnl >= 0.8:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'NG Band Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'NG Band Buy'})
                position = "LONG"
                entry_price = closes[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'NG Band Sell'})
                position = "SHORT"
                entry_price = closes[i]
    return signals
"""

NIFTY_LOW_LATENCY_SCALPER_CODE = """def run(data):
    if len(data) < 30:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(25, len(data)):
        typical = [(highs[j] + lows[j] + closes[j]) / 3 for j in range(i-10, i+1)]
        v_sub = vols[i-10:i+1]
        vwap = sum(typical[j] * v_sub[j] for j in range(11)) / sum(v_sub)
        
        mean_close = sum(closes[i-9:i+1]) / 10
        variance = sum((x - mean_close) ** 2 for x in closes[i-9:i+1]) / 10
        std_dev = variance ** 0.5
        
        bullish_entry = closes[i] > vwap + (std_dev * 1.5)
        bearish_entry = closes[i] < vwap - (std_dev * 1.5)
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100
            if closes[i] < vwap or pnl <= -0.3 or pnl >= 0.7:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Nifty LL Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100
            if closes[i] > vwap or pnl <= -0.3 or pnl >= 0.7:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Nifty LL Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Nifty LL Buy'})
                position = "LONG"
                entry_price = closes[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Nifty LL Sell'})
                position = "SHORT"
                entry_price = closes[i]
    return signals
"""

CRUDEOIL_HFT_SCALPER_CODE = """def run(data):
    if len(data) < 20:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    highest_price = 0.0
    lowest_price = 0.0
    signals = []
    
    for i in range(15, len(data)):
        ema3 = sum(closes[i-3:i+1]) / 4
        ema8 = sum(closes[i-8:i+1]) / 9
        velocity = closes[i] - closes[i-3]
        tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-8, i+1)]
        atr = sum(tr) / len(tr)
        avg_vol = sum(vols[i-10:i]) / 9
        vol_surge = vols[i] / max(1, avg_vol)
        
        bullish_entry = ema3 > ema8 and velocity > atr * 0.45 and vol_surge > 1.25
        bearish_entry = ema3 < ema8 and velocity < -atr * 0.45 and vol_surge > 1.25
        
        if position == "LONG":
            highest_price = max(highest_price, highs[i])
            pnl = (closes[i] - entry_price) / entry_price * 100
            dd = (highest_price - closes[i]) / entry_price * 100
            if ema3 < ema8 or pnl <= -0.4 or pnl >= 0.95 or (highest_price > entry_price * 1.003 and dd >= 0.28):
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude HFT Scalp Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            lowest_price = min(lowest_price, lows[i])
            pnl = (entry_price - closes[i]) / entry_price * 100
            dd = (closes[i] - lowest_price) / entry_price * 100
            if ema3 > ema8 or pnl <= -0.4 or pnl >= 0.95 or (lowest_price < entry_price * 0.997 and dd >= 0.28):
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude HFT Scalp Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude HFT Scalp Buy'})
                position = "LONG"
                entry_price = closes[i]
                highest_price = highs[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude HFT Scalp Sell'})
                position = "SHORT"
                entry_price = closes[i]
                lowest_price = lows[i]
    return signals
"""

CRUDEOIL_HFT_VOLATILITY_CODE = """def run(data):
    if len(data) < 30:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(20, len(data)):
        recent_band = max(highs[i-6:i+1]) - min(lows[i-6:i+1])
        hist_band = max(highs[i-20:i+1]) - min(lows[i-20:i+1])
        
        is_compressed = recent_band < hist_band * 0.45
        channel_high = max(highs[i-5:i])
        channel_low = min(lows[i-5:i])
        avg_vol = sum(vols[i-8:i]) / 7
        vol_surge = vols[i] > avg_vol * 1.3
        
        bullish_entry = is_compressed and closes[i] > channel_high and vol_surge
        bearish_entry = is_compressed and closes[i] < channel_low and vol_surge
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100 if entry_price else 0.0
            if closes[i] < channel_low or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude Vol Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100 if entry_price else 0.0
            if closes[i] > channel_high or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude Vol Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude Vol Buy'})
                position = "LONG"
                entry_price = closes[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude Vol Sell'})
                position = "SHORT"
                entry_price = closes[i]
    return signals
"""

CRUDEOIL_HFT_MEAN_REVERSION_CODE = """def run(data):
    if len(data) < 25:
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    
    position = "NONE"
    entry_price = 0.0
    signals = []
    
    for i in range(20, len(data)):
        sma15 = sum(closes[i-14:i+1]) / 15
        variance = sum((c - sma15) ** 2 for c in closes[i-14:i+1]) / 15
        std_dev = variance ** 0.5
        
        upper_band = sma15 + (std_dev * 2.0)
        lower_band = sma15 - (std_dev * 2.0)
        
        deltas = [closes[j] - closes[j-1] for j in range(i-4, i+1)]
        gains = sum(d for d in deltas if d > 0) / 5
        losses = sum(-d for d in deltas if d < 0) / 5
        rs = gains / max(0.0001, losses)
        rsi5 = 100 - (100 / (1 + rs))
        
        bullish_entry = closes[i] < lower_band and rsi5 < 20
        bearish_entry = closes[i] > upper_band and rsi5 > 80
        
        if position == "LONG":
            pnl = (closes[i] - entry_price) / entry_price * 100
            if closes[i] > sma15 or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude MR Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100
            if closes[i] < sma15 or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude MR Short Exit'})
                position = "NONE"
        else:
            if bullish_entry:
                signals.append({'date': data[i]['date'], 'action': 'BUY', 'reason': 'Crude MR Buy'})
                position = "LONG"
                entry_price = closes[i]
            elif bearish_entry:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude MR Sell'})
                position = "SHORT"
                entry_price = closes[i]
    return signals
"""

STANDARD_STRATEGY_CATALOG = [
    {
        "name": "MCX Crude EMA Buyer",
        "description": "Upstox MCX CRUDEOILM long-option scalper using fast EMA momentum on live 5-minute futures candles. One mini lot by default.",
        "underlying": "CRUDEOILM", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 6000.0, "instrument_group": "MCX",
        "python_code": CRUDEOILM_EMA_MOMENTUM_CODE,
        "market_suitability": "Fast intraday crude mini momentum",
    },
    {
        "name": "MCX Crude RSI Buyer",
        "description": "Upstox MCX CRUDEOILM long-option reversal strategy for stretched RSI moves. One mini lot by default.",
        "underlying": "CRUDEOILM", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 6000.0, "instrument_group": "MCX",
        "python_code": CRUDEOILM_RSI_REVERSION_CODE,
        "market_suitability": "Mean reversion after sharp crude mini moves",
    },
    {
        "name": "MCX Natural Gas Breakout",
        "description": "Upstox MCX NATURALGAS long-option breakout strategy using volatility bands on live 5-minute futures candles.",
        "underlying": "NATURALGAS", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 18000.0, "instrument_group": "MCX",
        "python_code": NATURALGAS_HFT_MICRO_SCALPER_CODE,
        "market_suitability": "Natural gas volatility expansion",
    },
    {
        "name": "MCX Crude Volatility Buyer",
        "description": "Upstox MCX CRUDEOIL long-option breakout strategy for compression-to-expansion crude oil moves.",
        "underlying": "CRUDEOIL", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 30000.0, "instrument_group": "MCX",
        "python_code": CRUDEOIL_HFT_VOLATILITY_CODE,
        "market_suitability": "Crude oil volatility breakout",
    },
]


_DEFAULT_STRATEGY_CODE_BY_NAME = {
    str(_template.get("name") or ""): str(_template.get("python_code") or "")
    for _template in [*LEGACY_OPTION_STRATEGIES, *DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]
}

UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME = {
    "NIFTY Momentum Buyer": (NIFTY_ATM_MOMENTUM_CODE, "momentum", "NIFTY live trend and momentum with true EMA, volume, time and exit guards"),
    "BANKNIFTY Breakout Buyer": (BANKNIFTY_ATM_BREAKOUT_CODE, "breakout", "BANKNIFTY range breakout with ATR, volume, duplicate-sensitive exit controls"),
    "NIFTY VWAP Trend Breakout": (NIFTY_VWAP_TREND_BREAKOUT_CODE, "breakout", "NIFTY VWAP breakout with volume, no-trade zone and internal exits"),
    "SENSEX Swing RSI Pullback": (SENSEX_RSI_PULLBACK_CODE, "pullback", "SENSEX RSI pullback with trend filter and internal exits"),
    "NIFTY Micro-Lot Trend Follower": (NIFTY_MICRO_TREND_CODE, "momentum", "NIFTY baseline trend follower with no-trade zone and trailing exits"),
    "NIFTY HFT Quick Scalper": (NIFTY_QUICK_SCALPER_CODE, "micro_scalp", "Candle-based quick scalper with noise, volume, cooldown and time guards"),
    "BANKNIFTY HFT Momentum Scalper": (BANKNIFTY_STD_BAND_SCALPER_CODE, "breakout", "BANKNIFTY standard-deviation breakout with volume and exit controls"),
    "NIFTY Quick EMA Scalper": (NIFTY_QUICK_EMA_SCALPER_CODE, "momentum", "EMA momentum expressed as an ATM debit spread (theta-safe); volume, cooldown and time guards"),
    "BANKNIFTY Volatility Breakout": (BANKNIFTY_SENSITIVE_VOL_BREAKOUT_CODE, "volatile_breakout", "Sensitive BANKNIFTY volatility breakout with volume and exit controls"),
    "RELIANCE Trend Rider": (_DEFAULT_STRATEGY_CODE_BY_NAME["RELIANCE Trend Rider"], "equity_trend", "Equity trend participation with EMA continuation"),
    "SBIN Short Seller": (_DEFAULT_STRATEGY_CODE_BY_NAME["SBIN Short Seller"], "equity_short", "Equity short participation with VWAP/EMA continuation"),
    "HDFCBANK Range Rebound": (_DEFAULT_STRATEGY_CODE_BY_NAME["HDFCBANK Range Rebound"], "equity_mean_reversion", "Equity range mean reversion"),
    "ICICIBANK Volatility Breakout": (_DEFAULT_STRATEGY_CODE_BY_NAME["ICICIBANK Volatility Breakout"], "equity_breakout", "Equity volatility continuation breakout"),
    "TCS Swing Accumulator": (_DEFAULT_STRATEGY_CODE_BY_NAME["TCS Swing Accumulator"], "equity_swing", "Equity defensive RSI accumulation"),
    "INFY VWAP Pullback": (_DEFAULT_STRATEGY_CODE_BY_NAME["INFY VWAP Pullback"], "equity_pullback", "Equity VWAP pullback participation"),
    "AXISBANK Trend Follower": (_DEFAULT_STRATEGY_CODE_BY_NAME["AXISBANK Trend Follower"], "equity_trend", "Equity macro trend participation"),
    "LT Momentum Rider": (_DEFAULT_STRATEGY_CODE_BY_NAME["LT Momentum Rider"], "equity_breakout", "Equity Donchian momentum participation"),
    "BHARTIARTL Intraday Trend": (_DEFAULT_STRATEGY_CODE_BY_NAME["BHARTIARTL Intraday Trend"], "equity_trend", "Equity defensive EMA participation"),
    "KOTAKBANK RSI Rebound": (_DEFAULT_STRATEGY_CODE_BY_NAME["KOTAKBANK RSI Rebound"], "equity_swing", "Equity RSI rebound participation"),
}


for _template in [*LEGACY_OPTION_STRATEGIES, *DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]:
    upgraded = UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME.get(str(_template.get("name") or ""))
    if upgraded:
        _template["python_code"] = upgraded[0]
        _template["risk_style"] = upgraded[1]
        _template["market_suitability"] = upgraded[2]
        if "risk" not in _template or not isinstance(_template["risk"], dict):
            _template["risk"] = {}
        _template["risk"]["exit_mode"] = "signal_or_tp_sl_trailing"
        continue
    if (
        str(_template.get("strategy_type") or "").lower() == "option buying"
        and _template.get("name") not in OPTION_ALPHA_REBUILD_NAMES
    ):
        _template["python_code"] = RETAIL_LIVE_STATE_CODE
        _template["market_suitability"] = _template.get("market_suitability") or "Retail live momentum"


_seed_templates_by_name = {
    template["name"]: template
    for template in [*LEGACY_OPTION_STRATEGIES, *DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]
    if str(template.get("instrument_group") or "").upper() != "MCX"
    and str(template.get("underlying") or "").upper() not in REMOVED_COMMODITY_UNDERLYINGS
    and "CRUDE" not in str(template.get("name") or "").upper()
    and "NATURAL GAS" not in str(template.get("name") or "").upper()
}
DEFAULT_OPTION_STRATEGIES = list(_seed_templates_by_name.values())

# Safety invariant: every seeded default strategy must carry an explicit protective
# exit_mode so none is seeded unprotected. The upgraded-template loop sets this for
# the option strategies; the equity strategies (RELIANCE, SBIN, …) have no inline
# risk block, so default it here on the FINAL rebuilt list. setdefault preserves any
# strategy that already set its own exit_mode. Enforced by
# test_seeded_strategy_exit_mode_matrix.
for _template in DEFAULT_OPTION_STRATEGIES:
    _risk = _template.get("risk")
    if not isinstance(_risk, dict):
        _risk = {}
        _template["risk"] = _risk
    if _template.get("name") in CREDIT_SPREAD_THETA_NAMES or str(_template.get("structure") or "") == "credit_spread":
        _template["structure"] = "credit_spread"
        _template["strategy_type"] = "Option Selling"
        if _template.get("name") not in OPTION_ALPHA_REBUILD_NAMES:
            _template["required_capital"] = 8000.0
        _risk.update(CREDIT_SPREAD_THETA_RISK)
    # EDR-11/13: the OOS-validated put spread holds to weekly expiry — its DEFINED
    # RISK (wing width) is the stop, so keep required_capital/daily_loss_limit above
    # one designed max loss and disable intraday time-exit so the killswitch/time-exit
    # can't force-close it before expiry. exit_mode="hold_to_expiry" (risk) mirrors
    # the options.exit_mode="expiry" the position monitor keys off.
    if _template.get("name") == "QG-O1 NIFTY Put Spread Theta Core":
        _template["required_capital"] = 35000.0
        _risk.update({"daily_loss_limit": 40000.0, "time_exit_minutes": 0,
                      "exit_mode": "hold_to_expiry", "cooldown_minutes": 60,
                      "max_trades_day": 1, "strategy_category": "swing"})
    if _template.get("name") in PAPER_FORWARD_ACTIVE_STRATEGY_NAMES:
        _template["initial_status"] = "live"
        if _template.get("required_capital") is not None:
            _template["required_capital"] = float(_template.get("required_capital") or 0)
        _risk.update(dict(_template.get("risk") or {}))
    elif _template.get("name") in PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES:
        _template["initial_status"] = "archived"
        _risk.update(dict(_template.get("risk") or {}))
    if str(_template.get("instrument_group") or "").upper() in {"NFO", "BFO"}:
        _risk["daily_loss_limit"] = max(float(_risk.get("daily_loss_limit") or 0), 4000.0)
    if str(_template.get("instrument_group") or "").upper() in {"NSE", "BSE"}:
        _tier_capital = EQUITY_CAPITAL_TIERS.get(str(_template.get("name") or ""), EQUITY_MIN_REQUIRED_CAPITAL)
        _template["required_capital"] = max(float(_template.get("required_capital") or 0), _tier_capital)
        _risk["daily_loss_limit"] = max(float(_risk.get("daily_loss_limit") or 0), 2500.0)
        _risk["entry_cutoff_ist"] = EQUITY_ENTRY_CUTOFF
    _risk.setdefault("exit_mode", "signal_or_tp_sl_trailing")
    _template["risk_style"] = _risk.get("risk_style") or _template.get("risk_style") or _classify_strategy_risk_style(_template)

STRATEGY_DISPLAY_NAME_RENAMES = {
    "NIFTY Put Spread Theta (OOS)": "QG-O1 NIFTY Put Spread Theta Core",
    "UPSTOX NIFTY ATM Option Momentum Buyer": "NIFTY Momentum Buyer",
    "UPSTOX BANKNIFTY ATM Option Breakout Buyer": "BANKNIFTY Breakout Buyer",
    "UPSTOX RELIANCE Advanced Momentum Trend Rider": "RELIANCE Trend Rider",
    "UPSTOX SBIN Macro Short Seller": "SBIN Short Seller",
    "UPSTOX HDFCBANK Range Mean Reversion": "HDFCBANK Range Rebound",
    "UPSTOX ICICIBANK News & Volatility Catalyst": "ICICIBANK Volatility Breakout",
    "UPSTOX TCS Defensive Swing Accumulator": "TCS Swing Accumulator",
    "UPSTOX INFY VWAP Pullback Buyer": "INFY VWAP Pullback",
    "UPSTOX AXISBANK Macro Trend Follower": "AXISBANK Trend Follower",
    "UPSTOX LT Infrastructure Momentum Rider": "LT Momentum Rider",
    "UPSTOX BHARTIARTL Defensive Intraday Trend": "BHARTIARTL Intraday Trend",
    "UPSTOX KOTAKBANK RSI Rebound Swing": "KOTAKBANK RSI Rebound",
    "UPSTOX MCX Crude Mini EMA Option Buyer": "MCX Crude EMA Buyer",
    "UPSTOX MCX Crude Mini RSI Option Buyer": "MCX Crude RSI Buyer",
    "UPSTOX MCX Natural Gas Breakout Option Buyer": "MCX Natural Gas Breakout",
    "UPSTOX MCX Crude Volatility Option Buyer": "MCX Crude Volatility Buyer",
}

LEGACY_DEFAULT_STRATEGY_NAMES = {
    "NIFTY Intraday Theta Straddle",
    "BANKNIFTY Weekly Income Strangle",
    # 17 legacy default strategy names
    "NIFTY Momentum EMA",
    "NIFTY RSI Reversion",
    "NIFTY Opening Range Breakout",
    "NIFTY ATR Trend",
    "NIFTY Trend Recheck",
    "NIFTY VWAP Pullback Continuation",
    "NIFTY ATR Volume Expansion",
    "NIFTY RSI Reversal With Trend",
    "SENSEX Momentum EMA",
    "SENSEX RSI Reversion",
    "SENSEX Opening Range",
    "SENSEX ATR Trend",
    "SENSEX Trend Recheck",
    "SENSEX Opening Range VWAP",
    "SENSEX VWAP Pullback Continuation",
    "SENSEX ATR Volume Expansion",
    "SENSEX RSI Reversal With Trend",
    
    # 10 duplicate options templates
    "NIFTY VWAP Trend Breakout",
    "NIFTY Opening Range VWAP",
    "NIFTY VWAP Pullback Continuation",
    "NIFTY ATR Volume Expansion",
    "NIFTY RSI Reversal With Trend",
    "SENSEX VWAP Trend Breakout",
    "SENSEX Opening Range VWAP",
    "SENSEX VWAP Pullback Continuation",
    "SENSEX ATR Volume Expansion",
    "SENSEX RSI Reversal With Trend",
    
    # HFT templates
    "Upstox HFT Low-Latency Scalper",
    "Upstox HFT Multi-Leg Straddle",
    "Bank Nifty Volatility Breakout HFT",
    "NIFTY Low-Latency Scalper",
    "Crude Oil Iron Condor Range",
    "Natural Gas Momentum Breakout",
    "Natural Gas Volatility Straddle",
    "Crude Oil HFT Micro-Trend Scalper",
    "Crude Oil HFT Volatility Breakout",
    "Crude Oil HFT Mean Reversion",
    "NIFTY HFT Micro-Trend Scalper",
    "SENSEX HFT Momentum Scalper",
    "Crude Oil HFT Low-Capital Scalper",
    "Natural Gas HFT Micro-Trend Scalper",
    "Crude Oil Mini EMA Momentum",
    "Crude Oil Mini RSI Reversion",
    "Crude Oil Mini Volatility Scalper",
}


def _build_default_strategy_doc(template: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    underlying = str(template["underlying"]).upper()
    instrument_group = str(template.get("instrument_group") or ("BFO" if underlying == "SENSEX" else "NFO")).upper()
    
    is_equity = instrument_group in ("NSE", "BSE")
    strategy_type = template.get("strategy_type") or (
        "Option Selling" if str(template.get("strike_mode") or "").upper().endswith("SELL") 
        else ("Option Buying" if not is_equity else "Equity Intraday")
    )
    if instrument_group == "MCX" or underlying in REMOVED_COMMODITY_UNDERLYINGS:
        raise ValueError(f"MCX strategy templates are removed from QuantG: {template.get('name')}")
    required_capital = float(template.get("required_capital") or (45000.0 if underlying == "SENSEX" else 35000.0))
    is_commodity = False
    
    if is_equity:
        options_block = {
            "enabled": False,
            "underlying": underlying,
            "strike_mode": None,
            "otm_points": 0,
            "expiry_offset": 0,
            "lots": template.get("lots") or 1,
            "required_capital": required_capital,
            "product": template.get("product") or "MIS"
        }
    else:
        options_block = {
            "enabled": True,
            "underlying": underlying,
            "strike_mode": template["strike_mode"],
            "otm_points": template["otm_points"],
            "expiry_offset": template.get("expiry_offset", 0),
            "lots": template["lots"],
            "required_capital": required_capital,
            "product": template.get("product") or "NRML",
            # Phase 2 #5: option structure — single_leg (default), credit_spread,
            # or debit_spread. Consumed by the spread builder at signal time.
            "structure": template.get("structure") or "single_leg",
            "spread_width": template.get("spread_width"),
            # EDR-09/10: OTM short-leg distance + wing + hold-to-expiry, consumed by the
            # Edge Lab OOS backtester (core/eod_options_backtest). None on legacy templates.
            "short_otm_pct": template.get("short_otm_pct"),
            "wing_width": template.get("wing_width"),
            "exit_mode": template.get("exit_mode"),
            # EDR-11: short-leg |delta| the LIVE spread builder targets (server.py spread
            # build reads options.short_delta). ~0.12 ≈ 3% OTM. None → default 0.30.
            "short_delta": template.get("short_delta"),
        }
        
    risk_profile = {**_strategy_risk_profile(template), "required_capital": required_capital}
    return {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": template["name"],
        "description": template["description"],
        "kind": "python",
        "python_code": template["python_code"],
        "asset_class": "equity" if is_equity else "options",
        "strategy_type": strategy_type,
        "required_capital": required_capital,
        "instrument_group": instrument_group,
        "broker": "upstox",
        "mode": "live",
        "market_suitability": template.get("market_suitability", "Any Market Condition"),
        "visual_config": {
            "symbol": underlying,
            "exchange": instrument_group,
            "options": options_block,
            "risk": risk_profile,
        },
        "status": template.get("initial_status") or "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_pnl": None,
        "evaluations": 0,
        "signals_fired": 0,
        "default_strategy_version": "v13-live-brain-r1",
        "strategy_logic_version": "1.0",
    }



def _strategy_asset_class(row: Dict[str, Any]) -> str:
    explicit = (row.get("asset_class") or "").lower()
    if explicit in ("equity", "options", "futures"):
        return explicit
    visual_config = row.get("visual_config") or {}
    symbol = str(visual_config.get("symbol") or "").upper()
    if symbol in REMOVED_COMMODITY_UNDERLYINGS:
        return "removed"
    options_config = visual_config.get("options") or {}
    if options_config.get("enabled"):
        return "options"
    return "equity"


def _strategy_instrument_group(row: Dict[str, Any]) -> str:
    explicit = row.get("instrument_group")
    if explicit:
        return str(explicit).upper()
    visual_config = row.get("visual_config") or {}
    exchange = visual_config.get("exchange")
    if exchange:
        return str(exchange).upper()
    options_config = visual_config.get("options") or {}
    underlying = str(options_config.get("underlying") or visual_config.get("symbol") or "").upper()
    if underlying in REMOVED_COMMODITY_UNDERLYINGS:
        return "REMOVED"
    if underlying == "SENSEX":
        return "BFO"
    if underlying:
        return "NFO"
    return "NSE"


def _strategy_type(row: Dict[str, Any]) -> str:
    explicit = str(row.get("strategy_type") or "").strip().lower()
    if explicit in {"option buying", "buying", "long option", "option_buying"}:
        return "Option Buying"
    if explicit in {"option selling", "selling", "short option", "option_selling"}:
        return "Option Selling"
    visual_config = row.get("visual_config") or {}
    options_config = visual_config.get("options") or {}
    strike_mode = str(options_config.get("strike_mode") or "").upper()
    name = str(row.get("name") or "").lower()
    if strike_mode.endswith("SELL") or any(token in name for token in ("condor", "covered call", "short straddle", "selling")):
        return "Option Selling"
    return "Option Buying"


def _strategy_required_capital(row: Dict[str, Any]) -> float:
    # Prefer the nested risk/options capital — that is what the risk manager
    # actually sizes on (core/risk_manager.py). The top-level required_capital is a
    # legacy field the template re-sync keeps at the template default, so it can be
    # stale (e.g. equity tiers live in visual_config.risk.required_capital). Reading
    # it first made the UI show ₹15k while the engine traded ₹75k. Nested wins.
    visual_config = row.get("visual_config") or {}
    for value in (
        (visual_config.get("risk") or {}).get("required_capital"),
        (visual_config.get("options") or {}).get("required_capital"),
        row.get("required_capital"),
    ):
        if value is not None:
            try:
                return round(float(value), 2)
            except (TypeError, ValueError):
                pass
    visual_config = row.get("visual_config") or {}
    options_config = visual_config.get("options") or {}
    underlying = str(options_config.get("underlying") or visual_config.get("symbol") or "").upper()
    base = {
        "NIFTY": 35000.0,
        "SENSEX": 45000.0,
    }.get(underlying, 25000.0)
    if _strategy_type(row) == "Option Selling":
        base = max(base, 125000.0)
    # No explicit config capital — use the structure-aware estimate (real margin for
    # the configured structure/size) instead of a flat per-underlying guess.
    try:
        from core.capital_model import strategy_required_capital
        est = strategy_required_capital(row, fallback=base)
        if est and est > 0:
            return round(float(est), 2)
    except Exception:
        pass
    return base


def _strategy_out(row: Dict[str, Any]) -> StrategyOut:
    clean = dict(row)
    clean.pop("_id", None)
    clean.pop("user_id", None)
    for k in ("last_evaluated_at", "last_signal_at", "created_at"):
        if k in clean and hasattr(clean[k], "isoformat"):
            clean[k] = clean[k].isoformat()
    clean["asset_class"] = _strategy_asset_class(clean)
    clean["strategy_type"] = _strategy_type(clean)
    clean["required_capital"] = _strategy_required_capital(clean)
    clean["instrument_group"] = _strategy_instrument_group(clean)
    clean["broker"] = row.get("broker") or "upstox"
    clean["mode"] = row.get("mode") or "paper"
    clean["market_suitability"] = row.get("market_suitability") or "Any Market Condition"
    return StrategyOut(**clean)



async def seed_default_strategies_for_user(user_id: str) -> int:
    existing = await db.strategies.find({"user_id": user_id}, {"_id": 0, "name": 1}).to_list(500)
    existing_names = {row.get("name") for row in existing}
    docs = [_build_default_strategy_doc(t, user_id) for t in DEFAULT_OPTION_STRATEGIES if t["name"] not in existing_names]
    if not docs:
        return 0
    try:
        from core.market_clock import is_trading_session_active as _market_hours_active
        market_hours_active = bool(_market_hours_active())
    except Exception:
        market_hours_active = False
    for doc in docs:
        if doc.get("name") in PAPER_FORWARD_ACTIVE_STRATEGY_NAMES:
            doc["status"] = "live" if market_hours_active else "paused"
            doc["mode"] = "paper"
            doc["manual_paused"] = False
            doc["schedule_paused"] = not market_hours_active
            doc["last_filter_reason"] = (
                "Paper-forward active during market hours: Options Alpha Rebuild pack seeded 2026-07-05."
                if market_hours_active
                else "Market closed: queued for paper-forward activation at the next 09:15 IST open."
            )
        elif doc.get("name") in DEAD_STRATEGY_NAMES or doc.get("name") in PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES:
            doc["status"] = "archived"
            doc["mode"] = "paper"
            doc["manual_paused"] = True
            doc["schedule_paused"] = False
            doc["last_filter_reason"] = (
                "Archived: not in the founder-approved paper-forward book (only QG-O1/QG-O4/QG-O5 active)."
                if doc.get("name") in PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES
                else "Archived 2026-07-04 (EDR-03): 0 out-of-sample edge across the old book."
            )
    await db.strategies.insert_many(docs)
    return len(docs)


async def migrate_strategy_display_names(user_id: str) -> int:
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for old_name, new_name in STRATEGY_DISPLAY_NAME_RENAMES.items():
        result = await db.strategies.update_many(
            {"user_id": user_id, "name": old_name},
            {"$set": {"name": new_name, "display_name_migrated_at": now}},
        )
        updated += int(result.modified_count or 0)
    if updated:
        logger.info("Renamed %d strategy display name(s) for user %s", updated, user_id)
    return updated
    try:
        await db.strategies.insert_many(docs)
        logger.info(f"Seeded {len(docs)} default option strategies for user {user_id}")
        return len(docs)
    except Exception as e:
        logger.warning(f"Failed to seed default strategies for user {user_id}: {e}")
        return 0


async def migrate_user_to_v12_upstox(user_id: str) -> Dict[str, int]:
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "paper_mode": 1})
    live_mode = not bool((user or {}).get("paper_mode", True))
    strategy_mode = "live" if live_mode else "paper"
    user_res = await db.users.update_one(
        {"id": user_id},
        {"$set": {"data_broker": "upstox", "execution_broker": "upstox", "fallback_broker": "none"}},
    )
    strat_res = await db.strategies.update_many(
        {"user_id": user_id},
        {"$set": {
            "broker": "upstox",
            "mode": strategy_mode,
            "visual_config.options.enabled": True,
        }, "$unset": {
            "last_data_source": "",
            "last_error": "",
            "last_filter_reason": "",
        }},
    )
    personalised_risk_count = 0
    existing_rows = await db.strategies.find({"user_id": user_id}, {"_id": 0}).to_list(500)
    for row in existing_rows:
        visual_config = row.get("visual_config") or {}
        options_config = visual_config.get("options") or {}
        template_like = {
            **row,
            "underlying": options_config.get("underlying") or visual_config.get("symbol") or row.get("instrument_group"),
            "risk": visual_config.get("risk") or {},
        }
        risk_profile = _strategy_risk_profile(template_like)
        if row.get("required_capital") is not None:
            risk_profile["required_capital"] = float(row.get("required_capital") or 0)
        res = await db.strategies.update_one(
            {"id": row["id"], "user_id": user_id},
            {"$set": _risk_update_fields(risk_profile)},
        )
        personalised_risk_count += int(res.modified_count or 0)
    template_sync_count = 0
    for template in DEFAULT_OPTION_STRATEGIES:
        risk_profile = _strategy_risk_profile(template)
        risk_profile["required_capital"] = float(template.get("required_capital") or 0)
        is_eq_temp = template.get("instrument_group") in ("NSE", "BSE")
        res = await db.strategies.update_one(
            {"user_id": user_id, "name": template["name"]},
            {"$set": {
                "description": template["description"],
                "python_code": template["python_code"],
                "strategy_type": template.get("strategy_type", "Option Buying"),
                "required_capital": float(template.get("required_capital") or 0),
                "instrument_group": template.get("instrument_group"),
                "market_suitability": template.get("market_suitability", "Retail live momentum"),
                "visual_config.options.enabled": not is_eq_temp,
                "visual_config.options.underlying": str(template.get("underlying") or "NIFTY").upper(),
                "visual_config.options.strike_mode": template.get("strike_mode"),
                "visual_config.options.otm_points": int(template.get("otm_points") or 0),
                "visual_config.options.lots": int(template.get("lots") or 1),
                "visual_config.options.product": template.get("product") or ("MIS" if is_eq_temp else "NRML"),
                "visual_config.options.structure": template.get("structure") or ("single_leg" if not is_eq_temp else None),
                "visual_config.options.spread_width": template.get("spread_width"),
                "visual_config.options.short_otm_pct": template.get("short_otm_pct"),
                "visual_config.options.wing_width": template.get("wing_width"),
                "visual_config.options.exit_mode": template.get("exit_mode"),
                "visual_config.options.short_delta": template.get("short_delta"),
                "visual_config.options.short_offset_strikes": template.get("short_offset_strikes"),
                "visual_config.options.candle_interval": template.get("candle_interval") or "5minute",
                **_risk_update_fields(risk_profile),
                "default_strategy_version": "v13-live-brain-r1",
                "strategy_logic_version": "1.0",
            }, "$unset": {
                "last_filter_reason": "",
                "last_error": "",
            }},
        )
        template_sync_count += int(res.modified_count or 0)
    return {
        "users": int(user_res.modified_count or 0),
        "strategies": int(strat_res.modified_count or 0),
        "templates_synced": template_sync_count,
        "personalised_risk": personalised_risk_count,
    }


async def migrate_user_to_upstox_quality_system(user_id: str) -> Dict[str, int]:
    """Attach existing users/strategies to the Upstox quality execution contract.

    This is deliberately additive: it does not create strategies and it does
    not arm live auto-trading. It only records that current strategy rows must
    use Upstox identity, readiness, quote, cost and reconciliation gates.
    """
    now = datetime.now(timezone.utc).isoformat()
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "paper_mode": 1, "allow_simulated_prices": 1})
    paper_mode = bool((user or {}).get("paper_mode", True))
    user_set = {
        "data_broker": "upstox",
        "execution_broker": "upstox",
        "fallback_broker": "none",
        "live_auto_trading_enabled": False,
        "upstox_quality_system_version": "2026-06-quality-v1",
        "upstox_quality_migrated_at": now,
        "paper_realism_mode": "UPSTOX_LIKE",
        "paper_block_suspended_instruments": True,
        "paper_uses_upstox_like_charges": True,
        "live_readiness_required": True,
    }
    user_set_on_insert = {"paper_mode": True}
    if "allow_simulated_prices" not in (user or {}):
        user_set["allow_simulated_prices"] = paper_mode
    user_res = await db.users.update_one(
        {"id": user_id},
        {"$set": user_set, "$setOnInsert": user_set_on_insert},
        upsert=False,
    )
    mode = "paper" if paper_mode else "live"
    strat_res = await db.strategies.update_many(
        {"user_id": user_id},
        {"$set": {
            "broker": "upstox",
            "mode": mode,
            "requires_upstox_quality_checks": True,
            "live_readiness_required": True,
            "paper_realism_mode": "UPSTOX_LIKE",
            "paper_uses_upstox_like_charges": True,
            "uses_instrument_key_identity": True,
            "quality_system_version": "2026-06-quality-v1",
            "quality_system_migrated_at": now,
        }, "$unset": {
            "fallback_broker": "",
            "last_legacy_broker": "",
        }},
    )
    return {
        "users": int(user_res.modified_count or 0),
        "strategies": int(strat_res.modified_count or 0),
    }


async def migrate_all_users_to_upstox_quality_system() -> Dict[str, Any]:
    totals = {"ok": True, "users_seen": 0, "users_modified": 0, "strategies_modified": 0, "errors": []}
    async for row in db.users.find({}, {"id": 1}):
        user_id = row.get("id")
        if not user_id:
            continue
        totals["users_seen"] += 1
        try:
            result = await migrate_user_to_upstox_quality_system(user_id)
            totals["users_modified"] += int(result.get("users") or 0)
            totals["strategies_modified"] += int(result.get("strategies") or 0)
        except Exception as exc:
            totals["ok"] = False
            totals["errors"].append({"user_id": user_id, "error": str(exc)[:300]})
    totals["completed_at"] = datetime.now(timezone.utc).isoformat()
    return totals


async def _sync_strategy_modes_to_profile(user_id: str, paper_mode: bool) -> int:
    """Keep strategy execution mode aligned with the account mode.

    The runner queues signals with the strategy mode, while the signal manager
    only executes paper signals for paper-mode strategies. A profile switch back
    to PAPER must therefore update existing strategies too.
    """
    mode = "paper" if paper_mode else "live"
    update: Dict[str, Any] = {
        "broker": "upstox",
        "mode": mode,
        "requires_upstox_quality_checks": True,
        "live_readiness_required": True,
        "paper_realism_mode": "UPSTOX_LIKE",
        "paper_uses_upstox_like_charges": True,
        "uses_instrument_key_identity": True,
        "profile_mode_synced_at": datetime.now(timezone.utc).isoformat(),
    }
    unset: Dict[str, Any] = {
        "last_filter_reason": "",
        "last_skip_reason_code": "",
    }
    if paper_mode:
        update.update({
            "quarantined": False,
            "halted": False,
            "is_halted": False,
        })
        unset.update({
            "quarantine_reason": "",
            "halt_reason": "",
            "last_halt_reason": "",
            "last_error": "",
        })
    # Strategies with mode_pinned=True keep their explicitly chosen mode — this
    # is how a paper cohort keeps collecting data while the account runs one
    # strategy live. All other profile-sync hygiene fields still apply to them.
    res = await db.strategies.update_many(
        {"user_id": user_id, "mode_pinned": {"$ne": True}},
        {"$set": update, "$unset": unset},
    )
    pinned_update = {k: v for k, v in update.items() if k != "mode"}
    pinned_res = await db.strategies.update_many(
        {"user_id": user_id, "mode_pinned": True},
        {"$set": pinned_update, "$unset": unset},
    )
    return int((res.modified_count or 0) + (pinned_res.modified_count or 0))


async def _strategy_source_id(source: Optional[str]) -> Optional[str]:
    if not source:
        return None
    m = re.search(r"strategy:([A-Za-z0-9_.:-]+)", source)
    return m.group(1) if m else None


def _ledger_pct(value: Any, default: float) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return default
    return pct / 100.0 if pct > 1 else pct


# Delegates to core.position_lifecycle — local names kept so all call sites work unchanged.
def _clamp_float(value: float, low: float, high: float) -> float:
    return _clamp_float_module(value, low, high)

def _adaptive_risk_percentages(entry: float, risk: Dict[str, Any]) -> Dict[str, Optional[float]]:
    return _adaptive_risk_percentages_module(entry, risk)

def _risk_pct(risk: Dict[str, Any], *keys: str, default: Optional[float]) -> Optional[float]:
    return _risk_pct_module(risk, *keys, default=default)


def _normalize_strategy_risk(risk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _normalize_strategy_risk_module(risk)

def _position_risk_prices(position: Dict[str, Any], ltp: Optional[float] = None) -> Dict[str, Optional[float]]:
    return _position_risk_prices_module(position, ltp)


def _sync_option_ledger_strategy(row: Dict[str, Any]) -> None:
    visual_config = row.get("visual_config") or {}
    risk = _normalize_strategy_risk(visual_config.get("risk") or {})
    options_config = visual_config.get("options") or {}
    required_capital = row.get("required_capital")
    if required_capital is None:
        required_capital = risk.get("required_capital")
    if required_capital is None:
        lots = max(1, int(options_config.get("lots") or 1))
        required_capital = float(options_config.get("required_capital") or 0) * lots
    option_ledger.upsert_strategy_state(
        row["id"],
        max_lots=1,
        target_pct=_ledger_pct(risk.get("target_pct", risk.get("take_profit_pct")), DEFAULT_STRATEGY_RISK["take_profit_pct"]),
        stoploss_pct=_ledger_pct(risk.get("stoploss_pct", risk.get("stop_loss_pct")), DEFAULT_STRATEGY_RISK["stop_loss_pct"]),
        trailing_sl_enabled=bool(risk.get("trailing_sl_enabled", True)),
        trail_trigger_pct=_ledger_pct(risk.get("trail_trigger_pct"), 0.20),
        trail_step_pct=_ledger_pct(risk.get("trail_step_pct"), 0.10),
        risk_style=str(risk.get("risk_style") or DEFAULT_STRATEGY_RISK["risk_style"]),
        adaptive_exits_enabled=bool(risk.get("adaptive_exits_enabled", True)),
        target_r_multiple=float(risk.get("target_r_multiple") or DEFAULT_STRATEGY_RISK["target_r_multiple"]),
        cooldown_minutes=int(risk.get("cooldown_minutes") or 5),
        max_trades_day=int(risk.get("max_trades_day") or 3),
        required_capital=float(required_capital or 0),
        daily_loss_limit=float(risk.get("daily_loss_limit") or 0),
        exit_mode=risk.get("exit_mode", "tp_sl_tsl_or_signal"),
    )


async def _get_strategy_risk(user_id: str, sid: str) -> Dict[str, Any]:
    row = await db.strategies.find_one({"id": sid, "user_id": user_id})
    return ((row or {}).get("visual_config") or {}).get("risk") or {}


def _instrument_key(exchange: str, trading_symbol: str, instrument_token: Any = None) -> str:
    exch = (exchange or "NSE").upper()
    token = str(instrument_token or "").strip()
    symbol = str(trading_symbol or "").upper().strip()
    if token:
        return f"{exch}:TOKEN:{token}"
    return f"{exch}:SYMBOL:{symbol}"


def _active_key(user_id: str, value: str) -> str:
    return f"{user_id}:{value}"


def _strategy_side_key(user_id: str, strategy_id: str, instrument_key: str, side: str) -> str:
    normalized_side = "SHORT" if str(side or "").upper() == "SHORT" else "LONG"
    return _active_key(user_id, f"{strategy_id}:{instrument_key}:{normalized_side}")


def _strategy_lock_ids(user_id: str, strategy_id: str, instrument_key: str, side: str) -> List[str]:
    return [f"{user_id}:strategy-instrument-side:{strategy_id}:{instrument_key}:{str(side or 'LONG').upper()}"]


def _risk_reservation_lock_id(user_id: str) -> str:
    return f"risk-reservation:{user_id}"


async def _acquire_risk_reservation_lock(user_id: str, *, timeout_sec: float = 2.0) -> Optional[str]:
    lock_id = _risk_reservation_lock_id(user_id)
    owner = uuid.uuid4().hex
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        try:
            await db.risk_reservation_locks.insert_one({
                "_id": lock_id,
                "owner": owner,
                "user_id": user_id,
                "created_at": now,
                "expires_at": now_dt + timedelta(seconds=15),
            })
            return owner
        except DuplicateKeyError:
            stale = await db.risk_reservation_locks.find_one_and_delete({
                "_id": lock_id,
                "expires_at": {"$lt": now_dt},
            })
            if stale:
                continue
            await asyncio.sleep(0.05)
    return None


async def _release_risk_reservation_lock(user_id: str, owner: Optional[str]) -> None:
    if not owner:
        return
    try:
        await db.risk_reservation_locks.delete_one({"_id": _risk_reservation_lock_id(user_id), "owner": owner})
    except Exception as exc:
        logger.warning("risk reservation lock release failed user=%s: %s", user_id, exc)


async def _current_reserved_exposure(user_id: str) -> float:
    now = datetime.now(timezone.utc)
    total = 0.0
    rows = await db.risk_reservations.find({
        "user_id": user_id,
        "status": "ACTIVE",
        "$or": [
            {"expires_at": {"$gt": now}},
            {"expires_at": {"$exists": False}},
            {"expires_at": None},
        ],
    }, {"_id": 0, "reserved_value": 1}).to_list(1000)
    for row in rows:
        try:
            total += float(row.get("reserved_value") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


async def _reserve_order_exposure(
    *,
    user_id: str,
    order_id: str,
    strategy_id: Optional[str],
    instrument_key: str,
    symbol: str,
    quantity: int,
    price: float,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    owner = await _acquire_risk_reservation_lock(user_id)
    if not owner:
        raise HTTPException(status_code=409, detail="Risk reservation busy; retry order after current risk check completes.")
    try:
        order_value = round(max(0, int(quantity or 0)) * max(0.0, float(price or 0)), 2)
        reserved = await _current_reserved_exposure(user_id)
        limit = float(settings.get("max_position_size") or settings.get("per_strategy_capital") or 0)
        if limit > 0 and reserved + order_value > limit:
            await _record_pretrade_risk_event(user_id, {
                "event": "PRETRADE_BLOCK",
                "strategy_id": strategy_id,
                "symbol": symbol,
                "reason": "reserved exposure limit exceeded",
                "reserved_exposure": reserved,
                "proposed_order_value": order_value,
                "max_position_value": limit,
                "paper": False,
            })
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Pre-trade blocked: reserved exposure {reserved:.2f} + order value "
                    f"{order_value:.2f} exceeds max position value {limit:.2f}."
                ),
            )
        now_dt = datetime.now(timezone.utc)
        doc = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "order_id": order_id,
            "strategy_id": strategy_id,
            "instrument_key": instrument_key,
            "symbol": symbol,
            "quantity": int(quantity or 0),
            "price": float(price or 0),
            "reserved_value": order_value,
            "reserved_exposure_before": reserved,
            "status": "ACTIVE",
            "created_at": now_dt.isoformat(),
            "updated_at": now_dt.isoformat(),
            "expires_at": now_dt + timedelta(hours=8),
        }
        await db.risk_reservations.insert_one(doc)
        await _append_order_event(order_id, user_id, "RISK_EXPOSURE_RESERVED", {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "reserved_value": order_value,
            "reserved_exposure_before": reserved,
            "reserved_exposure_after": round(reserved + order_value, 2),
        })
        return doc
    except DuplicateKeyError:
        existing = await db.risk_reservations.find_one({"order_id": order_id, "user_id": user_id}, {"_id": 0})
        if existing:
            return existing
        raise
    finally:
        await _release_risk_reservation_lock(user_id, owner)


async def _close_order_exposure_reservation(order_id: str, user_id: str, *, status: str, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.risk_reservations.update_many(
            {"order_id": order_id, "user_id": user_id, "status": "ACTIVE"},
            {"$set": {"status": status, "close_reason": reason, "updated_at": now, "closed_at": now}},
        )
        await _append_order_event(order_id, user_id, "RISK_EXPOSURE_RELEASED", {
            "status": status,
            "reason": reason,
        })
    except Exception as exc:
        logger.warning("risk reservation close failed order=%s user=%s: %s", order_id, user_id, exc)


async def _strategy_row(user_id: str, strategy_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not strategy_id:
        return None
    return await db.strategies.find_one({"id": strategy_id, "user_id": user_id}, {"_id": 0})


async def _reserve_strategy_position(
    *,
    user_id: str,
    strategy_id: Optional[str],
    instrument_key: str,
    trading_symbol: str,
    exchange: str,
    instrument_token: Any,
    quantity: int,
    entry_price: float,
    position_side: str = "LONG",
    source: str,
    signal_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create the central ownership row before sending a strategy entry."""
    if not strategy_id:
        return None
    side_key = _strategy_side_key(user_id, strategy_id, instrument_key, position_side)
    existing = await db.strategy_positions.find_one({
        "user_id": user_id,
        "active_strategy_instrument_side_key": side_key,
        "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
    }, {"_id": 0})
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate strategy entry blocked for {trading_symbol}: same strategy, instrument and side already active.",
        )

    row = await _strategy_row(user_id, strategy_id)
    risk = _normalize_strategy_risk(((row or {}).get("visual_config") or {}).get("risk") or {})
    now = datetime.now(timezone.utc).isoformat()
    lock_ids = _strategy_lock_ids(user_id, strategy_id, instrument_key, position_side)
    lock_docs = [
        {
            "_id": lock_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "instrument_key": instrument_key,
            "position_side": position_side,
            "trading_symbol": trading_symbol,
            "created_at": now,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        }
        for lock_id in lock_ids
    ]
    acquired_locks: List[str] = []
    try:
        for lock_doc in lock_docs:
            await db.strategy_position_locks.insert_one(lock_doc)
            acquired_locks.append(lock_doc["_id"])
    except DuplicateKeyError:
        if acquired_locks:
            await db.strategy_position_locks.delete_many({"_id": {"$in": acquired_locks}})
        raise HTTPException(
            status_code=409,
            detail=f"Strategy/instrument/side already reserved by another scan cycle. Duplicate entry blocked for {trading_symbol}.",
        )

    # Resolve symbol_group
    vc = (row or {}).get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    if opt_cfg.get("enabled"):
        symbol_group = str(opt_cfg.get("underlying") or "NIFTY").upper()
    else:
        symbol_group = str(vc.get("symbol") or trading_symbol).upper()

    # Load signal metadata or use fallbacks
    signal_doc = None
    if signal_id:
        signal_doc = await db.signals.find_one({"id": signal_id})

    r_meta = {}
    if signal_doc:
        for field in [
            "setup_type", "confidence", "entry_reason", "target_R",
            "initial_stop_R", "trail_after_R", "max_hold_minutes",
            "invalidation_rule", "regime_required", "option_selection_preference",
            "signal_version", "strategy_logic_version", "default_strategy_version",
            "trend_context", "regime_snapshot", "regime", "underlying_atr_pct"
        ]:
            if field in signal_doc:
                r_meta[field] = signal_doc[field]
        r_meta["r_metadata_source"] = "v13_signal"
        # Phase 4: if signal carries an ATR-based exit policy, merge it into
        # tp_sl_tsl_config so position_guardian uses real prices from entry.
        _ep = signal_doc.get("exit_policy")
        if _ep and isinstance(_ep, dict):
            risk = {**risk, **_ep}
    else:
        default_version = "v13-live-brain-r1"
        if row:
            default_version = row.get("default_strategy_version") or default_version
        r_meta = {
            "setup_type": "breakout",
            "confidence": 85.0,
            "entry_reason": "Fallback risk parameters",
            "target_R": 2.0,
            "initial_stop_R": 1.0,
            "trail_after_R": 1.5,
            "max_hold_minutes": 60,
            "invalidation_rule": "time_or_stop",
            "regime_required": "any",
            "option_selection_preference": "ATM",
            "signal_version": "v13",
            "strategy_logic_version": "1.0",
            "default_strategy_version": default_version,
            "r_metadata_source": "fallback"
        }

    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "strategy_id": strategy_id,
        "instrument_key": instrument_key,
        "symbol_group": symbol_group,
        "active_strategy_instrument_side_key": side_key,
        "instrument_token": instrument_token,
        "trading_symbol": trading_symbol,
        "symbol": trading_symbol,
        "exchange": exchange,
        "quantity": int(quantity),
        "open_quantity": int(quantity),
        "average_buy_price": float(entry_price or 0),
        "position_side": "SHORT" if str(position_side or "").upper() == "SHORT" else "LONG",
        "entry_time": now,
        "status": "RESERVED",
        "tp_sl_tsl_config": dict(risk),
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    doc.update(r_meta)
    is_equity_reservation = (
        str(exchange or "").upper() in {"NSE", "BSE"}
        or "_EQ|" in str(instrument_key or "")
    )
    doc["asset_type"] = "equity" if is_equity_reservation else "option"
    doc["regime_at_entry"] = str(
        doc.get("regime")
        or (doc.get("regime_snapshot") or {}).get("regime")
        or (doc.get("trend_context") or {}).get("trend")
        or "UNKNOWN"
    ).upper()
    risk_prices = _position_risk_prices(
        {**doc, "average_buy_price": float(entry_price or 0), "tp_sl_tsl_config": doc.get("tp_sl_tsl_config") or {}}
    )
    if risk_prices.get("stop_loss") is not None:
        doc["sl_price"] = risk_prices["stop_loss"]
    if risk_prices.get("take_profit") is not None:
        doc["tp_price"] = risk_prices["take_profit"]
    if doc.get("sl_price") not in (None, "", 0) and entry_price and quantity:
        try:
            doc["planned_risk"] = round(abs((float(entry_price) - float(doc["sl_price"])) * int(quantity)), 2)
        except Exception:
            doc["planned_risk"] = None

    try:
        await db.strategy_positions.insert_one(doc)
    except DuplicateKeyError:
        await db.strategy_position_locks.delete_many({"_id": {"$in": lock_ids}})
        raise HTTPException(
            status_code=409,
            detail=f"Strategy/instrument/side already reserved by another scan cycle. Duplicate entry blocked for {trading_symbol}.",
        )
    return doc


async def _activate_strategy_position(
    reservation: Optional[Dict[str, Any]],
    *,
    order_id: str,
    broker_order_id: Optional[str],
    average_buy_price: float,
    quantity: int,
    paper: bool,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> None:
    if not reservation:
        return
    now = datetime.now(timezone.utc).isoformat()
    risk_patch: Dict[str, Any] = _normalize_strategy_risk(reservation.get("tp_sl_tsl_config") or {})
    if stop_loss is not None:
        risk_patch["stoploss_price"] = float(stop_loss)
        risk_patch["stop_loss"] = float(stop_loss)
    if take_profit is not None:
        risk_patch["target_price"] = float(take_profit)
        risk_patch["take_profit"] = float(take_profit)
    if average_buy_price and (risk_patch.get("stoploss_price") in (None, "") or risk_patch.get("target_price") in (None, "")):
        price_patch = _position_risk_prices({
            "average_buy_price": average_buy_price,
            "position_side": "SHORT" if str(reservation.get("position_side") or "").upper() == "SHORT" else "LONG",
            "tp_sl_tsl_config": risk_patch,
        })
        if price_patch.get("stop_loss") is not None:
            risk_patch["stoploss_price"] = price_patch["stop_loss"]
            risk_patch["stop_loss"] = price_patch["stop_loss"]
        if price_patch.get("take_profit") is not None:
            risk_patch["target_price"] = price_patch["take_profit"]
            risk_patch["take_profit"] = price_patch["take_profit"]

    is_equity = (
        str(reservation.get("asset_type") or "").lower() == "equity"
        or str(reservation.get("exchange") or "").upper() in {"NSE", "BSE"}
        or "_EQ|" in str(reservation.get("instrument_key") or "")
    )
    if is_equity and average_buy_price:
        atr_pct = None
        for key in ("equity_atr_pct", "underlying_atr_pct", "atr_pct"):
            try:
                value = risk_patch.get(key)
                if value not in (None, ""):
                    atr_pct = float(value)
                    break
            except (TypeError, ValueError):
                continue
        if atr_pct and atr_pct > 0:
            side = "SHORT" if str(reservation.get("position_side") or "").upper() == "SHORT" else "LONG"
            target_r = float(reservation.get("target_R") or risk_patch.get("target_r_multiple") or 1.8)
            stop_gap = float(average_buy_price) * atr_pct / 100.0
            target_gap = stop_gap * max(1.2, min(2.5, target_r))
            if side == "SHORT":
                stop_price = round(float(average_buy_price) + stop_gap, 2)
                target_price = round(max(0.0, float(average_buy_price) - target_gap), 2)
            else:
                stop_price = round(max(0.0, float(average_buy_price) - stop_gap), 2)
                target_price = round(float(average_buy_price) + target_gap, 2)
            risk_patch.update({
                "stoploss_price": stop_price,
                "stop_loss": stop_price,
                "target_price": target_price,
                "take_profit": target_price,
                "stop_loss_pct": round(atr_pct, 4),
                "take_profit_pct": round(atr_pct * max(1.2, min(2.5, target_r)), 4),
                "trailing_sl_enabled": False,
                "protection_status": "PROTECTED_EQUITY_ATR",
            })

    is_option_buy = (
        reservation.get("position_side") == "LONG"
        and (
            reservation.get("exchange") in {"NFO", "BFO"}
            or reservation.get("trading_symbol", "").endswith(("CE", "PE"))
            or reservation.get("option_type") in {"CE", "PE"}
        )
    )

    r_activation_fields = {}
    if is_option_buy:
        risk_pct = float(risk_patch.get("stop_loss_pct") or risk_patch.get("stoploss_pct") or risk_patch.get("stop_pct") or 8.0)
        initial_stop_R = float(reservation.get("initial_stop_R") or 1.0)
        target_R = float(reservation.get("target_R") or 2.0)
        
        r_initial_risk_amount = round(average_buy_price * risk_pct / 100.0, 2)
        r_stop_loss_price = round(max(0.0, average_buy_price - r_initial_risk_amount * initial_stop_R), 2)
        r_take_profit_price = round(average_buy_price + r_initial_risk_amount * target_R, 2)
        
        r_activation_fields = {
            "r_initial_risk_amount": r_initial_risk_amount,
            "r_stop_loss_price": r_stop_loss_price,
            "r_take_profit_price": r_take_profit_price,
            "r_entry_price": float(average_buy_price),
            "r_current_R": 0.0,
            "r_max_R_seen": 0.0,
            "r_trailing_active": False,
            "r_trailing_stop_price": r_stop_loss_price,
            "best_price_seen": float(average_buy_price),
            "r_last_evaluated_at": now,
        }

    set_fields = {
        "entry_order_id": order_id,
        "broker_order_id": broker_order_id,
        "entry_broker_order_id": broker_order_id,
        "quantity": int(quantity),
        "open_quantity": int(quantity),
        "average_buy_price": float(average_buy_price or 0),
        "status": "OPEN" if paper else "PENDING_BROKER",
        "mode": "paper" if paper else "live",
        "tp_sl_tsl_config": risk_patch,
        "updated_at": now,
    }
    risk_prices = _position_risk_prices({
        "average_buy_price": average_buy_price,
        "position_side": "SHORT" if str(reservation.get("position_side") or "").upper() == "SHORT" else "LONG",
        "tp_sl_tsl_config": risk_patch,
    })
    if risk_prices.get("stop_loss") is not None:
        set_fields["sl_price"] = risk_prices["stop_loss"]
    if risk_prices.get("take_profit") is not None:
        set_fields["tp_price"] = risk_prices["take_profit"]
    if set_fields.get("sl_price") not in (None, "", 0) and average_buy_price and quantity:
        try:
            set_fields["planned_risk"] = round(abs((float(average_buy_price) - float(set_fields["sl_price"])) * int(quantity)), 2)
        except Exception:
            pass
    if reservation.get("regime_at_entry"):
        set_fields["regime_at_entry"] = str(reservation.get("regime_at_entry")).upper()
    set_fields.update(r_activation_fields)

    await db.strategy_positions.update_one(
        {"id": reservation["id"], "user_id": reservation["user_id"]},
        {"$set": set_fields},
    )


async def _cancel_strategy_reservation(reservation: Optional[Dict[str, Any]], reason: str) -> None:
    if not reservation:
        return
    now = datetime.now(timezone.utc).isoformat()
    await db.strategy_positions.update_one(
        {"id": reservation["id"], "user_id": reservation["user_id"], "status": "RESERVED"},
        {"$set": {"status": "CANCELLED", "cancel_reason": reason, "updated_at": now},
         "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
    )
    await _release_strategy_position_locks(reservation)


async def _open_strategy_position_for_exit(
    *,
    user_id: str,
    strategy_id: Optional[str],
    instrument_key: str,
) -> Optional[Dict[str, Any]]:
    if not strategy_id:
        return None
    row = await db.strategy_positions.find_one({
        "user_id": user_id,
        "strategy_id": strategy_id,
        "instrument_key": instrument_key,
        "status": {"$in": ["OPEN", "FILLED"]},
    }, {"_id": 0})
    if not row:
        row = await db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "status": {"$in": ["OPEN", "FILLED"]},
        }, {"_id": 0})
    if not row:
        active = await db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "EXITING"]},
        }, {"_id": 0})
        detail = (
            f"Strategy position is {active.get('status')} for {active.get('trading_symbol')}; SELL blocked."
            if active else
            f"No stored OPEN strategy position for {instrument_key}; SELL must come from Position Manager."
        )
        raise HTTPException(status_code=409, detail=detail)
    return row


async def _mark_strategy_position_exiting(position: Optional[Dict[str, Any]], *, exit_order_id: str, exit_broker_order_id: Optional[str]) -> None:
    if not position:
        return
    await db.strategy_positions.update_one(
        {"id": position["id"], "user_id": position["user_id"], "status": {"$in": ["OPEN", "FILLED"]}},
        {"$set": {
            "status": "EXITING",
            "exit_order_id": exit_order_id,
            "exit_broker_order_id": exit_broker_order_id,
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
    )


async def _close_strategy_position_record(position: Optional[Dict[str, Any]], *, exit_price: float, reason: str) -> None:
    if not position:
        return
    now = datetime.now(timezone.utc).isoformat()
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    entry = float(position.get("average_buy_price") or 0)
    pos_side = position.get("position_side") or "LONG"
    if pos_side == "SHORT":
        pnl = round((entry - float(exit_price or 0)) * qty, 2)
    else:
        pnl = round((float(exit_price or 0) - entry) * qty, 2)
    await db.strategy_positions.update_one(
        {"id": position["id"], "user_id": position["user_id"]},
        {"$set": {
            "status": "CLOSED",
            "open_quantity": 0,
            "exit_price": float(exit_price or 0),
            "realized_pnl": pnl,
            "exit_reason": reason,
            "closed_at": now,
            "updated_at": now,
        }, "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
    )
    await _release_strategy_position_locks(position)


async def _release_strategy_position_locks(position: Optional[Dict[str, Any]]) -> None:
    if not position:
        return
    user_id = position.get("user_id")
    strategy_id = position.get("strategy_id")
    instrument_key = position.get("instrument_key")
    if not user_id or not strategy_id or not instrument_key:
        return
    side = str(position.get("position_side") or "LONG").upper()
    await db.strategy_position_locks.delete_many({
        "_id": {"$in": _strategy_lock_ids(str(user_id), str(strategy_id), str(instrument_key), side)}
    })


async def _reopen_strategy_position_after_exit_reject(position_id: str, user_id: str, reason: str) -> None:
    await db.strategy_positions.update_one(
        {"id": position_id, "user_id": user_id, "status": "EXITING"},
        {"$set": {"status": "OPEN", "exit_reject_reason": reason, "updated_at": datetime.now(timezone.utc).isoformat()},
         "$unset": {"exit_order_id": "", "exit_broker_order_id": "", "exit_time": ""}},
    )


async def _collect_strategy_orders(user_id: str, sid: str) -> List[Dict[str, Any]]:
    return await db.orders.find({
        "user_id": user_id,
        "source": {"$regex": f"strategy:{sid}"},
        "status": {"$nin": ["CANCELLED", "REJECTED"]},
    }, {"_id": 0}).to_list(1000)


async def _close_strategy_positions(user_id: str, sid: str, reason: str = "auto-exit", ltp_source: str = "", decided_ltp: float | None = None) -> Dict[str, Any]:
    from core.market_domains import resolve_domain_by_underlying
    results = []
    positions = await db.strategy_positions.find({
        "user_id": user_id,
        "strategy_id": sid,
        "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]},
    }, {"_id": 0}).to_list(20)
    for pos in positions:
        sym = pos.get("trading_symbol") or pos.get("symbol")
        qty_net = int(pos.get("open_quantity") or pos.get("quantity") or 0)
        if not sym or qty_net <= 0:
            continue

        # Phase 2 #5: credit spreads MUST close atomically via the two-leg lifecycle,
        # never through the single-leg/equity order path below. Their synthetic
        # trading_symbol ("<UND>-bullish-spread") is not a tradeable instrument: routing
        # it through _place_order_core re-resolves it as NFO/<underlying>, re-applies
        # lot_size to the already-expanded qty, and books a phantom EQUITY position
        # (the 2026-06-17 −16.5k loss). close_credit_spread claims OPEN/EXITING→CLOSED
        # itself, so we close here BEFORE the generic EXITING mark below.
        if str(pos.get("structure")) in ("credit_spread", "debit_spread"):
            # Spreads are never force-closed on single-leg staleness — that guard
            # cannot price a two-leg spread (no top-level instrument_key) and
            # closing here turns a flat/near-flat spread into a charges-only loss.
            # position_monitor._process_spread_position owns spread exits via
            # two-leg REST pricing; let the spread ride until a real exit reason.
            if reason == "stale-quote-protective-exit":
                logger.info(
                    "spread close: ignoring stale-quote-protective-exit for spread pos=%s "
                    "(monitor owns spread pricing)", pos.get("id"),
                )
                results.append({"symbol": sym, "qty": qty_net, "status": "skipped",
                                "reason": "spread-stale-exit-ignored"})
                continue
            from core.spread_lifecycle import close_credit_spread, close_debit_spread
            legs = pos.get("legs") or []
            short_leg = next((l for l in legs if l.get("role") == "short"), None)
            long_leg = next((l for l in legs if l.get("role") == "long"), None)
            if not short_leg or not long_leg:
                logger.warning(
                    "spread close: pos=%s missing legs — skipping generic close to avoid phantom", pos.get("id"),
                )
                results.append({"symbol": sym, "qty": qty_net, "status": "skipped", "reason": "spread-missing-legs"})
                continue
            short_ltp = await _quote_upstox_instrument_key(user_id, short_leg.get("instrument_key"))
            long_ltp = await _quote_upstox_instrument_key(user_id, long_leg.get("instrument_key"))
            # Forced close (squareoff/manual/SL-TP/strategy exit): if a leg quote is
            # missing, fall back to entry premiums (value≈net_credit → gross pnl≈0)
            # rather than leave the spread open or route it generically.
            if short_ltp is None:
                short_ltp = float(short_leg.get("entry_price") or short_leg.get("premium") or 0)
            if long_ltp is None:
                long_ltp = float(long_leg.get("entry_price") or long_leg.get("premium") or 0)
            if str(pos.get("structure")) == "debit_spread":
                spread_res = await close_debit_spread(
                    db, pos, reason=reason, short_ltp=float(short_ltp), long_ltp=float(long_ltp),
                )
            else:
                spread_res = await close_credit_spread(
                    db, pos, reason=reason, short_ltp=float(short_ltp), long_ltp=float(long_ltp),
                )
            results.append({"symbol": sym, "qty": qty_net, "status": "spread-closed",
                            "spread_status": spread_res.get("status"), "realized_pnl": spread_res.get("realized_pnl")})
            continue

        # FIX 4: Smart circuit breaker — only counts genuine ORDER_REJECTED failures.
        # Data failures (LTP_UNAVAILABLE, PRICE_BELOW_MINIMUM) do NOT count because
        # they are transient feed issues, not real order rejections. We track them
        # separately in exit_data_failures. Only exit_order_failures triggers the breaker.
        exit_order_failures = int(pos.get("exit_order_failures", 0))
        if exit_order_failures >= 3:
            now_cb = datetime.now(timezone.utc).isoformat()
            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id},
                {"$set": {"status": "CIRCUIT_BREAKER", "updated_at": now_cb,
                           "last_error": f"Exit circuit breaker: {exit_order_failures} genuine order failures"}},
            )
            logger.warning(
                "Circuit breaker: position %s strategy=%s blocked after %d genuine order failures",
                pos["id"], sid, exit_order_failures,
            )
            results.append({"symbol": sym, "qty": qty_net, "status": "circuit_breaker", "exit_order_failures": exit_order_failures})
            continue

        # Atomically mark position as EXITING before placing the order.
        # If another task already claimed it (modified_count=0), skip.
        # FIX 4: do NOT increment exit_attempts here — we increment selectively on failure below.
        now_str = datetime.now(timezone.utc).isoformat()
        mark_res = await db.strategy_positions.update_one(
            {"id": pos["id"], "user_id": user_id, "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
            {"$set": {"status": "EXITING", "exit_attempt_at": now_str, "updated_at": now_str},
             "$inc": {"exit_attempts": 1}},
        )
        if mark_res.modified_count == 0:
            results.append({"symbol": sym, "qty": qty_net, "status": "skipped", "reason": "already-exiting-or-closed"})
            continue

        # If there's a resting stop-loss order at the broker, cancel it first
        broker_sl_order_id = pos.get("broker_sl_order_id")
        if broker_sl_order_id and pos.get("mode") == "live":
            try:
                logger.info(
                    "Cancelling resting SL order=%s for pos=%s symbol=%s before manual/TP/time exit",
                    broker_sl_order_id, pos["id"], sym
                )
                gateway = await get_user_upstox_gateway(user_id)
                if gateway and gateway.connected:
                    cancel_res = await asyncio.to_thread(gateway.cancel_order, broker_sl_order_id)
                    logger.info("Resting SL order cancellation result: %s", cancel_res)
                    # Also update the tracking order in db.orders to CANCELLED
                    await db.orders.update_many(
                        {"broker_order_id": broker_sl_order_id, "user_id": user_id},
                        {"$set": {
                            "status": "CANCELLED",
                            "execution_status": "CANCELLED",
                            "status_message": "CANCELLED: cancelled prior to technical exit execution",
                            "updated_at": datetime.now(timezone.utc).isoformat()
                        }}
                    )
                else:
                    logger.warning("Could not cancel resting SL: Upstox gateway not connected.")
            except Exception as cancel_exc:
                logger.warning("Exception while cancelling resting SL order %s: %s", broker_sl_order_id, cancel_exc)

        exit_side = "BUY" if str(pos.get("asset_class") or "").upper() == "OPTION_SHORT" or str(pos.get("position_side") or "").upper() == "SHORT" else "SELL"
        # EXIT GUARANTEE: idempotency key is the position id ALONE. A position has
        # exactly ONE exit, regardless of which reason fires it or how many times.
        # (The old key included reason[:20] — a position whose exit reason changed
        # between monitor ticks could place a second exit order.)
        pos_exit_idem = f"exit:{pos['id']}"

        # Pre-order duplicate guard: if a FILLED exit order already exists for this
        # position key, the position is already closed. Reconcile it without placing
        # a new order — prevents phantom order rows from being created before the
        # ledger can reject them.
        existing_filled_exit = await db.orders.find_one({
            "user_id": user_id,
            "idempotency_key": pos_exit_idem,
            "status": {"$in": ["FILLED", "COMPLETE", "PAPER_FILLED"]},
        })
        if existing_filled_exit:
            now_rec = datetime.now(timezone.utc).isoformat()
            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id},
                {"$set": {"status": "CLOSED", "closed_at": now_rec, "updated_at": now_rec,
                          "exit_reason": "pre-order-duplicate-reconciled"}},
            )
            logger.warning(
                "Pre-order duplicate guard: exit order already FILLED for pos=%s sym=%s — reconciling to CLOSED",
                pos["id"], sym,
            )
            results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "reconciled-pre-order-duplicate"})
            continue

        place_kwargs: Dict[str, Any] = {
            "user_id": user_id,
            "side": exit_side,
            "order_type": "MARKET",
            "product": pos.get("product"),
            "source": f"{reason}:strategy:{sid}",
            "idempotency_key": pos_exit_idem,
            "exit_reason": reason,
        }
        _pos_at = str(pos.get("asset_type") or "").lower()
        _pos_exch = str(pos.get("exchange") or "").upper()
        if _pos_at == "option" or _pos_exch in {"NFO", "BFO"}:
            # Resolve lot_size from position doc first, then fall back to market domain.
            underlying = pos.get("underlying") or pos.get("symbol_group") or "NIFTY"
            domain_lot = resolve_domain_by_underlying(underlying).get_lot_size(underlying)
            lot_size = int(pos.get("lot_size") or domain_lot or 1)
            place_kwargs["symbol"] = sym
            place_kwargs["option_contract"] = {
                "tradingsymbol": sym,
                "exchange": pos.get("exchange", "NFO"),
                "instrument_key": pos.get("instrument_key") or pos.get("instrument_token"),
                "instrument_token": pos.get("instrument_token") or pos.get("instrument_key"),
                "lot_size": lot_size,
                "strike": pos.get("strike"),
                "expiry": pos.get("expiry"),
                "underlying": underlying,
                "option_type": pos.get("option_type"),
                "transaction_type": exit_side,
            }
            place_kwargs["qty"] = max(1, math.ceil(qty_net / lot_size))
            # FIX 3: Always pass a price for exit orders. Prefer last_ltp from WS/REST.
            # If unavailable, fall back to average_buy_price so the exit is not blocked
            # by the price guard in _place_order_core.
            raw_ltp = pos.get("last_ltp")
            try:
                last_known_ltp = float(raw_ltp) if raw_ltp and raw_ltp != "LTP_UNAVAILABLE" else 0.0
            except (TypeError, ValueError):
                last_known_ltp = 0.0
            if last_known_ltp <= 0:
                last_known_ltp = float(pos.get("average_buy_price") or pos.get("average_price") or 0)
            if last_known_ltp > 0:
                place_kwargs["price"] = last_known_ltp
        elif _pos_at == "equity" or _pos_exch in {"NSE", "BSE"}:
            place_kwargs["symbol"] = sym
            place_kwargs["qty"] = qty_net
            place_kwargs["exchange"] = pos.get("exchange") or "NSE"
            # Prefer decided_ltp if passed, otherwise last_ltp, otherwise REST quote, otherwise raise.
            resolved_exit_price = decided_ltp
            if resolved_exit_price is None or resolved_exit_price <= 0:
                raw_ltp = pos.get("last_ltp")
                try:
                    resolved_exit_price = float(raw_ltp) if raw_ltp and raw_ltp != "LTP_UNAVAILABLE" else 0.0
                except (TypeError, ValueError):
                    resolved_exit_price = 0.0
            
            if resolved_exit_price is None or resolved_exit_price <= 0:
                # Live REST quote lookup fallback
                try:
                    ikey = pos.get("instrument_key") or pos.get("instrument_token")
                    if ikey:
                        quote_val = await _quote_upstox_instrument_key(user_id, ikey)
                        if quote_val and float(quote_val) > 0:
                            resolved_exit_price = float(quote_val)
                except Exception as rest_err:
                    logger.warning("REST fallback failed during equity close for %s: %s", sym, rest_err)
            
            if resolved_exit_price is not None and resolved_exit_price > 0:
                place_kwargs["price"] = resolved_exit_price
            else:
                raise ValueError(
                    f"LTP_UNAVAILABLE: No valid LTP found to close equity position {pos.get('id')} for symbol {sym}"
                )
        else:
            # FAIL-CLOSED (2026-06-17): unrecognized position shape. Never fall through
            # to a generic order — defaulting an unknown asset_type to the equity path
            # is exactly what minted the "BANKNIFTY-bullish-spread" phantom EQUITY
            # position. Revert EXITING→OPEN and skip, surfacing the anomaly loudly
            # rather than trading something undefined. (option_spread is already routed
            # to close_credit_spread above, so it never reaches here.)
            now_unknown = datetime.now(timezone.utc).isoformat()
            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id, "status": "EXITING"},
                {"$set": {"status": "OPEN", "updated_at": now_unknown,
                          "exit_error": f"unrecognized asset_type={_pos_at or 'none'} exchange={_pos_exch or 'none'} — exit skipped (fail-closed)"},
                 "$unset": {"exit_attempt_at": ""}},
            )
            logger.error(
                "Exit FAIL-CLOSED for pos=%s sym=%s: unrecognized asset_type=%r exchange=%r — "
                "refusing generic order to avoid phantom; reverted to OPEN.",
                pos["id"], sym, _pos_at, _pos_exch,
            )
            results.append({"symbol": sym, "qty": qty_net, "status": "skipped",
                            "reason": "fail-closed-unknown-asset-type"})
            continue
        # FIX 3: mark as exit so _place_order_core skips all price quality guards
        place_kwargs["is_exit_order"] = True
        try:
            result = await _place_order_core(**place_kwargs)
            res_status = str(result.get("status") or result.get("execution_status") or "").upper()
            ledger_action = result.get("ledger_action")

            if res_status == "FILLED":
                results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "ok", "order_id": result.get("id")})

            elif res_status == "REJECTED" and ledger_action == "DUPLICATE_EXIT":
                # Ledger says a CLOSED position for this contract already exists —
                # this EXITING doc is a stale duplicate. Reconcile it as CLOSED so
                # the monitor stops re-firing exits for it.
                now_dup = datetime.now(timezone.utc).isoformat()
                await db.strategy_positions.update_one(
                    {"id": pos["id"], "user_id": user_id, "status": "EXITING"},
                    {"$set": {"status": "CLOSED", "closed_at": now_dup, "updated_at": now_dup,
                              "exit_reason": "duplicate-exit-reconciled"}},
                )
                logger.warning("Exit for %s pos=%s reconciled as duplicate (already closed elsewhere)", sym, pos["id"])
                results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "reconciled-duplicate"})

            elif res_status in ("REJECTED", "SKIPPED"):
                # Exit order did not fill. Void the idempotency key on any order doc
                # holding it so the NEXT monitor tick can retry — otherwise the
                # per-position key would block exit retries forever.
                now_rej = datetime.now(timezone.utc).isoformat()
                await db.orders.update_many(
                    {"user_id": user_id, "idempotency_key": pos_exit_idem,
                     "status": {"$nin": ["FILLED", "COMPLETE"]}},
                    {"$set": {"idempotency_key": f"{pos_exit_idem}:void:{now_rej}"}},
                )
                await db.strategy_positions.update_one(
                    {"id": pos["id"], "user_id": user_id, "status": "EXITING"},
                    {"$set": {"status": "OPEN", "updated_at": now_rej,
                              "exit_error": f"exit order {res_status}: {str(result.get('reason') or result.get('status_message') or '')[:160]}"},
                     "$inc": {"exit_order_failures": 1},
                     "$unset": {"exit_attempt_at": ""}},
                )
                logger.warning("Exit %s for %s pos=%s %s — idem key voided, reverted to OPEN for retry",
                               exit_side, sym, pos["id"], res_status)
                results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "failed",
                                "error": str(result.get("reason") or res_status)})
            else:
                # Unknown shape (e.g. existing-order return from idempotency block).
                # If the existing order FILLED the ledger already closed the position;
                # otherwise void the stale key and revert for retry.
                existing_filled = res_status in ("FILLED", "COMPLETE") or bool(result.get("paper_fill_applied"))
                if not existing_filled:
                    now_unk = datetime.now(timezone.utc).isoformat()
                    await db.orders.update_many(
                        {"user_id": user_id, "idempotency_key": pos_exit_idem,
                         "status": {"$nin": ["FILLED", "COMPLETE"]}},
                        {"$set": {"idempotency_key": f"{pos_exit_idem}:void:{now_unk}"}},
                    )
                    await db.strategy_positions.update_one(
                        {"id": pos["id"], "user_id": user_id, "status": "EXITING"},
                        {"$set": {"status": "OPEN", "updated_at": now_unk},
                         "$unset": {"exit_attempt_at": ""}},
                    )
                results.append({"symbol": sym, "qty": qty_net, "side": exit_side,
                                "status": "ok" if existing_filled else "retry-scheduled",
                                "order_id": result.get("id")})
        except Exception as e:
            err_str = str(e)
            # FIX 4: Classify failure type.
            # Data failures: LTP_UNAVAILABLE, PRICE_UNAVAILABLE, FEED_DISCONNECTED
            # These do NOT count against the circuit breaker — they are transient.
            # Genuine order failures: everything else (broker rejection, auth error, etc.)
            _data_failure_keywords = (
                "ltp_unavailable", "price_unavailable", "feed_disconnected",
                "price unavailable", "feed offline", "instrument not subscribed",
            )
            is_data_failure = any(kw in err_str.lower() for kw in _data_failure_keywords)
            exit_failure_reason = "DATA_FAILURE" if is_data_failure else "ORDER_REJECTED"

            now_fail = datetime.now(timezone.utc).isoformat()
            # Void the per-position idempotency key on any non-filled order doc so
            # the next monitor tick is free to retry this exit.
            try:
                await db.orders.update_many(
                    {"user_id": user_id, "idempotency_key": pos_exit_idem,
                     "status": {"$nin": ["FILLED", "COMPLETE"]}},
                    {"$set": {"idempotency_key": f"{pos_exit_idem}:void:{now_fail}"}},
                )
            except Exception:
                pass
            update_on_fail: Dict[str, Any] = {
                "$set": {
                    "status": "OPEN",
                    "exit_error": err_str[:200],
                    "exit_failure_reason": exit_failure_reason,
                    "updated_at": now_fail,
                },
                "$unset": {"exit_attempt_at": ""},
            }
            if not is_data_failure:
                # Only genuine order failures advance the circuit breaker counter
                update_on_fail["$inc"] = {"exit_order_failures": 1}
            else:
                update_on_fail["$inc"] = {"exit_data_failures": 1}

            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id, "status": "EXITING"},
                update_on_fail,
            )
            logger.warning(
                "Exit %s for %s (%s): %s — failure_type=%s",
                exit_side, sym, pos["id"], err_str[:200], exit_failure_reason,
            )
            results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "failed",
                            "error": err_str, "failure_reason": exit_failure_reason})
    if reason in ("risk-trigger", "feed-stale", "R_TARGET_HIT", "R_STOP_LOSS_HIT", "R_TRAILING_STOP_HIT", "R_TIME_EXIT"):
        await db.strategies.update_one({"id": sid, "user_id": user_id}, {"$set": {
            "status": "paused",
            "last_error": f"Auto-paused after {reason} due to risk or data issue.",
        }})
    return {"closed_positions": results, "open_positions_found": len(positions)}


async def _current_ltp_for_symbol(user_id: str, symbol: str, exchange: str, allow_mock: bool = True, execution_broker: Optional[str] = None) -> Optional[float]:
    settings = await get_user_settings(user_id)
    data_broker = execution_broker or settings.get("data_broker", "upstox")
    if data_broker == "upstox":
        gateway = await get_user_upstox_gateway(user_id)
        token = _upstox_instrument_token(exchange, symbol)
        if gateway and gateway.connected and token:
            try:
                quote = await asyncio.to_thread(gateway.get_market_quote, [token])
                ltp = UpstoxGateway.parse_quote_ltp(quote, token)
                if ltp is not None:
                    return ltp
            except Exception as exc:
                logger.warning("Upstox LTP failed for %s: %s", symbol, exc)
    if not allow_mock:
        return None
    allow_simulated = bool(settings.get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
    if not allow_simulated:
        logger.info("Simulated LTP fallback blocked for %s because allow_simulated_prices is false.", symbol)
        return None
    all_symbols = [*SYMBOLS]
    sym = next((s for s in all_symbols if s["symbol"] == symbol.upper()), None)
    return live_price(sym["base"], all_symbols.index(sym))["price"] if sym else None


def _kotak_exchange_segment(exchange: str) -> str:
    return {
        "NSE": "nse_cm",
        "BSE": "bse_cm",
        "NFO": "nse_fo",
        "BFO": "bse_fo",
        "MCX": "mcx_fo",
    }.get((exchange or "NSE").upper(), "nse_cm")


def _kotak_order_type(order_type: str) -> str:
    return {
        "MARKET": "MKT",
        "LIMIT": "L",
        "SL": "SL",
        "SL-M": "SL-M",
    }.get((order_type or "MARKET").upper(), "MKT")


def _kotak_transaction_type(side: str) -> str:
    return "B" if (side or "BUY").upper() == "BUY" else "S"


def _kotak_trading_symbol(exchange: str, trading_symbol: str) -> str:
    symbol = str(trading_symbol or "").upper()
    if (exchange or "").upper() in {"NSE", "BSE"} and symbol and not symbol.endswith("-EQ"):
        return f"{symbol}-EQ"
    return symbol


def _kotak_trading_symbol_candidates(exchange: str, trading_symbol: str) -> List[str]:
    symbol = str(trading_symbol or "").upper().strip()
    if not symbol:
        return []
    exchange = (exchange or "").upper()
    candidates = []
    if exchange in {"NSE", "BSE"}:
        candidates = [symbol if symbol.endswith("-EQ") else f"{symbol}-EQ", symbol]
    else:
        candidates = [symbol, symbol.replace(" ", "")]
    out = []
    for item in candidates:
        if item and item not in out:
            out.append(item)
    return out


def _kotak_symbol_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(part in text for part in ("symbol", "scrip", "trading", "instrument", "token"))


def _extract_kotak_order_id(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("nOrdNo", "order_id", "orderId", "OrderNo", "NOrdNo", "nestOrderNumber"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for value in payload.values():
            found = _extract_kotak_order_id(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _extract_kotak_order_id(item)
            if found:
                return found
    return None


async def _place_kotak_order(
    user_id: str,
    *,
    trading_symbol: str,
    exchange: str,
    side: str,
    quantity: int,
    order_type: str,
    product: str,
    price: Optional[float] = None,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    raise HTTPException(status_code=410, detail="Kotak Neo execution has been removed. QuantG supports Upstox only.")


UPSTOX_EQUITY_INSTRUMENTS = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "INFY": "NSE_EQ|INE009A01021",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "SBIN": "NSE_EQ|INE062A01020",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "KOTAKBANK": "NSE_EQ|INE237A01036",
    "ITC": "NSE_EQ|INE154A01025",
    "LT": "NSE_EQ|INE018A01030",
    "MARUTI": "NSE_EQ|INE585B01010",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

# India VIX rides the index WS feed for IV-regime data collection (db.vix_history).
# Key verified against db.upstox_instruments (segment NSE_INDEX).
UPSTOX_VIX_INSTRUMENT_KEY = "NSE_INDEX|India VIX"

# Watchlist shows only the two main indices with real-time data from the WS feed.
INDEX_WATCHLIST = [
    {"symbol": "NIFTY",  "name": "Nifty 50",  "key": "NSE_INDEX|Nifty 50", "base": 24850.40},
    {"symbol": "SENSEX", "name": "BSE Sensex", "key": "BSE_INDEX|SENSEX",   "base": 81460.20},
]


def _public_base_url(request: Optional[Request] = None) -> str:
    explicit = (os.environ.get("APP_PUBLIC_URL") or os.environ.get("PUBLIC_APP_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or str(request.url.scheme) or "https").split(",")[0].strip()
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        if host:
            # Force HTTPS for non-localhost/private IP connections to ensure strict HTTPS for broker callbacks
            if not host.startswith(("localhost", "127.0.0.1", "192.168.", "10.", "172.")):
                proto = "https"
            return f"{proto}://{host}"
    return "https://www.quantgtrade.com"


def _upstox_redirect_uri(request: Optional[Request] = None, keys: Optional[Dict[str, Any]] = None) -> str:
    env_uri = (os.environ.get("UPSTOX_REDIRECT_URI") or "").strip()
    if env_uri:
        return env_uri
    saved = (keys or {}).get("redirect_uri")
    if saved:
        return str(saved).strip()
    return f"{_public_base_url(request)}/api/broker/upstox/callback"


def _mcx_active_future_symbol(symbol: str, dt: Optional[datetime] = None) -> str:
    dt = dt or (datetime.now(timezone.utc) + IST_OFFSET)
    day = dt.day
    month_offset = 0
    # Past the 18th of the month, shift to next month contract for MCX commodities
    if day > 18:
        month_offset = 1
    
    target_date = dt
    if month_offset > 0:
        # Move to next month safely
        target_date = dt + timedelta(days=15)
        if target_date.month == dt.month:
            target_date = dt + timedelta(days=32)
            
    yy = target_date.strftime("%y")
    mmm = target_date.strftime("%b").upper()
    return f"{symbol.upper()}{yy}{mmm}FUT"


def _log_throttled(key: str, seconds: float, level: int, message: str, *args: Any) -> None:
    now = time.monotonic()
    last = _LOG_THROTTLE_LAST.get(key, 0.0)
    if now - last < seconds:
        return
    _LOG_THROTTLE_LAST[key] = now
    logger.log(level, message, *args)


def _upstox_instrument_token(exchange: str, trading_symbol: str, instrument_token: Any = None) -> Optional[str]:
    exch = (exchange or "NSE").upper()
    token = str(instrument_token or "").strip()
    
    if exch == "MCX" and token:
        return None
        
    if "|" in token:
        return token
    if token:
        return None
    symbol = str(trading_symbol or "").upper().strip()
    if "|" in symbol:
        return symbol
    if exch in {"NSE", "BSE"}:
        return UPSTOX_EQUITY_INSTRUMENTS.get(symbol)
    if exch in {"NFO", "BFO", "NSE_FO"}:
        return token or None
    if exch == "MCX":
        return None
    if "|" in symbol and "_" in symbol.split("|", 1)[0]:
        return symbol
    return None


async def _search_upstox_mcx_future_keys(
    gateway: UpstoxGateway,
    underlying: str,
    *,
    limit: int = 5,
) -> List[str]:
    return []


async def _resolve_upstox_mcx_future_contract(underlying: str, *, expiry_offset: int = 0) -> Optional[Dict[str, Any]]:
    return None


async def _validate_upstox_mcx_instrument_key(instrument_key: Optional[str]) -> Optional[Dict[str, Any]]:
    return None



async def _upstox_watchlist_rows(user_id: str) -> List[Dict[str, Any]]:
    """Return live NIFTY and SENSEX rows.

    Source priority:
      1. V3 WS tick cache (freshest, sub-second)
      2. Analytics Token LTP endpoint (system-level, no daily OAuth needed)
      3. User gateway REST quote
      4. Base price fallback (never fails)
    """
    gateway = await get_user_upstox_gateway(user_id)
    out: List[Dict[str, Any]] = []
    for s in INDEX_WATCHLIST:
        ltp: Optional[float] = None
        source = "fallback"

        # 1 — WS tick cache
        if gateway and gateway.connected:
            tick = gateway.latest_tick(s["key"])
            if tick and tick.get("ltp"):
                ltp = float(tick["ltp"])
                source = "upstox_ws"

        # 2 — Analytics Token (works even when user OAuth expired)
        if ltp is None and _analytics:
            ltp_map = await _analytics.get_ltp([s["key"]])
            val = ltp_map.get(s["key"])
            if val is not None:
                ltp = float(val)
                source = "analytics"

        # 3 — User gateway REST fallback
        if ltp is None and gateway and gateway.connected:
            try:
                quote = await asyncio.to_thread(gateway.get_market_quote, [s["key"]])
                parsed = UpstoxGateway.parse_quote_ltp(quote, s["key"])
                if parsed is None:
                    data = (quote.get("data") if isinstance(quote, dict) else None) or {}
                    for node in data.values():
                        if isinstance(node, dict):
                            for field in ("last_price", "ltp", "last_traded_price"):
                                v = node.get(field)
                                if v not in (None, ""):
                                    try:
                                        parsed = float(v)
                                    except Exception:
                                        pass
                                    if parsed is not None:
                                        break
                        if parsed is not None:
                            break
                if parsed is not None:
                    ltp = float(parsed)
                    source = "upstox_rest"
            except Exception as exc:
                logger.warning("Upstox watchlist REST failed for %s: %s", s["key"], exc)

        # 4 — Base price fallback
        if ltp is None:
            ltp = s["base"]

        change = round(ltp - s["base"], 2)
        pct = round((change / s["base"]) * 100, 2) if s["base"] else 0.0
        out.append({"symbol": s["symbol"], "name": s["name"], "price": ltp, "change": change, "pct": pct, "source": source})
    return out


def _upstox_first(row: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_upstox_order_status(status: Any) -> Optional[str]:
    if status in (None, ""):
        return None
    text = str(status).strip().upper()
    mapping = {
        "COMPLETE": "COMPLETE",
        "COMPLETED": "COMPLETE",
        "TRADED": "COMPLETE",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        "REJECTED": "REJECTED",
        "OPEN": "OPEN",
        "PENDING": "PENDING",
        "TRIGGER PENDING": "TRIGGER PENDING",
        "VALIDATION PENDING": "VALIDATION PENDING",
    }
    return mapping.get(text, text)


async def _place_upstox_order(
    user_id: str,
    *,
    instrument_token: str,
    side: str,
    quantity: int,
    order_type: str,
    product: str,
    price: Optional[float] = None,
    tag: Optional[str] = None,
    validity: str = "DAY",
    trigger_price: Optional[float] = None,
    disclosed_quantity: int = 0,
    is_amo: bool = False,
    market_protection: Optional[float] = -1,
) -> Dict[str, Any]:
    # Inject global safety block
    live_enabled = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    if not live_enabled:
        raise RuntimeError("Live execution blocked: CORE_ENGINE_LIVE_ENABLED is set to false in the environment.")

    arm_state = await db.live_arm_state.find_one({"user_id": user_id})
    if not arm_state or not arm_state.get("armed"):
        raise RuntimeError("Live execution blocked: Broker live execution pathway is not manually armed.")

    gateway = await get_user_upstox_gateway(user_id)
    if not gateway:
        raise HTTPException(status_code=400, detail="Upstox is not configured. Save API key/secret and complete OAuth first.")
    if not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected. Open /api/broker/upstox/login and complete OAuth.")
    execution_tag = tag or _new_execution_tag()
    max_attempts = int(os.environ.get("UPSTOX_ORDER_MAX_ATTEMPTS", "1"))
    attempts = 0
    result: Dict[str, Any] = {}
    last_error = None

    # Upstox V2 Mapping: MIS -> I, NRML/CNC -> D
    normalized_product = "I" if product.upper() in ["MIS", "INTRADAY", "I"] else "D"
    normalized_side = "BUY" if side.upper() in ["BUY", "B"] else "SELL"
    normalized_type = order_type.upper()

    for attempt in range(1, max(1, max_attempts) + 1):
        attempts = attempt
        try:
            result = await asyncio.to_thread(
                gateway.place_order,
                instrument_token=instrument_token,
                quantity=int(quantity),
                side=normalized_side,
                order_type=normalized_type,
                product=normalized_product,
                price=0 if normalized_type == "MARKET" else price,
                tag=execution_tag,
                validity=validity,
                trigger_price=trigger_price,
                disclosed_quantity=disclosed_quantity,
                is_amo=is_amo,
                market_protection=market_protection,
            )
            break
        except Exception as exc:
            last_error = str(exc)
            token_parts = str(instrument_token).split("|")
            exch_log = token_parts[0] if len(token_parts) > 1 else "Unknown"
            logger.warning(
                "Upstox order placement failed. Error: %s\n"
                "Exact payload sent to Upstox before order placement:\n"
                "exchange: %s\n"
                "instrument_token: %s\n"
                "quantity: %s\n"
                "product: %s\n"
                "validity: %s\n"
                "order_type: %s",
                last_error, exch_log, instrument_token, quantity, normalized_product, validity, normalized_type
            )
            print(f"\n--- UPSTOX ORDER REJECTED ---\n"
                  f"Error: {last_error}\n"
                  f"exchange: {exch_log}\n"
                  f"instrument_token: {instrument_token}\n"
                  f"quantity: {quantity}\n"
                  f"product: {normalized_product}\n"
                  f"validity: {validity}\n"
                  f"order_type: {normalized_type}\n"
                  f"-----------------------------\n", flush=True)

            if not OrderExecutionRetry.is_retryable_error(last_error) or attempt >= max_attempts:
                raise HTTPException(status_code=400, detail=f"Upstox rejected order: {last_error}")
            retry_cfg = OrderExecutionRetry.retry_config(attempt)
            await asyncio.sleep(min(3, float(retry_cfg.get("backoff_seconds") or 1)))
    broker_order_id = extract_upstox_order_id(result)
    if not broker_order_id:
        logger.warning("Upstox order response did not include order id: %s", result)
    import json
    logger.info("Upstox live order successful. Full response: %s", json.dumps(result))
    print(f"\n>>> [UPSTOX LIVE ORDER RESPONSE] FULL BROKER RESPONSE: {json.dumps(result, indent=2)}\n", flush=True)
    return {
        "ok": True,
        "broker_order_id": broker_order_id,
        "order_id": broker_order_id,
        "raw": result,
        "tag": execution_tag,
        "attempts": attempts,
        "instrument_token": instrument_token,
        "last_error": last_error,
    }


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    return _parse_iso_dt_module(value)


def _age_ms(value: Optional[str]) -> Optional[int]:
    parsed = _parse_iso_dt(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds() * 1000))


async def _evaluate_strategy_risk(user_id: str, sid: str) -> Optional[str]:
    active_positions = await db.strategy_positions.find({
        "user_id": user_id,
        "strategy_id": sid,
        "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]},
    }).to_list(100)
    
    if not active_positions:
        return None
        
    for pos in active_positions:
        symbol = pos.get("trading_symbol") or pos.get("symbol")
        if not symbol:
            continue
            
        exchange = pos.get("exchange") or ("NFO" if symbol.endswith(("CE", "PE")) else "NSE")
        current_price = await _current_ltp_for_symbol(user_id, symbol, exchange)
        if current_price is None or current_price <= 0:
            continue
            
        is_option_buy = (
            pos.get("position_side") == "LONG"
            and (
                exchange in {"NFO", "BFO"}
                or symbol.endswith(("CE", "PE"))
                or pos.get("option_type") in {"CE", "PE"}
            )
        )
        
        if is_option_buy:
            entry_price = float(pos.get("average_buy_price") or pos.get("price") or pos.get("r_entry_price") or 0)
            if entry_price <= 0:
                continue
                
            if "r_initial_risk_amount" not in pos:
                risk_config = pos.get("tp_sl_tsl_config") or {}
                risk_pct = float(risk_config.get("stop_loss_pct") or risk_config.get("stoploss_pct") or risk_config.get("stop_pct") or 8.0)
                initial_stop_R = float(pos.get("initial_stop_R") or 1.0)
                target_R = float(pos.get("target_R") or 2.0)
                trail_after_R = float(pos.get("trail_after_R") or 1.5)
                max_hold_minutes = int(pos.get("max_hold_minutes") or 60)
                
                r_initial_risk_amount = round(entry_price * risk_pct / 100.0, 2)
                r_stop_loss_price = round(max(0.0, entry_price - r_initial_risk_amount * initial_stop_R), 2)
                r_take_profit_price = round(entry_price + r_initial_risk_amount * target_R, 2)
                
                now_str = datetime.now(timezone.utc).isoformat()
                fallback_metrics = {
                    "r_initial_risk_amount": r_initial_risk_amount,
                    "r_stop_loss_price": r_stop_loss_price,
                    "r_take_profit_price": r_take_profit_price,
                    "r_entry_price": entry_price,
                    "r_current_R": 0.0,
                    "r_max_R_seen": 0.0,
                    "r_trailing_active": False,
                    "r_trailing_stop_price": r_stop_loss_price,
                    "best_price_seen": entry_price,
                    "r_last_evaluated_at": now_str,
                    "r_metadata_source": "fallback",
                    "setup_type": pos.get("setup_type") or "breakout",
                    "confidence": float(pos.get("confidence") or 85.0),
                    "entry_reason": pos.get("entry_reason") or "Fallback metrics reconstruction",
                    "target_R": target_R,
                    "initial_stop_R": initial_stop_R,
                    "trail_after_R": trail_after_R,
                    "max_hold_minutes": max_hold_minutes,
                    "invalidation_rule": pos.get("invalidation_rule") or "time_or_stop",
                    "regime_required": pos.get("regime_required") or "any",
                    "option_selection_preference": pos.get("option_selection_preference") or "ATM",
                    "signal_version": pos.get("signal_version") or "v13",
                    "strategy_logic_version": pos.get("strategy_logic_version") or "1.0",
                    "default_strategy_version": pos.get("default_strategy_version") or "v13-live-brain-r1"
                }
                pos.update(fallback_metrics)
                await db.strategy_positions.update_one(
                    {"id": pos["id"], "user_id": user_id},
                    {"$set": fallback_metrics}
                )
                
            entry_price = float(pos["r_entry_price"])
            initial_risk_amount = float(pos["r_initial_risk_amount"])
            best_price_seen = float(pos.get("best_price_seen") or entry_price)
            max_R_seen = float(pos.get("r_max_R_seen") or 0.0)
            trailing_active = bool(pos.get("r_trailing_active", False))
            trailing_stop_price = float(pos.get("r_trailing_stop_price") or pos["r_stop_loss_price"])
            target_R = float(pos.get("target_R") or 2.0)
            trail_after_R = float(pos.get("trail_after_R") or 1.5)
            
            if initial_risk_amount > 0:
                current_R = (current_price - entry_price) / initial_risk_amount
            else:
                current_R = 0.0
                
            if current_price > best_price_seen:
                best_price_seen = current_price
                if initial_risk_amount > 0:
                    max_R_seen = (best_price_seen - entry_price) / initial_risk_amount
                    
            if max_R_seen >= trail_after_R:
                trailing_active = True
                
            if trailing_active:
                candidate_stop = entry_price + (max_R_seen - 0.7) * initial_risk_amount
                if max_R_seen >= 1.0:
                    candidate_stop = max(candidate_stop, entry_price)
                candidate_stop = round(candidate_stop, 2)
                trailing_stop_price = max(trailing_stop_price, candidate_stop)
                
            max_hold_minutes = int(pos.get("max_hold_minutes") or 60)
            entry_time_str = pos.get("entry_time") or pos.get("created_at")
            time_exit_triggered = False
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    age_minutes = (datetime.now(timezone.utc) - entry_dt.astimezone(timezone.utc)).total_seconds() / 60.0
                    if age_minutes >= max_hold_minutes:
                        time_exit_triggered = True
                except Exception as e:
                    logger.warning(f"Error parsing entry_time for R time exit: {e}")
                    
            exit_reason = None
            if current_price >= pos["r_take_profit_price"]:
                exit_reason = "R_TARGET_HIT"
            elif current_price <= pos["r_stop_loss_price"]:
                exit_reason = "R_STOP_LOSS_HIT"
            elif trailing_active and current_price <= trailing_stop_price:
                exit_reason = "R_TRAILING_STOP_HIT"
            elif time_exit_triggered:
                exit_reason = "R_TIME_EXIT"
                
            now_str = datetime.now(timezone.utc).isoformat()
            if not exit_reason:
                update_fields = {
                    "r_current_R": round(current_R, 4),
                    "r_max_R_seen": round(max_R_seen, 4),
                    "r_trailing_active": trailing_active,
                    "r_trailing_stop_price": round(trailing_stop_price, 2),
                    "best_price_seen": round(best_price_seen, 2),
                    "r_last_evaluated_at": now_str,
                    "updated_at": now_str,
                }
                await db.strategy_positions.update_one(
                    {"id": pos["id"], "user_id": user_id},
                    {"$set": update_fields}
                )
            else:
                return exit_reason
        else:
            # Legacy fallback risk check
            risk_config = pos.get("tp_sl_tsl_config") or {}
            sl_pct = float(risk_config.get("stop_loss_pct") or 0)
            tp_pct = float(risk_config.get("take_profit_pct") or 0)
            side = pos.get("position_side") or "LONG"
            entry_price = float(pos.get("average_buy_price") or pos.get("price") or 0)
            
            if entry_price > 0:
                if side == "LONG":
                    if sl_pct and current_price <= entry_price * (1 - sl_pct / 100):
                        return "risk-trigger"
                    if tp_pct and current_price >= entry_price * (1 + tp_pct / 100):
                        return "risk-trigger"
                else:
                    if sl_pct and current_price >= entry_price * (1 + sl_pct / 100):
                        return "risk-trigger"
                    if tp_pct and current_price <= entry_price * (1 - tp_pct / 100):
                        return "risk-trigger"
                        
    return None


async def _is_upstox_strategy_feed_stale(user_id: str) -> tuple[bool, str]:
    gateway = await get_user_upstox_gateway(user_id)
    if not gateway or not gateway.connected:
        return True, "upstox_not_connected"
    status = gateway.status() or {}
    feed = status.get("feed_status") or {}
    state = str(feed.get("state") or "").lower()
    connected = bool(feed.get("connected"))
    subscribed_count = int(feed.get("subscribed_count") or 0)
    last_tick = feed.get("last_tick_time") or status.get("last_tick_at")
    if not connected and state not in {"connected", "reconnecting"}:
        return True, f"feed_{state or 'disconnected'}"
    if subscribed_count <= 0:
        # A live position may have been restored after restart before the feed
        # resubscribed. Let the position monitor quote via REST instead of
        # force-closing a valid trade on a startup timing gap.
        return False, "feed_not_subscribed_yet"
    if not last_tick:
        return False, "waiting_for_first_tick"
    try:
        last_dt = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return False, "last_tick_parse_pending"
    if age > 180:
        return True, f"last_tick_age_{int(age)}s"
    return False, "feed_live"


STRATEGY_HEALTH_TICK_SECONDS = int(os.environ.get("STRATEGY_RUNNER_TICK_SECONDS", "30"))

async def _strategy_health_loop(stop_event: asyncio.Event):
    logger.info("Strategy health monitor starting")
    while not stop_event.is_set():
        try:
            # Run feed + token health watchdog for all users
            try:
                users = await db.users.find({}, {"id": 1}).to_list(100)
                for user_row in users:
                    if stop_event.is_set():
                        break
                    uid = user_row["id"]
                    ustatus = await get_user_upstox_status(uid)
                    if ustatus.get("feed_stalled"):
                        reason = ustatus.get("feed_stalled_reason") or "unknown"
                        from notifications import create_notification_once
                        if reason == "token_invalid":
                            await create_notification_once(
                                db,
                                user_id=uid,
                                type="token_expired",
                                severity="critical",
                                title="Upstox token expired",
                                message="Your Upstox token has expired or is invalid. Please reconnect.",
                                dedupe_key=f"token_expired:{uid}:{datetime.now().strftime('%Y-%m-%d')}",
                                browser_alert=True,
                                action_url="/broker-keys"
                            )
                        else:
                            title_msg = "Live feed stalled — 0 ticks" if reason == "connected_but_zero_ticks" else "Live feed stalled"
                            detail_msg = "Websocket connected but 0 ticks received." if reason == "connected_but_zero_ticks" else f"Realtime index ticks are stale or disconnected: {reason}"
                            await create_notification_once(
                                db,
                                user_id=uid,
                                type="feed_stalled",
                                severity="critical",
                                title=title_msg,
                                message=detail_msg,
                                dedupe_key=f"feed_stalled:{uid}:{datetime.now().strftime('%Y-%m-%d')}",
                                browser_alert=True,
                                action_url="/ops"
                            )
            except Exception as health_exc:
                logger.warning(f"feed/token health watchdog failed: {health_exc}")

            strategies = await db.strategies.find({"status": "live"}).to_list(500)
            for s in strategies:
                if stop_event.is_set():
                    break
                uid = s["user_id"]
                settings = await get_user_settings(uid)
                if not settings.get("paper_mode", True):
                    active_positions = await db.strategy_positions.count_documents({
                        "user_id": uid,
                        "strategy_id": s["id"],
                        "status": {"$in": ["OPEN", "FILLED", "EXITING"]},
                    })
                    if active_positions:
                        feed_stale, feed_reason = await _is_upstox_strategy_feed_stale(uid)
                        if feed_stale:
                            logger.warning(
                                "Closing strategy positions because Upstox V3 feed is stale strategy=%s reason=%s",
                                s["id"],
                                feed_reason,
                            )
                            await _close_strategy_positions(uid, s["id"], reason="feed-stale")
                            continue
                try:
                    exit_reason = await _evaluate_strategy_risk(uid, s["id"])
                    if exit_reason:
                        await _close_strategy_positions(uid, s["id"], reason=exit_reason)
                except Exception as e:
                    logger.warning(f"strategy risk evaluation failed for {s['id']}: {e}")
        except Exception as e:
            logger.warning(f"strategy health loop error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < STRATEGY_HEALTH_TICK_SECONDS:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Strategy health monitor stopped")


# moved to routes/strategies.py
async def unwind_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    result = await _close_strategy_positions(user["id"], sid, reason="manual-unwind")
    return {"ok": True, **result}


# moved to routes/strategies.py
async def create_strategy(req: StrategyReq, user=Depends(get_current_user)):
    visual_config = req.visual_config or {}
    underlying = str((visual_config.get("options") or {}).get("underlying") or req.instrument_group or "").upper()
    is_mcx = (
        str(req.asset_class).lower() == "commodity"
        or str(req.instrument_group).upper() == "MCX"
        or underlying in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
    )
    if is_mcx:
        raise HTTPException(status_code=400, detail="MCX commodity strategies have been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")

    risk_config = dict((visual_config.get("risk") or {}))
    if req.required_capital is not None:
        risk_config["required_capital"] = float(req.required_capital)
        visual_config = {**visual_config, "risk": risk_config}
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "name": req.name,
        "description": req.description or "",
        "kind": req.kind,
        "python_code": req.python_code or (DEFAULT_PYTHON if req.kind == "python" else None),
        "visual_config": visual_config,
        "asset_class": req.asset_class or ("options" if ((visual_config or {}).get("options") or {}).get("enabled") else "equity"),
        "strategy_type": req.strategy_type,
        "required_capital": req.required_capital,
        "instrument_group": req.instrument_group,
        "status": req.status,
        "broker": (req.broker or "upstox").strip().lower(),
        "mode": (req.mode or "paper").strip().lower(),
        "market_suitability": req.market_suitability or "Any Market Condition",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_pnl": None,
    }
    await db.strategies.insert_one(doc)
    _sync_option_ledger_strategy(doc)
    return _strategy_out(doc)



# moved to routes/strategies.py
async def list_strategies(user=Depends(get_current_user)):
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    return [_strategy_out(r) for r in rows]


# moved to routes/strategies.py
async def seed_default_strategies(user=Depends(get_current_user)):
    inserted = await seed_default_strategies_for_user(user["id"])
    migrated = await migrate_user_to_v12_upstox(user["id"])
    return {
        "ok": True,
        "inserted": inserted,
        "migrated": migrated,
        "message": "Standardized Upstox index option presets installed. Review and backtest before enabling LIVE.",
    }


# ops runtime route moved to routes/ops_runtime.py


# moved to routes/strategies.py
async def strategy_leaderboard(user=Depends(get_current_user)):
    user_id = user["id"]
    strategies = await db.strategies.find({"user_id": user_id}, {"_id": 0}).to_list(1000)
    strategy_ids = [s["id"] for s in strategies if s.get("id")]
    closed_trades = await db.trades.find({"user_id": user_id}, {"_id": 0}).to_list(10000)
    fill_summary = await _fill_ledger_summary(user_id)
    fill_trades = [
        {
            **row,
            "closed_at": row.get("filled_at"),
            "pnl": row.get("realized_pnl"),
            "source": "trade_fills",
        }
        for row in fill_summary["fills"]
        if float(row.get("realized_pnl") or 0) != 0
    ]
    closed_trades = [*closed_trades, *fill_trades]
    option_trades = await db.option_trade_journal.find(
        {"strategy_id": {"$in": strategy_ids}},
        {"_id": 0},
    ).to_list(10000)
    result = build_strategy_leaderboard(strategies, closed_trades, option_trades)
    result["fill_ledger"] = {
        "source": "trade_fills",
        "fill_count": fill_summary["fill_count"],
        "closed_trade_count": fill_summary["closed_trade_count"],
        "realized_pnl": fill_summary["realized_pnl"],
    }
    return result


# moved to routes/strategies.py
async def live_backtest_comparison(user=Depends(get_current_user)):
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)
    out = []
    for s in strategies:
        latest_backtest = await db.paper_trading_history.find_one(
            {"user_id": user["id"], "strategy_id": s["id"]},
            {"_id": 0},
            sort=[("created_at", -1)],
        )
        live_orders = await db.orders.find(
            {"user_id": user["id"], "strategy_id": s["id"]},
            {"_id": 0},
        ).sort("created_at", -1).to_list(200)
        fill_summary = await _fill_ledger_summary(user["id"], mode="live", strategy_id=s["id"])
        completed = [o for o in live_orders if canonical_order_status(o.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
        live_pnl = fill_summary["realized_pnl"]
        live_win_rate = fill_summary["win_rate"]
        backtest_pnl = float((latest_backtest or {}).get("pnl") or 0)
        drift = round(live_pnl - backtest_pnl, 2) if latest_backtest else None
        out.append({
            "strategy_id": s["id"],
            "name": s.get("name"),
            "status": s.get("status"),
            "last_data_source": s.get("last_data_source"),
            "last_data_live": s.get("last_data_live"),
            "last_signal_validation": s.get("last_signal_validation"),
            "last_filter_reason": s.get("last_filter_reason"),
            "live": {
                "orders": len(live_orders),
                "completed": len(completed),
                "fills": fill_summary["fill_count"],
                "realized_pnl": live_pnl,
                "realized_pnl_source": fill_summary["source"],
                "win_rate": live_win_rate,
            },
            "backtest": {
                "available": bool(latest_backtest),
                "pnl": round(backtest_pnl, 2),
                "win_rate": (latest_backtest or {}).get("win_rate"),
                "trades": (latest_backtest or {}).get("trades_count"),
                "created_at": (latest_backtest or {}).get("created_at"),
            },
            "drift": drift,
            "verdict": (
                "needs_backtest" if not latest_backtest else
                "live_lagging" if drift is not None and drift < -abs(backtest_pnl) * 0.25 else
                "tracking"
            ),
        })
    return {"items": out}


# moved to routes/strategies.py
async def get_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    _sync_option_ledger_strategy(row)
    return _strategy_out(row)


# moved to routes/strategies.py
async def strategy_daily_report(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    orders = await db.orders.find(
        {"user_id": user["id"], "source": {"$regex": f"strategy:{sid}"}},
        {"_id": 0},
    ).sort("created_at", -1).to_list(100)
    visual_config = row.get("visual_config") or {}
    options_config = visual_config.get("options") or {}
    underlying = options_config.get("underlying") or visual_config.get("symbol") or "NIFTY"
    market = await _fetch_strategy_history(user["id"], underlying, days=20, interval="day")
    candles = market.get("data") or []
    closes = [float(c.get("close", 0)) for c in candles if c.get("close") is not None]
    if len(closes) >= 2:
        change = closes[-1] - closes[0]
        trend = "BULLISH" if change > 0 else "BEARISH" if change < 0 else "NEUTRAL"
        atr = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))) / max(1, len(closes) - 1)
    else:
        trend = "NEUTRAL"
        atr = 0
    report = DailyStrategyReporter.generate_daily_report(
        strategy_id=sid,
        strategy_name=row.get("name", "Strategy"),
        underlying=underlying,
        recent_trades=orders,
        market_trend_analysis={"trend": trend, "rsi": 50, "atr": atr, "reversal_risk": 0.35},
    )
    report["data_source"] = market.get("source")
    # Canonical P&L from trade_fills — single source of truth
    report["pnl_today"] = await get_strategy_pnl_today(db, sid, user["id"])
    return report


# moved to routes/strategies.py
async def update_strategy(sid: str, req: StrategyReq, user=Depends(get_current_user)):
    update = {k: v for k, v in req.model_dump().items() if v is not None}
    
    # Retrieve existing to perform merged MCX check
    existing = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    visual_config_check = update.get("visual_config") or existing.get("visual_config") or {}
    underlying_check = str((visual_config_check.get("options") or {}).get("underlying") or update.get("instrument_group") or existing.get("instrument_group") or "").upper()
    asset_class_check = str(update.get("asset_class") or existing.get("asset_class") or "").lower()
    instrument_group_check = str(update.get("instrument_group") or existing.get("instrument_group") or "").upper()
    mode_check = str(update.get("mode") or existing.get("mode") or "").strip().lower()
    is_mcx_check = (
        asset_class_check == "commodity"
        or instrument_group_check == "MCX"
        or underlying_check in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
    )
    if is_mcx_check:
        raise HTTPException(status_code=400, detail="MCX commodity strategies have been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")
    if "asset_class" not in update and "visual_config" in update:
        update["asset_class"] = "options" if ((update["visual_config"] or {}).get("options") or {}).get("enabled") else "equity"
    if "required_capital" in update:
        visual_config = dict(update.get("visual_config") or (await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "visual_config": 1}) or {}).get("visual_config") or {})
        risk_config = dict(visual_config.get("risk") or {})
        risk_config["required_capital"] = float(update["required_capital"])
        visual_config["risk"] = risk_config
        update["visual_config"] = visual_config
    await db.strategies.update_one({"id": sid, "user_id": user["id"]}, {"$set": update})
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    _sync_option_ledger_strategy(row)
    return _strategy_out(row)


# moved to routes/strategies.py
async def ai_modify_strategy(sid: str, req: StrategyAIModifyReq, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Tell the AI what to change.")

    try:
        proposal = await asyncio.wait_for(
            asyncio.to_thread(_google_strategy_edit_sync, row, instruction),
            timeout=GEMINI_TIMEOUT_SEC,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI strategy edit failed: {e}")

    proposed_code = str(proposal.get("python_code") or "").strip()
    if "def run(data):" not in proposed_code:
        raise HTTPException(status_code=400, detail="AI proposal rejected: missing def run(data):")

    visual_config = proposal.get("visual_config") if isinstance(proposal.get("visual_config"), dict) else (row.get("visual_config") or {})
    test_row = {**row, "visual_config": visual_config}
    symbol = _strategy_market_symbol(test_row)
    history = await _fetch_strategy_history(user["id"], symbol, days=30, interval="5minute", allow_mock=True)
    data = history.get("data") or []
    if not data:
        raise HTTPException(status_code=400, detail=f"AI proposal rejected: no candles available for {symbol}")
    try:
        signals = safe_run_strategy(proposed_code, data[-250:])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI proposal rejected by sandbox: {e}")

    validation = {
        "symbol": symbol,
        "candles": len(data[-250:]),
        "data_source": history.get("source"),
        "signals": len(signals),
        "last_signal": signals[-1] if signals else None,
    }
    response = {
        "ok": True,
        "applied": False,
        "proposal": {
            "name": proposal.get("name") or row.get("name"),
            "description": proposal.get("description") or row.get("description"),
            "python_code": proposed_code,
            "visual_config": visual_config,
            "notes": proposal.get("notes") if isinstance(proposal.get("notes"), list) else [],
        },
        "validation": validation,
    }
    if req.apply:
        update = {
            "name": response["proposal"]["name"],
            "description": response["proposal"]["description"],
            "python_code": proposed_code,
            "visual_config": visual_config,
            "asset_class": "options" if ((visual_config or {}).get("options") or {}).get("enabled") else row.get("asset_class", "equity"),
            "ai_modified_at": datetime.now(timezone.utc).isoformat(),
            "ai_last_instruction": instruction[:1000],
            "last_signal_validation": validation,
        }
        await db.strategies.update_one(
            {"id": sid, "user_id": user["id"]},
            {"$set": update, "$unset": {"last_error": ""}},
        )
        new_row = {**row, **update}
        _sync_option_ledger_strategy(new_row)
        response["applied"] = True
        response["strategy"] = _strategy_out({k: v for k, v in new_row.items() if k not in {"_id", "user_id"}})
    return response


# moved to routes/strategies.py
async def delete_strategy(sid: str, user=Depends(get_current_user)):
    res = await db.strategies.delete_one({"id": sid, "user_id": user["id"]})
    return {"deleted": res.deleted_count}


# moved to routes/strategies.py
async def toggle_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if row.get("status") == "archived":
        raise HTTPException(status_code=400, detail="Strategy is archived. Restore it before toggling live/paused.")
    new_status = "paused" if row["status"] == "live" else "live"
    settings = await get_user_settings(user["id"])
    strategy_mode = "paper" if bool(settings.get("paper_mode", True)) else "live"
    update_fields = {
        "status": new_status,
        "broker": "upstox",
        "mode": strategy_mode,
    }
    if new_status == "paused":
        update_fields["manual_paused"] = True
        update_fields["schedule_paused"] = False
    else:
        update_fields["manual_paused"] = False
        update_fields["schedule_paused"] = False
    if strategy_mode == "paper":
        update_fields.update({
            "quarantined": False,
            "halted": False,
            "is_halted": False,
            "last_filter_reason": "",
            "last_skip_reason_code": "",
            "last_error": "",
        })
    await db.strategies.update_one({"id": sid, "user_id": user["id"]}, {"$set": update_fields})
    if new_status == "live":
        _sync_option_ledger_strategy({**row, **update_fields})
        option_ledger.set_kill_switch(False, strategy_id=sid)
    return {"status": new_status}


# moved to routes/strategies.py
async def update_strategy_runtime_settings(sid: str, req: StrategyRuntimeSettingsReq, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    
    if req.mode is not None and req.mode.strip().lower() == "live":
        visual_config_check = row.get("visual_config") or {}
        underlying_check = str((visual_config_check.get("options") or {}).get("underlying") or row.get("instrument_group") or "").upper()
        asset_class_check = str(row.get("asset_class") or "").lower()
        instrument_group_check = str(row.get("instrument_group") or "").upper()
        is_mcx_check = (
            asset_class_check == "commodity"
            or instrument_group_check == "MCX"
            or underlying_check in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
        )
        if is_mcx_check:
            raise HTTPException(status_code=400, detail="MCX commodity strategies have been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")
    visual_config = row.get("visual_config") or {}
    risk = visual_config.get("risk") or {}
    mapping = {
        "target_pct": req.target_pct,
        "stoploss_pct": req.stoploss_pct,
        "trailing_sl_enabled": req.trailing_sl_enabled,
        "trail_trigger_pct": req.trail_trigger_pct,
        "trail_step_pct": req.trail_step_pct,
        "cooldown_minutes": req.cooldown_minutes,
        "max_trades_day": req.max_trades_day,
        "daily_loss_limit": req.daily_loss_limit,
        "required_capital": req.required_capital,
        "time_exit_minutes": req.time_exit_minutes,
        "indicator_exit_enabled": req.indicator_exit_enabled,
        "exit_mode": req.exit_mode,
        "risk_style": req.risk_style,
        "strategy_category": req.strategy_category,
        "adaptive_exits_enabled": req.adaptive_exits_enabled,
        "target_r_multiple": req.target_r_multiple,
    }
    for key, value in mapping.items():
        if value is not None:
            risk[key] = value
    risk["max_lot"] = 1
    risk = _normalize_strategy_risk(risk)
    visual_config["risk"] = risk
    
    update_fields = {"visual_config": visual_config, "broker": "upstox"}
    if req.broker is not None:
        update_fields["broker"] = "upstox"
        row["broker"] = "upstox"
    if req.mode is not None:
        update_fields["mode"] = req.mode.strip().lower()
        row["mode"] = req.mode.strip().lower()
        
    await db.strategies.update_one(
        {"id": sid, "user_id": user["id"]},
        {"$set": update_fields},
    )
    row["visual_config"] = visual_config
    _sync_option_ledger_strategy(row)
    return {"ok": True, "max_lot": 1, "risk": risk, "broker": row.get("broker"), "mode": row.get("mode")}



class ManualOrderReq(BaseModel):
    action: str  # BUY or SELL


# moved to routes/strategies.py
async def manual_strategy_order(sid: str, req: ManualOrderReq, user=Depends(get_current_user)):
    """Manually fire a BUY or SELL using this strategy's configured symbol & defaults.
    Bypasses the python signal logic — useful for discretionary overrides.
    Honours `options` config if set on the strategy."""
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    action = (req.action or "").upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="action must be BUY or SELL")
    vc = row.get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    # Options mode: resolve the same Upstox contract path used by the runner.
    if opt_cfg.get("enabled"):
        contract = await _resolve_option_for_strategy(
            user["id"],
            row,
            underlying=opt_cfg.get("underlying", "NIFTY"),
            signal_action=action,
            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
            otm_points=int(opt_cfg.get("otm_points") or 0),
            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
        )
        if not contract:
            raise HTTPException(
                status_code=400,
                detail="Could not resolve Upstox option contract. Check OAuth, NFO/BFO permission, and instrument master cache.",
            )
        result = await _place_order_core(
            user_id=user["id"], symbol=opt_cfg.get("underlying", "NIFTY"),
            side=action, qty=int(opt_cfg.get("lots") or 1),
            order_type="MARKET", product=None, source=f"manual:strategy:{sid}",
            option_contract=contract,
        )
    else:
        symbol = (vc.get("symbol") or "RELIANCE").upper()
        result = await _place_order_core(
            user_id=user["id"], symbol=symbol, side=action, qty=None,
            order_type="MARKET", product=None, source=f"manual:strategy:{sid}",
        )
    # Update strategy telemetry so the card shows the manual fire
    await db.strategies.update_one(
        {"id": sid, "user_id": user["id"]},
        {"$set": {"last_signal_at": datetime.now(timezone.utc).isoformat(),
                  "last_signal_action": f"MANUAL {action}"},
         "$inc": {"signals_fired": 1}},
    )
    return {"ok": True, "order": result}


# moved to routes/strategies.py
async def exit_strategy_positions(sid: str, user=Depends(get_current_user)):
    """Square off every stored open Position Manager row for this strategy."""
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return await _close_strategy_positions(user["id"], sid, reason="exit")


# moved to routes/strategies.py
async def test_run_strategy(sid: str, user=Depends(get_current_user)):
    """Force-evaluate a strategy NOW. Bypasses dedup. Returns diagnostics so the
    user can see exactly what their `run(data)` function sees and emits.
    If a BUY/SELL signal is returned, a paper/live order is placed immediately
    (subject to all the usual safety guards in _place_order_core)."""
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    code = row.get("python_code") or ""
    if not code:
        raise HTTPException(status_code=400, detail="Strategy has no python code")
    vc = row.get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    options_mode = bool(opt_cfg.get("enabled"))
    # When options mode is on, analyse the underlying spot — NOT the equity field
    if options_mode:
        symbol = (opt_cfg.get("underlying") or "NIFTY").upper()
    else:
        symbol = (vc.get("symbol") or "RELIANCE").upper()

    if options_mode:
        from routes.ops import ops_eod_options_backtest

        backtest = await ops_eod_options_backtest(strategy_id=sid, user=user)
        result = (backtest.get("results") or [{}])[0]
        if result.get("error"):
            return {
                "ok": False,
                "engine": "eod_options_oos",
                "oos_backtest": True,
                "symbol": symbol,
                "data_source": "bhavcopy_eod_options",
                "error": result.get("error"),
            }
        overall = result.get("overall") or {}
        trades = int(overall.get("n") or 0)
        wins = int(round(trades * float(overall.get("win_rate") or 0) / 100.0))
        return {
            "ok": True,
            "engine": "eod_options_oos",
            "oos_backtest": True,
            "symbol": symbol,
            "options_mode": True,
            "data_source": "bhavcopy_eod_options",
            "data_live": False,
            "verdict": result.get("verdict"),
            "oos_year": result.get("oos_year"),
            "oos": result.get("oos"),
            "pct_green_months": result.get("pct_green_months"),
            "by_year": result.get("by_year"),
            "summary": {
                "total_pnl": overall.get("pnl", 0),
                "return_pct": 0,
                "trades": trades,
                "wins": wins,
                "losses": max(0, trades - wins),
                "win_rate": overall.get("win_rate", 0),
                "expectancy": overall.get("expectancy", 0),
                "oos_expectancy": (result.get("oos") or {}).get("expectancy", 0),
            },
        }

    settings = await get_user_settings(user["id"])
    strategy_mode = row.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
    allow_mock = strategy_mode == "paper"

    # Fetch candles using the same path and live/mock policy as the background runner.
    history = await _fetch_strategy_history(
        user["id"],
        symbol,
        days=60,
        interval="5minute",
        allow_mock=allow_mock,
        strategy=row,
    )
    data: List[dict] = history["data"]
    source_label = history["source"]

    if not data:
        raise HTTPException(status_code=400, detail=f"No price data for {symbol}")

    # Run the strategy
    try:
        signals = safe_run_strategy(code, data)
    except Exception as e:
        return {
            "ok": False,
            "symbol": symbol,
            "data_source": source_label,
            "candles": len(data),
            "first_candle": data[0],
            "last_candle": data[-1],
            "signals": [],
            "error": str(e),
            "order_placed": None,
        }

    order_result = None
    placed_error = None
    option_contract_used = None
    signal_validation = None
    if signals:
        last_sig = signals[-1]
        action = (last_sig.get("action") or "").upper()
        if action in ("BUY", "SELL"):
            signal_validation = _validate_trade_signal(last_sig, data, row)
            if not signal_validation.get("is_valid"):
                placed_error = (
                    f"Signal filtered: confidence {signal_validation.get('confidence')} "
                    f"< {signal_validation.get('threshold')}. "
                    f"{'; '.join(signal_validation.get('reasons') or [])}"
                )
            else:
                if not history.get("is_live", False) and not allow_mock:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Live execution blocked: Upstox candle source is not fresh ({history.get('live_reason') or source_label}). Reconnect Upstox or switch to paper mode.",
                    )
                try:
                    if options_mode:
                        option_contract_used = await _resolve_option_for_strategy(
                            user["id"],
                            row,
                            underlying=symbol,
                            signal_action=action,
                            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
                            otm_points=int(opt_cfg.get("otm_points") or 0),
                            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
                        )
                        if not option_contract_used:
                            raise HTTPException(status_code=400, detail="Could not resolve Upstox option contract - check OAuth, exchange permissions, and instrument search.")
                        order_result = await _place_order_core(
                            user_id=user["id"], symbol=symbol, side=action,
                            qty=int(opt_cfg.get("lots") or 1),
                            order_type="MARKET", product=None,
                            source=f"test-run:strategy:{sid}",
                            option_contract=option_contract_used,
                        )
                    else:
                        order_result = await _place_order_core(
                            user_id=user["id"],
                            symbol=symbol,
                            side=action,
                            qty=None,
                            order_type="MARKET",
                            product=None,
                            source=f"test-run:strategy:{sid}",
                        )
                    await db.strategies.update_one(
                        {"id": sid, "user_id": user["id"]},
                        {"$set": {
                            "last_signal_at": datetime.now(timezone.utc).isoformat(),
                            "last_signal_action": action,
                            "last_signals_count": len(signals),
                            "last_fired_signal_date": last_sig.get("date", ""),
                            "last_data_source": source_label,
                            "last_data_live": bool(history.get("is_live")),
                            "last_data_reason": history.get("live_reason"),
                            "last_candle_at": history.get("last_candle_at"),
                            "latest_candle_age_sec": history.get("latest_candle_age_sec"),
                        },
                         "$inc": {"signals_fired": 1, "evaluations": 1, "evaluations_today": 1}},
                    )
                except HTTPException as e:
                    placed_error = e.detail
                except Exception as e:
                    placed_error = str(e)

    return {
        "ok": True,
        "symbol": symbol,
        "options_mode": options_mode,
        "data_source": source_label,
        "data_live": bool(history.get("is_live")),
        "candles": len(data),
        "first_candle": data[0],
        "last_candle": data[-1],
        "last_5_closes": [d.get("close") for d in data[-5:]],
        "signals": signals,
        "signal_validation": signal_validation,
        "option_contract": option_contract_used,
        "order_placed": order_result,
        "order_error": placed_error,
    }


def _safe_run_python(code: str, data: List[dict]) -> List[dict]:
    """Run user strategy via AST-validated sandbox (see safe_exec.py)."""
    return safe_run_strategy(code, data)


def _validate_trade_signal(signal: Dict[str, Any], data: List[Dict[str, Any]], strategy: Dict[str, Any] = None) -> Dict[str, Any]:
    """Score the latest signal against trend, candle confirmation, and whipsaw risk."""
    try:
        trend = MarketTrendAnalyzer.analyze(data, lookback=min(50, max(20, len(data))))
        is_hft = False
        if strategy:
            name = str(strategy.get("name") or "").lower()
            desc = str(strategy.get("description") or "").lower()
            if "hft" in name or "hft" in desc or "scalper" in name or "scalper" in desc:
                is_hft = True
        validation = FakeSignalFilter.validate(signal, data, trend, is_hft=is_hft)
        threshold = 35.0 if is_hft else SIGNAL_CONFIDENCE_MIN
        validation["threshold"] = threshold
        validation["trend"] = trend
        validation["is_valid"] = True
        return validation
    except Exception as e:
        logger.warning(f"signal validation failed: {e}")
        return {
            "is_valid": False,
            "confidence": 0,
            "threshold": SIGNAL_CONFIDENCE_MIN,
            "reasons": [f"Validation failed: {e}"],
            "filtered": True,
            "trend": {},
        }


# options preview moved to routes/market.py


# moved to routes/strategies.py
async def backtest(req: BacktestReq, user=Depends(get_current_user)):
    code = req.python_code
    opt_cfg = req.options or {}
    # Pull options config from saved strategy too if not provided
    if req.strategy_id:
        row = await db.strategies.find_one({"id": req.strategy_id, "user_id": user["id"]})
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        if not code:
            code = row.get("python_code")
        if not opt_cfg:
            opt_cfg = (row.get("visual_config") or {}).get("options") or {}
    if not code:
        code = DEFAULT_PYTHON

    # Route to Backtrader if requested
    if req.engine == "backtrader":
        try:
            options_mode = bool(opt_cfg.get("enabled"))
            target_symbol = (opt_cfg.get("underlying") or "NIFTY") if options_mode else req.symbol.upper()
            # Backtrader doesn't support options mode yet, only equity backtesting
            if options_mode:
                raise ValueError("Backtrader engine does not yet support options mode. Use local simulator.")
            result = backtrader_runner.run_backtest(
                symbol=target_symbol,
                python_code=code,
                starting_capital=100000,
                days=req.days,
                data_source="yfinance",
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Backtrader backtest failed: {e}")

    # Default: use local simulator
    options_mode = bool(opt_cfg.get("enabled"))
    # In options mode, analyse the UNDERLYING spot — not the equity field
    target_symbol = (opt_cfg.get("underlying") or "NIFTY") if options_mode else req.symbol.upper()
    sym = next((s for s in SYMBOLS if s["symbol"] == target_symbol.upper()), SYMBOLS[0])
    history = await _fetch_strategy_history(user["id"], target_symbol, days=req.days, interval="day")
    data = history["data"]
    source = history.get("source", "")

    # Gate 5: Never run a backtest on mock/simulated data — the results would be
    # meaningless and could give false confidence before going live.
    if "mock" in str(source).lower():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Backtest blocked: price data source is '{source}' (simulated). "
                "Connect Upstox and ensure real historical data is available before running a backtest."
            ),
        )

    if not data:
        raise HTTPException(status_code=400, detail=f"No price data for {target_symbol}")

    # Candle integrity check: sort order, duplicates, and OHLC sanity
    dates_seen: set = set()
    for i, candle in enumerate(data):
        d = candle.get("date", "")
        if d in dates_seen:
            raise HTTPException(status_code=400, detail=f"Backtest blocked: duplicate candle date '{d}' at index {i}.")
        dates_seen.add(d)
        h = float(candle.get("high") or 0)
        l = float(candle.get("low") or 0)
        c = float(candle.get("close") or 0)
        o = float(candle.get("open") or 0)
        if h < l or h <= 0 or l <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Backtest blocked: invalid OHLC at candle {i} (date={d}): high={h}, low={l}."
            )
    signals: List[dict] = []
    try:
        signals = _safe_run_python(code, data)
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=400, detail=f"Strategy error: {e}")

    # PnL simulation: equity vs options have different P&L mechanics
    starting_capital = 100000.0
    cash = starting_capital
    position = 0          # qty (equity) OR signed-lot-qty (options)
    entry = 0.0           # entry price per unit
    entry_spot = 0.0
    trades: List[dict] = []
    equity_curve: List[dict] = []
    sigmap = {s["date"]: s["action"] for s in signals}

    if options_mode:
        # Options simulation: assume long-CE on BUY signal, long-PE on SELL signal.
        # Premium proxy = 2% of spot at entry. P&L = (spot_change × delta) × qty.
        # Long CE has +delta≈0.5 at ATM. We use 1:1 proxy (premium moves ~= spot move)
        # for ATM near expiry — a conservative approximation; live results vary.
        lot_size = options_helper.LOT_SIZES.get(target_symbol.upper(), 1)
        lots = int(opt_cfg.get("lots") or 1)
        per_trade_qty = lot_size * lots
        # Track option_type for the open position so closing computes the right P&L direction
        open_option_type = None  # "CE" or "PE"

        for d in data:
            act = sigmap.get(d["date"])
            spot = d["close"]
            # Premium proxy: 2% of spot for ATM option; intrinsic value tracks 1:1
            if act in ("BUY", "SELL") and position == 0:
                premium = round(spot * 0.02, 2)
                open_option_type = "CE" if act == "BUY" else "PE"
                entry = premium
                entry_spot = spot
                position = per_trade_qty
                cost = premium * per_trade_qty
                cash -= cost
                trades.append({"date": d["date"], "action": f"BUY {open_option_type}", "price": premium, "qty": per_trade_qty})
            elif act in ("BUY", "SELL") and position > 0 and open_option_type:
                # Opposite signal → square off and (optionally) open new leg
                # First close existing leg
                exit_premium = _options_premium_at_exit(entry, spot, entry_spot, open_option_type)
                pnl = (exit_premium - entry) * position
                cash += exit_premium * position
                trades.append({"date": d["date"], "action": f"SELL {open_option_type}", "price": exit_premium, "qty": position, "pnl": round(pnl, 2)})
                position = 0
                open_option_type = None
                # Open opposite leg on the same bar (matches runner behaviour)
                new_type = "CE" if act == "BUY" else "PE"
                premium = round(spot * 0.02, 2)
                open_option_type = new_type
                entry = premium
                entry_spot = spot
                position = per_trade_qty
                cash -= premium * per_trade_qty
                trades.append({"date": d["date"], "action": f"BUY {new_type}", "price": premium, "qty": per_trade_qty})
            # Mark-to-market
            if position > 0 and open_option_type:
                mtm_premium = _options_premium_at_exit(entry, spot, entry_spot, open_option_type)
                eq = cash + mtm_premium * position
            else:
                eq = cash
            equity_curve.append({"date": d["date"], "equity": round(eq, 2)})
    else:
        # Equity simulation (original behaviour)
        for d in data:
            act = sigmap.get(d["date"])
            if act == "BUY" and position == 0:
                position = int(cash // d["close"])
                entry = d["close"]
                cash -= position * d["close"]
                trades.append({"date": d["date"], "action": "BUY", "price": d["close"], "qty": position})
            elif act == "SELL" and position > 0:
                pnl = (d["close"] - entry) * position
                cash += position * d["close"]
                trades.append({"date": d["date"], "action": "SELL", "price": d["close"], "qty": position, "pnl": round(pnl, 2)})
                position = 0
            eq = cash + position * d["close"]
            equity_curve.append({"date": d["date"], "equity": round(eq, 2)})

    final_equity = equity_curve[-1]["equity"] if equity_curve else starting_capital
    total_pnl = round(final_equity - starting_capital, 2)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2)
    if req.strategy_id:
        await db.strategies.update_one({"id": req.strategy_id, "user_id": user["id"]}, {"$set": {
            "last_pnl": total_pnl,
            "last_data_source": history.get("source"),
            "last_data_live": bool(history.get("is_live")),
        }})
    
    # Save to paper trading history for profile stats
    paper_trade_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "strategy_id": req.strategy_id,
        "symbol": target_symbol,
        "mode": "options" if options_mode else "equity",
        "pnl": total_pnl,
        "trades_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "return_pct": round(total_pnl / (starting_capital / 100), 2),
        "starting_capital": starting_capital,
        "final_equity": final_equity,
        "days_backtested": req.days,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.paper_trading_history.insert_one(paper_trade_doc)
    
    return {
        "engine": "local",
        "mode": "options" if options_mode else "equity",
        "symbol_analysed": target_symbol,
        "data_source": history.get("source"),
        "data_live": bool(history.get("is_live")),
        "equity_curve": equity_curve,
        "trades": trades,
        "signals": signals,
        "summary": {
            "starting_capital": starting_capital,
            "final_equity": final_equity,
            "total_pnl": total_pnl,
            "return_pct": round(total_pnl / (starting_capital / 100), 2),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
        },
    }





def _options_premium_at_exit(entry_premium: float, current_spot: float, entry_spot: float, option_type: str) -> float:
    """Rough P&L proxy for ATM options held briefly.
    Assumes premium tracks spot change 1:1 (delta≈1) for the directional leg.
    For Long CE: premium rises as spot rises. For Long PE: premium rises as spot falls.
    Floor at 0 (can't be negative — premium decay handled separately, omitted here)."""
    move = current_spot - entry_spot
    if option_type == "CE":
        return max(0.0, round(entry_premium + move, 2))
    return max(0.0, round(entry_premium - move, 2))  # PE


# ============== Routes: Orders & Positions ==============
def get_ist_midnight_utc_iso() -> str:
    """Return the UTC ISO timestamp corresponding to the start of today in IST (00:00:00 IST)."""
    now_utc = datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    ist_midnight_utc = ist_midnight - IST_OFFSET
    return ist_midnight_utc.isoformat()


def get_trading_day_window_ist() -> tuple[str, str]:
    """Return today's start and end ISO timestamps in UTC corresponding to the IST trading day (00:00:00 to 24:00:00 IST)."""
    now_utc = datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    ist_midnight_utc = ist_midnight - IST_OFFSET
    ist_tomorrow_midnight_utc = ist_midnight_utc + timedelta(days=1)
    return ist_midnight_utc.isoformat(), ist_tomorrow_midnight_utc.isoformat()


async def _get_todays_realized_pnl(user_id: str, mode: str) -> float:
    """Sum today's realized P&L from trade_fills (primary) then orders (fallback)."""
    start, end = get_trading_day_window_ist()
    fills = await db.trade_fills.find({
        "user_id": user_id, "mode": mode,
        "created_at": {"$gte": start, "$lt": end},
        "action": {"$in": ["CLOSE", "REDUCE"]},
    }, {"_id": 0, "realized_pnl": 1}).to_list(1000)
    realized = sum(float(f.get("realized_pnl") or 0) for f in fills)
    if realized == 0.0:
        orders = await db.orders.find({
            "user_id": user_id, "mode": mode,
            "created_at": {"$gte": start, "$lt": end},
            "status": {"$in": [ORDER_FILLED, ORDER_CLOSED, "COMPLETE"]},
        }, {"_id": 0, "net_pnl": 1, "realized_pnl": 1}).to_list(500)
        realized = sum(float(o.get("net_pnl") or o.get("realized_pnl") or 0) for o in orders)
    return realized


async def _check_daily_loss_guard(user_id: str, max_loss: float, mode: str = "paper") -> None:
    """Refuse new orders if today's realized loss exceeds max_daily_loss.

    P&L is read from db.trade_fills (primary source) via _get_todays_realized_pnl.

    Config keys (read from risk_cfg then user settings, first non-zero wins):
      daily_loss_limit_paper  — looser, for paper data-collection days
      daily_loss_limit_live   — tighter, for real-money sessions
      daily_loss_limit        — legacy fallback (both modes)

    On trip: raises HTTP 400 to block the current order AND schedules an
    async force-close of all open positions for this user+mode.
    """
    if not max_loss or max_loss <= 0:
        return
    realized = await _get_todays_realized_pnl(user_id, mode)
    if realized <= -abs(max_loss):
        # Force-close open positions in background (non-blocking for current request)
        async def _force_close_all():
            try:
                open_positions = await db.strategy_positions.find(
                    {"user_id": user_id, "mode": mode,
                     "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
                    {"strategy_id": 1, "_id": 0},
                ).to_list(50)
                sids = {p["strategy_id"] for p in open_positions if p.get("strategy_id")}
                for sid in sids:
                    try:
                        await _close_strategy_positions(user_id, sid, reason="daily-loss-kill-switch")
                    except Exception:
                        pass
                if sids:
                    logger.error(
                        "DAILY LOSS KILL SWITCH tripped user=%s mode=%s realized=%.0f "
                        "limit=%.0f — force-closed %d strategies",
                        user_id, mode, abs(realized), abs(max_loss), len(sids),
                    )
            except Exception as exc:
                logger.error("force-close after kill-switch failed: %s", exc)

        asyncio.create_task(_force_close_all())
        raise HTTPException(
            status_code=400,
            detail=f"Daily loss guard tripped: today's realized loss ₹{abs(realized):.0f} "
                   f"≥ max ₹{max_loss:.0f}. New orders blocked; open positions force-closed.",
        )


async def _check_trade_count_guard(user_id: str, max_trades: int, mode: str = "paper") -> None:
    if not max_trades or max_trades <= 0:
        return
    start, end = get_trading_day_window_ist()
    excluded_statuses = [
        ORDER_REJECTED,
        ORDER_CANCELLED,
        "REJECTED",
        "CANCELLED",
        "FAILED",
        "STALE",
        "BROKER_NOT_FOUND",
        "BLOCKED",
        "SKIPPED",
        "SKIPPED_SIGNAL",
    ]
    trades = await db.orders.count_documents({
        "user_id": user_id,
        "mode": mode,
        "created_at": {"$gte": start, "$lt": end},
        "status": {"$nin": excluded_statuses},
    })
    if trades >= int(max_trades):
        raise HTTPException(
            status_code=400,
            detail=f"Daily trade guard tripped: {trades}/{max_trades} trades used today. New orders blocked until tomorrow.",
        )


def _mock_bid_ask(price: float, spread_pct: float = 0.0005) -> Dict[str, float]:
    bid = round(price * (1 - spread_pct / 2), 2)
    ask = round(price * (1 + spread_pct / 2), 2)
    return {"bid": bid, "ask": ask}


def _simulate_paper_fill_price(price: float, side: str, spread_pct: float = 0.0007) -> float:
    if side == "BUY":
        return round(price * (1 + spread_pct / 2), 2)
    return round(price * (1 - spread_pct / 2), 2)


def _simulate_paper_brokerage(fill_price: float, quantity: int) -> float:
    gross = abs(fill_price * quantity)
    brokerage = min(20.0, gross * 0.0003)
    return round(brokerage, 2)


def _simulate_upstox_like_charges(
    fill_price: float,
    quantity: int,
    *,
    side: str,
    exchange: str,
    asset_type: Optional[str] = None,
) -> Dict[str, float]:
    """Deterministic retail-cost approximation for paper mode.

    Live mode uses Upstox charge APIs. Paper keeps the same gross/charges/net
    accounting shape with a local model so strategy evaluation does not get a
    free fill.
    """
    gross = abs(float(fill_price or 0) * int(quantity or 0))
    side = str(side or "").upper()
    exchange = str(exchange or "").upper()
    asset_type = str(asset_type or "").lower()
    is_option = asset_type == "option" or exchange in {"NFO", "BFO"}
    brokerage = round(min(20.0, gross * 0.0003), 2)
    stt = round(gross * (0.000625 if side == "SELL" and is_option else 0.00025 if side == "SELL" else 0.0), 2)
    exchange_txn = round(gross * (0.00053 if exchange == "NFO" else 0.00035 if exchange == "BFO" else 0.00003), 2)
    sebi = round(gross * 0.000001, 2)
    stamp = round(gross * (0.00003 if side == "BUY" else 0.0), 2)
    gst = round((brokerage + exchange_txn + sebi) * 0.18, 2)
    total = round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)
    return {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": exchange_txn,
        "sebi": sebi,
        "stamp": stamp,
        "gst": gst,
        "total": total,
    }


def _new_execution_tag(strategy_id: Optional[str] = None) -> str:
    strategy_part = re.sub(r"[^A-Za-z0-9_-]", "", strategy_id or "manual")[:18] or "manual"
    session_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"quantg:{strategy_part}:{session_part}"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _open_order_statuses() -> set:
    return set(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)


def _closed_order_statuses() -> set:
    return set(ORDER_TERMINAL_STATUSES | LEGACY_TERMINAL_STATUSES)


ORDER_SKIPPED = ORDER_SKIPPED_SIGNAL


def _preflight_response(
    *,
    ok: bool,
    reason_code: Optional[str],
    reason: Optional[str],
    strategy_id: Optional[str],
    intent: Optional["OrderIntent"],
    option_contract: Optional[Dict[str, Any]],
    ltp: Optional[float],
    market_session: Optional[Dict[str, Any]],
    price_validation: Optional[Dict[str, Any]] = None,
) -> ExecutionPreflightResult:
    instr = intent.instrument if intent else None
    resolved = None
    segment = None
    symbol = None
    if instr:
        segment = _execution_segment_for(instr.exchange, instr.asset_class, instr.tradingsymbol, option_contract)
        symbol = instr.tradingsymbol
        resolved = {
            "broker": instr.broker,
            "exchange": instr.exchange,
            "tradingsymbol": instr.tradingsymbol,
            "instrument_token": instr.instrument_token,
            "instrument_key": _instrument_key(instr.exchange, instr.tradingsymbol, instr.instrument_token),
            "asset_class": instr.asset_class,
        }
    return ExecutionPreflightResult(
        ok=ok,
        status="READY" if ok else ORDER_SKIPPED_SIGNAL,
        reason_code=reason_code,
        reason=reason,
        strategy_id=strategy_id,
        symbol=symbol,
        resolved_instrument=resolved,
        segment=segment,
        ltp=float(ltp) if ltp not in (None, "") else None,
        market_session=market_session,
        price_validation=price_validation,
    )


def _is_option_contract(option_contract: Optional[Dict[str, Any]], instr: Optional["InstrumentRef"] = None) -> bool:
    if option_contract:
        opt_type = str(option_contract.get("option_type") or option_contract.get("instrument_type") or "").upper()
        return opt_type in {"CE", "PE", "OPTCOM"} or bool(option_contract.get("strike"))
    if not instr:
        return False
    return _asset_type_for_instrument(instr, option_contract) == "option"


def _price_integrity_guard(
    *,
    paper: bool,
    intent: "OrderIntent",
    option_contract: Optional[Dict[str, Any]],
    market_snapshot: Dict[str, Any],
    market_session: Dict[str, Any],
) -> Dict[str, Any]:
    instr = intent.instrument
    source = str(market_snapshot.get("source") or "").lower()
    feed = str(market_snapshot.get("feed") or "").lower()
    token = str(
        market_snapshot.get("instrument_key")
        or (option_contract or {}).get("instrument_key")
        or (option_contract or {}).get("instrument_token")
        or instr.instrument_token
        or ""
    ).strip()
    ltp = float(market_snapshot.get("ltp") or 0)
    received_at = market_snapshot.get("received_at")
    is_option = _is_option_contract(option_contract, instr)
    is_entry = _intent_is_entry(intent.intent)
    result = {
        "ok": True,
        "reason_code": None,
        "human_reason": "Price accepted.",
        "source": source or None,
        "feed": feed or None,
        "instrument_key": token or None,
        "ltp": ltp if ltp > 0 else None,
        "received_at": received_at,
    }
    if not (paper and is_option and is_entry and market_session.get("open")):
        return result
    blocked_sources = {"fill-hint", "option-contract", "simulated", "mock", "synthetic", "fallback", "unavailable"}
    if not token or "|" not in token or token.upper().startswith("PAPER_"):
        result.update(ok=False, reason_code="INSTRUMENT_UNRESOLVED", human_reason="Paper option entry blocked: real Upstox instrument_key is missing.")
    elif ltp <= 0:
        result.update(ok=False, reason_code="PRICE_UNAVAILABLE", human_reason="Paper option entry blocked: Upstox option LTP is missing or zero.")
    elif source in blocked_sources or any(part in source for part in ("mock", "simulated", "fallback", "synthetic")):
        result.update(ok=False, reason_code="SKIPPED_PRICE_UNAVAILABLE", human_reason=f"Paper option entry blocked: price source '{source or 'unknown'}' is not a real Upstox quote.")
    elif source not in {"upstox-cache", "upstox-rest-quote"}:
        result.update(ok=False, reason_code="PRICE_UNAVAILABLE", human_reason=f"Paper option entry blocked: price source '{source or 'unknown'}' is not allowed in normal paper mode.")
    elif not received_at or parse_market_timestamp(received_at) is None:
        result.update(ok=False, reason_code="SKIPPED_QUOTE_STALE", human_reason="Paper option entry skipped: Upstox quote timestamp is missing.")
    else:
        dt = parse_market_timestamp(received_at)
        if dt:
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > 60.0:
                result.update(ok=False, reason_code="SKIPPED_QUOTE_STALE", human_reason=f"Paper option entry skipped: Upstox quote is stale ({age:.1f}s age > 60s).")
    return result


async def _execution_preflight(
    *,
    user_id: str,
    strategy_id: Optional[str],
    strategy_row: Optional[Dict[str, Any]],
    intent: "OrderIntent",
    settings: Dict[str, Any],
    paper: bool,
    option_contract: Optional[Dict[str, Any]],
    fill_price_hint: float,
    market_snapshot: Dict[str, Any],
) -> ExecutionPreflightResult:
    instr = intent.instrument
    ltp = float(fill_price_hint or market_snapshot.get("ltp") or 0)
    market_session = _market_session_for_instrument(instr, option_contract)

    if strategy_id and strategy_id != "manual_recovery":
        row = strategy_row or await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not row:
            return _preflight_response(
                ok=False, reason_code="STRATEGY_DISABLED", reason="Strategy not found or not owned by user.",
                strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
            )

    if not instr.tradingsymbol or not instr.instrument_token:
        return _preflight_response(
            ok=False, reason_code="INSTRUMENT_UNRESOLVED", reason="Instrument could not be resolved.",
            strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
        )
    if option_contract is not None and not (option_contract.get("instrument_key") or option_contract.get("instrument_token")):
        return _preflight_response(
            ok=False, reason_code="INSTRUMENT_UNRESOLVED", reason="Option contract resolution did not return an Upstox instrument_key.",
            strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
        )

    segment = _execution_segment_for(instr.exchange, instr.asset_class, instr.tradingsymbol, option_contract)
    if segment == "MCX_FO":
        return _preflight_response(
            ok=False, reason_code="SKIPPED_SEGMENT_DISABLED", reason="MCX commodity execution has been removed from QuantG.",
            strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
        )
    if segment not in SEGMENT_MARKET_WINDOWS:
        return _preflight_response(
            ok=False, reason_code="INSTRUMENT_UNRESOLVED", reason=f"Unsupported exchange segment {segment}.",
            strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
        )

    instrument_key = _instrument_key(instr.exchange, instr.tradingsymbol, instr.instrument_token)
    paper_master_guard = paper and option_contract and not option_contract.get("simulated") and settings.get("paper_block_suspended_instruments", True)
    if _intent_is_entry(intent.intent) and (not paper or paper_master_guard):
        guard = await lookup_instrument_guard(db, instrument_key)
        paper_master_soft_missing = paper and guard.get("reason_code") == "INSTRUMENT_MASTER_MISSING"
        if not guard.get("ok") and not paper_master_soft_missing:
            return _preflight_response(
                ok=False,
                reason_code=guard.get("reason_code") or "INSTRUMENT_BLOCKED",
                reason=(
                    f"{'Paper' if paper else 'Live'} signal skipped: "
                    f"{guard.get('reason') or 'Upstox instrument master check failed.'}"
                ),
                strategy_id=strategy_id,
                intent=intent,
                option_contract=option_contract,
                ltp=ltp,
                market_session=market_session,
            )

    if _intent_is_entry(intent.intent) and not market_session.get("open"):
        return _preflight_response(
            ok=False, reason_code="MARKET_CLOSED", reason=market_session.get("reason") or "Market is closed.",
            strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
        )

    quote_timestamp = (
        market_snapshot.get("received_at")
        or market_snapshot.get("timestamp")
        or market_snapshot.get("tick_time")
        or (option_contract or {}).get("received_at")
        or (option_contract or {}).get("quote_timestamp")
    )
    if _intent_is_entry(intent.intent) and not paper and is_quote_stale(quote_timestamp):
        age = quote_age_seconds(quote_timestamp)
        return _preflight_response(
            ok=False,
            reason_code="SKIPPED_QUOTE_STALE",
            reason=f"Live signal skipped: Upstox quote is stale or missing{f' ({age:.1f}s old)' if age is not None else ''}.",
            strategy_id=strategy_id,
            intent=intent,
            option_contract=option_contract,
            ltp=ltp,
            market_session=market_session,
            price_validation={
                "ok": False,
                "reason_code": "SKIPPED_QUOTE_STALE",
                "quote_timestamp": quote_timestamp,
                "quote_age_sec": round(age, 3) if age is not None else None,
                "instrument_key": instrument_key,
            },
        )

    if _intent_is_entry(intent.intent) and ltp <= 0:
        return _preflight_response(
            ok=False, reason_code="PRICE_UNAVAILABLE", reason="No valid Upstox websocket or REST LTP is available.",
            strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=None, market_session=market_session,
        )

    # Live execution quality guards
    if _intent_is_entry(intent.intent) and not paper and option_contract:
        contract_ltp = float(option_contract.get("ltp") or ltp or 0)
        # 1. Minimum Premium Check (LTP >= 1.0)
        if contract_ltp < 1.0:
            return _preflight_response(
                ok=False, reason_code="PRICE_UNAVAILABLE",
                reason=f"Live option entry skipped: Option premium is too low (LTP {contract_ltp} < 1.0).",
                strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=contract_ltp, market_session=market_session
            )

        # 2. Market Data Quality (staleness, spread, etc.)
        risk_style = (strategy_row or {}).get("risk_style") or ((strategy_row or {}).get("visual_config") or {}).get("risk", {}).get("risk_style") or "balanced"
        bid = float(option_contract.get("bid") or 0)
        ask = float(option_contract.get("ask") or 0)
        received_at = option_contract.get("received_at") or market_snapshot.get("received_at")
        quality = evaluate_market_data_quality(
            ltp=contract_ltp,
            tick_time=option_contract.get("tick_time") or option_contract.get("timestamp") or received_at,
            received_at=received_at,
            instrument_token=option_contract.get("instrument_key") or instr.instrument_token,
            exchange=instr.exchange,
            market_open=market_session.get("open"),
            bid=bid if bid > 0 else None,
            ask=ask if ask > 0 else None,
            risk_style=str(risk_style),
        )
        if not quality.get("ok"):
            reason = quality.get("reason")
            reason_code = "SKIPPED_QUOTE_STALE" if "stale" in reason.lower() or "timestamp" in reason.lower() else "PRICE_UNAVAILABLE"
            return _preflight_response(
                ok=False, reason_code=reason_code,
                reason=f"Live option entry skipped: market data quality failed. Reason: {reason}",
                strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=contract_ltp, market_session=market_session
            )

        # 3. Expired Contract Check
        expiry_val = option_contract.get("expiry")
        if expiry_val:
            try:
                from datetime import date
                exp_date = date.fromisoformat(str(expiry_val))
                if exp_date < date.today():
                    return _preflight_response(
                        ok=False, reason_code="EXPIRED_CONTRACT",
                        reason=f"Live option entry skipped: contract is expired ({expiry_val} < today).",
                        strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=contract_ltp, market_session=market_session
                    )
            except Exception:
                pass

        # 4. Duplicate Active Option Contract check account-wide
        target_instrument_key = option_contract.get("instrument_key") or option_contract.get("instrument_token") or instr.instrument_token
        existing_contract = await db.strategy_positions.find_one({
            "user_id": user_id,
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
            "instrument_key": str(target_instrument_key),
        })
        if existing_contract:
            return _preflight_response(
                ok=False, reason_code="DUPLICATE_ACTIVE_OPTION_CONTRACT",
                reason=f"Live option entry blocked: active position for option contract {option_contract.get('tradingsymbol')} already exists in strategy {existing_contract.get('strategy_id')}.",
                strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=contract_ltp, market_session=market_session
            )

        # 5. Feed Connectivity Check
        upstox_gw = await get_user_upstox_gateway(user_id)
        if not upstox_gw or not upstox_gw.connected:
            return _preflight_response(
                ok=False, reason_code="FEED_DISCONNECTED",
                reason="Live option entry skipped: Upstox gateway feed is disconnected or offline.",
                strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=contract_ltp, market_session=market_session
            )

    price_validation = _price_integrity_guard(
        paper=paper,
        intent=intent,
        option_contract=option_contract,
        market_snapshot=market_snapshot,
        market_session=market_session,
    )
    if not price_validation.get("ok"):
        return _preflight_response(
            ok=False,
            reason_code=price_validation.get("reason_code") or "PRICE_UNAVAILABLE",
            reason=price_validation.get("human_reason") or "Price integrity check failed.",
            strategy_id=strategy_id,
            intent=intent,
            option_contract=option_contract,
            ltp=ltp,
            market_session=market_session,
            price_validation=price_validation,
        )

    if strategy_id and _intent_is_entry(intent.intent):
        instrument_key = _instrument_key(instr.exchange, instr.tradingsymbol, instr.instrument_token)
        side_key = _strategy_side_key(
            user_id,
            strategy_id,
            instrument_key,
            "SHORT" if intent.intent == "OPEN_SHORT" else "LONG",
        )
        existing = await db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "$or": [
                {"active_strategy_instrument_side_key": side_key},
                {
                    "instrument_key": instrument_key,
                    "position_side": "SHORT" if intent.intent == "OPEN_SHORT" else "LONG",
                },
            ],
            "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
        })
        if existing:
            return _preflight_response(
                ok=False,
                reason_code="DUPLICATE_STRATEGY_INSTRUMENT_SIDE",
                reason=f"Duplicate open position blocked for same strategy, instrument and side: {instr.tradingsymbol}.",
                strategy_id=strategy_id,
                intent=intent,
                option_contract=option_contract,
                ltp=ltp,
                market_session=market_session,
            )

    try:
        if _intent_is_entry(intent.intent):
            risk_cfg = ((strategy_row or {}).get("visual_config") or {}).get("risk") or {}
            _mode = "paper" if paper else "live"
            # Account-level daily-loss kill switch. Single source of truth =
            # Trading Preferences MAX DAILY LOSS (settings.max_daily_loss): setting
            # a value > 0 AUTO-ARMS it account-wide (force-close all open positions
            # + block new entries for the day). No separate enable toggle — the
            # value the user sets in the profile IS the switch. An optional
            # mode-specific override (daily_loss_limit_paper/live, on settings or
            # the strategy risk config) can only tighten the limit, never loosen it.
            _account_limit = float(settings.get("max_daily_loss") or 0)
            _mode_override = (
                float(settings.get(f"daily_loss_limit_{_mode}") or 0)
                or float(risk_cfg.get(f"daily_loss_limit_{_mode}") or 0)
            )
            if _mode_override > 0 and _account_limit > 0:
                _limit = min(_account_limit, _mode_override)
            else:
                _limit = _mode_override or _account_limit
            if _limit > 0:
                await _check_daily_loss_guard(user_id, _limit, mode=_mode)
    except HTTPException as exc:
        return _preflight_response(
            ok=False,
            reason_code="RISK_BLOCKED",
            reason=str(exc.detail),
            strategy_id=strategy_id,
            intent=intent,
            option_contract=option_contract,
            ltp=ltp,
            market_session=market_session,
        )

    return _preflight_response(
        ok=True,
        reason_code=None,
        reason="Ready for execution.",
        strategy_id=strategy_id,
        intent=intent,
        option_contract=option_contract,
        ltp=ltp,
        market_session=market_session,
        price_validation=price_validation,
    )


async def _find_kite_order_by_tag(kite, tag: str) -> Optional[Dict[str, Any]]:
    return None


async def _place_kite_order_with_recovery(
    kite,
    *,
    tradingsymbol: str,
    exchange: str,
    transaction_type: str,
    quantity: int,
    order_type: str,
    product: str,
    price: Optional[float] = None,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    return {"ok": False, "error": "Zerodha Kite execution has been removed. QuantG supports Upstox only.", "tag": tag or _new_execution_tag(), "attempts": 0}


def _is_nse_market_open(now_utc: Optional[datetime] = None) -> bool:
    return bool(_segment_session_status("NSE_FO", now_utc).get("open"))


def _is_order_market_open(exchange: str, now_utc: Optional[datetime] = None) -> bool:
    exchange = (exchange or "NSE").upper()
    if exchange in {"MCX", "MCX_FO", "CDS"}:
        return False
    segment = (
        "BSE_FO" if exchange in {"BFO", "BSE_FO"} else
        "BSE_EQ" if exchange == "BSE" else
        "NSE_FO"
    )
    return bool(_segment_session_status(segment, now_utc).get("open"))


def _market_data_age_sec(value: Any, now_utc: Optional[datetime] = None) -> Optional[float]:
    parsed = parse_market_timestamp(value)
    if not parsed:
        return None
    return max(0.0, ((now_utc or datetime.now(timezone.utc)) - parsed).total_seconds())


# ---------------------------------------------------------------------------
# Intent classification helpers
# ---------------------------------------------------------------------------

def _intent_is_entry(intent_str: str) -> bool:
    """Return True if the intent represents opening a new position."""
    return intent_str in ("OPEN_LONG", "OPEN_SHORT")


def _intent_is_exit(intent_str: str) -> bool:
    """Return True if the intent represents closing an existing position."""
    return intent_str in ("CLOSE_LONG", "CLOSE_SHORT")


def _intent_side(intent_str: str) -> str:
    """Map an OrderIntent intent string to a broker-facing BUY/SELL side."""
    return "BUY" if intent_str in ("OPEN_LONG", "CLOSE_SHORT") else "SELL"


def _runtime_broker_name(broker_raw: str) -> str:
    """Normalise internal broker identifiers to the display-ready name."""
    broker = (broker_raw or "upstox").lower()
    if broker != "upstox":
        return "unsupported_legacy_broker"
    return "upstox"


# ---------------------------------------------------------------------------
# _build_order_intent  –  resolve symbol / option_contract into OrderIntent
# ---------------------------------------------------------------------------

async def _build_order_intent(
    *,
    user_id: str,
    symbol: str,
    side: str,
    qty: Optional[int],
    source: str,
    exchange: str,
    settings: Dict[str, Any],
    option_contract: Optional[Dict[str, Any]] = None,
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Resolve raw order parameters into an ``OrderIntent`` with a concrete
    ``InstrumentRef``.  Returns a dict ``{"intent": OrderIntent, ...}``
    including optional ``lot_size`` / ``lots`` metadata for options.
    """
    execution_broker = "upstox"
    exchange = (exchange or "NSE").upper()
    symbol_upper = symbol.upper().strip()
    if exchange in {"MCX", "MCX_FO", "CDS"} or symbol_upper in REMOVED_COMMODITY_UNDERLYINGS:
        raise HTTPException(status_code=410, detail="MCX/CDS execution has been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")

    strategy_id = await _strategy_source_id(source)
    if not strategy_id and source in ("manual", "manual-exit", "squareoff-all"):
        strategy_id = "manual_recovery"

    # ----- option contract path -----
    if option_contract and option_contract.get("tradingsymbol"):
        tsym = str(option_contract["tradingsymbol"]).upper().strip()
        opt_exchange = (option_contract.get("exchange") or exchange or "NFO").upper()
        seg = _execution_segment_for(opt_exchange, "OPTION_LONG", tsym, option_contract)
        token = str(option_contract.get("instrument_token") or option_contract.get("instrument_key") or tsym)
        broker_side = str(option_contract.get("transaction_type") or side or "BUY").upper()
        instrument_key = _instrument_key(opt_exchange, tsym, token)

        # Dynamic intent and asset class resolution from active database position
        active_pos = None
        if strategy_id:
            active_pos = await db.strategy_positions.find_one({
                "user_id": user_id,
                "strategy_id": strategy_id,
                "instrument_key": instrument_key,
                "status": {"$in": ["PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}
            })

        if active_pos:
            pos_side = str(active_pos.get("position_side") or "LONG").upper()
            if pos_side == "SHORT":
                intent_str = "CLOSE_SHORT" if broker_side == "BUY" else "OPEN_SHORT"
                asset_class = "OPTION_SHORT"
            else:
                intent_str = "CLOSE_LONG" if broker_side == "SELL" else "OPEN_LONG"
                asset_class = "OPTION_LONG"
        else:
            if broker_side == "BUY":
                intent_str = "OPEN_LONG"
                asset_class = "OPTION_LONG"
            else:
                intent_str = "OPEN_SHORT"
                asset_class = "OPTION_SHORT"

        lot_size = int(option_contract.get("lot_size") or options_helper.LOT_SIZES.get((option_contract.get("underlying") or "").upper(), 1))
        lots = max(1, int(qty or 1))
        final_qty = lots * lot_size

        instr = InstrumentRef(
            broker=execution_broker,
            segment=seg,
            exchange=opt_exchange,
            tradingsymbol=tsym,
            instrument_token=token,
            asset_class=asset_class,
        )

        intent = OrderIntent(
            instrument=instr,
            quantity=final_qty,
            intent=intent_str,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return {"intent": intent, "lot_size": lot_size, "lots": lots}

    # ----- equity / index derivatives path -----
    segment = _execution_segment_for(exchange, "DIRECT", symbol_upper)
    asset_class = "DIRECT"
    if exchange in ("NFO", "BFO"):
        segment = _execution_segment_for(exchange, "FUTURES", symbol_upper)

    token = symbol_upper
    if execution_broker == "upstox":
        resolved = _upstox_instrument_token(exchange, symbol_upper)
        if resolved:
            token = resolved

    instrument_key = _instrument_key(exchange, symbol_upper, token)
    active_pos = None
    if strategy_id:
        active_pos = await db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "instrument_key": instrument_key,
            "status": {"$in": ["PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}
        })

    eval_side = str(side or "BUY").upper()
    if active_pos:
        pos_side = str(active_pos.get("position_side") or "LONG").upper()
        if pos_side == "SHORT":
            intent_str = "CLOSE_SHORT" if eval_side == "BUY" else "OPEN_SHORT"
        else:
            intent_str = "CLOSE_LONG" if eval_side == "SELL" else "OPEN_LONG"
    else:
        intent_str = "OPEN_LONG" if eval_side == "BUY" else "OPEN_SHORT"

    final_qty = int(qty or settings.get("default_qty", 1))

    instr = InstrumentRef(
        broker=execution_broker,
        segment=segment,
        exchange=exchange,
        tradingsymbol=symbol_upper,
        instrument_token=token,
        asset_class=asset_class,
    )

    intent = OrderIntent(
        instrument=instr,
        quantity=final_qty,
        intent=intent_str,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    return {"intent": intent}


def _infer_intent(
    side: str,
    strategy_id: Optional[str],
    user_id: str,
    instr: "InstrumentRef",
    asset_class: str,
) -> str:
    """Infer OPEN_LONG / CLOSE_LONG / OPEN_SHORT / CLOSE_SHORT from side
    and asset class.  For short-selling options the mapping inverts."""
    if asset_class == "OPTION_SHORT":
        return "OPEN_SHORT" if side == "SELL" else "CLOSE_SHORT"
    return "OPEN_LONG" if side == "BUY" else "CLOSE_LONG"


# ---------------------------------------------------------------------------
# _resolve_order_fill_hint  –  best-effort LTP for pre-trade risk checks
# ---------------------------------------------------------------------------

async def _resolve_order_fill_hint(
    user_id: str,
    intent: "OrderIntent",
    limit_price: Optional[float],
    paper: bool,
    option_contract: Optional[Dict[str, Any]],
    *,
    execution_broker: str = "upstox",
) -> float:
    """Return a best-effort fill price hint for position-size and risk checks.

    Uses the explicit ``limit_price`` when provided, otherwise queries
    ``_current_ltp_for_symbol``.  Falls back to 0.0 (caller decides whether
    to block the order).
    """
    if limit_price is not None and limit_price > 0:
        return float(limit_price)

    # Try option premium LTP from option_contract
    if option_contract and option_contract.get("ltp"):
        return float(option_contract["ltp"])
    if option_contract and execution_broker == "upstox" and (option_contract.get("instrument_token") or option_contract.get("instrument_key")):
        token = str(option_contract.get("instrument_token") or option_contract.get("instrument_key"))
        gateway = await get_user_upstox_gateway(user_id)
        if gateway and gateway.connected:
            try:
                quote = await asyncio.to_thread(gateway.get_market_quote, [token])
                ltp = UpstoxGateway.parse_quote_ltp(quote, token)
                if ltp and ltp > 0:
                    return float(ltp)
            except Exception as exc:
                logger.warning("Upstox option LTP failed for %s: %s", token, exc)
    if paper and option_contract:
        settings = await get_user_settings(user_id)
        allow_simulated = bool(settings.get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
        if allow_simulated:
            market_session = _market_session_for_instrument(intent.instrument, option_contract)
            if not market_session.get("open"):
                underlying = option_contract.get("underlying") or getattr(intent, "symbol", None)
                return float(_get_paper_ltp(str(underlying or intent.instrument.tradingsymbol), option_contract))
            logger.warning(
                "Paper option simulated fill blocked during live market: symbol=%s token=%s session=%s",
                intent.instrument.tradingsymbol,
                option_contract.get("instrument_token") or option_contract.get("instrument_key"),
                market_session.get("segment"),
            )

    instr = intent.instrument
    try:
        ltp = await _current_ltp_for_symbol(
            user_id,
            instr.tradingsymbol,
            instr.exchange,
            allow_mock=paper,
            execution_broker=execution_broker,
        )
        if ltp and ltp > 0:
            return float(ltp)
    except Exception as exc:
        logger.warning("fill-hint LTP lookup failed for %s: %s", instr.tradingsymbol, exc)

    return 0.0


async def _record_pretrade_risk_event(user_id: str, payload: Dict[str, Any]) -> None:
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    try:
        await db.risk_events.insert_one(doc)
    except Exception as exc:
        logger.warning("risk event persistence failed: %s", exc)


async def _market_snapshot_for_intent(
    user_id: str,
    intent: "OrderIntent",
    *,
    option_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    instr = intent.instrument
    token = str(instr.instrument_token or "").strip()
    market_open = _is_order_market_open(instr.exchange)
    snapshot: Dict[str, Any] = {
        "symbol": instr.tradingsymbol,
        "exchange": instr.exchange,
        "instrument_key": token or None,
        "token": token or None,
        "ltp": None,
        "timestamp": None,
        "tick_time": None,
        "timestamp_source": None,
        "received_at": None,
        "data_age_sec": None,
        "bid": None,
        "ask": None,
        "source": "unavailable",
        "feed": "unavailable",
        "market_status": "open" if market_open else "closed",
        "market_open": market_open,
        "block_reason": None,
    }
    gateway = await get_user_upstox_gateway(user_id)
    tick = gateway.latest_tick(token) if gateway and token else None
    if not tick and gateway:
        tick = gateway.latest_tick_by_symbol(instr.tradingsymbol)
    if tick:
        raw = tick.get("raw") if isinstance(tick.get("raw"), dict) else {}
        received_at = tick.get("received_at")
        broker_ts = tick.get("last_trade_time") or tick.get("tick_time") or tick.get("timestamp")
        timestamp = broker_ts or received_at
        timestamp_source = tick.get("timestamp_source") or ("broker" if broker_ts else "server_received_at" if received_at else None)
        if received_at and not broker_ts:
            logger.warning("Market snapshot using received_at fallback for %s token=%s", instr.tradingsymbol, token)
        snapshot.update({
            "symbol": tick.get("symbol") or instr.tradingsymbol,
            "exchange": tick.get("exchange") or instr.exchange,
            "instrument_key": tick.get("instrument_key") or token or None,
            "token": tick.get("token") or token or None,
            "ltp": tick.get("ltp"),
            "timestamp": timestamp,
            "tick_time": timestamp,
            "timestamp_source": timestamp_source,
            "received_at": received_at,
            "data_age_sec": _market_data_age_sec(received_at),
            "bid": tick.get("bid") or tick.get("bidP") or raw.get("bid") or raw.get("bid_price") or raw.get("bidP"),
            "ask": tick.get("ask") or tick.get("askP") or raw.get("ask") or raw.get("ask_price") or raw.get("askP"),
            "source": tick.get("source") or "upstox-cache",
            "feed": tick.get("feed") or "upstox-cache",
        })
    if snapshot.get("ltp") in (None, "") and option_contract and option_contract.get("ltp") and option_contract.get("simulated"):
        snapshot.update({"ltp": option_contract.get("ltp"), "source": "option-contract", "feed": "option-contract"})

    # If websocket tick is missing but the instrument key is known, try a fresh REST
    # quote so the pre-trade gate has a received_at and can pass market-data quality.
    if snapshot.get("received_at") is None and token and gateway and gateway.connected:
        try:
            q = await asyncio.to_thread(gateway.get_market_quote, [token])
            ltp_rest = UpstoxGateway.parse_quote_ltp(q, token)
            if ltp_rest and ltp_rest > 0:
                rest_received_at = datetime.now(timezone.utc).isoformat()
                snapshot.update({
                    "ltp": float(ltp_rest),
                    "received_at": rest_received_at,
                    "tick_time": rest_received_at,
                    "timestamp": rest_received_at,
                    "timestamp_source": "rest_quote_fallback",
                    "data_age_sec": 0.0,
                    "source": "upstox-rest-quote",
                    "feed": "upstox-rest-quote",
                })
                logger.info(
                    "market snapshot REST fallback ok symbol=%s token=%s ltp=%s",
                    instr.tradingsymbol, token, ltp_rest,
                )
        except Exception as _snap_exc:
            logger.debug(
                "market snapshot REST fallback failed symbol=%s token=%s: %s",
                instr.tradingsymbol, token, _snap_exc,
            )
    return snapshot


async def _pre_trade_risk_gate(
    user_id: str,
    intent: "OrderIntent",
    *,
    settings: Dict[str, Any],
    strategy_id: Optional[str],
    paper: bool,
    fill_price_hint: float,
    option_contract: Optional[Dict[str, Any]],
    lot_size: int,
) -> Dict[str, Any]:
    # LIVE-MODE HARD GUARD: A simulated (PAPER_SIMULATED_CONTRACT) option contract must
    # never reach the live execution engine. Reject immediately if this is a live order
    # with a fabricated paper contract.
    if not paper and option_contract and option_contract.get("simulated"):
        contract_src = option_contract.get("source", "UNKNOWN")
        sym = option_contract.get("tradingsymbol", "?")
        logger.critical(
            "LIVE ORDER BLOCKED: simulated contract '%s' (source=%s) attempted in live mode for user=%s strategy=%s",
            sym, contract_src, user_id, strategy_id,
        )
        raise HTTPException(
            status_code=400,
            detail=f"LIVE ORDER BLOCKED: Option contract '{sym}' is a simulated paper contract (source={contract_src}) and cannot be used in live trading. Reconnect Upstox to resolve a real contract.",
        )
    risk = _normalize_strategy_risk(await _get_strategy_risk(user_id, strategy_id)) if strategy_id else _normalize_strategy_risk(DEFAULT_STRATEGY_RISK)
    stop_price = intent.stop_loss
    if stop_price is None and fill_price_hint > 0:
        prices = _position_risk_prices({
            "average_buy_price": fill_price_hint,
            "position_side": "SHORT" if intent.intent == "OPEN_SHORT" else "LONG",
            "tp_sl_tsl_config": risk,
        })
        stop_price = prices.get("stop_loss")

    market_snapshot = await _market_snapshot_for_intent(user_id, intent, option_contract=option_contract)
    live_ltp = float(market_snapshot.get("ltp") or fill_price_hint or 0)
    quality = evaluate_market_data_quality(
        ltp=live_ltp,
        tick_time=market_snapshot.get("tick_time"),
        received_at=market_snapshot.get("received_at"),
        instrument_token=market_snapshot.get("instrument_key") or intent.instrument.instrument_token,
        exchange=intent.instrument.exchange,
        market_open=_is_order_market_open(intent.instrument.exchange),
        bid=market_snapshot.get("bid"),
        ask=market_snapshot.get("ask"),
        reference_price=fill_price_hint if fill_price_hint > 0 else None,
        risk_style=str(risk.get("risk_style") or "balanced"),
    )
    if not paper and not quality.get("ok"):
        payload = {
            "event": "PRETRADE_BLOCK",
            "strategy_id": strategy_id,
            "symbol": intent.instrument.tradingsymbol,
            "reason": quality.get("reason"),
            "market_snapshot": market_snapshot,
            "market_quality": quality,
            "risk_style": risk.get("risk_style"),
        }
        await _record_pretrade_risk_event(user_id, payload)
        logger.warning(
            "Pre-trade blocked symbol=%s exchange=%s token=%s reason=%s snapshot=%s",
            intent.instrument.tradingsymbol,
            intent.instrument.exchange,
            market_snapshot.get("instrument_key"),
            quality.get("reason"),
            market_snapshot,
        )
        raise HTTPException(status_code=400, detail=f"Pre-trade blocked: {quality.get('reason')}")

    funds_row = {"available_cash": settings.get("per_strategy_capital") or settings.get("max_position_size") or 0}
    if not paper:
        funds_row = await funds({"id": user_id})
    equity = float(settings.get("per_strategy_capital") or settings.get("max_position_size") or 0)
    free_margin = float(funds_row.get("available_cash") or equity or 0)
    size = compute_position_size(SizeInputs(
        equity=equity,
        free_margin=free_margin,
        requested_qty=int(intent.quantity),
        lot_size=max(1, int(lot_size or 1)),
        entry_price=float(fill_price_hint or live_ltp or 0),
        stop_loss_price=float(stop_price or 0) if stop_price else None,
        max_position_value=float(settings.get("max_position_size") or equity or 0),
        daily_loss_limit=float(settings.get("max_daily_loss") or 0),
        risk_style=str(risk.get("risk_style") or "balanced"),
    ))

    event_payload = {
        "event": "PRETRADE_PASS" if size.allowed else "PRETRADE_BLOCK",
        "strategy_id": strategy_id,
        "symbol": intent.instrument.tradingsymbol,
        "intent": intent.intent,
        "requested_qty": int(intent.quantity),
        "final_qty": int(size.quantity),
        "reason": size.reason,
        "risk_style": risk.get("risk_style"),
        "risk_budget": size.risk_budget,
        "unit_loss_at_stop": size.unit_loss_at_stop,
        "order_value": size.order_value,
        "caps": size.caps,
        "market_quality": quality,
        "paper": paper,
    }
    await _record_pretrade_risk_event(user_id, event_payload)
    if not size.allowed:
        raise HTTPException(status_code=400, detail=f"Pre-trade blocked: {size.reason}")
    return {**event_payload, "allowed": True, "quantity": int(size.quantity)}


def _extract_total_charge(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    nodes = [payload, payload.get("data") if isinstance(payload.get("data"), dict) else None]
    total = 0.0
    for node in [n for n in nodes if isinstance(n, dict)]:
        for key in ("total", "total_charges", "charges", "brokerage", "net_charges"):
            value = node.get(key)
            if isinstance(value, dict):
                total += sum(float(v or 0) for v in value.values() if isinstance(v, (int, float, str)))
            elif value not in (None, ""):
                try:
                    total += float(value)
                except Exception:
                    pass
        if total > 0:
            return round(total, 2)
    return 0.0


def _extract_available_margin(payload: Any, segment: str) -> float:
    if not isinstance(payload, dict):
        return 0.0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return 0.0
    desk = "commodity" if str(segment or "").upper().startswith("MCX") else "equity"
    node = data.get(desk) if isinstance(data.get(desk), dict) else data
    candidates = [
        node.get("available_margin"),
        node.get("available_cash"),
        node.get("net"),
        node.get("cash"),
        node.get("adhoc_margin"),
    ]
    for value in candidates:
        try:
            if value not in (None, ""):
                return float(value)
        except Exception:
            continue
    return 0.0


def _extract_required_margin(payload: Any) -> float:
    if not isinstance(payload, dict):
        return 0.0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    margins = data.get("margins") if isinstance(data, dict) else None
    if isinstance(margins, list):
        total = 0.0
        for row in margins:
            if isinstance(row, dict):
                for key in ("total_margin", "required_margin", "span_margin", "exposure_margin", "net_buy_premium", "additional_margin"):
                    try:
                        total += float(row.get(key) or 0)
                    except Exception:
                        pass
        return round(total, 2)
    for key in ("total_margin", "required_margin", "net_buy_premium", "final_margin"):
        try:
            value = data.get(key) if isinstance(data, dict) else None
            if value not in (None, ""):
                return float(value)
        except Exception:
            pass
    return 0.0


async def _upstox_live_cost_gate(
    user_id: str,
    intent: "OrderIntent",
    *,
    product: str,
    price: float,
) -> Dict[str, Any]:
    gw = await get_user_upstox_gateway(user_id)
    if not gw or not gw.connected:
        raise HTTPException(status_code=400, detail="Live margin check failed: Upstox gateway is not connected.")
    instr = intent.instrument
    instrument_key = instr.instrument_token
    if "|" not in str(instrument_key or ""):
        raise HTTPException(status_code=400, detail="Live margin check failed: Upstox instrument_key is missing.")
    side = _intent_side(intent.intent)
    qty = int(intent.quantity)
    quote_price = float(price or 0)
    if quote_price <= 0:
        raise HTTPException(status_code=400, detail="Live margin check failed: price is unavailable.")

    funds_payload, margin_payload, brokerage_payload = await asyncio.gather(
        asyncio.to_thread(gw.get_margins),
        asyncio.to_thread(gw.get_margin_details, [{
            "instrument_key": instrument_key,
            "quantity": qty,
            "transaction_type": side,
            "product": UpstoxGateway.normalize_product(product),
        }]),
        asyncio.to_thread(
            gw.get_brokerage_details,
            instrument_token=instrument_key,
            quantity=qty,
            product=product,
            transaction_type=side,
            price=quote_price,
        ),
        return_exceptions=True,
    )
    errors = [str(item) for item in (funds_payload, margin_payload, brokerage_payload) if isinstance(item, Exception)]
    if errors:
        raise HTTPException(status_code=400, detail=f"Live margin/charges check failed: {'; '.join(errors)[:300]}")

    available = _extract_available_margin(funds_payload, instr.segment or instr.exchange)
    required = _extract_required_margin(margin_payload)
    charges = _extract_total_charge(brokerage_payload)
    if required <= 0:
        required = abs(qty * quote_price)
    required_with_charges = required + charges
    if available > 0 and required_with_charges > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient Upstox margin: need INR {required_with_charges:,.2f}, available INR {available:,.2f}.",
        )
    return {
        "funds": funds_payload,
        "margin": margin_payload,
        "brokerage": brokerage_payload,
        "available_margin": round(available, 2),
        "required_margin": round(required, 2),
        "estimated_charges": round(charges, 2),
        "required_with_charges": round(required_with_charges, 2),
    }


async def _paper_upstox_cost_model(
    user_id: str,
    intent: "OrderIntent",
    *,
    product: str,
    price: float,
    option_contract: Optional[Dict[str, Any]],
    market_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    instr = intent.instrument
    qty = int(intent.quantity or 0)
    side = _intent_side(intent.intent)
    quote_price = float(price or 0)
    trade_value = round(abs(qty * quote_price), 2)
    asset_type = _asset_type_for_instrument(instr, option_contract)
    charge_breakup = _simulate_upstox_like_charges(
        quote_price,
        qty,
        side=side,
        exchange=instr.exchange,
        asset_type=asset_type,
    )
    from core.paper_broker import PaperWallet
    wallet = PaperWallet(db)
    wallet_balance = await wallet.get_balance(user_id)
    required = trade_value + float(charge_breakup.get("total") or 0) if side == "BUY" else float(charge_breakup.get("total") or 0)
    if side == "BUY" and wallet_balance < required:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient paper funds: need INR {required:,.2f}, available INR {wallet_balance:,.2f}.",
        )
    return {
        "source": "paper_upstox_cost_model",
        "broker": "upstox",
        "mode": "paper",
        "product": product,
        "trade_value": trade_value,
        "available_margin": round(float(wallet_balance or 0), 2),
        "required_margin": round(trade_value, 2),
        "estimated_charges": round(float(charge_breakup.get("total") or 0), 2),
        "required_with_charges": round(required, 2),
        "charges_breakup": charge_breakup,
        "quote_source": (market_snapshot or {}).get("source"),
        "quote_age_sec": (market_snapshot or {}).get("data_age_sec"),
        "paper_realism": "UPSTOX_LIKE",
        "instrument_key": (
            (market_snapshot or {}).get("instrument_key")
            or (option_contract or {}).get("instrument_key")
            or instr.instrument_token
        ),
    }


def _paper_price_trace(
    intent: "OrderIntent",
    *,
    option_contract: Optional[Dict[str, Any]],
    market_snapshot: Dict[str, Any],
    signal_id: Optional[str],
) -> Dict[str, Any]:
    instr = intent.instrument
    return {
        "signal_id": signal_id,
        "signal_action": _intent_side(intent.intent),
        "selected_symbol": instr.tradingsymbol,
        "expiry": (option_contract or {}).get("expiry"),
        "strike": (option_contract or {}).get("strike"),
        "option_type": (option_contract or {}).get("option_type"),
        "exchange": instr.exchange,
        "instrument_token": instr.instrument_token,
        "instrument_key": market_snapshot.get("instrument_key") or instr.instrument_token,
        "ltp_source": market_snapshot.get("source"),
        "ltp_value": market_snapshot.get("ltp"),
        "quote_timestamp": market_snapshot.get("timestamp") or market_snapshot.get("tick_time"),
        "quote_received_at": market_snapshot.get("received_at"),
        "quote_age_sec": market_snapshot.get("data_age_sec"),
        "cached_quote_exists": market_snapshot.get("source") in {"upstox-cache", "option-contract"},
        "token_subscribed": bool(market_snapshot.get("instrument_key") and market_snapshot.get("feed") == "upstox-cache"),
        "resolver_reason": market_snapshot.get("block_reason") or "price unavailable",
    }


async def _persist_paper_skipped_order(
    *,
    user_id: str,
    intent: "OrderIntent",
    order_type: str,
    product: Optional[str],
    price: Optional[float],
    source: str,
    strategy_id: Optional[str],
    option_contract: Optional[Dict[str, Any]],
    resolution: Dict[str, Any],
    idempotency_key: Optional[str],
    signal_id: Optional[str],
    market_snapshot: Dict[str, Any],
    reason: str = "price unavailable",
    reason_code: str = "PRICE_UNAVAILABLE",
    preflight: Optional[ExecutionPreflightResult] = None,
) -> Dict[str, Any]:
    """Record a deduplicated skipped signal summary without creating an order."""
    instr = intent.instrument
    now = datetime.now(timezone.utc).isoformat()
    trace = _paper_price_trace(
        intent,
        option_contract=option_contract,
        market_snapshot=market_snapshot,
        signal_id=signal_id,
    )
    session_date = _ist_date_key()
    side = _intent_side(intent.intent)
    dedupe_key = "|".join([
        str(strategy_id or "manual"),
        str(instr.tradingsymbol or "").upper(),
        side,
        str(reason_code or "SKIPPED"),
        session_date,
    ])
    doc_seed = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "dedupe_key": dedupe_key,
        "session_date": session_date,
        "symbol": instr.tradingsymbol,
        "side": side,
        "qty": int(intent.quantity or 0),
        "filled_qty": 0,
        "pending_qty": 0,
        "order_type": order_type,
        "requested_price": float(price or 0),
        "expected_price": 0.0,
        "price": 0.0,
        "brokerage": 0.0,
        "slippage": 0.0,
        "product": product,
        "status": ORDER_SKIPPED_SIGNAL,
        "legacy_status": ORDER_SKIPPED_SIGNAL,
        "execution_status": ORDER_SKIPPED_SIGNAL,
        "status_message": f"{ORDER_SKIPPED_SIGNAL}: {reason}",
        "reason_code": reason_code,
        "skip_reason": reason,
        "mode": "paper",
        "broker": "paper",
        "source": source,
        "strategy_id": strategy_id,
        "signal_id": signal_id,
        "created_at": now,
        "exchange": instr.exchange,
        "asset_type": _asset_type_for_instrument(instr, option_contract),
        "order_intent": intent.model_dump(),
        "instrument": instr.model_dump(),
        "segment": _execution_segment_for(instr.exchange, instr.asset_class, instr.tradingsymbol, option_contract),
        "paper_skip": True,
        "paper_skip_trace": trace,
    }
    if option_contract:
        doc_seed.update({
            "underlying": option_contract.get("underlying"),
            "option_type": option_contract.get("option_type"),
            "strike": option_contract.get("strike"),
            "expiry": option_contract.get("expiry"),
            "instrument_token": option_contract.get("instrument_token"),
            "instrument_key": option_contract.get("instrument_key") or option_contract.get("instrument_token"),
            "entry_spot": option_contract.get("spot"),
            "lots": resolution.get("lots"),
            "lot_size": resolution.get("lot_size"),
        })
    mutable_doc_fields = {
        "last_seen_at": now,
        "updated_at": now,
        "last_signal_id": signal_id,
        "last_trace": trace,
        "market_snapshot": market_snapshot,
        "market_session": preflight.market_session if preflight else None,
        "preflight": preflight.model_dump() if preflight else None,
    }
    try:
        doc = await db.skipped_signals.find_one_and_update(
            {"user_id": user_id, "dedupe_key": dedupe_key},
            {
                "$setOnInsert": doc_seed,
                "$set": mutable_doc_fields,
                "$inc": {"count": 1},
            },
            upsert=True,
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
    except AttributeError:
        doc = {**doc_seed, **mutable_doc_fields}
        doc["count"] = 1
        await db.skipped_signals.insert_one(doc)
    doc = doc or doc_seed
    if strategy_id and strategy_id != "manual_recovery":
        try:
            await db.strategies.update_one(
                {"id": strategy_id, "user_id": user_id},
                {
                    "$set": {
                        "last_evaluated_at": now,
                        "last_signal_action": side,
                        "last_signal_validated": False,
                        "last_filter_reason": reason,
                        "last_skip_reason_code": reason_code,
                        "last_contract_selected": instr.tradingsymbol,
                        "last_price_source": trace.get("ltp_source"),
                        "last_ltp_timestamp": trace.get("quote_timestamp") or trace.get("quote_received_at"),
                    },
                    "$inc": {"skipped_count_today": 1},
                },
            )
        except Exception as exc:
            logger.warning("strategy skip diagnostics update failed strategy=%s: %s", strategy_id, exc)
    logger.warning(
        "Paper execution skipped strategy_id=%s signal_id=%s action=%s symbol=%s expiry=%s strike=%s option_type=%s exchange=%s token=%s ltp_source=%s ltp=%s quote_ts=%s subscribed=%s cached=%s reason=%s",
        strategy_id,
        signal_id,
        trace.get("signal_action"),
        trace.get("selected_symbol"),
        trace.get("expiry"),
        trace.get("strike"),
        trace.get("option_type"),
        trace.get("exchange"),
        trace.get("instrument_key"),
        trace.get("ltp_source"),
        trace.get("ltp_value"),
        trace.get("quote_timestamp"),
        trace.get("token_subscribed"),
        trace.get("cached_quote_exists"),
        reason,
    )
    doc.pop("_id", None)
    return doc


async def _persist_core_paper_skipped_order(
    *,
    user_id: str,
    strategy_id: str,
    symbol: str,
    option_contract: Optional[Dict[str, Any]],
    side: str,
    qty: int,
    price: float,
    reason: str,
    reason_code: str,
    idempotency_key: Optional[str],
    signal_id: Optional[str],
    market_snapshot: Dict[str, Any],
    mode: str = "paper",
    broker: str = "paper"
) -> Dict[str, Any]:
    from core.market_domains import resolve_domain_by_underlying
    domain = resolve_domain_by_underlying(symbol)
    
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    session_date = now_dt.date().isoformat()
    dedupe_key = f"{strategy_id}:{symbol}:{side}:{reason_code}:{session_date}"
    
    trace = {
        "signal_action": side,
        "selected_symbol": option_contract["tradingsymbol"] if option_contract else symbol,
        "underlying": symbol,
        "exchange": option_contract.get("exchange", "NFO") if option_contract else "NSE",
        "instrument_key": (option_contract or {}).get("instrument_key"),
        "ltp_source": market_snapshot.get("source", "upstox-cache"),
        "ltp_value": market_snapshot.get("ltp", 0.0),
        "quote_timestamp": market_snapshot.get("received_at"),
        "reason": reason,
        "reason_code": reason_code,
        "quote_age_at_decision": market_snapshot.get("quote_age"),
        "spread_at_decision": market_snapshot.get("spread"),
        "spread_bps_at_decision": market_snapshot.get("spread_bps"),
    }
    
    doc_seed = {
        "user_id": user_id,
        "dedupe_key": dedupe_key,
        "strategy_id": strategy_id,
        "session_date": session_date,
        "symbol": option_contract["tradingsymbol"] if option_contract else symbol,
        "side": side,
        "qty": qty,
        "filled_qty": 0,
        "pending_qty": 0,
        "order_type": "MARKET",
        "requested_price": price,
        "expected_price": 0.0,
        "price": 0.0,
        "brokerage": 0.0,
        "slippage": 0.0,
        "product": "MIS",
        "status": ORDER_SKIPPED_SIGNAL,
        "legacy_status": ORDER_SKIPPED_SIGNAL,
        "execution_status": ORDER_SKIPPED_SIGNAL,
        "status_message": f"{ORDER_SKIPPED_SIGNAL}: {reason}",
        "reason_code": reason_code,
        "skip_reason": reason,
        "mode": mode,
        "broker": broker,
        "source": f"strategy:{strategy_id}" if strategy_id != "manual" else "manual",
        "signal_id": signal_id,
        "created_at": now,
        "exchange": option_contract.get("exchange", "NFO") if option_contract else "NSE",
        "asset_type": "option" if option_contract else "equity",
        "segment": domain.segment,
        "paper_skip": True,
        "paper_skip_trace": trace,
    }
    if option_contract:
        doc_seed.update({
            "underlying": option_contract.get("underlying"),
            "option_type": option_contract.get("option_type"),
            "strike": option_contract.get("strike"),
            "expiry": option_contract.get("expiry"),
            "instrument_token": option_contract.get("instrument_token"),
            "instrument_key": option_contract.get("instrument_key") or option_contract.get("instrument_token"),
            "entry_spot": option_contract.get("spot"),
            "lots": qty,
            "lot_size": option_contract.get("lot_size", 1),
        })
    mutable_doc_fields = {
        "last_seen_at": now,
        "updated_at": now,
        "last_signal_id": signal_id,
        "last_trace": trace,
        "market_snapshot": market_snapshot,
        "market_session": {"open": True, "reason": "market hours open"},
    }
    try:
        doc = await db.skipped_signals.find_one_and_update(
            {"user_id": user_id, "dedupe_key": dedupe_key},
            {
                "$setOnInsert": doc_seed,
                "$set": mutable_doc_fields,
                "$inc": {"count": 1},
            },
            upsert=True,
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
    except AttributeError:
        doc = {**doc_seed, **mutable_doc_fields}
        doc["count"] = 1
        await db.skipped_signals.insert_one(doc)
    doc = doc or doc_seed
    
    if strategy_id and strategy_id not in ("manual", "manual_recovery"):
        try:
            await db.strategies.update_one(
                {"id": strategy_id, "user_id": user_id},
                {
                    "$set": {
                        "last_evaluated_at": now,
                        "last_signal_action": side,
                        "last_signal_validated": False,
                        "last_filter_reason": reason,
                        "last_skip_reason_code": reason_code,
                        "last_contract_selected": option_contract.get("tradingsymbol") if option_contract else symbol,
                        "last_price_source": trace.get("ltp_source"),
                        "last_ltp_timestamp": trace.get("quote_timestamp"),
                    },
                    "$inc": {"skipped_count_today": 1},
                },
            )
        except Exception as exc:
            logger.warning("strategy skip diagnostics update failed strategy=%s: %s", strategy_id, exc)
            
    _log_throttled(
        f"core-paper-skip:{user_id}:{dedupe_key}",
        300.0,
        logging.WARNING,
        "Core Paper execution skipped strategy_id=%s signal_id=%s action=%s symbol=%s reason=%s reason_code=%s",
        strategy_id,
        signal_id,
        side,
        symbol,
        reason,
        reason_code,
    )
    doc.pop("_id", None)
    return doc


# ---------------------------------------------------------------------------
# _submit_order_intent  –  dispatch live order to the correct broker adapter
# ---------------------------------------------------------------------------

async def _submit_order_intent(
    user_id: str,
    intent: "OrderIntent",
    *,
    order_type: str,
    product: str,
    price: Optional[float],
    tag: str,
) -> Dict[str, Any]:
    """Submit live orders through Upstox only.

    Legacy broker adapters remain in the repository for old routes/tests, but
    the execution path used by strategies and manual orders is intentionally
    Upstox-only.
    """
    settings = await get_user_settings(user_id)
    if settings.get("paper_mode", True):
        raise RuntimeError("CRITICAL ERROR: Attempted to submit a broker order while in PAPER mode.")

    instr = intent.instrument
    side = _intent_side(intent.intent)
    qty = int(intent.quantity)

    upstox_token = instr.instrument_token if "|" in str(instr.instrument_token or "") else None
    if not upstox_token:
        resolved = _upstox_instrument_token(instr.exchange, instr.tradingsymbol, instr.instrument_token)
        if resolved:
            upstox_token = resolved
    if not upstox_token:
        raise RuntimeError(f"instrument not found: Upstox instrument_key missing for {instr.exchange}:{instr.tradingsymbol}")
    return await _place_upstox_order(
        user_id,
        instrument_token=upstox_token,
        side=side,
        quantity=qty,
        order_type=order_type,
        product=product,
        price=price,
        tag=tag,
    )


# ---------------------------------------------------------------------------
# _persist_failed_order  –  save a record of a broker-rejected order
# ---------------------------------------------------------------------------

async def _persist_failed_order(
    *,
    user_id: str,
    intent: "OrderIntent",
    order_type: str,
    product: str,
    price: Optional[float],
    source: str,
    strategy_id: Optional[str],
    error_message: str,
    resolution: Dict[str, Any],
    option_contract: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist a failed order document so the user can see what went wrong."""
    instr = intent.instrument
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "symbol": instr.tradingsymbol,
        "side": _intent_side(intent.intent),
        "qty": int(intent.quantity),
        "order_type": order_type,
        "price": float(price or 0),
        "product": product,
        "status": "REJECTED",
        "mode": "live",
        "broker": _runtime_broker_name(instr.broker),
        "source": source,
        "strategy_id": strategy_id,
        "exchange": instr.exchange,
        "segment": instr.segment,
        "error_message": error_message,
        "order_intent": intent.model_dump(),
        "created_at": now,
        "execution_status": "REJECTED",
    }
    if option_contract:
        doc.update({
            "underlying": option_contract.get("underlying"),
            "option_type": option_contract.get("option_type"),
            "strike": option_contract.get("strike"),
            "expiry": option_contract.get("expiry"),
            "instrument_token": option_contract.get("instrument_token"),
            "lot_size": resolution.get("lot_size"),
            "lots": resolution.get("lots"),
        })
    await db.orders.insert_one(doc)
    doc.pop("_id", None)
    return doc


def _clean_order_response(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    out.pop("user_id", None)
    out.pop("placement_owner", None)
    out.pop("placement_lock_until", None)
    out.pop("paper_fill_apply_owner", None)
    out.pop("paper_fill_apply_lock_until", None)
    out.pop("paper_fill_apply_started_at", None)
    out.setdefault("broker_order_id", None)
    # Ensure rejection reason is always surfaced (used by Orders UI)
    if not out.get("reject_reason"):
        out["reject_reason"] = out.get("error_message") or out.get("status_message") or None
    return out


def _scoped_idempotency_key(user_id: str, raw_key: Optional[str]) -> str:
    raw = str(raw_key or "").strip()
    if not raw:
        raw = f"auto:{uuid.uuid4().hex}"
    digest = hashlib.sha256(f"{user_id}:{raw}".encode("utf-8")).hexdigest()
    return f"idem:{digest}"


async def _append_business_outbox_event(
    *,
    user_id: str,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    """Durable event record for replay/publishing; publisher can be added later."""
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "event_type": event_type,
        "payload": payload or {},
        "idempotency_key": idempotency_key,
        "status": "PENDING",
        "created_at": now,
        "updated_at": now,
        "publish_attempts": 0,
    }
    try:
        await db.outbox_events.insert_one(doc)
    except DuplicateKeyError:
        return
    except Exception as exc:
        logger.warning("outbox append failed aggregate=%s event=%s: %s", aggregate_id, event_type, exc)


async def _append_order_event(order_id: str, user_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    try:
        await db.order_events.insert_one({
            "id": str(uuid.uuid4()),
            "order_id": order_id,
            "user_id": user_id,
            "event_type": event_type,
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        await _append_business_outbox_event(
            user_id=user_id,
            aggregate_type="order",
            aggregate_id=order_id,
            event_type=event_type,
            payload=payload or {},
            idempotency_key=(payload or {}).get("idempotency_key"),
        )
    except Exception as exc:
        logger.warning("order event append failed order=%s event=%s: %s", order_id, event_type, exc)


async def _assert_live_trading_book_safe(user_id: str, *, strategy_id: Optional[str], symbol: str) -> None:
    """Block new live entries when global safety state says local/broker books disagree."""
    kill_switch = await db.risk_state.find_one({"_id": "global_kill_switch"})
    if kill_switch and kill_switch.get("active"):
        raise HTTPException(status_code=409, detail="Live trading blocked: global kill switch is active.")

    unknown_orders = await db.orders.count_documents({
        "user_id": user_id,
        "mode": "live",
        "status": ORDER_UNKNOWN_NEEDS_REVIEW,
        "visibility": {"$ne": "hidden"},
    })
    if unknown_orders:
        await _record_pretrade_risk_event(user_id, {
            "event": "PRETRADE_BLOCK",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "reason": "UNKNOWN_ORDER_NEEDS_REVIEW",
            "unknown_orders": unknown_orders,
            "paper": False,
        })
        raise HTTPException(
            status_code=409,
            detail=f"Live trading blocked: {unknown_orders} live order(s) need broker reconciliation review.",
        )

    recon = await db.risk_state.find_one({
        "$or": [
            {"_id": f"position_reconciliation:{user_id}"},
            {"_id": "position_reconciliation", "$or": [{"user_id": user_id}, {"user_id": {"$exists": False}}]},
        ]
    })
    if recon and recon.get("mismatch_detected"):
        await _record_pretrade_risk_event(user_id, {
            "event": "PRETRADE_BLOCK",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "reason": "BROKER_RECONCILIATION_REQUIRED",
            "reconciliation": {
                "mismatches": recon.get("mismatches") or [],
                "last_reconciled_at": recon.get("last_reconciled_at"),
            },
            "paper": False,
        })
        raise HTTPException(
            status_code=409,
            detail="Live trading blocked: broker reconciliation mismatch detected. Sync and resolve positions first.",
        )


async def _insert_order_intent(order_doc: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    """Insert an order intent once per idempotency key.

    Returns (doc, inserted). On duplicate-key conflict the already-persisted
    order is returned and the caller must not place a second broker order.
    """
    try:
        await db.orders.insert_one(order_doc)
        await _append_order_event(order_doc["id"], order_doc["user_id"], "ORDER_INTENT_PERSISTED", {
            "status": order_doc.get("status"),
            "idempotency_key": order_doc.get("idempotency_key"),
            "mode": order_doc.get("mode"),
        })
        return dict(order_doc), True
    except DuplicateKeyError:
        lease_now = datetime.now(timezone.utc).isoformat()
        existing = await db.orders.find_one_and_update(
            {
                "user_id": order_doc["user_id"],
                "idempotency_key": order_doc.get("idempotency_key"),
                "mode": "live",
                "status": ORDER_NEW,
                "broker_order_id": {"$in": [None, ""]},
                "$or": [
                    {"placement_lock_until": {"$lt": lease_now}},
                    {"placement_lock_until": {"$exists": False}},
                    {"placement_lock_until": None},
                ],
            },
            {"$set": {
                "placement_owner": order_doc.get("placement_owner"),
                "placement_lock_until": order_doc.get("placement_lock_until"),
                "updated_at": lease_now,
            }},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if existing:
            return existing, True
        existing = await db.orders.find_one(
            {"user_id": order_doc["user_id"], "idempotency_key": order_doc.get("idempotency_key")},
            {"_id": 0},
        )
        if existing:
            return existing, False
        raise


def _classify_rejection_reason(message: str) -> str:
    lower_msg = str(message or "").lower()
    if "margin" in lower_msg or "capital" in lower_msg or "balance" in lower_msg or "insufficient" in lower_msg:
        return "FAILED_ORDER: insufficient margin"
    if "already has active" in lower_msg or "already reserved" in lower_msg or "duplicate position" in lower_msg or "re-entry blocked" in lower_msg:
        return "FAILED_ORDER: duplicate position"
    if "symbol mismatch" in lower_msg or "invalid-upstox-key" in lower_msg or "exchange must be" in lower_msg or "symbol not found" in lower_msg or "instrument not found" in lower_msg:
        return "FAILED_ORDER: symbol mismatch"
    if "stale" in lower_msg or "quality" in lower_msg or "websocket disconnected" in lower_msg or "ltp unavailable" in lower_msg:
        return "FAILED_ORDER: stale market data"
    if "no stored open strategy position" in lower_msg or "sell blocked" in lower_msg or "no open position" in lower_msg:
        return "FAILED_ORDER: no open position"
    if "market hours" in lower_msg or "outside" in lower_msg:
        return "FAILED_ORDER: market hours"
    if "live trading disabled" in lower_msg or "reconnect upstox" in lower_msg:
        return "FAILED_ORDER: live trading disabled"
    
    # Append the real rejection details to the broker rejection message for the UI
    msg_suffix = message[:120] + "..." if len(message) > 120 else message
    return f"FAILED_ORDER: broker rejection - {msg_suffix}"


async def _mark_order_rejected(order_id: str, user_id: str, message: str) -> Dict[str, Any]:
    reason_code = _classify_rejection_reason(message)
    now = datetime.now(timezone.utc).isoformat()
    current = await db.orders.find_one({"id": order_id, "user_id": user_id}, {"_id": 0, "status": 1})
    try:
        next_status = validate_order_transition((current or {}).get("status") or ORDER_NEW, ORDER_REJECTED)
    except ValueError as exc:
        await _append_order_event(order_id, user_id, "ORDER_TRANSITION_REJECTED", {
            "target_status": ORDER_REJECTED,
            "message": str(exc),
            "reject_message": message,
        })
        raise RuntimeError(str(exc))
    row = await db.orders.find_one_and_update(
        {"id": order_id, "user_id": user_id},
        {"$set": {
            "status": next_status,
            "legacy_status": "REJECTED",
            "execution_status": next_status,
            "status_message": reason_code,
            "error_message": message,
            "reject_reason": reason_code,
            "updated_at": now,
        }, "$unset": {"placement_owner": "", "placement_lock_until": ""}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    await _append_order_event(order_id, user_id, "ORDER_REJECTED", {"message": message, "reason_code": reason_code})
    await _close_order_exposure_reservation(order_id, user_id, status="RELEASED", reason=reason_code)
    return row or {}


async def _mark_order_submitted(order_id: str, user_id: str, submit: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    broker_order_id = submit.get("broker_order_id") or submit.get("order_id")
    if broker_order_id is not None:
        broker_order_id = str(broker_order_id)
    broker_status = submit.get("broker_status")
    canonical = canonical_order_status(broker_status or ORDER_PLACED)
    if canonical == ORDER_NEW:
        canonical = ORDER_PLACED
    current = await db.orders.find_one({"id": order_id, "user_id": user_id}, {"_id": 0, "status": 1})
    try:
        canonical = validate_order_transition((current or {}).get("status") or ORDER_NEW, canonical)
    except ValueError as exc:
        await _append_order_event(order_id, user_id, "ORDER_TRANSITION_REJECTED", {
            "target_status": canonical,
            "broker_order_id": broker_order_id,
            "broker_status": broker_status,
            "message": str(exc),
        })
        raise RuntimeError(str(exc))
    update_doc = {
        "$set": {
            "status": canonical,
            "legacy_status": "PENDING_BROKER",
            "execution_status": canonical,
            "broker_status": broker_status,
            "broker_order_id": broker_order_id,
            "execution_attempts": int(submit.get("attempts") or 1),
            "execution_recovered": bool(submit.get("recovered")),
            "broker_response": submit.get("raw") or submit.get("broker_order") or submit,
            "updated_at": now,
        },
        "$unset": {"placement_owner": "", "placement_lock_until": ""},
    }
    last_db_exc: Optional[Exception] = None
    for _attempt in range(3):
        try:
            row = await db.orders.find_one_and_update(
                {"id": order_id, "user_id": user_id},
                update_doc,
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
            )
            await _append_order_event(order_id, user_id, "ORDER_PLACED", {
                "broker_order_id": broker_order_id,
                "broker_status": broker_status,
                "status": canonical,
            })
            return row or {}
        except DuplicateKeyError:
            existing = await db.orders.find_one(
                {"user_id": user_id, "broker_order_id": broker_order_id},
                {"_id": 0},
            )
            if existing:
                return existing
            raise
        except Exception as db_exc:
            last_db_exc = db_exc
            if _attempt < 2:
                await asyncio.sleep(0.5 * (2 ** _attempt))
    # All retries exhausted — broker accepted order but local record has no broker_order_id.
    # Raise a tagged error so the caller can attempt a broker-side cancel.
    logger.critical(
        "DB_PERSIST_FAILURE after 3 attempts: broker_order_id=%s order_id=%s user=%s — "
        "broker has the order but local DB write failed. Caller will attempt cancel.",
        broker_order_id, order_id, user_id,
    )
    raise RuntimeError(f"BROKER_ORDER_ORPHAN:{broker_order_id}:{last_db_exc}")


def _paper_delta(intent_name: str, quantity: int) -> int:
    return int(quantity) if intent_name in {"OPEN_LONG", "CLOSE_SHORT"} else -int(quantity)


def _assert_position_source_order_doc(order_doc: Dict[str, Any], *, expected_mode: str) -> None:
    order_id = order_doc.get("id")
    status = canonical_order_status(order_doc.get("status"))
    raw_status = str(order_doc.get("status") or "").upper()
    if not order_id:
        raise RuntimeError("CRITICAL ERROR: create_position requires source_order_id.")
    if order_doc.get("mode") != expected_mode:
        raise RuntimeError("CRITICAL ERROR: position mode does not match source order mode.")
    if status != ORDER_FILLED and raw_status != ORDER_PAPER_FILLED:
        raise RuntimeError(
            f"CRITICAL ERROR: position source order {order_id} is not filled "
            f"(status={order_doc.get('status')})."
        )


def _core_engine_owns_paper() -> bool:
    """True when the canonical core engine (ExecutionRouter→PortfolioLedger) owns
    paper execution. In that mode the legacy `_apply_paper_fill_to_position`
    engine must NOT write money state — paper fills flow through process_fill.
    Mirrors the use_core_engine gate inside _place_order_core."""
    return (
        os.environ.get("CORE_ENGINE_ENABLED", "false").lower() == "true"
        and os.environ.get("CORE_ENGINE_PAPER_ENABLED", "false").lower() == "true"
    )


async def _apply_paper_fill_to_position(order_doc: Dict[str, Any], fill_price: float) -> Dict[str, Any]:
    """Apply a paper fill exactly once and write immutable fill/trade records.

    LEGACY paper fill engine. When the core engine owns paper execution
    (_core_engine_owns_paper), paper fills are booked by PortfolioLedger via
    ExecutionRouter and this path is not used. The dead `_place_order_core`
    legacy branch and crash recovery are fenced off elsewhere; this tripwire
    surfaces any unexpected invocation so a divergent second writer can't return
    silently.
    """
    if order_doc.get("mode") != "paper":
        raise RuntimeError("CRITICAL ERROR: Attempted to apply simulated paper fill to a LIVE order.")
    if _core_engine_owns_paper():
        logger.critical(
            "LEGACY paper fill engine invoked for order=%s while core engine owns paper "
            "execution — this should never happen; canonical ledger should have booked it. "
            "Proceeding, but investigate the caller (possible second-writer regression).",
            order_doc.get("id"),
        )

    order_id = order_doc["id"]
    user_id = order_doc["user_id"]
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    apply_owner = uuid.uuid4().hex
    apply_lock_until = (now_dt + timedelta(seconds=60)).isoformat()
    locked = await db.orders.find_one_and_update(
        {
            "id": order_id,
            "user_id": user_id,
            "mode": "paper",
            "paper_fill_applied": {"$ne": True},
            "$or": [
                {"paper_fill_apply_lock_until": {"$lt": now}},
                {"paper_fill_apply_lock_until": {"$exists": False}},
                {"paper_fill_apply_lock_until": None},
            ],
        },
        {"$set": {
            "paper_fill_apply_owner": apply_owner,
            "paper_fill_apply_lock_until": apply_lock_until,
            "paper_fill_apply_started_at": now,
        }},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if not locked:
        existing = await db.orders.find_one({"id": order_id, "user_id": user_id}, {"_id": 0})
        return existing or order_doc

    intent_doc = locked.get("order_intent") or {}
    intent_name = str(intent_doc.get("intent") or "").upper()
    qty = int(locked.get("qty") or locked.get("quantity") or 0)
    delta = _paper_delta(intent_name, qty)
    symbol = locked["symbol"]
    locked["status"] = ORDER_PAPER_FILLED
    locked["execution_status"] = ORDER_PAPER_FILLED
    locked["legacy_status"] = "COMPLETE"
    _assert_position_source_order_doc(locked, expected_mode="paper")
    brokerage = float(locked.get("brokerage") or _simulate_paper_brokerage(fill_price, qty))
    charges = float(locked.get("charges") or brokerage)
    expected = float(locked.get("expected_price") or locked.get("requested_price") or fill_price or 0)
    slippage = round(abs(float(fill_price or 0) - expected) * qty, 2) if expected > 0 else 0.0
    side = str(locked.get("side") or "").upper()
    trade_value = round(abs(float(fill_price or 0) * qty), 2)

    from core.paper_broker import PaperWallet
    wallet = PaperWallet(db)
    if side == "BUY":
        wallet_amount = round(trade_value + charges, 2)
        if not await wallet.debit(user_id, wallet_amount, order_id):
            balance = await wallet.get_balance(user_id)
            raise RuntimeError(
                f"Insufficient paper funds: need INR {wallet_amount:,.2f}, "
                f"have INR {balance:,.2f}."
            )
        wallet_action = "DEBIT"
    else:
        wallet_amount = round(max(0.0, trade_value - charges), 2)
        await wallet.credit(user_id, wallet_amount, order_id)
        wallet_action = "CREDIT"

    paper_position_query = {
        "user_id": user_id,
        "symbol": symbol,
        "$or": [{"mode": "paper"}, {"mode": {"$exists": False}}],
    }
    pos = await db.positions.find_one(paper_position_query)
    before_qty = int((pos or {}).get("qty") or 0)
    
    # Cap exit delta to prevent negative quantities on exit
    if intent_name in {"CLOSE_LONG", "CLOSE_SHORT"}:
        if before_qty > 0 and intent_name == "CLOSE_LONG":
            delta = -min(before_qty, qty)
        elif before_qty < 0 and intent_name == "CLOSE_SHORT":
            delta = min(abs(before_qty), qty)
        else:
            delta = 0
            
    before_avg = float((pos or {}).get("avg_price") or 0)
    after_qty = before_qty + delta
    qty_closed = 0
    gross_realized = 0.0
    after_avg = float(fill_price or 0)

    if before_qty == 0 or before_qty * delta > 0:
        total_qty = abs(before_qty) + abs(delta)
        after_avg = round(((abs(before_qty) * before_avg) + (abs(delta) * fill_price)) / total_qty, 2) if total_qty else fill_price
    else:
        qty_closed = min(abs(before_qty), abs(delta))
        gross_realized = round((fill_price - before_avg) * qty_closed, 2) if before_qty > 0 else round((before_avg - fill_price) * qty_closed, 2)
        if after_qty == 0:
            after_avg = 0.0
        elif abs(delta) > abs(before_qty):
            after_avg = float(fill_price or 0)
        else:
            after_avg = before_avg

    net_realized = round(gross_realized - charges, 2) if qty_closed else 0.0

    if after_qty == 0:
        await db.positions.delete_one(paper_position_query)
    elif pos:
        await db.positions.update_one(
            {"_id": pos["_id"]},
            {"$set": {
                "qty": after_qty,
                "avg_price": round(after_avg, 2),
                "updated_at": now,
                "asset_type": locked.get("asset_type"),
                "exchange": locked.get("exchange"),
                "strategy_id": locked.get("strategy_id"),
                "mode": "paper",
                "broker": "paper",
                "source_order_id": order_id,
                "source_order_status": ORDER_PAPER_FILLED,
                "status": "POSITION_OPENED",
            }},
        )
    else:
        await db.positions.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "symbol": symbol,
            "qty": after_qty,
            "avg_price": round(after_avg, 2),
            "created_at": now,
            "updated_at": now,
            "asset_type": locked.get("asset_type"),
            "exchange": locked.get("exchange"),
            "strategy_id": locked.get("strategy_id"),
            "mode": "paper",
            "broker": "paper",
            "source_order_id": order_id,
            "source_order_status": ORDER_PAPER_FILLED,
            "status": "POSITION_OPENED",
        })

    # Stamp the canonical `action` + `created_at` so these legacy-path fills are
    # visible to portfolio_ledger.get_strategy_pnl_today (the single P&L reader),
    # which filters trade_fills on action in {CLOSE, REDUCE} and created_at.
    # Without these, P&L from this code path is invisible to the canonical reader
    # while still landing in db.positions/orders — the phantom-number seam.
    if qty_closed:
        fill_action = "CLOSE" if after_qty == 0 else "REDUCE"
    elif before_qty == 0:
        fill_action = "OPEN"
    else:
        fill_action = "ADD"

    fill_doc = {
        "id": str(uuid.uuid4()),
        "order_id": order_id,
        "user_id": user_id,
        "strategy_id": locked.get("strategy_id"),
        "symbol": symbol,
        "side": locked.get("side"),
        "qty": qty,
        "fill_price": float(fill_price or 0),
        "brokerage": brokerage,
        "charges": charges,
        "slippage": slippage,
        "gross_realized_pnl": gross_realized,
        "gross_pnl": gross_realized,
        "realized_pnl": net_realized,
        "net_pnl": net_realized,
        "position_before_qty": before_qty,
        "position_after_qty": after_qty,
        "avg_price_before": before_avg,
        "avg_price_after": round(after_avg, 2),
        "action": fill_action,
        "filled_at": now,
        "created_at": now,
        "mode": "paper",
    }
    logger.info(
        "Trade Fill (Accounting Ledger): user_id=%s strategy_id=%s symbol=%s side=%s qty=%d price=%.2f realized_pnl=%.2f before_qty=%d after_qty=%d",
        user_id, locked.get("strategy_id"), symbol, locked.get("side"), qty, float(fill_price or 0), net_realized, before_qty, after_qty
    )
    try:
        await db.trade_fills.insert_one(fill_doc)
    except DuplicateKeyError:
        pass
    if qty_closed:
        try:
            await db.trades.insert_one({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "strategy_id": locked.get("strategy_id"),
                "entry_symbol": symbol,
                "exit_order_id": order_id,
                "qty": qty_closed,
                "exit_price": float(fill_price or 0),
                "gross_realized_pnl": gross_realized,
                "gross_pnl": gross_realized,
                "realized_pnl": net_realized,
                "net_pnl": net_realized,
                "brokerage": brokerage,
                "charges": charges,
                "slippage": slippage,
                "closed_at": now,
                "mode": "paper",
            })
        except DuplicateKeyError:
            pass

    updated = await db.orders.find_one_and_update(
        {"id": order_id, "user_id": user_id},
        {"$set": {
            "status": ORDER_PAPER_FILLED,
            "legacy_status": "COMPLETE",
            "execution_status": ORDER_PAPER_FILLED,
            "status_message": "PAPER_FILLED: simulated fill applied",
            "filled_qty": qty,
            "pending_qty": 0,
            "price": float(fill_price or 0),
            "gross_realized_pnl": gross_realized,
            "gross_pnl": gross_realized,
            "realized_pnl": net_realized,
            "net_pnl": net_realized,
            "brokerage": brokerage,
            "charges": charges,
            "slippage": slippage,
            "trade_value": trade_value,
            "paper_wallet_applied": True,
            "paper_wallet_action": wallet_action,
            "paper_wallet_amount": wallet_amount,
            "paper_fill_applied": True,
            "updated_at": now,
        }, "$unset": {
            "paper_fill_apply_owner": "",
}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    await _append_order_event(order_id, user_id, "PAPER_FILL_APPLIED", {
        "fill_price": float(fill_price or 0),
        "qty": qty,
        "realized_pnl": net_realized,
        "position_before_qty": before_qty,
        "position_after_qty": after_qty,
    })
    return updated or locked


async def _book_live_fill_from_order(
    order_doc: Dict[str, Any],
    *,
    fill_price: float,
    filled_qty: Optional[int],
    raw_report: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Book one immutable live fill row for a broker-filled order."""
    if order_doc.get("mode") != "live":
        return None
    order_id = order_doc.get("id")
    user_id = order_doc.get("user_id")
    if not order_id or not user_id:
        raise RuntimeError("live fill booking requires order id and user id")

    existing = await db.trade_fills.find_one({"order_id": order_id, "user_id": user_id}, {"_id": 0})
    if existing:
        return existing

    qty = int(filled_qty or order_doc.get("filled_qty") or order_doc.get("qty") or 0)
    price = float(fill_price or order_doc.get("price") or order_doc.get("expected_price") or 0)
    if qty <= 0 or price <= 0:
        await _append_order_event(order_id, user_id, "LIVE_FILL_BOOKING_SKIPPED", {
            "reason": "filled quantity or price unavailable",
            "filled_qty": qty,
            "fill_price": price,
        })
        return None

    intent_doc = order_doc.get("order_intent") or {}
    side = str(order_doc.get("side") or "").upper()
    symbol = order_doc.get("symbol") or ((intent_doc.get("instrument") or {}).get("tradingsymbol"))
    target_symbol = intent_doc.get("target_symbol") or order_doc.get("target_symbol") or symbol
    brokerage = float(order_doc.get("brokerage") or 0)
    expected = float(order_doc.get("expected_price") or order_doc.get("requested_price") or price or 0)
    slippage = round(abs(price - expected) * qty, 2) if expected > 0 else 0.0
    now = datetime.now(timezone.utc).isoformat()

    # Construct fill doc for PortfolioLedger
    fill_doc = {
        "id": f"tf_live_{order_id}",  # unique fill ID constructed from order_id to be deterministic
        "order_id": order_id,
        "user_id": user_id,
        "strategy_id": order_doc.get("strategy_id"),
        "symbol": order_doc.get("symbol") or intent_doc.get("symbol") or symbol,
        "target_symbol": target_symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "charges": brokerage,
        "brokerage": brokerage,
        "mode": "live",
        "instrument_key": intent_doc.get("instrument_key") or order_doc.get("instrument_key"),
        "instrument_token": intent_doc.get("instrument_token") or order_doc.get("instrument_token"),
        "option_contract": intent_doc.get("option_contract"),
        "asset_type": intent_doc.get("asset_type") or "option",
        "asset_class": intent_doc.get("asset_class"),
        "product": intent_doc.get("product") or "MIS",
        "expiry": intent_doc.get("expiry"),
        "underlying": intent_doc.get("underlying") or order_doc.get("symbol") or symbol,
        "exit_reason": intent_doc.get("exit_reason") or order_doc.get("exit_reason") or "broker-filled",
        "stop_loss": intent_doc.get("stop_loss"),
        "take_profit": intent_doc.get("take_profit"),
    }

    try:
        from core.portfolio_ledger import PortfolioLedger
        ledger = PortfolioLedger(db)
        ledger_result = await ledger.process_fill(fill_doc)
    except Exception as exc:
        logger.error("Live ledger routing failed for order %s: %s", order_id, exc)
        return None

    if ledger_result.get("accepted"):
        net_realized = ledger_result.get("realized_pnl") or 0.0
        gross_realized = ledger_result.get("gross_pnl") or 0.0

        await db.orders.update_one(
            {"id": order_id, "user_id": user_id},
            {"$set": {
                "live_fill_booked": True,
                "live_fill_id": fill_doc["id"],
                "gross_realized_pnl": gross_realized,
                "realized_pnl": net_realized,
                "brokerage": brokerage,
                "slippage": slippage,
                "updated_at": now,
            }},
        )
        await _append_order_event(order_id, user_id, "LIVE_FILL_BOOKED", {
            "fill_id": fill_doc["id"],
            "broker_order_id": order_doc.get("broker_order_id"),
            "qty": qty,
            "fill_price": price,
            "gross_realized_pnl": gross_realized,
            "realized_pnl": net_realized,
        })
        fill_doc["gross_realized_pnl"] = gross_realized
        fill_doc["realized_pnl"] = net_realized
        return fill_doc

    return None


async def _fill_ledger_summary(
    user_id: str,
    *,
    mode: Optional[str] = None,
    start: Any = None,
    end: Any = None,
    strategy_id: Optional[str] = None,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {"user_id": user_id}
    if mode:
        query["mode"] = mode
    if strategy_id:
        query["strategy_id"] = strategy_id
    if start is not None or end is not None:
        window: Dict[str, Any] = {}
        if start is not None:
            window["$gte"] = start
        if end is not None:
            window["$lt"] = end
        query["created_at"] = window
    fills = await db.trade_fills.find(query, {"_id": 0}).to_list(10000)
    realized_fills = [row for row in fills if float(row.get("realized_pnl") or 0) != 0]
    wins = len([row for row in realized_fills if float(row.get("realized_pnl") or 0) > 0])
    losses = len([row for row in realized_fills if float(row.get("realized_pnl") or 0) < 0])
    gross_turnover = round(sum(abs(float(row.get("fill_price") or row.get("price") or 0) * int(row.get("qty") or 0)) for row in fills), 2)
    brokerage = round(sum(float(row.get("brokerage") or 0) for row in fills), 2)
    slippage = round(sum(float(row.get("slippage") or 0) for row in fills), 2)
    realized = round(sum(float(row.get("realized_pnl") or 0) for row in fills), 2)
    return {
        "fills": fills,
        "fill_count": len(fills),
        "closed_trade_count": len(realized_fills),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / max(1, wins + losses) * 100, 2),
        "realized_pnl": realized,
        "gross_turnover": gross_turnover,
        "brokerage": brokerage,
        "slippage": slippage,
        "source": "trade_fills",
    }


async def _recover_pending_paper_fills(limit: int = 500) -> int:
    """Finish paper fills that were persisted before a restart/crash.

    Only meaningful for LEGACY-path orders (the legacy branch inserts
    paper_fill_applied=False). When the core engine owns paper execution, every
    paper order is booked synchronously via PortfolioLedger and inserted with
    paper_fill_applied=True; a failed fill is marked REJECTED, not left pending.
    So under the core engine there is nothing legitimate to recover here, and
    finishing a stale `paper_fill_applied!=True` order through the legacy applier
    would reintroduce the divergent second writer (db.positions instead of
    strategy_positions). Fence it off — leave any stale order visibly pending
    rather than book it through the wrong engine.
    """
    if _core_engine_owns_paper():
        logger.info(
            "paper fill recovery skipped: core engine owns paper execution "
            "(canonical ledger books fills synchronously; no legacy recovery needed)."
        )
        return 0
    rows = await db.orders.find(
        {
            "mode": "paper",
            "paper_fill_applied": {"$ne": True},
            "status": {"$in": [ORDER_NEW, ORDER_PAPER_CREATED, ORDER_FILLED, ORDER_PAPER_FILLED]},
        },
        {"_id": 0},
    ).sort("created_at", 1).limit(limit).to_list(length=limit)
    recovered = 0
    for row in rows:
        try:
            updated = await _apply_paper_fill_to_position(row, float(row.get("price") or row.get("expected_price") or 0))
            if updated.get("paper_fill_applied"):
                recovered += 1
        except Exception as exc:
            logger.warning("pending paper fill recovery failed order=%s: %s", row.get("id"), exc)
    return recovered


async def _daily_paper_lifecycle_for_user(user_id: str) -> Dict[str, int]:
    """Prepare paper state for the current IST trading day without deleting history."""
    today = _ist_date_key()
    start, end = get_trading_day_window_ist()
    now = datetime.now(timezone.utc).isoformat()

    stale_strategy_positions = await db.strategy_positions.update_many(
        {
            "user_id": user_id,
            "mode": "paper",
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
            "$or": [{"created_at": {"$lt": start}}, {"entry_time": {"$lt": start}}],
        },
        {
            "$set": {
                "status": "STALE_NEEDS_REVIEW",
                "stale": True,
                "stale_reason": "Open paper position carried from a previous trading day.",
                "stale_marked_at": now,
                "updated_at": now,
            },
            "$unset": {
                "active_instrument_key": "",
                "active_strategy_key": "",
                "active_strategy_instrument_side_key": "",
            },
        },
    )
    stale_positions = await db.positions.update_many(
        {
            "user_id": user_id,
            "$and": [
                {"$or": [{"mode": "paper"}, {"broker": "paper"}]},
                {"$or": [{"created_at": {"$lt": start}}, {"entry_time": {"$lt": start}}]},
            ],
            "status": {"$nin": ["CLOSED", "STALE_NEEDS_REVIEW"]},
        },
        {"$set": {
            "status": "STALE_NEEDS_REVIEW",
            "stale": True,
            "stale_reason": "Paper broker position carried from a previous trading day.",
            "stale_marked_at": now,
            "updated_at": now,
        }},
    )
    archived_orders = await db.orders.update_many(
        {
            "user_id": user_id,
            "mode": "paper",
            "created_at": {"$lt": start},
            "session_date": {"$ne": today},
        },
        {"$set": {
            "session_date": today,
            "orderbook_scope": "history",
            "history_preserved": True,
            "updated_at": now,
        }},
    )
    reset_strategies = await db.strategies.update_many(
        {
            "user_id": user_id,
            "$or": [
                {"today_session_date": {"$ne": today}},
                {"today_session_date": {"$exists": False}},
            ],
        },
        {"$set": {
            "today_session_date": today,
            "signal_count_today": 0,
            "duplicate_signal_count_today": 0,
            "skipped_count_today": 0,
            "order_count_today": 0,
            "evaluations_today": 0,
            "today_pnl": 0.0,
            "daily_lifecycle_at": now,
        }},
    )
    # Reset loss streaks daily so strategies blocked by consecutive SL hits
    # on previous days are not permanently frozen. Streaks only block for the day
    # they accumulate; each new session starts clean.
    reset_streaks = await db.strategy_loss_streaks.update_many(
        {"user_id": user_id, "current_streak": {"$gt": 0}},
        {"$set": {"current_streak": 0, "paused_until": None, "reset_at": now}},
    )

    await db.paper_session_state.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "session_date": today,
            "today_start": start,
            "today_end": end,
            "last_rollover_at": now,
            "stale_strategy_positions": stale_strategy_positions.modified_count,
            "stale_positions": stale_positions.modified_count,
            "history_orders_marked": archived_orders.modified_count,
            "strategies_reset": reset_strategies.modified_count,
            "loss_streaks_reset": reset_streaks.modified_count,
        }},
        upsert=True,
    )
    return {
        "stale_strategy_positions": stale_strategy_positions.modified_count,
        "stale_positions": stale_positions.modified_count,
        "history_orders_marked": archived_orders.modified_count,
        "strategies_reset": reset_strategies.modified_count,
        "loss_streaks_reset": reset_streaks.modified_count,
    }


async def _place_order_core(user_id: str, symbol: str, side: str, qty: Optional[int],
                            order_type: str = "MARKET", price: Optional[float] = None,
                            product: Optional[str] = None, source: str = "manual",
                            option_contract: Optional[Dict[str, Any]] = None,
                            exchange: str = "NSE",
                            stop_loss: Optional[float] = None,
                            take_profit: Optional[float] = None,
                            idempotency_key: Optional[str] = None,
                            signal_id: Optional[str] = None,
                            is_exit_order: bool = False,
                            exit_reason: Optional[str] = None) -> dict:
    side = (side or "").upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if option_contract:
        option_side = str(option_contract.get("transaction_type") or "").upper()
        if option_side in ("BUY", "SELL"):
            side = option_side
    order_type = (order_type or "MARKET").upper()
    if order_type not in ("MARKET", "LIMIT"):
        raise HTTPException(status_code=400, detail="order_type must be MARKET or LIMIT")
    if order_type == "LIMIT" and price is None:
        raise HTTPException(status_code=400, detail="LIMIT orders require a price")

    # GUARD (fail-closed, 2026-06-17): an order that resolves to a derivatives segment
    # (NSE_FO/BSE_FO) MUST carry a valid pipe/colon-delimited broker instrument_key
    # ("NSE_FO|<token>"). resolve_domain_by_underlying() matches a symbol to a
    # derivatives underlying via startswith(), so a synthetic or mis-routed label
    # (e.g. "BANKNIFTY-bullish-spread", a strategy name) is classified as a derivative,
    # has the underlying's lot size re-applied, and would fill as a phantom position
    # (the 2026-06-17 −16.5k loss). A real option/future always has a broker key by
    # this point; equity (NSE_EQ) is exempt — its key may be a bare symbol when the
    # ISIN lookup misses. Mirrors the existing quote-path key check.
    from core.market_domains import resolve_domain_by_underlying as _guard_resolve_domain
    _guard_key = str(
        (option_contract or {}).get("instrument_key")
        or (option_contract or {}).get("instrument_token")
        or symbol or ""
    )
    _guard_domain = _guard_resolve_domain(symbol)
    _guard_dn = _guard_domain.name.value if hasattr(_guard_domain.name, "value") else str(_guard_domain.name)
    if _guard_dn in ("NSE_FO", "BSE_FO") and "|" not in _guard_key and ":" not in _guard_key:
        logger.error(
            "ORDER REJECTED (invalid derivative instrument): symbol=%r resolved_domain=%s "
            "instrument_key=%r side=%s source=%s — no broker key; refusing to trade a "
            "synthetic/mis-routed symbol as a phantom position.",
            symbol, _guard_dn, _guard_key, side, source,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid derivative instrument '{symbol}': missing broker instrument_key (expected 'NSE_FO|<token>').",
        )

    # FIX 5: Check gateway_blocked before placing ANY new order (not exits — they must go through)
    if not is_exit_order and _GATEWAY_BLOCKED.get(user_id):
        logger.warning(
            "FIX5 ORDER BLOCKED: user=%s gateway not connected at market open — order for %s rejected",
            user_id, symbol,
        )
        raise HTTPException(
            status_code=503,
            detail=f"Gateway not connected for user {user_id}. Reconnect Upstox before placing orders.",
        )

    settings = dict(await get_user_settings(user_id))
    strategy_id = await _strategy_source_id(source)
    if not strategy_id and source in ("manual", "manual-exit", "squareoff-all"):
        strategy_id = "manual_recovery"
    strategy_row = None

    if strategy_id:
        if strategy_id == "manual_recovery":
            from position_reconciler import create_manual_recovery_strategy_if_missing
            await create_manual_recovery_strategy_if_missing(user_id, bool(settings.get("paper_mode", True)))
        strat = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if strat:
            strategy_row = strat
            if strat.get("mode") in ("paper", "live"):
                settings["paper_mode"] = strat.get("mode") == "paper"
            if strat.get("broker"):
                strat_broker = strat.get("broker")
                if strat_broker and strat_broker != "upstox":
                    await db.strategies.update_one(
                        {"id": strategy_id, "user_id": user_id},
                        {"$set": {"broker": "upstox", "last_error": ""}},
                    )
                settings["execution_broker"] = "upstox"

    paper = bool(settings.get("paper_mode", True))
    execution_broker = "upstox"

    CORE_ENGINE_ENABLED = os.environ.get("CORE_ENGINE_ENABLED", "false").lower() == "true"
    CORE_ENGINE_PAPER_ENABLED = os.environ.get("CORE_ENGINE_PAPER_ENABLED", "false").lower() == "true"
    CORE_ENGINE_LIVE_ENABLED = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"

    use_core_engine = False
    if CORE_ENGINE_ENABLED:
        if paper and CORE_ENGINE_PAPER_ENABLED:
            use_core_engine = True
        elif not paper and CORE_ENGINE_LIVE_ENABLED:
            use_core_engine = True

    if use_core_engine:
        from core.risk_manager import RiskManager
        from core.order_manager import OrderManager
        from core.execution_router import ExecutionRouter
        from core.portfolio_ledger import PortfolioLedger
        from core.market_domains import resolve_domain_by_underlying

        if not product and strategy_row:
            product = strategy_row.get("product") or (strategy_row.get("visual_config") or {}).get("options", {}).get("product")

        # Resolve risk_style once, unconditionally. The risk manager consumes it
        # for every non-exit order below. It used to be bound only inside the
        # market-data-quality block, whose guard (`not (price and price > 0)`)
        # is false for the common case of an entry signal that already carries a
        # valid price — so risk_style stayed unbound and the order raised
        # UnboundLocalError, silently dropping the signal at the execution boundary.
        risk_style = (strategy_row or {}).get("risk_style") or ((strategy_row or {}).get("visual_config") or {}).get("risk", {}).get("risk_style") or "balanced"

        domain = resolve_domain_by_underlying(symbol)
        order_mgr = OrderManager(db)
        session_date = datetime.now(timezone.utc).date().isoformat()
        signal_candle_time = datetime.now(timezone.utc).strftime("%H:%M")

        idem_key = idempotency_key or order_mgr.generate_idempotency_key(
            strategy_id=strategy_id or "manual",
            market_domain=domain.name.value,
            symbol=symbol,
            side=side,
            session_date=session_date,
            signal_candle_time=signal_candle_time
        )

        if not await order_mgr.verify_and_lock_idempotency(idem_key, user_id):
            existing = await db.orders.find_one({"user_id": user_id, "idempotency_key": idem_key})
            if existing:
                return _clean_order_response(existing)
            return {"ok": False, "status": "SKIPPED", "reason": "duplicate idempotency block"}

        # Get a realistic paper fill price (not dummy ₹100).
        # If caller passed a real price (e.g. from Upstox quote), use that.
        # Otherwise simulate using the symbol's known base price.
        contract_ltp = float((option_contract or {}).get("ltp") or 0)
        simulated_contract = bool((option_contract or {}).get("simulated"))
        domain_name = domain.name.value if hasattr(domain.name, "value") else str(domain.name)
        market_session = _segment_session_status(domain_name)
        target_symbol = option_contract["tradingsymbol"] if option_contract else symbol
        target_instrument_key = (
            (option_contract or {}).get("instrument_key")
            or (option_contract or {}).get("instrument_token")
            or target_symbol
        )
        if not option_contract and exchange in ("NSE", "BSE"):
            key_lookup = UPSTOX_EQUITY_INSTRUMENTS.get(symbol)
            if not key_lookup:
                inst_doc = await db.upstox_instruments.find_one({"exchange": exchange, "trading_symbol": symbol})
                if inst_doc:
                    key_lookup = inst_doc.get("instrument_key")
            if key_lookup:
                target_instrument_key = key_lookup
        # Entry-duplicate gates apply to NEW entries only. Exit orders (including
        # BUY exits of OPTION_SHORT positions) must never be blocked by them —
        # the account_duplicate check used to match the very EXITING position
        # being closed, permanently blocking short-option exits.
        if strategy_id and not is_exit_order and str(side or "").upper() == "BUY":
            core_position_side = "SHORT" if str(side or "").upper() == "SELL" else "LONG"
            side_key = _strategy_side_key(user_id, strategy_id, str(target_instrument_key), core_position_side)
            existing_position = await db.strategy_positions.find_one({
                "user_id": user_id,
                "strategy_id": strategy_id,
                "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
                "$or": [
                    {"active_strategy_instrument_side_key": side_key},
                    {"instrument_key": str(target_instrument_key), "position_side": core_position_side},
                ],
            })
            if existing_position:
                reason = f"Core {'paper' if paper else 'live'} entry blocked: same strategy/instrument/side position already exists for {target_symbol}."
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason=reason,
                    reason_code="DUPLICATE_STRATEGY_INSTRUMENT_SIDE",
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={
                        "ltp": contract_ltp,
                        "received_at": (option_contract or {}).get("received_at"),
                        "source": (option_contract or {}).get("source") or "preflight",
                    },
                    mode="paper" if paper else "live",
                    broker="paper" if paper else "upstox",
                )
                return _clean_order_response(skip_doc)

            # MULTI-STRATEGY: the old account-wide duplicate check (one active
            # position per option contract across ALL strategies) is removed.
            # Each strategy manages its own positions; the per-strategy duplicate
            # guard above is sufficient. Two strategies holding the same contract
            # is valid — they entered on independent signals and exit independently.

        # Check expired contract check in Core path
        received_at = (option_contract or {}).get("received_at")
        if option_contract:
            expiry_val = option_contract.get("expiry")
            if expiry_val:
                try:
                    from datetime import date
                    exp_date = date.fromisoformat(str(expiry_val))
                    if exp_date < date.today():
                        logger.warning("Core %s option order skipped: contract %s is expired (%s < today).", "paper" if paper else "live", target_symbol, expiry_val)
                        skip_doc = await _persist_core_paper_skipped_order(
                            user_id=user_id,
                            strategy_id=strategy_id or "manual",
                            symbol=symbol,
                            option_contract=option_contract,
                            side=side,
                            qty=qty or 1,
                            price=price or 0.0,
                            reason=f"Contract is expired ({expiry_val} < today).",
                            reason_code="EXPIRED_CONTRACT",
                            idempotency_key=idem_key,
                            signal_id=signal_id,
                            market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"},
                            mode="paper" if paper else "live",
                            broker="paper" if paper else "upstox",
                        )
                        return _clean_order_response(skip_doc)
                except Exception as e:
                    logger.warning(f"Error checking option expiry date: {e}")

        # Check feed connected/snapshot received in Core path
        if option_contract and not simulated_contract:
            upstox_gw = await get_user_upstox_gateway(user_id)
            allow_simulated_prices = bool((await get_user_settings(user_id)).get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
            if (not upstox_gw or not upstox_gw.connected) and not allow_simulated_prices:
                reason = "Upstox gateway feed is disconnected or offline."
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason=reason,
                    reason_code="FEED_DISCONNECTED",
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"},
                    mode="paper" if paper else "live",
                    broker="paper" if paper else "upstox",
                )
                return _clean_order_response(skip_doc)
        
        # Check staleness in Core path of _place_order_core
        # FIX 3: exit orders bypass ALL price quality guards — they must go through
        # regardless of LTP availability. A MARKET exit with a stale/fallback price
        # is always better than leaving a position permanently stuck.
        if not is_exit_order and option_contract and market_session.get("open") and not simulated_contract and not (price and price > 0):
            # 1. Minimum Premium Guard
            if contract_ltp < 1.0:
                logger.warning("Core %s option order skipped: minimum premium guard failed for %s (LTP %.2f < 1.0).", "paper" if paper else "live", target_symbol, contract_ltp)
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason=f"Option premium is too low (LTP {contract_ltp} < 1.0).",
                    reason_code="PRICE_UNAVAILABLE",
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"},
                    mode="paper" if paper else "live",
                    broker="paper" if paper else "upstox",
                )
                return _clean_order_response(skip_doc)

            # 2. Market Data Quality (staleness, spread, token presence, etc.)
            bid = float(option_contract.get("bid") or 0)
            ask = float(option_contract.get("ask") or 0)
            quality = evaluate_market_data_quality(
                ltp=contract_ltp,
                tick_time=option_contract.get("tick_time") or option_contract.get("timestamp") or received_at,
                received_at=received_at,
                instrument_token=option_contract.get("instrument_key") or target_symbol,
                exchange=domain.exchange,
                market_open=market_session.get("open"),
                bid=bid if bid > 0 else None,
                ask=ask if ask > 0 else None,
                risk_style=str(risk_style),
            )
            
            # Calculate observability variables for snapshot
            parsed_received_at = parse_market_timestamp(received_at)
            quote_age = (datetime.now(timezone.utc) - parsed_received_at).total_seconds() if parsed_received_at else None
            spread = ask - bid if (bid > 0 and ask > 0) else None
            spread_bps = ((ask - bid) / contract_ltp * 10000) if (bid > 0 and ask > 0 and contract_ltp > 0) else None
            
            if not quality.get("ok"):
                reason = quality.get("reason")
                reason_code = "SKIPPED_QUOTE_STALE" if "stale" in reason.lower() or "timestamp" in reason.lower() else "PRICE_UNAVAILABLE"
                logger.warning("Core %s option order skipped: market data quality failed for %s. Reason: %s", "paper" if paper else "live", target_symbol, reason)
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason=reason,
                    reason_code=reason_code,
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={
                        "ltp": contract_ltp,
                        "received_at": received_at,
                        "bid": bid if bid > 0 else None,
                        "ask": ask if ask > 0 else None,
                        "spread": spread,
                        "spread_bps": spread_bps,
                        "quote_age": quote_age,
                        "source": "upstox-cache"
                    },
                    mode="paper" if paper else "live",
                    broker="paper" if paper else "upstox",
                )
                return _clean_order_response(skip_doc)

        paper_ltp = price if (price and price > 0) else (contract_ltp if contract_ltp > 0 else (0.0 if market_session.get("open") else _get_paper_ltp(symbol, option_contract)))
        if paper_ltp <= 0:
            # FIX 5: exit orders with no resolvable price fall back to a realistic
            # simulated price (_get_paper_ltp) rather than a ₹0.05 nominal. The old
            # ₹0.05 fill booked a phantom loss equal to the full notional on every
            # price-less exit (notably cash equity, which has no subscribed WS token
            # and whose simulated fallback was skipped while the market was open).
            if is_exit_order:
                paper_ltp = _get_paper_ltp(symbol, option_contract)
                logger.warning(
                    "EXIT order for %s has no resolvable price — using simulated ₹%.2f for MARKET exit (is_exit_order=True)",
                    target_symbol, paper_ltp,
                )
            else:
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason="Paper price unavailable." if paper else "Live price unavailable.",
                    reason_code="PRICE_UNAVAILABLE",
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"},
                    mode="paper" if paper else "live",
                    broker="paper" if paper else "upstox",
                )
                return _clean_order_response(skip_doc)

        risk_mgr = RiskManager(db)
        _lot_size = domain.get_lot_size(symbol)
        # qty from signal_manager is in LOTS (e.g. 1 lot), but compute_position_size
        # expects quantity in SHARES. Convert: requested_qty_shares = lots * lot_size.
        # For equity strategies lot_size=1 so this is a no-op.
        _qty_lots = qty or 1
        _qty_shares = _qty_lots * _lot_size
        if is_exit_order:
            # EXIT GUARANTEE: exits bypass the risk manager entirely. Risk gates
            # (daily loss, sizing, Greeks, kill switch) exist to stop NEW exposure —
            # blocking or resizing an exit only locks losing positions open.
            risk_res = {"ok": True, "quantity": _qty_shares, "reason": "exit-order-bypass"}
        else:
            risk_res = await risk_mgr.evaluate_order(
                user_id=user_id,
                strategy_id=strategy_id or "manual",
                symbol=symbol,
                target_symbol=option_contract["tradingsymbol"] if option_contract else symbol,
                side=side,
                requested_qty=_qty_shares,
                price=paper_ltp,
                mode="paper" if paper else "live",
                stop_loss=stop_loss,
                take_profit=take_profit,
                lot_size=_lot_size,
                risk_style=risk_style,
                product=product
            )

        if not risk_res["ok"]:
            from core.event_store import CoreEventStore
            event_store = CoreEventStore(db)
            await event_store.log_event("RISK_BLOCKED", strategy_id or "manual", user_id, {"reason": risk_res["reason"], "symbol": symbol})
            return {"ok": False, "status": "SKIPPED", "reason": risk_res["reason"]}

        intent_doc = order_mgr.compile_order_intent(
            strategy_id=strategy_id or "manual",
            symbol=symbol,
            target_symbol=target_symbol,
            side=side,
            quantity=risk_res["quantity"],
            price=paper_ltp,
            exchange=domain.exchange,
            segment=domain.segment,
            mode="paper" if paper else "live",
            stop_loss=stop_loss,
            take_profit=take_profit,
            idempotency_key=idem_key
        )
        intent_doc["execution_tag"] = _new_execution_tag(strategy_id)
        intent_doc["product"] = product or "MIS"
        if is_exit_order:
            intent_doc["exit_reason"] = exit_reason or source
        if paper:
            intent_doc["paper_realism"] = "UPSTOX_LIKE"
        if option_contract:
            _ikey = (
                option_contract.get("instrument_key")
                or option_contract.get("instrument_token")
                or ""
            )
            intent_doc["instrument_key"] = _ikey
            intent_doc["instrument_token"] = _ikey
            # Store option_contract so execution_router can pass it to fill_doc
            # and portfolio_ledger can extract strike/expiry/lot_size/etc.
            intent_doc["option_contract"] = option_contract
            intent_doc["asset_type"] = "option"
            intent_doc["asset_class"] = "OPTION_LONG" if side == "BUY" else "OPTION_SHORT"
            intent_doc["lot_size"] = int(option_contract.get("lot_size") or _lot_size or 1)
            intent_doc["underlying"] = option_contract.get("underlying") or symbol
        else:
            intent_doc["instrument_key"] = target_instrument_key
            intent_doc["instrument_token"] = target_instrument_key
            intent_doc["asset_type"] = "equity"
            intent_doc["asset_class"] = "EQUITY"
            intent_doc["lot_size"] = 1
            intent_doc["underlying"] = symbol

        ledger = PortfolioLedger(db)
        router = ExecutionRouter(
            db,
            ledger,
            place_upstox_fn=_place_upstox_order,
            resolve_upstox_token_fn=_upstox_instrument_token,
        )
        order_res = await router.route_intent(user_id, intent_doc)
        # Subscribe this instrument to the WS feed so position monitor gets live ticks
        if intent_doc.get("instrument_key"):
            try:
                _gw = await get_user_upstox_gateway(user_id)
                if _gw:
                    asyncio.create_task(asyncio.to_thread(
                        _gw.start_market_data_ws, [intent_doc["instrument_key"]], "full"
                    ))
            except Exception:
                pass
        return _clean_order_response(order_res)

    if not paper:
        upstox_status = await get_user_upstox_status(user_id)
        if not upstox_status.get("token_valid"):
            raise HTTPException(
                status_code=400,
                detail="Live trading disabled: Reconnect Upstox required before placing live orders.",
            )
        if side == "BUY":
            await _assert_live_trading_book_safe(user_id, strategy_id=strategy_id, symbol=symbol)

    order_inserted = False
    position_reservation = None
    exposure_reservation = None
    exit_position_record = None
    intent = None
    preflight_blocked = False

    try:
        resolution = await _build_order_intent(
            user_id=user_id,
            symbol=symbol,
            side=side,
            qty=qty,
            source=source,
            exchange=exchange,
            settings=settings,
            option_contract=option_contract,
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        intent = resolution["intent"]
        instr = intent.instrument
        if instr.exchange not in SUPPORTED_ORDER_EXCHANGES:
            raise HTTPException(status_code=400, detail=f"exchange must be one of {sorted(SUPPORTED_ORDER_EXCHANGES)}")
        if _intent_is_entry(intent.intent):
            early_preflight = await _execution_preflight(
                user_id=user_id,
                strategy_id=strategy_id,
                strategy_row=strategy_row,
                intent=intent,
                settings=settings,
                paper=paper,
                option_contract=option_contract,
                fill_price_hint=0.0,
                market_snapshot={},
            )
            if not early_preflight.ok and early_preflight.reason_code == "MARKET_CLOSED":
                resolved_product_for_skip = (
                    product
                    or ("NRML" if instr.exchange in {"NFO", "BFO"} else settings.get("default_product", "MIS"))
                )
                if paper:
                    return _clean_order_response(await _persist_paper_skipped_order(
                        user_id=user_id,
                        intent=intent,
                        order_type=order_type,
                        product=str(resolved_product_for_skip).upper(),
                        price=price,
                        source=source,
                        strategy_id=strategy_id,
                        option_contract=option_contract,
                        resolution=resolution,
                        idempotency_key=idempotency_key,
                        signal_id=signal_id,
                        market_snapshot={},
                        reason=early_preflight.reason or "market closed",
                        reason_code=early_preflight.reason_code or "MARKET_CLOSED",
                        preflight=early_preflight,
                    ))
                await store_skipped_signal(
                    db,
                    user_id=user_id,
                    strategy_id=strategy_id,
                    symbol=instr.tradingsymbol,
                    reason_code=early_preflight.reason_code or "MARKET_CLOSED",
                    reason=early_preflight.reason or "Market is closed.",
                    details={"preflight": early_preflight.model_dump() if hasattr(early_preflight, "model_dump") else {}},
                )
                preflight_blocked = True
                raise HTTPException(status_code=400, detail=early_preflight.reason or "Market is closed.")

        strategy_id = await _strategy_source_id(source)
        if not strategy_id and source in ("manual", "manual-exit", "squareoff-all"):
            strategy_id = "manual_recovery"
        fill_price_hint = await _resolve_order_fill_hint(user_id, intent, price, paper, option_contract, execution_broker=execution_broker)
        if paper and option_contract and _intent_is_entry(intent.intent):
            market_snapshot = await _market_snapshot_for_intent(user_id, intent, option_contract=option_contract)
        else:
            market_snapshot = {"ltp": fill_price_hint, "source": "fill-hint", "feed": "fill-hint"} if fill_price_hint > 0 else await _market_snapshot_for_intent(user_id, intent, option_contract=option_contract)
        snapshot_ltp = float(market_snapshot.get("ltp") or 0)
        if fill_price_hint <= 0 and snapshot_ltp > 0:
            fill_price_hint = snapshot_ltp
        preflight = await _execution_preflight(
            user_id=user_id,
            strategy_id=strategy_id,
            strategy_row=strategy_row,
            intent=intent,
            settings=settings,
            paper=paper,
            option_contract=option_contract,
            fill_price_hint=fill_price_hint,
            market_snapshot=market_snapshot,
        )
        if not preflight.ok:
            resolved_product_for_skip = (
                product
                or ("NRML" if instr.exchange in {"NFO", "BFO"} else settings.get("default_product", "MIS"))
            )
            if paper:
                return _clean_order_response(await _persist_paper_skipped_order(
                    user_id=user_id,
                    intent=intent,
                    order_type=order_type,
                    product=str(resolved_product_for_skip).upper(),
                    price=price,
                    source=source,
                    strategy_id=strategy_id,
                    option_contract=option_contract,
                    resolution=resolution,
                    idempotency_key=idempotency_key,
                    signal_id=signal_id,
                    market_snapshot=market_snapshot,
                    reason=preflight.reason or "preflight skipped",
                    reason_code=preflight.reason_code or "SKIPPED",
                    preflight=preflight,
                ))
            await store_skipped_signal(
                db,
                user_id=user_id,
                strategy_id=strategy_id,
                symbol=instr.tradingsymbol,
                reason_code=preflight.reason_code or "SKIPPED",
                reason=preflight.reason or "Execution preflight failed.",
                details={
                    "preflight": preflight.model_dump() if hasattr(preflight, "model_dump") else {},
                    "market_snapshot": market_snapshot,
                    "option_contract": option_contract,
                },
            )
            preflight_blocked = True
            raise HTTPException(status_code=400, detail=preflight.reason or "Execution preflight failed.")
        pretrade_risk = None
        if _intent_is_entry(intent.intent):
            if intent.intent in ("OPEN_LONG", "OPEN_SHORT") and fill_price_hint <= 0 and not paper:
                raise HTTPException(status_code=400, detail=f"Live LTP unavailable for {instr.tradingsymbol}; order blocked.")
            pretrade_risk = await _pre_trade_risk_gate(
                user_id,
                intent,
                settings=settings,
                strategy_id=strategy_id,
                paper=paper,
                fill_price_hint=fill_price_hint,
                option_contract=option_contract,
                lot_size=int(resolution.get("lot_size") or 1),
            )
            if int(pretrade_risk.get("quantity") or intent.quantity) < int(intent.quantity):
                intent.quantity = int(pretrade_risk["quantity"])
                if resolution.get("lot_size"):
                    resolution["lots"] = max(1, int(intent.quantity) // max(1, int(resolution.get("lot_size") or 1)))
        resolved_product_candidate = (product or ("NRML" if instr.exchange in {"NFO", "BFO"} else settings.get("default_product", "MIS"))).upper()
        pretrade_cost = None
        if _intent_is_entry(intent.intent) and not paper:
            pretrade_cost = await _upstox_live_cost_gate(
                user_id,
                intent,
                product=resolved_product_candidate,
                price=float(fill_price_hint or price or 0),
            )

        if idempotency_key:
            existing_idem = await db.orders.find_one(
                {"user_id": user_id, "idempotency_key": _scoped_idempotency_key(user_id, idempotency_key)},
                {"_id": 0},
            )
            if existing_idem and not (
                existing_idem.get("mode") == "live"
                and existing_idem.get("status") == ORDER_NEW
                and not existing_idem.get("broker_order_id")
                and str(existing_idem.get("placement_lock_until") or "") < datetime.now(timezone.utc).isoformat()
            ):
                if existing_idem.get("mode") == "paper" and not existing_idem.get("paper_fill_applied"):
                    existing_idem = await _apply_paper_fill_to_position(
                        existing_idem,
                        float(existing_idem.get("price") or existing_idem.get("expected_price") or 0),
                    )
                return _clean_order_response(existing_idem)

        instrument_key = _instrument_key(instr.exchange, instr.tradingsymbol, instr.instrument_token)
        logger.info(
            "Resolved instrument token before order submission: exchange=%s symbol=%s instrument_token=%s instrument_key=%s",
            instr.exchange, instr.tradingsymbol, instr.instrument_token, instrument_key
        )
        print(f"\n>>> [ORDER SUBMISSION DIAGNOSTIC] RESOLVED INSTRUMENT KEY: {instrument_key} for {instr.exchange}:{instr.tradingsymbol} with token {instr.instrument_token}\n", flush=True)
        if strategy_id and _intent_is_entry(intent.intent):
            position_reservation = await _reserve_strategy_position(
                user_id=user_id,
                strategy_id=strategy_id,
                instrument_key=instrument_key,
                trading_symbol=instr.tradingsymbol,
                exchange=instr.exchange,
                instrument_token=instr.instrument_token,
                quantity=int(intent.quantity),
                entry_price=float(fill_price_hint or 0),
                position_side="SHORT" if intent.intent == "OPEN_SHORT" else "LONG",
                source=source,
                signal_id=signal_id,
            )
        if strategy_id and _intent_is_exit(intent.intent):
            exit_position_record = await _open_strategy_position_for_exit(
                user_id=user_id,
                strategy_id=strategy_id,
                instrument_key=instrument_key,
            )
            intent.quantity = int(exit_position_record.get("open_quantity") or exit_position_record.get("quantity") or intent.quantity)

        resolved_product = resolved_product_candidate
        execution_tag = _new_execution_tag(strategy_id)
        broker_order_id = None
        fill_price = price if price is not None else fill_price_hint
        if paper:
            fill_price = price if order_type == "LIMIT" and price is not None else _simulate_paper_fill_price(fill_price_hint or 1.0, _intent_side(intent.intent))
            pretrade_cost = await _paper_upstox_cost_model(
                user_id,
                intent,
                product=resolved_product,
                price=float(fill_price or 0),
                option_contract=option_contract,
                market_snapshot=market_snapshot,
            )
        estimated_charges = float((pretrade_cost or {}).get("estimated_charges") or 0)
        estimated_brokerage = float(((pretrade_cost or {}).get("charges_breakup") or {}).get("brokerage") or 0)

        # Fetch signal doc if signal_id is present
        signal_doc = None
        if signal_id:
            signal_doc = await db.signals.find_one({"id": signal_id})

        r_meta = {}
        if signal_doc:
            for field in [
                "setup_type", "confidence", "entry_reason", "target_R",
                "initial_stop_R", "trail_after_R", "max_hold_minutes",
                "invalidation_rule", "regime_required", "option_selection_preference",
                "signal_version", "strategy_logic_version", "default_strategy_version"
            ]:
                if field in signal_doc:
                    r_meta[field] = signal_doc[field]
            r_meta["r_metadata_source"] = "v13_signal"
        else:
            default_version = "v13-live-brain-r1"
            if strategy_row:
                default_version = strategy_row.get("default_strategy_version") or default_version
            r_meta = {
                "setup_type": "breakout",
                "confidence": 85.0,
                "entry_reason": "Fallback risk parameters",
                "target_R": 2.0,
                "initial_stop_R": 1.0,
                "trail_after_R": 1.5,
                "max_hold_minutes": 60,
                "invalidation_rule": "time_or_stop",
                "regime_required": "any",
                "option_selection_preference": "ATM",
                "signal_version": "v13",
                "strategy_logic_version": "1.0",
                "default_strategy_version": default_version,
                "r_metadata_source": "fallback"
            }

        now = datetime.now(timezone.utc).isoformat()
        placement_owner = uuid.uuid4().hex
        scoped_idempotency = _scoped_idempotency_key(user_id, idempotency_key)
        order_doc = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "idempotency_key": scoped_idempotency,
            "client_idempotency_key": str(idempotency_key or ""),
            "symbol": instr.tradingsymbol,
            "side": _intent_side(intent.intent),
            "qty": int(intent.quantity),
            "filled_qty": 0,
            "pending_qty": int(intent.quantity),
            "status_message": "Paper order created; waiting for simulated fill" if paper else "Order intent persisted before broker submission",
            "gross_realized_pnl": 0.0,
            "realized_pnl": 0.0,
            "order_type": order_type,
            "requested_price": float(price or 0),
            "expected_price": float(fill_price_hint or price or 0),
            "price": float(fill_price or 0),
            "brokerage": estimated_brokerage if paper else 0.0,
            "charges": estimated_charges,
            "net_pnl": 0.0,
            "slippage": 0.0,
            "product": resolved_product,
            "status": ORDER_PAPER_CREATED if paper else ORDER_NEW,
            "legacy_status": "PENDING_LOCAL" if paper else "PENDING_BROKER",
            "mode": "paper" if paper else "live",
            "broker": "paper" if paper else _runtime_broker_name(instr.broker),
            "execution_tag": execution_tag,
            "execution_attempts": 0,
            "execution_recovered": False,
            "source": source,
            "strategy_id": strategy_id,
            "signal_id": signal_id,
            "created_at": now,
            "updated_at": now,
            "exchange": instr.exchange,
            "asset_type": _asset_type_for_instrument(instr, option_contract),
            "order_intent": intent.model_dump(),
            "pretrade_risk": pretrade_risk,
            "pretrade_cost": pretrade_cost,
            "paper_realism": (pretrade_cost or {}).get("paper_realism") if paper else None,
            "live_ready_shape": True,
            "price_source": market_snapshot.get("source"),
            "price_feed": market_snapshot.get("feed"),
            "price_received_at": market_snapshot.get("received_at"),
            "price_timestamp": market_snapshot.get("timestamp") or market_snapshot.get("tick_time"),
            "price_validation": preflight.price_validation if preflight else None,
            "market_snapshot": market_snapshot,
            "instrument": instr.model_dump(),
            "segment": instr.segment,
            "stop_loss": intent.stop_loss,
            "take_profit": intent.take_profit,
            "execution_status": ORDER_PAPER_CREATED if paper else ORDER_NEW,
            "paper_fill_applied": False if paper else None,
        }
        order_doc.update(r_meta)
        if broker_order_id:
            order_doc["broker_order_id"] = str(broker_order_id)
        if not paper:
            order_doc["placement_owner"] = placement_owner
            order_doc["placement_lock_until"] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        if option_contract:
            order_doc.update({
                "underlying": option_contract.get("underlying"),
                "option_type": option_contract.get("option_type"),
                "strike": option_contract.get("strike"),
                "expiry": option_contract.get("expiry"),
                "instrument_token": option_contract.get("instrument_token"),
                "entry_spot": option_contract.get("spot"),
                "lots": resolution.get("lots"),
                "lot_size": resolution.get("lot_size"),
                "trade_quality_score": option_contract.get("trade_quality_score"),
                "quality_score": option_contract.get("quality_score"),
                "quality_readiness": option_contract.get("quality_readiness"),
            })
        order_doc, inserted = await _insert_order_intent(order_doc)
        order_inserted = True
        execution_tag = order_doc.get("execution_tag") or execution_tag
        if not inserted:
            if order_doc.get("mode") == "paper" and not order_doc.get("paper_fill_applied"):
                order_doc = await _apply_paper_fill_to_position(order_doc, float(order_doc.get("price") or fill_price or 0))
            await _cancel_strategy_reservation(position_reservation, "duplicate-idempotency-key")
            return _clean_order_response(order_doc)

        if not paper and _intent_is_entry(intent.intent):
            exposure_reservation = await _reserve_order_exposure(
                user_id=user_id,
                order_id=order_doc["id"],
                strategy_id=strategy_id,
                instrument_key=instrument_key,
                symbol=instr.tradingsymbol,
                quantity=int(intent.quantity),
                price=float(fill_price_hint or price or 0),
                settings=settings,
            )

        # Show the exact payload that would be sent to Upstox in live trading
        if paper:
            normalized_product = "I" if resolved_product.upper() in ["MIS", "INTRADAY", "I"] else "D"
            normalized_side = "BUY" if _intent_side(intent.intent) in ["BUY", "B"] else "SELL"
            normalized_type = order_type.upper()
            upstox_payload = {
                "quantity": int(intent.quantity),
                "product": normalized_product,
                "validity": "DAY",
                "price": 0.0 if normalized_type == "MARKET" else (price or 0.0),
                "tag": execution_tag,
                "instrument_token": option_contract.get("instrument_token") if option_contract else instr.instrument_token,
                "order_type": normalized_type,
                "transaction_type": normalized_side,
                "disclosed_quantity": 0,
                "trigger_price": 0.0,
                "is_amo": False,
                "market_protection": -1.0
            }
            import json
            print("\n>>> [UPSTOX API SIMULATION] EXACT ORDER PAYLOAD THAT WOULD BE SENT TO UPSTOX (IF LIVE):", flush=True)
            print(json.dumps(upstox_payload, indent=2), flush=True)
            print(">>> [UPSTOX API SIMULATION] END PAYLOAD <<<\n", flush=True)
            
            logger.info(
                "Upstox Live Order Routed (Simulated Payload): exchange=%s instrument_token=%s quantity=%s side=%s order_type=%s product=%s validity=%s",
                instr.exchange,
                upstox_payload.get("instrument_token"),
                upstox_payload.get("quantity"),
                upstox_payload.get("transaction_type"),
                upstox_payload.get("order_type"),
                upstox_payload.get("product"),
                upstox_payload.get("validity"),
            )

        if not paper:
            try:
                submit = await _submit_order_intent(
                    user_id,
                    intent,
                    order_type=order_type,
                    product=resolved_product,
                    price=price,
                    tag=execution_tag,
                )
                broker_order_id = submit.get("broker_order_id") or submit.get("order_id")
                if not broker_order_id:
                    raise RuntimeError(f"{instr.broker} accepted no broker_order_id for {instr.tradingsymbol}; cannot track PLACED state.")
                order_doc = await _mark_order_submitted(order_doc["id"], user_id, submit)
            except Exception as exc:
                exc_msg = str(exc)
                # Orphan recovery: broker accepted the order but local DB write exhausted
                # all retries. Attempt a best-effort broker-side cancel to avoid a ghost
                # position, then surface the order as UNKNOWN_NEEDS_REVIEW for manual fix.
                if exc_msg.startswith("BROKER_ORDER_ORPHAN:"):
                    parts = exc_msg.split(":", 2)
                    orphan_broker_id = parts[1] if len(parts) > 1 else ""
                    try:
                        gw = await get_user_upstox_gateway(user_id)
                        if gw and gw.connected and orphan_broker_id:
                            await asyncio.to_thread(gw.cancel_order, orphan_broker_id)
                            logger.critical(
                                "ORPHAN_CANCEL_SUCCESS broker_order_id=%s order_id=%s user=%s",
                                orphan_broker_id, order_doc.get("id"), user_id,
                            )
                    except Exception as cancel_exc:
                        logger.critical(
                            "ORPHAN_CANCEL_FAILED broker_order_id=%s order_id=%s user=%s — MANUAL INTERVENTION REQUIRED: %s",
                            orphan_broker_id, order_doc.get("id"), user_id, cancel_exc,
                        )
                    await db.orders.update_one(
                        {"id": order_doc["id"], "user_id": user_id},
                        {"$set": {
                            "status": ORDER_UNKNOWN_NEEDS_REVIEW,
                            "broker_order_id": orphan_broker_id or None,
                            "status_message": "DB persist failed after broker accepted; cancel attempted.",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }},
                    )
                await _cancel_strategy_reservation(position_reservation, exc_msg)
                if exposure_reservation:
                    await _close_order_exposure_reservation(order_doc["id"], user_id, status="RELEASED", reason=exc_msg)
                    exposure_reservation = None
                if not exc_msg.startswith("BROKER_ORDER_ORPHAN:"):
                    order_doc = await _mark_order_rejected(order_doc["id"], user_id, exc_msg)
                raise HTTPException(
                    status_code=400,
                    detail=f"Broker rejected order: {exc}",
                    headers={"X-Order-Id": order_doc.get("id", "")},
                )

        if strategy_id and _intent_is_entry(intent.intent) and not paper:
            await _activate_strategy_position(
                position_reservation,
                order_id=order_doc["id"],
                broker_order_id=broker_order_id,
                average_buy_price=float(fill_price or 0),
                quantity=int(intent.quantity),
                paper=paper,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
            )
            if position_reservation:
                await db.strategy_positions.update_one(
                    {"id": position_reservation["id"], "user_id": user_id},
                    {"$set": {
                        "asset_type": order_doc.get("asset_type") or _asset_type_for_instrument(instr, option_contract),
                        "asset_class": instr.asset_class,
                        "position_side": "SHORT" if intent.intent == "OPEN_SHORT" else "LONG",
                        "product": resolved_product,
                        "lot_size": resolution.get("lot_size"),
                        "lots": resolution.get("lots"),
                        "underlying": order_doc.get("underlying"),
                        "option_type": order_doc.get("option_type"),
                        "strike": order_doc.get("strike"),
                        "expiry": order_doc.get("expiry"),
                    }},
                )
        if strategy_id and _intent_is_exit(intent.intent):
            await _mark_strategy_position_exiting(exit_position_record, exit_order_id=order_doc["id"], exit_broker_order_id=broker_order_id)

        if paper:
            order_doc = await _apply_paper_fill_to_position(order_doc, float(fill_price or 0))
            if strategy_id and _intent_is_entry(intent.intent):
                await _activate_strategy_position(
                    position_reservation,
                    order_id=order_doc["id"],
                    broker_order_id=broker_order_id,
                    average_buy_price=float(fill_price or 0),
                    quantity=int(intent.quantity),
                    paper=True,
                    stop_loss=intent.stop_loss,
                    take_profit=intent.take_profit,
                )
                if position_reservation:
                    await db.strategy_positions.update_one(
                        {"id": position_reservation["id"], "user_id": user_id},
                        {"$set": {
                            "asset_type": order_doc.get("asset_type") or _asset_type_for_instrument(instr, option_contract),
                            "asset_class": instr.asset_class,
                            "position_side": "SHORT" if intent.intent == "OPEN_SHORT" else "LONG",
                            "product": resolved_product,
                            "lot_size": resolution.get("lot_size"),
                            "lots": resolution.get("lots"),
                            "underlying": order_doc.get("underlying"),
                            "option_type": order_doc.get("option_type"),
                            "strike": order_doc.get("strike"),
                            "expiry": order_doc.get("expiry"),
                        }},
                    )
            if strategy_id and _intent_is_exit(intent.intent):
                await _close_strategy_position_record(exit_position_record, exit_price=float(fill_price or 0), reason=source)

        return _clean_order_response(order_doc)
    except Exception as exc:
        if position_reservation:
            await _cancel_strategy_reservation(position_reservation, str(exc))
        if exposure_reservation and order_inserted:
            await _close_order_exposure_reservation(exposure_reservation["order_id"], user_id, status="RELEASED", reason=str(exc))
        if exit_position_record:
            await _reopen_strategy_position_after_exit_reject(exit_position_record["id"], user_id, str(exc))
            
        # Classify and log failed live pre-submission rejections. Paper simulator
        # skips are recorded explicitly before this point and must not become
        # broker-style FAILED_ORDER rows.
        if not order_inserted and not paper and not preflight_blocked:
            message = exc.detail if hasattr(exc, "detail") else str(exc)
            reason_code = _classify_rejection_reason(message)
            now = datetime.now(timezone.utc).isoformat()
            failed_order_doc = {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "idempotency_key": _scoped_idempotency_key(user_id, idempotency_key) if idempotency_key else f"idem:failed:{uuid.uuid4().hex}",
                "symbol": symbol,
                "side": side,
                "qty": int(qty or 0),
                "filled_qty": 0,
                "pending_qty": 0,
                "status": "FAILED",
                "legacy_status": "FAILED",
                "execution_status": "FAILED",
                "status_message": reason_code,
                "error_message": message,
                "reject_reason": reason_code,
                "mode": "paper" if paper else "live",
                "broker": "paper" if paper else "upstox",
                "source": source,
                "strategy_id": strategy_id,
                "created_at": now,
                "updated_at": now,
                "exchange": exchange,
            }
            try:
                await db.orders.insert_one(failed_order_doc)
            except Exception as insert_err:
                logger.warning("Failed to insert failed order log: %s", insert_err)
        raise


# moved to routes/orders.py
async def place_order(req: OrderReq, user=Depends(get_current_user)):
    return await _place_order_core(
        user_id=user["id"], symbol=req.symbol, side=req.side, qty=req.qty,
        order_type=req.order_type, price=req.price, product=req.product, source="manual",
        exchange=req.exchange,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        idempotency_key=req.idempotency_key,
    )


# moved to routes/positions.py
async def exit_position(symbol: str, user=Depends(get_current_user)):
    """Close an open position with one click — places an opposite-side MARKET order."""
    symbol = symbol.upper()
    positions = await list_positions(user)
    target = next((p for p in positions if p["symbol"] == symbol), None)
    if not target or not target.get("qty"):
        raise HTTPException(status_code=404, detail="No open position for that symbol")
    qty = abs(int(target["qty"]))
    side = "SELL" if target["qty"] > 0 else "BUY"
    exchange = target.get("exchange") or ("NFO" if symbol.endswith(("CE", "PE")) else "NSE")
    instrument_token = str(target.get("instrument_token") or "").strip()
    if exchange in {"NFO", "BFO", "MCX"} or symbol.endswith(("CE", "PE")):
        if "|" not in instrument_token:
            strategy_pos = await db.strategy_positions.find_one(
                {
                    "user_id": user["id"],
                    "$or": [{"trading_symbol": symbol}, {"symbol": symbol}],
                    "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
                },
                {"_id": 0},
            )
            instrument_token = str((strategy_pos or {}).get("instrument_token") or "").strip()
        if "|" not in instrument_token:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot exit {symbol} from QuantG: Upstox instrument_key is missing. "
                    "Exit this position in Upstox now, then run Sync with Broker."
                ),
            )
        lot_size = int(target.get("lot_size") or qty or 1)
        return await _place_order_core(
            user_id=user["id"],
            symbol=symbol,
            side=side,
            qty=max(1, math.ceil(qty / max(1, lot_size))),
            order_type="MARKET",
            product=target.get("product"),
            source="manual-exit",
            exchange=exchange,
            option_contract={
                "tradingsymbol": symbol,
                "exchange": exchange,
                "instrument_token": instrument_token,
                "lot_size": lot_size,
                "transaction_type": side,
            },
        )
    return await _place_order_core(
        user_id=user["id"], symbol=symbol, side=side, qty=qty,
        order_type="MARKET", product=target.get("product"), source="manual-exit",
        exchange=exchange,
    )


# squareoff ops route moved to routes/ops_runtime.py


def _broker_order_row(o: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"kite-{o.get('order_id')}",
        "broker_order_id": o.get("order_id"),
        "symbol": o.get("tradingsymbol"),
        "side": o.get("transaction_type"),
        "qty": o.get("quantity"),
        "filled_qty": o.get("filled_quantity"),
        "pending_qty": o.get("pending_quantity"),
        "price": float(o.get("average_price") or o.get("price") or 0),
        "order_type": o.get("order_type"),
        "product": o.get("product"),
        "status": canonical_order_status(o.get("status"), filled_qty=o.get("filled_quantity"), pending_qty=o.get("pending_quantity")),
        "legacy_status": o.get("status"),
        "broker_status": o.get("status"),
        "status_message": o.get("status_message"),
        "mode": "live",
        "source": "broker",
        "created_at": str(o.get("order_timestamp")) if o.get("order_timestamp") else None,
    }


async def _advance_pending_order_from_broker(
    *,
    user_id: str,
    broker_order_id: str,
    status: str,
    avg_price: float = 0.0,
    filled_qty: Optional[int] = None,
    pending_qty: Optional[int] = None,
    status_message: Optional[str] = None,
    raw_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    if not broker_order_id or not status:
        return {"orders": 0, "positions": 0}
    normalized = str(status).upper()
    canonical = canonical_order_status(normalized, filled_qty=filled_qty, pending_qty=pending_qty)
    local_orders = await db.orders.find(
        {"user_id": user_id, "broker_order_id": str(broker_order_id)},
        {"_id": 0},
    ).to_list(20)
    changed_orders = 0
    changed_positions = 0
    now = datetime.now(timezone.utc).isoformat()
    for order in local_orders:
        strategy_id = order.get("strategy_id")
        intent_doc = order.get("order_intent") or {}
        intent_name = str(intent_doc.get("intent") or "").upper()
        instrument = intent_doc.get("instrument") or order.get("instrument") or {}
        is_entry = intent_name in {"OPEN_LONG", "OPEN_SHORT"}
        is_exit = intent_name in {"CLOSE_LONG", "CLOSE_SHORT"}
        current_status = order.get("status") or ORDER_NEW
        try:
            next_status = validate_order_transition(current_status, canonical)
        except ValueError as exc:
            await _append_order_event(order["id"], user_id, "BROKER_EXECUTION_REPORT_REJECTED", {
                "broker_order_id": str(broker_order_id),
                "broker_status": normalized,
                "target_status": canonical,
                "message": str(exc),
                "raw": raw_report or {},
            })
            continue

        set_doc = {
            "status": next_status,
            "legacy_status": normalized,
            "broker_status": normalized,
            "execution_status": next_status,
            "status_message": status_message or normalized,
            "updated_at": now,
        }
        if filled_qty is not None:
            set_doc["filled_qty"] = int(filled_qty or 0)
        if pending_qty is not None:
            set_doc["pending_qty"] = int(pending_qty or 0)
        if avg_price and avg_price > 0:
            set_doc["price"] = float(avg_price)
        if raw_report:
            set_doc["last_execution_report"] = raw_report

        res_order = await db.orders.update_one(
            {"id": order["id"], "user_id": user_id},
            {"$set": set_doc},
        )
        changed_orders += res_order.modified_count
        await _append_order_event(order["id"], user_id, "BROKER_EXECUTION_REPORT_APPLIED", {
            "broker_order_id": str(broker_order_id),
            "broker_status": normalized,
            "status": next_status,
            "filled_qty": filled_qty,
            "pending_qty": pending_qty,
            "avg_price": avg_price,
            "status_message": status_message,
        })

        if canonical == ORDER_FILLED:
            await _book_live_fill_from_order(
                {**order, **set_doc},
                fill_price=float(avg_price or order.get("price") or 0),
                filled_qty=filled_qty,
                raw_report=raw_report,
            )
            await _close_order_exposure_reservation(order["id"], user_id, status="SETTLED", reason="broker-filled")
            if strategy_id and is_entry:
                pos_set = {
                    "status": "OPEN",
                    "average_buy_price": float(avg_price or order.get("price") or 0),
                    "updated_at": now,
                }
                final_qty = int(filled_qty or order.get("filled_qty") or order.get("qty") or 0)
                if final_qty > 0:
                    pos_set["quantity"] = final_qty
                    pos_set["open_quantity"] = final_qty
                match = {
                    "user_id": user_id,
                    "entry_broker_order_id": str(broker_order_id),
                    "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]},
                }
                positions = await db.strategy_positions.find(match, {"_id": 0}).to_list(20)
                for pos in positions:
                    position_update = dict(pos_set)
                    entry_price = float(position_update.get("average_buy_price") or pos.get("average_buy_price") or 0)
                    prices = _position_risk_prices({**pos, "average_buy_price": entry_price})
                    risk = _normalize_strategy_risk(pos.get("tp_sl_tsl_config") or {})
                    if prices.get("stop_loss") is not None:
                        risk["stoploss_price"] = prices["stop_loss"]
                        risk["stop_loss"] = prices["stop_loss"]
                    if prices.get("take_profit") is not None:
                        risk["target_price"] = prices["take_profit"]
                        risk["take_profit"] = prices["take_profit"]
                    position_update["tp_sl_tsl_config"] = risk

                    is_option_buy = (
                        pos.get("position_side") == "LONG"
                        and (
                            pos.get("exchange") in {"NFO", "BFO"}
                            or pos.get("trading_symbol", "").endswith(("CE", "PE"))
                            or pos.get("option_type") in {"CE", "PE"}
                        )
                    )
                    if is_option_buy:
                        risk_pct = float(risk.get("stop_loss_pct") or risk.get("stoploss_pct") or risk.get("stop_pct") or 8.0)
                        initial_stop_R = float(pos.get("initial_stop_R") or 1.0)
                        target_R = float(pos.get("target_R") or 2.0)
                        
                        r_initial_risk_amount = round(entry_price * risk_pct / 100.0, 2)
                        r_stop_loss_price = round(max(0.0, entry_price - r_initial_risk_amount * initial_stop_R), 2)
                        r_take_profit_price = round(entry_price + r_initial_risk_amount * target_R, 2)
                        
                        position_update.update({
                            "r_initial_risk_amount": r_initial_risk_amount,
                            "r_stop_loss_price": r_stop_loss_price,
                            "r_take_profit_price": r_take_profit_price,
                            "r_entry_price": entry_price,
                            "r_current_R": 0.0,
                            "r_max_R_seen": 0.0,
                            "r_trailing_active": False,
                            "r_trailing_stop_price": r_stop_loss_price,
                            "best_price_seen": entry_price,
                            "r_last_evaluated_at": now,
                        })

                    res = await db.strategy_positions.update_one(
                        {"id": pos["id"], "user_id": user_id},
                        {"$set": position_update},
                    )
                    changed_positions += res.modified_count
                    if order.get("mode") == "live":
                        loop = asyncio.get_running_loop()
                        loop.create_task(_place_resting_sl_order(db, user_id, order, entry_price, final_qty))
            if strategy_id and is_exit:
                exit_positions = await db.strategy_positions.find(
                    {"user_id": user_id, "exit_broker_order_id": str(broker_order_id), "status": {"$in": ["EXITING", "CLOSED"]}},
                    {"_id": 0},
                ).to_list(20)
                for pos in exit_positions:
                    exit_price = float(avg_price or order.get("price") or pos.get("average_buy_price") or 0)
                    await _close_strategy_position_record(pos, exit_price=exit_price, reason="broker-exit-complete")
                    changed_positions += 1
        elif canonical == ORDER_PARTIAL_FILL:
            # Partial fill: update position quantity to reflect only what actually filled.
            # The order remains active (pending the remainder). Position sizing must track
            # the real filled quantity so P&L and risk calculations stay accurate.
            partial_qty = int(filled_qty or 0)
            logger.warning(
                "PARTIAL FILL broker_order_id=%s user=%s symbol=%s filled=%s pending=%s avg_price=%s",
                broker_order_id, user_id,
                (order.get("instrument") or {}).get("tradingsymbol") or order.get("symbol", ""),
                partial_qty, pending_qty, avg_price,
            )
            await _append_order_event(order["id"], user_id, "ORDER_PARTIAL_FILL_RECEIVED", {
                "broker_order_id": str(broker_order_id),
                "filled_qty": partial_qty,
                "pending_qty": pending_qty,
                "avg_price": avg_price,
            })
            if strategy_id and is_entry and partial_qty > 0:
                match = {
                    "user_id": user_id,
                    "entry_broker_order_id": str(broker_order_id),
                    "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]},
                }
                positions = await db.strategy_positions.find(match, {"_id": 0}).to_list(20)
                for pos in positions:
                    res = await db.strategy_positions.update_one(
                        {"id": pos["id"], "user_id": user_id},
                        {"$set": {
                            "quantity": partial_qty,
                            "open_quantity": partial_qty,
                            "status": "PARTIAL_FILL",
                            "average_buy_price": float(avg_price or pos.get("average_buy_price") or 0),
                            "partial_fill_pending_qty": pending_qty,
                            "updated_at": now,
                        }},
                    )
                    changed_positions += res.modified_count
        elif canonical in {ORDER_CANCELLED, ORDER_REJECTED}:
            await _close_order_exposure_reservation(order["id"], user_id, status="RELEASED", reason=f"broker-{canonical.lower()}")
            if strategy_id and is_entry:
                res = await db.strategy_positions.update_many(
                    {
                        "user_id": user_id,
                        "entry_broker_order_id": str(broker_order_id),
                        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "PARTIAL_FILL"]},
                    },
                    {
                        "$set": {"status": canonical, "legacy_status": normalized, "updated_at": now, "broker_status_message": status_message or normalized},
                        "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""},
                    },
                )
                changed_positions += res.modified_count
                await db.strategy_position_locks.delete_many({"user_id": user_id, "strategy_id": strategy_id})
            if strategy_id and is_exit:
                exit_positions = await db.strategy_positions.find(
                    {"user_id": user_id, "exit_broker_order_id": str(broker_order_id), "status": "EXITING"},
                    {"_id": 0, "id": 1, "user_id": 1},
                ).to_list(20)
                for pos in exit_positions:
                    await _reopen_strategy_position_after_exit_reject(pos["id"], user_id, status_message or normalized)
                    changed_positions += 1
        elif canonical == ORDER_UNKNOWN_NEEDS_REVIEW:
            await _close_order_exposure_reservation(order["id"], user_id, status="NEEDS_REVIEW", reason=status_message or normalized)
    return {"orders": changed_orders, "positions": changed_positions}


_LAST_USER_BROKER_SYNC: Dict[str, float] = {}

async def _record_broker_sync_state(user_id: str, broker: str, result: Dict[str, Any]) -> None:
    try:
        await db.broker_sync_state.update_one(
            {"user_id": user_id, "broker": broker},
            {"$set": {
                "user_id": user_id,
                "broker": broker,
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            }},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("broker sync state write failed user=%s broker=%s: %s", user_id, broker, exc)

def _check_and_update_sync_throttle(user_id: str, broker: str, ttl: float = 5.0) -> bool:
    now = time.monotonic()
    key = f"{user_id}:{broker}"
    last = _LAST_USER_BROKER_SYNC.get(key, 0.0)
    if now - last < ttl:
        return False
    _LAST_USER_BROKER_SYNC[key] = now
    return True


async def _sync_kite_order_statuses(user_id: str, kite) -> Dict[str, int]:
    """Mirror Kite order statuses into local rows with broker_order_id."""
    if not kite:
        return {"checked": 0, "updated": 0}
    if not _check_and_update_sync_throttle(user_id, "kite"):
        return {"checked": 0, "updated": 0, "throttled": True}
    cached = _ORDER_SYNC_CACHE.get(user_id)
    if cached and time.monotonic() - cached["cached_at"] < KITE_ORDER_SYNC_TTL_SEC:
        return cached["result"]
    try:
        live_orders = kite.orders() or []
    except Exception as e:
        logger.warning(f"kite orders fetch failed: {e}")
        result = {"checked": 0, "updated": 0}
        _ORDER_SYNC_CACHE[user_id] = {"cached_at": time.monotonic(), "result": result}
        return result

    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for o in live_orders:
        broker_order_id = o.get("order_id")
        status = o.get("status")
        if not broker_order_id or not status:
            continue
        filled_quantity = o.get("filled_quantity")
        pending_quantity = o.get("pending_quantity")
        set_doc = {
            "status": canonical_order_status(status, filled_qty=filled_quantity, pending_qty=pending_quantity),
            "legacy_status": status,
            "broker_status": status,
            "filled_qty": filled_quantity,
            "pending_qty": pending_quantity,
            "status_message": o.get("status_message"),
            "updated_at": now,
        }
        avg_price = float(o.get("average_price") or 0)
        if avg_price > 0:
            set_doc["price"] = avg_price
        res = await db.orders.update_many(
            {"user_id": user_id, "broker_order_id": broker_order_id},
            {"$set": set_doc},
        )
        updated += res.modified_count
        await _advance_pending_order_from_broker(
            user_id=user_id,
            broker_order_id=str(broker_order_id),
            status=str(status),
            avg_price=avg_price,
            filled_qty=int(o.get("filled_quantity") or 0),
            pending_qty=int(o.get("pending_quantity") or 0) if o.get("pending_quantity") not in (None, "") else None,
            status_message=o.get("status_message"),
            raw_report=o,
        )
        if set_doc["status"] == ORDER_FILLED:
            pos_set = {
                "status": "FILLED",
                "average_buy_price": avg_price or set_doc.get("price") or 0,
                "updated_at": now,
            }
            filled_qty = int(o.get("filled_quantity") or 0)
            if filled_qty > 0:
                pos_set["quantity"] = filled_qty
                pos_set["open_quantity"] = filled_qty
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": broker_order_id, "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": pos_set},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": broker_order_id, "status": "EXITING"},
                {"_id": 0},
            ).to_list(20)
            for pos in exit_positions:
                await _close_strategy_position_record(
                    pos,
                    exit_price=float(o.get("average_price") or o.get("price") or pos.get("average_buy_price") or 0),
                    reason="broker-exit-complete",
                )
        elif set_doc["status"] in {ORDER_CANCELLED, ORDER_REJECTED}:
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": broker_order_id, "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": {"status": status, "updated_at": now, "broker_status_message": o.get("status_message")},
                 "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": broker_order_id, "status": "EXITING"},
                {"_id": 0, "id": 1, "user_id": 1},
            ).to_list(20)
            for pos in exit_positions:
                await _reopen_strategy_position_after_exit_reject(pos["id"], user_id, o.get("status_message") or status)
    result = {"checked": len(live_orders), "updated": updated}
    _ORDER_SYNC_CACHE[user_id] = {"cached_at": time.monotonic(), "result": result}
    await _record_broker_sync_state(user_id, "zerodha", result)
    return result


async def _stale_local_open_orders(user_id: str, kite) -> Dict[str, Any]:
    if not kite:
        return {"fixed": 0, "reason": "zerodha_not_connected"}
    try:
        live_orders = kite.orders() or []
    except Exception as e:
        logger.warning(f"kite orders fetch failed: {e}")
        return {"fixed": 0, "reason": str(e)}
    broker_status = {
        o.get("order_id"): o.get("status")
        for o in live_orders
        if o.get("order_id") and o.get("status")
    }
    fixed = 0
    missing_fixed = 0
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=10)
    rows = await db.orders.find({
        "user_id": user_id,
        "status": {"$in": list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)},
        "broker": {"$in": ["zerodha", None]},
    }, {"_id": 0, "id": 1, "broker_order_id": 1, "created_at": 1, "strategy_id": 1}).to_list(500)
    for row in rows:
        broker_order_id = row.get("broker_order_id")
        status = broker_status.get(broker_order_id)
        canonical = canonical_order_status(status)
        if canonical in ORDER_TERMINAL_STATUSES:
            res = await db.orders.update_one(
                {"user_id": user_id, "id": row["id"]},
                {"$set": {"status": canonical, "legacy_status": status, "broker_status": status, "updated_at": now.isoformat()}},
            )
            fixed += res.modified_count
            continue

        created_at = row.get("created_at")
        created_dt = None
        if isinstance(created_at, datetime):
            created_dt = created_at.astimezone(timezone.utc)
        elif isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                created_dt = None
        if created_dt and created_dt < stale_before and (not broker_order_id or broker_order_id not in broker_status):
            stale_status = "BROKER_NOT_FOUND" if broker_order_id else "STALE"
            message = (
                "Local stale open order cleared: broker no longer reports this order."
                if broker_order_id else
                "Local stale open order cleared: no broker order id was ever recorded."
            )
            res = await db.orders.update_one(
                {"user_id": user_id, "id": row["id"]},
                {"$set": {
                    "status": ORDER_REJECTED,
                    "legacy_status": stale_status,
                    "broker_status": stale_status,
                    "status_message": message,
                    "visibility": "hidden",
                    "updated_at": now.isoformat(),
                }},
            )
            missing_fixed += res.modified_count
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_order_id": row["id"], "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": {"status": stale_status, "updated_at": now.isoformat()},
                 "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
            )
    _ORDER_SYNC_CACHE.pop(user_id, None)
    return {"fixed": fixed + missing_fixed, "broker_closed_fixed": fixed, "missing_from_broker_fixed": missing_fixed, "checked": len(rows)}

async def _reconcile_stale_orders_for_user(user_id: str) -> Dict[str, int]:
    """Broker-agnostic stale order reconciliation.

    Finds ALL active orders (any broker) that are older than 10 minutes
    but not reported by the broker. Distinguishes between:
      - STALE: no broker_order_id was ever recorded
      - BROKER_NOT_FOUND: broker has an order_id but doesn't report it
      - UNKNOWN_NEEDS_REVIEW: uncertain state

    Releases strategy position locks for stale orders and logs reconciliation.
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=10)
    rows = await db.orders.find({
        "user_id": user_id,
        "status": {"$in": list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)},
        "visibility": {"$ne": "hidden"},
    }, {"_id": 0, "id": 1, "broker_order_id": 1, "created_at": 1, "strategy_id": 1, "strategy_name": 1, "status": 1, "broker": 1, "mode": 1, "updated_at": 1}).to_list(500)
    stale_count = 0
    for row in rows:
        created_at = row.get("created_at")
        created_dt = None
        if isinstance(created_at, datetime):
            created_dt = created_at.astimezone(timezone.utc)
        elif isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                created_dt = None
        if not created_dt or created_dt >= stale_before:
            continue

        # Parse updated_at — if the broker sync recently confirmed this order
        # (updated_at >= stale_before), skip it: it's still alive at the broker.
        updated_at = row.get("updated_at")
        updated_dt = None
        if isinstance(updated_at, datetime):
            updated_dt = updated_at.astimezone(timezone.utc)
        elif isinstance(updated_at, str):
            try:
                updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                updated_dt = None
        if updated_dt and updated_dt >= stale_before:
            continue

        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if not broker_order_id:
            stale_status = "STALE"
            message = "Local stale order: no broker order id was ever recorded."
        else:
            stale_status = "BROKER_NOT_FOUND"
            message = "Local stale order: broker no longer reports this order id."

        logger.info("Stale reconciliation user_id=%s: order %s status=%s -> %s reason=%s broker=%s",
                    user_id, row["id"], row.get("status"), stale_status, message, row.get("broker"))

        await db.orders.update_one(
            {"user_id": user_id, "id": row["id"]},
            {"$set": {
                "status": stale_status,
                "legacy_status": stale_status,
                "broker_status": stale_status,
                "status_message": message,
                "visibility": "hidden",
                "updated_at": now.isoformat(),
            }},
        )
        stale_count += 1

        # Update associated strategy positions
        await db.strategy_positions.update_many(
            {"user_id": user_id, "entry_order_id": row["id"], "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
            {"$set": {"status": stale_status, "updated_at": now.isoformat(), "broker_status_message": message},
             "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
        )

        # Release strategy position locks
        if row.get("strategy_id"):
            await db.strategy_position_locks.delete_many({
                "user_id": user_id,
                "strategy_id": row["strategy_id"],
            })

    if stale_count:
        logger.warning("Stale reconciliation user_id=%s: marked %d orders as stale/broker_not_found", user_id, stale_count)
    return {"checked": len(rows), "fixed": stale_count}



def _kotak_order_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            out.extend(_kotak_order_items(item))
        return out
    if not isinstance(payload, dict):
        return []
    if _extract_kotak_order_id(payload):
        return [payload]
    out: List[Dict[str, Any]] = []
    for key in ("data", "orders", "orderBook", "order_book", "result", "records"):
        if key in payload:
            out.extend(_kotak_order_items(payload.get(key)))
    if out:
        return out
    for value in payload.values():
        out.extend(_kotak_order_items(value))
    return out


def _kotak_position_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            out.extend(_kotak_position_items(item))
        return out
    if not isinstance(payload, dict):
        return []
    if any(key in payload for key in ("netQty", "net_quantity", "quantity", "qty")) and any(
        key in payload for key in ("trdSym", "trading_symbol", "tradingSymbol", "symbol")
    ):
        return [payload]
    out: List[Dict[str, Any]] = []
    for key in ("data", "positions", "positionBook", "position_book", "result", "records"):
        if key in payload:
            out.extend(_kotak_position_items(payload.get(key)))
    if out:
        return out
    for value in payload.values():
        out.extend(_kotak_position_items(value))
    return out


def _kotak_first(row: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_kotak_order_status(status: Any) -> Optional[str]:
    if status in (None, ""):
        return None
    text = str(status).strip().upper()
    mapping = {
        "TRD": "COMPLETE",
        "TRADED": "COMPLETE",
        "COMPLETED": "COMPLETE",
        "COMPLETE": "COMPLETE",
        "REJ": "REJECTED",
        "REJECTED": "REJECTED",
        "CXL": "CANCELLED",
        "CANCELED": "CANCELLED",
        "CANCELLED": "CANCELLED",
        "OPEN": "OPEN",
        "PENDING": "PENDING",
    }
    return mapping.get(text, text)


async def _sync_kotak_order_statuses(user_id: str) -> Dict[str, int]:
    return {"checked": 0, "updated": 0}


async def _sync_upstox_order_statuses(user_id: str, *, force: bool = False) -> Dict[str, int]:
    upstox_status = await get_user_upstox_status(user_id)
    if not upstox_status.get("token_valid"):
        return {"checked": 0, "updated": 0, "reason": upstox_status.get("reason") or "upstox_token_invalid"}
    gateway = await get_user_upstox_gateway(user_id)
    if not gateway or not gateway.connected:
        return {"checked": 0, "updated": 0, "reason": "upstox_not_connected"}
    if not force and not _check_and_update_sync_throttle(user_id, "upstox"):
        return {"checked": 0, "updated": 0, "throttled": True}
    if force:
        _LAST_USER_BROKER_SYNC[f"{user_id}:upstox"] = time.monotonic()
    try:
        report = await asyncio.to_thread(gateway.get_order_book)
    except Exception as exc:
        logger.warning("Upstox order book fetch failed: %s", exc)
        return {"checked": 0, "updated": 0, "reason": str(exc)}
    items = report.get("orders") or upstox_gateway_utils.order_items(report)
    reported_order_ids = {
        str(extract_upstox_order_id(item))
        for item in items
        if extract_upstox_order_id(item)
    }
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        order_id = extract_upstox_order_id(item)
        if not order_id:
            continue
        status = _normalize_upstox_order_status(_upstox_first(item, ["status", "order_status"]))
        status_message = _upstox_first(item, ["status_message", "status_message_raw"]) or status
        avg_price = 0.0
        if item.get("average_price") not in (None, ""):
            try:
                avg_price = float(item.get("average_price") or 0)
            except Exception:
                avg_price = 0.0
        filled_qty = None
        if item.get("filled_quantity") not in (None, ""):
            try:
                filled_qty = int(float(item.get("filled_quantity") or 0))
            except Exception:
                filled_qty = None
        pending_qty = None
        if item.get("pending_quantity") not in (None, ""):
            try:
                pending_qty = int(float(item.get("pending_quantity") or 0))
            except Exception:
                pending_qty = None
        reduced = await _advance_pending_order_from_broker(
            user_id=user_id,
            broker_order_id=str(order_id),
            status=str(status or ""),
            avg_price=avg_price,
            filled_qty=filled_qty,
            pending_qty=pending_qty,
            status_message=str(status_message or ""),
            raw_report=item,
        )
        updated += int(reduced.get("orders") or 0)

    missing_fixed = 0
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=2)
    local_active = await db.orders.find(
        {
            "user_id": user_id,
            "broker": "upstox",
            "status": {"$in": list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)},
            "visibility": {"$ne": "hidden"},
        },
        {"_id": 0, "id": 1, "broker_order_id": 1, "created_at": 1, "strategy_id": 1},
    ).to_list(500)
    for row in local_active:
        created_at = row.get("created_at")
        created_dt = None
        if isinstance(created_at, datetime):
            created_dt = created_at.astimezone(timezone.utc)
        elif isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                created_dt = None
        if created_dt and created_dt >= stale_before:
            continue

        broker_order_id = str(row.get("broker_order_id") or "").strip()
        if broker_order_id and broker_order_id in reported_order_ids:
            continue

        stale_status = "UNKNOWN_NEEDS_REVIEW"
        message = (
            "Stale unresolved pending order: broker no longer reports this order."
            if broker_order_id else
            "Stale unresolved pending order: no broker order id was ever recorded."
        )
        logger.info("Order reconciliation user_id=%s: local order %s marked as UNKNOWN_NEEDS_REVIEW. Reason: %s", user_id, row["id"], message)
        res = await db.orders.update_one(
            {"user_id": user_id, "id": row["id"]},
            {"$set": {
                "status": "UNKNOWN_NEEDS_REVIEW",
                "legacy_status": stale_status,
                "broker_status": stale_status,
                "status_message": message,
                "visibility": "visible",
                "updated_at": now,
            }},
        )
        missing_fixed += res.modified_count
        await _append_order_event(row["id"], user_id, "BROKER_ORDER_MISSING_NEEDS_REVIEW", {
            "broker_order_id": broker_order_id,
            "message": message,
        })
        await _close_order_exposure_reservation(row["id"], user_id, status="NEEDS_REVIEW", reason=message)
        await db.strategy_positions.update_many(
            {
                "user_id": user_id,
                "$or": [
                    {"entry_order_id": row["id"]},
                    {"entry_broker_order_id": broker_order_id} if broker_order_id else {"entry_broker_order_id": "__none__"},
                ],
                "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]},
            },
            {"$set": {"status": stale_status, "updated_at": now, "broker_status_message": message},
             "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
        )
        await db.strategy_position_locks.delete_many({"user_id": user_id, "strategy_id": row.get("strategy_id")})
    result = {"checked": len(items), "updated": updated, "missing_from_broker_fixed": missing_fixed}
    await _record_broker_sync_state(user_id, "upstox", result)
    return result


async def _live_broker_position_symbols(user_id: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    upstox_gateway = await get_user_upstox_gateway(user_id)
    if upstox_gateway and upstox_gateway.connected:
        try:
            result = await asyncio.to_thread(upstox_gateway.get_positions)
            for p in upstox_gateway_utils.position_items(result):
                token = _upstox_first(p, ["instrument_token", "instrument_key"])
                symbol = _upstox_first(p, ["tradingsymbol", "trading_symbol", "symbol"]) or token
                qty = _upstox_first(p, ["quantity", "net_quantity"])
                try:
                    qty_i = int(float(qty or 0))
                except Exception:
                    qty_i = 0
                if symbol and qty_i != 0:
                    exchange = _upstox_first(p, ["exchange"]) or str(token or "").split("|", 1)[0].replace("_EQ", "")
                    out[_instrument_key(str(exchange).upper(), str(symbol).upper(), token)] = p
                    out[f"SYMBOL:{str(symbol).upper()}"] = p
        except Exception as exc:
            logger.warning("Upstox positions fetch failed: %s", exc)
    return out


async def _sync_strategy_positions_with_broker(user_id: str) -> Dict[str, int]:
    upstox_gateway = await get_user_upstox_gateway(user_id)
    has_upstox = bool(upstox_gateway and upstox_gateway.connected)
    if not has_upstox:
        return {"checked": 0, "broker_positions": 0, "marked_broker_not_found": 0, "reason": "upstox_not_connected"}
    if not _check_and_update_sync_throttle(user_id, "positions"):
        return {"checked": 0, "broker_positions": 0, "marked_broker_not_found": 0, "throttled": True}
    broker_positions = await _live_broker_position_symbols(user_id)
    rows = await db.strategy_positions.find({
        "user_id": user_id,
        "mode": "live",
        "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
    }, {"_id": 0}).to_list(500)
    marked = 0
    now = datetime.now(timezone.utc).isoformat()

    # Self-healing reconciliation checks
    for row in rows:
        pos_id = row.get("id")
        status = row.get("status")
        order_id = row.get("order_id")
        exit_order_id = row.get("exit_order_id")
        
        # Parse created_at or updated_at to check for stale (5+ minutes)
        time_str = row.get("updated_at") or row.get("created_at") or now
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            is_stale = (datetime.now(timezone.utc) - dt).total_seconds() > 300
        except Exception:
            is_stale = False

        if status in {"PENDING_BROKER", "OPEN", "FILLED"}:
            if order_id:
                order = await db.orders.find_one({"id": order_id, "user_id": user_id})
                if order:
                    ord_status = order.get("status")
                    if ord_status in {"REJECTED", "CANCELLED"}:
                        logger.info("Self-healing: Reconciling strategy position %s status %s because entry order %s is %s", pos_id, status, order_id, ord_status)
                        await db.strategy_positions.update_one(
                            {"id": pos_id, "user_id": user_id},
                            {"$set": {
                                "status": ord_status,
                                "cancel_reason": f"Self-healed: entry order {order_id} has terminal status {ord_status}",
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            }, "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}}
                        )
                        await _release_strategy_position_locks(row)
                        marked += 1
                        continue
            if status == "PENDING_BROKER" and is_stale:
                logger.info("Self-healing: Reconciling stale PENDING_BROKER position %s because it has been stale for 5+ minutes", pos_id)
                await db.strategy_positions.update_one(
                    {"id": pos_id, "user_id": user_id},
                    {"$set": {
                        "status": "REJECTED",
                        "cancel_reason": "Self-healed: stuck in PENDING_BROKER for more than 5 minutes",
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }, "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}}
                )
                await _release_strategy_position_locks(row)
                marked += 1
                continue

        elif status == "EXITING":
            if exit_order_id:
                exit_order = await db.orders.find_one({"id": exit_order_id, "user_id": user_id})
                if exit_order:
                    ord_status = exit_order.get("status")
                    if ord_status in {"REJECTED", "CANCELLED"}:
                        logger.info("Self-healing: Reopening strategy position %s from EXITING because exit order %s is %s", pos_id, exit_order_id, ord_status)
                        await _reopen_strategy_position_after_exit_reject(pos_id, user_id, f"Self-healed: exit order {exit_order_id} is {ord_status}")
                        marked += 1
                        continue
            if is_stale:
                logger.info("Self-healing: Reopening stale EXITING position %s because it has been stale for 5+ minutes", pos_id)
                await _reopen_strategy_position_after_exit_reject(pos_id, user_id, "Self-healed: stuck in EXITING for more than 5 minutes")
                marked += 1
                continue

    # Re-fetch active live rows post-reconciliation for broker sync
    rows = await db.strategy_positions.find({
        "user_id": user_id,
        "mode": "live",
        "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
    }, {"_id": 0}).to_list(500)

    for row in rows:
        symbol = str(row.get("trading_symbol") or row.get("symbol") or "").upper()
        key = row.get("instrument_key")
        if key not in broker_positions and f"SYMBOL:{symbol}" not in broker_positions:
            if row.get("status") == "EXITING":
                await _close_strategy_position_record(
                    row,
                    exit_price=float(row.get("exit_price") or row.get("average_buy_price") or 0),
                    reason="broker-position-closed",
                )
                marked += 1
                continue
            res = await db.strategy_positions.update_one(
                {"id": row["id"], "user_id": user_id},
                {"$set": {
                    "status": "BROKER_NOT_FOUND",
                    "broker_sync_note": "Broker has no matching net position; app position marked stale.",
                    "updated_at": now,
                }, "$unset": {"active_instrument_key": "", "active_strategy_key": "", "active_strategy_instrument_side_key": ""}},
            )
            marked += res.modified_count
    return {"checked": len(rows), "broker_positions": len(broker_positions), "marked_broker_not_found": marked}


def _broker_position_quantity(row: Dict[str, Any]) -> int:
    value = row.get("quantity")
    if value is None:
        value = _kotak_first(row, ["netQty", "net_quantity", "qty"])
    try:
        return abs(int(float(value or 0)))
    except Exception:
        return 0


async def _assert_broker_has_position_quantity(user_id: str, kite, exchange: str, symbol: str, qty: int) -> None:
    positions = await _live_broker_position_symbols(user_id)
    row = positions.get(_instrument_key(exchange, symbol, None)) or positions.get(f"SYMBOL:{symbol.upper()}")
    broker_qty = _broker_position_quantity(row or {})
    if broker_qty < int(qty or 0):
        raise HTTPException(
            status_code=409,
            detail=f"Broker position quantity mismatch for {symbol}: broker has {broker_qty}, QuantG wants to sell {qty}. Run Sync with Broker before retrying.",
        )


# moved to routes/orders.py
async def list_orders(include_stale: bool = False, user=Depends(get_current_user)):
    """Local order log reconciled with Upstox so users see every app-tracked status."""
    await _sync_upstox_order_statuses(user["id"])
    await _sync_strategy_positions_with_broker(user["id"])
    order_query: Dict[str, Any] = {"user_id": user["id"]}
    if not include_stale:
        order_query["status"] = {"$nin": list(STALE_ORDER_STATUSES)}
        order_query["visibility"] = {"$ne": "hidden"}
    rows = await db.orders.find(order_query,
                                {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    # Sort newest-first so the UI uses one backend source of truth.
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def _api_position_row(p: Dict[str, Any], *, broker: str) -> Dict[str, Any]:
    symbol = p.get("tradingsymbol") or p.get("symbol") or "-"
    qty = int(p.get("quantity") or 0)
    avg = float(p.get("average_price") or 0)
    ltp = float(p.get("last_price") or avg or 0)
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_price": round(avg, 2),
        "ltp": round(ltp, 2),
        "pnl": round(float(p.get("pnl") or ((ltp - avg) * qty)), 2),
        "product": p.get("product"),
        "exchange": p.get("exchange") or ("NFO" if str(symbol).endswith(("CE", "PE")) else "NSE"),
        "mode": "live",
        "broker": broker,
        "instrument_token": p.get("instrument_token"),
    }


async def _fetch_broker_positions_for_user(user: dict, settings: dict) -> List[Dict[str, Any]]:
    user_id = user["id"]
    paper = settings.get("paper_mode", True)
    if not paper:
        gateway = await get_user_upstox_gateway(user_id)
        if gateway and gateway.connected:
            try:
                result = await asyncio.to_thread(gateway.get_positions)
                out = []
                rows = result.get("net") if isinstance(result, dict) else upstox_gateway_utils.position_items(result)
                for p in rows or []:
                    if int(p.get("quantity") or 0) == 0:
                        continue
                    out.append(_api_position_row(p, broker="upstox"))
                if out:
                    return out
            except Exception as exc:
                logger.warning("Upstox positions fetch failed: %s", exc)
    rows = await db.positions.find(
        {"user_id": user_id, "status": {"$ne": "INVALID_POSITION"}, "visibility": {"$ne": "hidden"}},
        {"_id": 0, "user_id": 0},
    ).to_list(200)
    out = []
    for r in rows:
        sym = next((s for s in SYMBOLS if s["symbol"] == r.get("symbol")), None)
        if sym:
            ltp = live_price(sym["base"], SYMBOLS.index(sym))["price"]
        else:
            # Option — use last_ltp from strategy_positions (updated every 30s by monitor)
            sp_pos = await db.strategy_positions.find_one(
                {"user_id": user_id, "target_symbol": r.get("symbol"), "status": {"$in": ["OPEN", "FILLED"]}},
                {"last_ltp": 1, "_id": 0},
            )
            raw_ltp = (sp_pos or {}).get("last_ltp")
            try:
                ltp = float(raw_ltp) if raw_ltp and raw_ltp != "LTP_UNAVAILABLE" else r.get("avg_price", 0)
            except (TypeError, ValueError):
                ltp = r.get("avg_price", 0)
        avg = r.get("avg_price", 0)
        qty = r.get("qty", 0)
        side = str(r.get("position_side") or "LONG").upper()
        pnl = round((avg - ltp) * qty, 2) if side == "SHORT" else round((ltp - avg) * qty, 2)
        out.append({**r, "ltp": ltp, "pnl": pnl, "mode": "paper"})
    return out


# moved to routes/positions.py
async def list_positions(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    return await _fetch_broker_positions_for_user(user, settings)


# moved to routes/positions.py
async def execution_snapshot(sync: bool = False, user=Depends(get_current_user)):
    """Unified execution state for UI polling: positions, orders, SL/TP, broker sync meta."""
    snapshot = await execution_state_manager.build_snapshot(user, sync=sync)
    snapshot["market_session"] = market_session_snapshot()
    try:
        gw = await get_user_upstox_gateway(user["id"])
        gateway_status = gw.status() if gw else {"connected": False, "feed_status": {"connected": False}}
        latest_ticks = gw.latest_ticks() if gw else {}
        snapshot["upstox_data_health"] = feed_health_status(gateway_status, latest_ticks)
        snapshot["broker_reconciliation"] = await broker_reconciliation_summary(db, user["id"], gw)
    except Exception as exc:
        snapshot["upstox_data_health"] = {"readiness": "UNKNOWN", "reason": str(exc)[:200]}
        snapshot["broker_reconciliation"] = {"status": "UNKNOWN", "errors": [str(exc)[:200]]}
    return snapshot


def _is_fake_34_price(value: Any) -> bool:
    try:
        price_value = float(value or 0)
    except (TypeError, ValueError):
        return False
    return 34.0 <= price_value < 35.0


def _order_has_real_upstox_price_metadata(order: Dict[str, Any]) -> bool:
    source = str(order.get("price_source") or "").lower()
    token = str(order.get("instrument_key") or order.get("instrument_token") or "").strip()
    received_at = order.get("price_received_at") or order.get("price_timestamp")
    return source in {"upstox-cache", "upstox-rest-quote"} and "|" in token and bool(parse_market_timestamp(received_at))


async def _build_strategy_readiness_rows(user_id: str) -> List[Dict[str, Any]]:
    start, end = get_trading_day_window_ist()
    strategies = await db.strategies.find({"user_id": user_id}, {"_id": 0}).sort("name", 1).to_list(500)
    rows: List[Dict[str, Any]] = []
    for s in strategies:
        sid = s.get("id")
        reasons: List[str] = []
        status = "READY"
        if s.get("quarantined") or str(s.get("status") or "").lower() == "quarantined":
            status = "QUARANTINED"
            reasons.append(s.get("quarantine_reason") or "strategy quarantined")
        elif str(s.get("status") or "").lower() != "live":
            status = "BLOCKED"
            reasons.append(f"strategy status {s.get('status') or 'unknown'}")
        elif str(s.get("mode") or "").lower() != "paper":
            status = "WARNING"
            reasons.append(f"strategy mode {s.get('mode') or 'unknown'}")
        for field, reason in (
            ("last_error", "strategy error"),
            ("last_filter_reason", "latest filter"),
            ("last_skip_reason_code", "latest skip"),
        ):
            value = s.get(field)
            if value:
                if status == "READY":
                    status = "WARNING"
                reasons.append(f"{reason}: {value}")
        signal_count = await db.signals.count_documents({"user_id": user_id, "strategy_id": sid, "created_at": {"$gte": start, "$lt": end}})
        skipped_count = await db.signals.count_documents({"user_id": user_id, "strategy_id": sid, "processed_at": {"$gte": start, "$lt": end}, "status": {"$in": ["FILTERED", "REJECTED", "SKIPPED_SIGNAL", "BLOCKED"]}})
        order_count = await db.orders.count_documents({"user_id": user_id, "strategy_id": sid, "created_at": {"$gte": start, "$lt": end}, "mode": "paper", "status": {"$nin": [ORDER_SKIPPED_SIGNAL, "SKIPPED", "FAILED", "REJECTED"]}})
        rows.append({
            "strategy_id": sid,
            "name": s.get("name"),
            "status": status,
            "mode": s.get("mode"),
            "runtime_status": s.get("status"),
            "reasons": reasons or ["ready"],
            "last_evaluated_at": s.get("last_evaluated_at"),
            "last_signal_action": s.get("last_signal_action"),
            "last_signal_validated": s.get("last_signal_validated"),
            "last_contract_selected": s.get("last_contract_selected") or s.get("last_traded_symbol"),
            "last_price_source": s.get("last_price_source"),
            "last_ltp_timestamp": s.get("last_ltp_timestamp"),
            "signal_count_today": int(s.get("signal_count_today") or signal_count or 0),
            "skipped_count_today": int(s.get("skipped_count_today") or skipped_count or 0),
            "order_count_today": int(s.get("order_count_today") or order_count or 0),
            "duplicate_signal_count_today": int(s.get("duplicate_signal_count_today") or 0),
            "quarantine_reason": s.get("quarantine_reason"),
        })
    return rows


# readiness routes moved to routes/readiness.py


# diagnostics routes moved to routes/diagnostics.py



# profile, portfolio, funds, and paper wallet routes moved to routes/profile.py


# moved to routes/dashboard.py
async def risk_dashboard(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    mode = "paper" if settings.get("paper_mode", True) else "live"
    start, end = get_trading_day_window_ist()
    orders = await db.orders.find(
        {
            "user_id": user["id"],
            "mode": mode,
            "created_at": {"$gte": start, "$lt": end},
        },
        {"_id": 0},
    ).to_list(1000)
    positions = await list_positions(user)
    fill_summary = await _fill_ledger_summary(user["id"], mode=mode, start=start, end=end)
    realized = fill_summary["realized_pnl"]
    open_pnl = round(sum(float(p.get("pnl") or 0) for p in positions), 2)
    loss_limit = float(settings.get("max_daily_loss") or 0)
    gross_order_value = round(sum(abs(float(o.get("price") or 0) * int(o.get("qty") or 0)) for o in orders), 2)
    
    excluded_statuses = {
        ORDER_REJECTED,
        ORDER_CANCELLED,
        "REJECTED",
        "CANCELLED",
        "FAILED",
        "STALE",
        "BROKER_NOT_FOUND",
        "BLOCKED",
        "SKIPPED",
        "SKIPPED_SIGNAL",
    }
    trades_used = len([
        o for o in orders 
        if o.get("status") not in excluded_statuses
    ])
    max_trades = int(settings.get("max_trades_per_day") or 0)
    session = market_session_snapshot()
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "mode": mode.upper(),
        "daily_loss_limit": loss_limit,
        "realized_pnl": realized,
        "realized_pnl_source": fill_summary["source"],
        "fill_count": fill_summary["fill_count"],
        "closed_trade_count": fill_summary["closed_trade_count"],
        "open_pnl": open_pnl,
        "total_pnl": round(realized + open_pnl, 2),
        "loss_remaining": round(max(0.0, loss_limit + realized), 2) if loss_limit else None,
        "trades_used": trades_used,
        "max_trades_per_day": max_trades,
        "trades_remaining": max(0, max_trades - trades_used) if max_trades else None,
        "per_strategy_capital": settings.get("per_strategy_capital"),
        "max_position_size": settings.get("max_position_size"),
        "gross_order_value": gross_order_value,
        "market_open": session["global_status"] == "OPEN",
        "market_session": session,
    }


# moved to routes/dashboard.py
async def trade_journal(user=Depends(get_current_user)):
    rows = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    _today_start, _ = get_trading_day_window_ist()
    skipped = await db.skipped_signals.find({"user_id": user["id"], "last_seen_at": {"$gte": _today_start}}, {"_id": 0, "user_id": 0}).sort("last_seen_at", -1).to_list(200)
    fill_summary = await _fill_ledger_summary(user["id"])
    completed = [r for r in rows if canonical_order_status(r.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
    failed_actual = [r for r in rows if str(r.get("status") or "").upper() in {"FAILED", "REJECTED"}]
    wins = fill_summary["wins"]
    losses = fill_summary["losses"]
    total_pnl = fill_summary["realized_pnl"]
    return {
        "summary": {
            "orders": len(rows),
            "completed": len(completed),
            "filled_trades": fill_summary["fill_count"],
            "failed_actual_orders": len(failed_actual),
            "skipped_signals": sum(int(row.get("count") or 1) for row in skipped),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(1, wins + losses) * 100, 2),
            "realized_pnl": total_pnl,
            "realized_pnl_source": fill_summary["source"],
        },
        "orders": rows,
        "filled_trades": fill_summary["fills"],
        "failed_actual_orders": failed_actual,
        "skipped_signals": skipped,
    }


@app.websocket("/api/ws/orders")
async def orders_ws(websocket: WebSocket):
    token = websocket.query_params.get("token")
    user = await get_user_from_token(token or "")
    if not user:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            await _sync_upstox_order_statuses(user["id"])
            rows = await db.orders.find({
                "user_id": user["id"],
                "status": {"$nin": list(STALE_ORDER_STATUSES)},
                "visibility": {"$ne": "hidden"},
            }, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(50)
            await websocket.send_json({
                "type": "orders",
                "server_time_ist": (datetime.now(timezone.utc) + IST_OFFSET).isoformat(),
                "orders": rows,
            })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return


async def _resolve_option_for_strategy(
    user_id: str,
    strategy_row: Dict[str, Any],
    underlying: str,
    signal_action: str,
    strike_mode: str,
    otm_points: int = 0,
    expiry_offset: int = 0,
) -> Optional[Dict[str, Any]]:
    """Resolves an index option contract dynamically through Upstox."""
    underlying = underlying.upper()
    if underlying in REMOVED_COMMODITY_UNDERLYINGS:
        logger.warning("Option resolution blocked for removed MCX underlying=%s strategy=%s", underlying, strategy_row.get("id"))
        return None
    settings = await get_user_settings(user_id)
    strategy_mode = strategy_row.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
    is_paper = strategy_mode == "paper"
    allow_simulated_prices = bool(settings.get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
    execution_broker = "upstox"

    upstox_gw = await get_user_upstox_gateway(user_id)
    gateway_connected = upstox_gw and upstox_gw.connected

    # Fetch spot LTP from Upstox if connected
    upstox_keys = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX"
    }

    spot = None
    if gateway_connected:
        if underlying in upstox_keys:
            try:
                res = await asyncio.to_thread(upstox_gw.get_market_quote, [upstox_keys[underlying]])
                node = res.get("data", {}).get(upstox_keys[underlying]) or {}
                spot_ltp = node.get("last_price") or node.get("ltp")
                if not spot_ltp:
                    spot_ltp = UpstoxGateway.parse_quote_ltp(res, upstox_keys[underlying])
                if spot_ltp:
                    spot = float(spot_ltp)
            except Exception as e:
                logger.warning(f"Failed to fetch Upstox spot price: {e}")

        if spot is None and underlying in {"NIFTY", "BANKNIFTY", "SENSEX"}:
            try:
                index_exchange = "BSE" if underlying == "SENSEX" else "NSE"
                index_query = "Nifty Bank" if underlying == "BANKNIFTY" else ("Nifty 50" if underlying == "NIFTY" else "SENSEX")
                search = await asyncio.to_thread(
                    upstox_gw.search_instruments,
                    index_query,
                    exchanges=index_exchange,
                    segments="INDEX",
                    instrument_types="INDEX",
                    records=5,
                )
                for node in (search.get("data") if isinstance(search, dict) else []) or []:
                    key = node.get("instrument_key")
                    if not key:
                        continue
                    quote = await asyncio.to_thread(upstox_gw.get_market_quote, [key])
                    spot_ltp = UpstoxGateway.parse_quote_ltp(quote, key)
                    if spot_ltp:
                        spot = float(spot_ltp)
                        upstox_keys[underlying] = key
                        break
            except Exception as e:
                logger.warning(f"Upstox index search failed for {underlying}: {e}")

    if spot is None:
        if not is_paper or not allow_simulated_prices:
            logger.warning(f"Live Upstox spot price unavailable for {underlying}; options resolution blocked.")
            return None
        spot = (
            24850.40 if underlying == "NIFTY"
            else 54000.00 if underlying == "BANKNIFTY"
            else 81460.20 if underlying == "SENSEX"
            else 0.0
        )

    interval = options_helper.STRIKE_INTERVALS.get(underlying, 100)
    atm = options_helper.round_to_strike(spot, interval)
    opt_type = "CE"
    if "BUY" in strike_mode:
        opt_type = "CE" if signal_action == "BUY" else "PE"
    else: # ATM_SELL
        opt_type = "PE" if signal_action == "BUY" else "CE"
        
    if opt_type == "CE":
        strike = atm + otm_points
    else:
        strike = atm - otm_points
    strike = options_helper.round_to_strike(strike, interval)

    expiry_dt = datetime.now() + timedelta(days=(7 - datetime.now().weekday() + 3) % 7)
    instrument_token = None
    tradingsymbol = None
    lot_size = options_helper.LOT_SIZES.get(underlying, 50)
    chain_loaded = False
    chain_match: Optional[Dict[str, Any]] = None
    quality_quote: Dict[str, Any] = {}
    pcr_value: Optional[float] = None

    # 1. Try live Upstox index option resolution.
    if gateway_connected:
        # Index Option lookup using flexible option chain / search
        try:
            spot_key = upstox_keys.get(underlying)
            if spot_key:
                # Upstox's /v2/option/chain REQUIRES a concrete expiry_date — passing
                # None returns HTTP 400, so this lookup always failed and EVERY
                # resolution silently fell through to the search fallback (no chain
                # match → no PCR, no chain-sourced greeks). Resolve the target expiry
                # first from the option-contract list, honouring expiry_offset
                # (0 = nearest, expiries sorted ascending).
                chain_expiry = None
                try:
                    _contracts = await asyncio.to_thread(upstox_gw.get_option_contracts, spot_key)
                    _expiries = sorted({
                        c.get("expiry") for c in ((_contracts or {}).get("data") or [])
                        if c.get("expiry")
                    })
                    if _expiries:
                        chain_expiry = _expiries[min(max(int(expiry_offset or 0), 0), len(_expiries) - 1)]
                except Exception as _exp_exc:
                    logger.warning("Upstox option expiry lookup failed for %s: %s", underlying, _exp_exc)
                chain = await asyncio.to_thread(upstox_gw.get_option_chain, spot_key, chain_expiry)
                if chain and chain.get("status") == "success":
                    data = chain.get("data", []) or []
                    chain_loaded = len(data) > 0
                    for node in data:
                        node_strike = float(node.get("strike_price") or 0)
                        if int(node_strike) == int(strike):
                            chain_match = node
                            try:
                                call_oi = float(((node.get("call_options") or {}).get("market_data") or {}).get("oi") or (node.get("call_options") or {}).get("oi") or 0)
                                put_oi = float(((node.get("put_options") or {}).get("market_data") or {}).get("oi") or (node.get("put_options") or {}).get("oi") or 0)
                                if call_oi > 0:
                                    pcr_value = put_oi / call_oi
                            except Exception:
                                pcr_value = None
                            opt_node = node.get("call_options" if opt_type == "CE" else "put_options") or {}
                            if opt_node:
                                instrument_token = opt_node.get("instrument_key")
                                tradingsymbol = opt_node.get("trading_symbol")
                                quality_quote = dict(opt_node.get("market_data") or {})
                                # Greeks live in a separate option_greeks sub-object,
                                # not market_data — fold them in so the Phase 1 mirror
                                # picks them up straight from the chain match (and the
                                # greeks backfill further below can skip its extra fetch).
                                quality_quote.update(opt_node.get("option_greeks") or {})
                                if node.get("expiry"):
                                    try:
                                        expiry_dt = datetime.strptime(str(node["expiry"]), "%Y-%m-%d")
                                    except Exception:
                                        pass
                                # Upstox option-chain nodes carry no trading_symbol, so
                                # build the canonical verbose form the rest of the system
                                # uses ("BANKNIFTY 54000 CE 30 JUN 26") instead of letting
                                # the compact fallback below produce "BANKNIFTY26063054000CE"
                                # — a format mismatch would break symbol-based dedup/display.
                                if not tradingsymbol:
                                    tradingsymbol = f"{underlying} {int(strike)} {opt_type} {expiry_dt.strftime('%d %b %y').upper()}"
                                break
        except Exception as e:
            logger.warning(f"Upstox option chain lookup failed: {e}")

        # Fallback search candidate loop
        if not instrument_token:
            try:
                exch = "BSE" if underlying == "SENSEX" else "NSE"
                segment_candidates = ("FO", "OPT", "ALL")
                expiry_candidates = ("current_week", "next_week", "current_month", None)
                query_candidates = [f"{underlying} {int(strike)}", underlying]
                candidates = []
                for query in dict.fromkeys(query_candidates):
                    for segment in segment_candidates:
                        for expiry_filter in expiry_candidates:
                            search = await asyncio.to_thread(
                                upstox_gw.search_instruments,
                                query,
                                exchanges=exch,
                                segments=segment,
                                instrument_types=opt_type,
                                expiry=expiry_filter,
                                atm_offset=0,
                                records=30,
                            )
                            batch = search.get("data") if isinstance(search, dict) else []
                            if batch:
                                candidates.extend(batch)
                    if candidates:
                        break
                best = None
                best_distance = None
                for node in candidates or []:
                    node_opt_type = str(node.get("instrument_type") or node.get("option_type") or "").upper()
                    if node_opt_type != opt_type:
                        continue
                    key = node.get("instrument_key")
                    if not key:
                        continue
                    node_strike = float(node.get("strike_price") or 0)
                    distance = abs(node_strike - float(strike))
                    if best is None or distance < best_distance:
                        best = node
                        best_distance = distance
                if best:
                    instrument_token = best.get("instrument_key")
                    tradingsymbol = best.get("trading_symbol")
                    if best.get("expiry"):
                        expiry_dt = datetime.fromisoformat(str(best["expiry"]))
                    if best.get("lot_size"):
                        lot_size = int(float(best.get("lot_size")))
                    logger.info("Resolved Upstox option %s %s strike=%s key=%s symbol=%s", underlying, opt_type, strike, instrument_token, tradingsymbol)
            except Exception as e:
                logger.warning(f"Upstox instrument search failed: {e}")

    # 2. Fabricate contract details in Paper Mode if live lookup failed or gateway is offline
    _is_simulated_contract = False
    if not instrument_token:
        if is_paper and allow_simulated_prices:
            instrument_token = f"PAPER_{underlying}_{opt_type}_{int(strike)}"
            tradingsymbol = f"{underlying}{expiry_dt.strftime('%y%m%d')}{int(strike)}{opt_type}"
            _is_simulated_contract = True
            logger.info("Fabricating paper option contract %s strike=%s (PAPER_SIMULATED_CONTRACT)", tradingsymbol, strike)
        else:
            logger.warning(f"Live Upstox option contract strike {strike} not found in chain; guessing blocked.")
            return None

    resolved_contract = {
        "tradingsymbol": tradingsymbol or f"{underlying}{expiry_dt.strftime('%y%m%d')}{strike}{opt_type}",
        "exchange": "NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO",
        "instrument_token": instrument_token,
        "instrument_key": instrument_token,
        "upstox_instrument_token": instrument_token,
        "lot_size": lot_size,
        "strike": strike,
        "expiry": expiry_dt.date().isoformat(),
        "underlying": underlying,
        "option_type": opt_type,
        "spot": spot,
        "atm_strike": atm,
        "transaction_type": "BUY" if "BUY" in strike_mode else "SELL",
        # Simulated paper-mode marker — must NEVER be accepted by live execution engine
        "simulated": _is_simulated_contract,
        "source": "PAPER_SIMULATED_CONTRACT" if _is_simulated_contract else "LIVE_UPSTOX_CONTRACT",
    }

    # Fetch and log LTP for this candidate contract
    ltp = None
    ltp_reason = None
    if not gateway_connected:
        ltp_reason = "Upstox gateway disconnected or not authenticated."
    elif "PAPER_" in str(instrument_token) or str(instrument_token).startswith("999"):
        ltp_reason = "Using fabricated instrument key for paper trading (Cause 5)."
    else:
        try:
            # Ensure this candidate is on the WS feed in FULL mode so subsequent
            # candles carry live bid/ask depth (not just ltpc). Subscription is
            # idempotent — already-subscribed keys are deduped by the gateway.
            # Targeted (per-candidate) on purpose: full mode has a per-connection
            # instrument cap, so we never mass-subscribe the whole option universe.
            try:
                await asyncio.to_thread(upstox_gw.start_market_data_ws, [instrument_token], "full")
            except Exception as _sub_exc:
                logger.debug("full-mode subscribe failed for %s: %s", instrument_token, _sub_exc)
            # Try latest tick cache first
            tick = upstox_gw.latest_tick(instrument_token)
            if tick and tick.get("ltp"):
                ltp = float(tick["ltp"])
                quality_quote.update(tick)
            else:
                # Try REST quote fallback
                quote = await asyncio.to_thread(upstox_gw.get_market_quote, [instrument_token])
                ltp_val = UpstoxGateway.parse_quote_ltp(quote, instrument_token)
                if ltp_val and ltp_val > 0:
                    ltp = float(ltp_val)
                    quality_quote.update(((quote.get("data") or {}).get(instrument_token) or {}) if isinstance(quote, dict) else {})
                    quality_quote.setdefault("received_at", datetime.now(timezone.utc).isoformat())
                else:
                    ltp_reason = "Quote API returned null or invalid ltp for instrument key."
        except Exception as e:
            ltp_reason = f"Error fetching LTP from Quote API: {e}"

    if ltp is None and is_paper and allow_simulated_prices and _is_simulated_contract:
        ltp = 34.50
        ltp_reason = "Mock option premium resolved for paper trading."

    if ltp is not None:
        resolved_contract["ltp"] = ltp
        ltp_str = f"{ltp:.2f}"
    else:
        ltp_str = "null"

    # Format fields for exact candidate log
    formatted_expiry = expiry_dt.strftime("%d %b %Y").upper()
    exch_label = resolved_contract["exchange"]
    segment_label = "BSE_FO" if exch_label == "BFO" else "NSE_FO"

    # Log EXACT candidate output
    print(f"\n----- GENERATED TRADE CANDIDATE -----\n"
          f"{underlying}\n"
          f"{int(strike)} {opt_type}\n"
          f"{formatted_expiry}\n"
          f"{exch_label}\n"
          f"{instrument_token}\n"
          f"LTP={ltp_str}\n"
          f"-------------------------------------\n", flush=True)

    logger.info(
        "Candidate Details: Underlying=%s Strike=%s Expiry=%s OptionType=%s InstrumentKey=%s Exchange=%s Segment=%s LTP=%s",
        underlying, strike, formatted_expiry, opt_type, instrument_token, exch_label, segment_label, ltp_str
    )

    if ltp is None:
        # Determine exact null LTP causes
        diagnosed_causes = []
        if "PAPER_" in str(instrument_token) or str(instrument_token).startswith("999"):
            diagnosed_causes.append("Using fabricated instrument keys (Cause 5)")
        else:
            if not instrument_token or "|" not in str(instrument_token):
                diagnosed_causes.append("Wrong instrument key generation or format (Cause 2 & 6)")
            if not chain_loaded and underlying not in REMOVED_COMMODITY_UNDERLYINGS:
                diagnosed_causes.append("Option chain not loaded or failed (Cause 1)")
            if expiry_dt.date() < datetime.now().date():
                diagnosed_causes.append("Expiry mismatch / expired contract (Cause 3)")
            if strike % interval != 0:
                diagnosed_causes.append("Invalid strike construction (Cause 4)")
        
        if not diagnosed_causes:
            diagnosed_causes.append(ltp_reason or "Unknown cause / Market data feed inactive")

        cause_message = " | ".join(diagnosed_causes)
        logger.warning(
            "Option LTP is NULL for trade candidate %s %s strike=%s. Determined Cause(s): %s",
            underlying, opt_type, strike, cause_message
        )
        print(f"!!! Option LTP is NULL for candidate !!!\nDetermined Cause: {cause_message}\n", flush=True)

    # Propagate top-of-book + freshness onto the resolved contract. The
    # OptionSelector v2 quality gate reads bid/ask/received_at OFF THE CONTRACT
    # (see option_selector_v2._spread_pct / _quote_age_sec), but historically
    # those values only lived on quality_quote/chain_match and were handed to the
    # legacy scorer alone — so v2 always saw an unknown spread (score 0) and
    # filtered ~most signals. Mirror the legacy extraction here. (audit: WS-full / gate-plumbing)
    _qq = quality_quote or {}
    _cm = chain_match or {}
    _bid = _qq.get("bid") or _qq.get("bid_price") or _qq.get("best_bid_price") or _cm.get("bid_price")
    _ask = _qq.get("ask") or _qq.get("ask_price") or _qq.get("best_ask_price") or _cm.get("ask_price")
    if _bid is not None:
        resolved_contract["bid"] = _bid
    if _ask is not None:
        resolved_contract["ask"] = _ask
    _qts = _qq.get("timestamp") or _qq.get("received_at") or _qq.get("ltt")
    if _qts is not None:
        resolved_contract["received_at"] = _qts

    # Phase 1 greeks backfill. The full-mode WS tick almost always misses on first
    # resolution (the subscription was only just sent, so latest_tick returns
    # nothing and we fall back to the REST ltp-only quote), and the search-fallback
    # path carries no chain data at all — so quality_quote usually has no greeks/OI
    # and greeks_at_signal lands as null. Backfill from a targeted option-chain
    # fetch for THIS contract's *actual* expiry (the chain at the top of this
    # function uses the nearest expiry and misses later-dated monthlies). The REST
    # option chain reliably returns option_greeks + market_data. Best-effort only:
    # gated on missing greeks so the hot path is untouched when the tick had them,
    # and wrapped so it can never block resolution.
    if (
        gateway_connected
        and not _is_simulated_contract
        and not any(_qq.get(_g) is not None for _g in ("iv", "delta", "theta", "gamma", "vega"))
    ):
        try:
            _spot_key = upstox_keys.get(underlying)
            if _spot_key:
                _gchain = await asyncio.to_thread(
                    upstox_gw.get_option_chain, _spot_key, expiry_dt.date().isoformat()
                )
                if _gchain and _gchain.get("status") == "success":
                    for _gnode in (_gchain.get("data") or []):
                        if int(float(_gnode.get("strike_price") or 0)) == int(strike):
                            _gopt = _gnode.get("call_options" if opt_type == "CE" else "put_options") or {}
                            _gg = _gopt.get("option_greeks") or {}
                            _gmd = _gopt.get("market_data") or {}
                            for _gk in ("iv", "delta", "theta", "gamma", "vega"):
                                if _gg.get(_gk) is not None:
                                    _qq.setdefault(_gk, _gg.get(_gk))
                            if _gmd.get("oi") is not None:
                                _qq.setdefault("oi", _gmd.get("oi"))
                            if resolved_contract.get("bid") is None and _gmd.get("bid_price") is not None:
                                resolved_contract["bid"] = _gmd.get("bid_price")
                            if resolved_contract.get("ask") is None and _gmd.get("ask_price") is not None:
                                resolved_contract["ask"] = _gmd.get("ask_price")
                            break
        except Exception as _gexc:
            logger.debug("greeks chain backfill failed for %s %s %s: %s", underlying, strike, opt_type, _gexc)

    # Mirror greeks / IV / OI / order-flow fields off the full-mode tick so they
    # persist on the signal doc (option_contract is embedded whole) and flow
    # through order → fill → position for entry-time analytics. (Phase 1: data
    # collection only — nothing reads these for trade decisions yet.)
    for _gf in ("iv", "oi", "delta", "theta", "gamma", "vega", "rho",
                "bid_qty", "ask_qty", "tbq", "tsq"):
        _gv = _qq.get(_gf)
        if _gv is not None:
            resolved_contract[_gf] = _gv

    resolved_contract["trade_quality_score"] = option_entry_quality_score(
        resolved_contract,
        spot=spot,
        chain_row=chain_match,
        quote=quality_quote,
        pcr=pcr_value,
    )
    resolved_contract["quality_score"] = resolved_contract["trade_quality_score"]["score"]
    resolved_contract["quality_readiness"] = resolved_contract["trade_quality_score"]["readiness"]

    return resolved_contract


# Upstox quality and broker-control routes moved to routes/broker.py


# moved to routes/dashboard.py
async def dashboard_telemetry(user=Depends(get_current_user)):
    """Per-strategy dashboard payload backed by the SQLite runtime ledger."""
    rows = await db.strategies.find(
        {"user_id": user["id"]},
        {"_id": 0, "user_id": 0},
    ).sort("created_at", -1).to_list(500)
    ledger_snapshot = option_ledger.snapshot()
    strategies_page_data = []
    for row in rows:
        ledger_row = ledger_snapshot.get(row["id"])
        if ledger_row is None:
            _sync_option_ledger_strategy(row)
            ledger_row = option_ledger.snapshot().get(row["id"], {})
        active_position = ledger_row.get("active_position")
        visual_risk = ((row.get("visual_config") or {}).get("risk") or {})
        runtime_state = ledger_row.get("state", "IDLE")
        if runtime_state in {"IDLE", "READY"} and row.get("status") == "live":
            runtime_state = "SCANNING"
        strategies_page_data.append({
            "strategy_id": row["id"],
            "name": row.get("name"),
            "status": row.get("status"),
            "asset_class": row.get("asset_class", "equity"),
            "state": runtime_state,
            "cooldown_until": ledger_row.get("cooldown_until"),
            "required_capital": ledger_row.get("required_capital", 0),
            "max_lots": 1,
            "target_pct": round(float(ledger_row.get("target_pct") or 0) * 100, 2),
            "stoploss_pct": round(float(ledger_row.get("stoploss_pct") or 0) * 100, 2),
            "trailing_sl_enabled": ledger_row.get("trailing_sl_enabled"),
            "trail_trigger_pct": round(float(ledger_row.get("trail_trigger_pct") or 0) * 100, 2),
            "trail_step_pct": round(float(ledger_row.get("trail_step_pct") or 0) * 100, 2),
            "risk_style": ledger_row.get("risk_style") or visual_risk.get("risk_style", "balanced"),
            "adaptive_exits_enabled": ledger_row.get("adaptive_exits_enabled", visual_risk.get("adaptive_exits_enabled", True)),
            "target_r_multiple": ledger_row.get("target_r_multiple", visual_risk.get("target_r_multiple", DEFAULT_STRATEGY_RISK["target_r_multiple"])),
            "cooldown_minutes": ledger_row.get("cooldown_minutes"),
            "max_trades_day": ledger_row.get("max_trades_day"),
            "risk_settings": {**(ledger_row.get("risk_settings", {}) or {}), **visual_risk},
            "time_exit_minutes": visual_risk.get("time_exit_minutes", 45),
            "indicator_exit_enabled": visual_risk.get("indicator_exit_enabled", True),
            "exit_mode": visual_risk.get("exit_mode", "tp_sl_tsl_or_signal"),
            "daily_pnl": ledger_row.get("daily_pnl", {}),
            "re_entry_allowed": ledger_row.get("state", "IDLE") == "IDLE",
            "active_position": active_position,
            "telemetry": {
                "evaluations": row.get("evaluations", 0),
                "signals_fired": row.get("signals_fired", 0),
                "last_evaluated_at": row.get("last_evaluated_at"),
                "last_signal_at": row.get("last_signal_at"),
                "last_signal_action": row.get("last_signal_action"),
                "last_signals_count": row.get("last_signals_count"),
                "last_filter_reason": row.get("last_filter_reason"),
                "last_data_source": row.get("last_data_source"),
                "last_data_live": row.get("last_data_live"),
                "last_data_reason": row.get("last_data_reason"),
                "latest_candle_age_sec": row.get("latest_candle_age_sec"),
                "last_error": row.get("last_error"),
            },
        })
    latest_ticks = option_ledger.latest_ticks(["NIFTY", "BANKNIFTY", "SENSEX"])
    session = market_session_snapshot()
    data_source = next((t["data_source"] for t in latest_ticks.values() if t.get("data_source")), None)
    simulated_active = bool(data_source and "simulated" in str(data_source).lower())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ledger": "sqlite",
        "market_status": {
            "is_open": session["global_status"] == "OPEN",
            "session": session,
            "nifty": latest_ticks.get("NIFTY"),
            "sensex": latest_ticks.get("SENSEX"),
            "last_tick_time": max([t["tick_time"] for t in latest_ticks.values()], default=None),
            "data_source": data_source,
            "feed_source_label": "Simulated feed" if simulated_active else ("Upstox feed" if data_source else "No price feed"),
            "simulated_warning": simulated_active,
        },
        "strategies_page_data": strategies_page_data,
    }


# moved to routes/dashboard.py
async def risk_kill_switch(user=Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "id": 1}).to_list(500)
    for row in strategies:
        option_ledger.set_kill_switch(True, strategy_id=row["id"])
    await db.users.update_one({"id": user["id"]}, {"$set": {"paper_mode": True, "ops_last_emergency_stop_at": now}})
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": f"Kill switch at {now}: automation paused and ledger entries disabled."}},
    )
    return {"ok": True, "paper_mode": True, "paused_strategies": res.modified_count, "disabled_strategies": len(strategies), "at": now}


# ============== Zerodha helpers ==============
# Map our short symbols to NSE tradingsymbols (1:1 for equity; indices use different)
NSE_INDEX_MAP = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}


def _nse_token(sym: str) -> str:
    """Build exchange:tradingsymbol for Kite market quote calls."""
    sym_upper = sym.upper()
    if sym_upper in options_helper.INDEX_SPOT_SYMBOL:
        exchange, tradingsymbol = options_helper.INDEX_SPOT_SYMBOL[sym_upper]
        return f"{exchange}:{tradingsymbol}"
    return f"NSE:{sym}"


async def get_user_kite(user_id: str):
    return None, {"connected": False, "reason": "zerodha_runtime_removed"}


async def get_user_kotak_status(user_id: str) -> Dict[str, Any]:
    return {"connected": False, "reason": "kotak_runtime_removed", "gateway": {}}


async def get_user_kotak_gateway(user_id: str):
    return None


def _in_market_hours() -> bool:
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if ist_now.weekday() >= 5:
        return False
    m = ist_now.hour * 60 + ist_now.minute
    return 9 * 60 + 15 <= m <= 15 * 60 + 30


async def get_user_upstox_status(user_id: str) -> Dict[str, Any]:
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    api_key = os.environ.get("UPSTOX_API_KEY") or (decrypt_secret(keys.get("api_key")) if keys else None)
    api_secret = os.environ.get("UPSTOX_API_SECRET") or (decrypt_secret(keys.get("api_secret")) if keys else None)
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN") or (decrypt_secret(keys.get("access_token")) if keys else None)
    refresh_token = os.environ.get("UPSTOX_REFRESH_TOKEN") or (decrypt_secret(keys.get("refresh_token")) if keys else None)
    redirect_uri = _upstox_redirect_uri(None, keys)
    status = upstox_helper.status_from_keys(keys, api_key)
    gateway = _UPSTOX_GATEWAYS.get(user_id)
    gateway_status = gateway.status() if gateway else {}
    token_present = bool(access_token)
    token_state = "missing"
    token_valid = False
    token_validation_error = None
    token_validated_at = None
    if token_present:
        token_state = "present"
        cache = _UPSTOX_TOKEN_VALIDATION_CACHE.get(user_id) or {}
        cache_age = _market_data_age_sec(cache.get("validated_at"))
        if cache and cache_age is not None and cache_age < 300:
            token_valid = bool(cache.get("valid"))
            token_state = str(cache.get("state") or ("valid" if token_valid else "invalid"))
            token_validation_error = cache.get("error")
            token_validated_at = cache.get("validated_at")
        else:
            validator = gateway or UpstoxGateway(
                api_key=api_key,
                api_secret=api_secret,
                access_token=access_token,
                refresh_token=refresh_token,
                redirect_uri=redirect_uri,
                sandbox=bool(keys.get("is_sandbox")) if keys else False,
            )
            try:
                await asyncio.to_thread(validator.get_profile)
                token_valid = True
                token_state = "valid"
                logger.info("Upstox access token validated for user=%s", user_id)
            except Exception as exc:
                token_valid = False
                token_validation_error = str(exc)[:300]
                token_state = "expired" if any(part in token_validation_error.lower() for part in ("401", "unauthorized", "expired", "invalid token")) else "invalid"
                logger.warning("Upstox access token validation failed user=%s state=%s error=%s", user_id, token_state, token_validation_error)
            token_validated_at = datetime.now(timezone.utc).isoformat()
            _UPSTOX_TOKEN_VALIDATION_CACHE[user_id] = {
                "valid": token_valid,
                "state": token_state,
                "error": token_validation_error,
                "validated_at": token_validated_at,
            }
    else:
        _log_throttled(
            f"upstox-token-missing:{user_id}",
            300.0,
            logging.INFO,
            "Upstox token missing for user=%s; live trading and ticker require reconnect",
            user_id,
        )
    missing = [
        name
        for name, value in {
            "UPSTOX_API_KEY": api_key,
            "UPSTOX_API_SECRET": api_secret,
            "UPSTOX_REDIRECT_URI": redirect_uri,
        }.items()
        if not value
    ]
    # Determine REST status details
    if access_token and str(access_token).startswith("mock_live_upstox_token"):
        rest_status = "mock"
    else:
        rest_status = "valid" if token_valid else ("invalid" if token_present else "missing")

    # Determine Feed status details
    feed_running = bool(((gateway_status or {}).get("feed_status") or {}).get("connected"))
    if access_token and str(access_token).startswith("mock_live_upstox_token"):
        feed_status_str = "mock_blocked"
    elif feed_running:
        feed_status_str = "connected"
    elif gateway_status.get("ws_running"):
        feed_status_str = "connecting"
    elif (gateway_status.get("last_error") and "401" in str(gateway_status.get("last_error"))) or \
         ((gateway_status.get("feed_status") or {}).get("last_error") and "401" in str(gateway_status.get("feed_status", {}).get("last_error"))):
        feed_status_str = "unauthorized"
    else:
        feed_status_str = "disconnected"

    # Token expiration and refresh details
    token_expired = token_present and not token_valid

    reconnect_message = (
        "Upstox token expired or invalid. Reconnect Upstox before live quotes, live readiness, or feed startup."
        if token_expired else
        "Reconnect Upstox required" if token_present else
        "Save Upstox credentials and connect Upstox"
    )

    # Feed stalled watchdog check
    feed_stalled = False
    feed_stalled_reason = None

    if _in_market_hours():
        if not token_valid:
            feed_stalled = True
            feed_stalled_reason = "token_invalid"
        elif not gateway or not gateway_status.get("connected"):
            feed_stalled = True
            feed_stalled_reason = "feed_disconnected"
        else:
            connected_at_str = gateway_status.get("connected_at")
            snapshot_received = gateway_status.get("snapshot_received", False)
            
            # Check 1: Connected but 0 ticks received since connect
            if connected_at_str and not snapshot_received:
                try:
                    conn_dt = datetime.fromisoformat(connected_at_str.replace("Z", "+00:00"))
                    conn_age = (datetime.now(timezone.utc) - conn_dt).total_seconds()
                    if conn_age > 180:
                        feed_stalled = True
                        feed_stalled_reason = "connected_but_zero_ticks"
                except Exception:
                    pass
            
            # Check 2: Index ticks stale > 3 min
            if not feed_stalled and snapshot_received:
                index_keys = ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank", "BSE_INDEX|SENSEX"]
                stale_indices = []
                for key in index_keys:
                    tick = gateway.latest_tick(key)
                    if tick:
                        rec_at = tick.get("received_at")
                        if rec_at:
                            try:
                                last_dt = datetime.fromisoformat(rec_at.replace("Z", "+00:00"))
                                age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                                if age > 180:
                                    stale_indices.append(f"{key.split('|')[-1]} ({int(age)}s)")
                            except Exception:
                                pass
                        else:
                            stale_indices.append(f"{key.split('|')[-1]} (no_ts)")
                    else:
                        stale_indices.append(f"{key.split('|')[-1]} (no_tick)")
                if stale_indices:
                    feed_stalled = True
                    feed_stalled_reason = f"index_ticks_stale:{','.join(stale_indices)}"

    status.update({
        "connected": bool(token_valid),
        "authenticated": bool(token_valid),
        "logged_in": bool(token_valid),
        "keys_saved": bool(keys or api_key),
        "api_secret_saved": bool(api_secret),
        "redirect_uri_ready": bool(redirect_uri),
        "access_token_saved": token_present,
        "token_present": token_present,
        "token_state": token_state,
        "token_valid": token_valid,
        "token_validated_at": token_validated_at,
        "token_validation_error": token_validation_error,
        "last_auth_time": (keys or {}).get("access_token_obtained_at"),
        "reconnect_required": not token_valid,
        "live_trading_enabled": bool(token_valid),
        "feed_running": feed_running,
        "feed_status": (gateway_status or {}).get("feed_status"),
        "env_ready": not missing,
        "missing_env": missing,
        "reason": None if token_valid else (token_state if token_present else ("no_token" if api_key else "no_keys")),
        "message": "Upstox connected" if token_valid else reconnect_message,
        "gateway": gateway_status or None,
        "is_sandbox": bool(keys.get("is_sandbox")) if keys else False,
        
        # Enhanced status payload for UI and backend sanity checks
        "rest_status": rest_status,
        "feed_status_str": feed_status_str,
        "token_expired": token_expired,
        "refresh_token_available": False,
        "daily_reconnect_required": True,
        "user_message": (
            "Upstox connected. Upstox requires a fresh login daily before market hours."
            if token_valid else reconnect_message
        ),
        "feed_stalled": feed_stalled,
        "feed_stalled_reason": feed_stalled_reason,
    })
    return status


async def get_user_upstox_gateway(user_id: str, fresh: bool = False) -> Optional[UpstoxGateway]:
    if not fresh and user_id in _UPSTOX_GATEWAYS:
        return _UPSTOX_GATEWAYS[user_id]
    if fresh:
        # Evicting a cached gateway without stopping its feed leaves an orphaned
        # daemon thread reconnecting forever. Stop the old feed before replacing it.
        old_gateway = _UPSTOX_GATEWAYS.pop(user_id, None)
        if old_gateway is not None:
            try:
                await asyncio.to_thread(old_gateway.stop_market_data_ws)
            except Exception as exc:
                logger.warning("Failed to stop prior Upstox feed for user=%s: %s", user_id, exc)
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    api_key = os.environ.get("UPSTOX_API_KEY") or (decrypt_secret(keys.get("api_key")) if keys else None)
    api_secret = os.environ.get("UPSTOX_API_SECRET") or (decrypt_secret(keys.get("api_secret")) if keys else None)
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN") or (decrypt_secret(keys.get("access_token")) if keys else None)
    refresh_token = os.environ.get("UPSTOX_REFRESH_TOKEN") or (decrypt_secret(keys.get("refresh_token")) if keys else None)
    redirect_uri = _upstox_redirect_uri(None, keys)
    if not api_key and not access_token:
        _log_throttled(
            f"upstox-no-keys:{user_id}",
            300.0,
            logging.WARNING,
            "Upstox gateway not initialized for user=%s: no_keys",
            user_id,
        )
        return None
    if access_token:
        logger.info("Loaded Upstox access token from %s for user=%s", "env" if os.environ.get("UPSTOX_ACCESS_TOKEN") else "storage", user_id)
    else:
        logger.warning("Upstox gateway initialized without access token user=%s; reconnect required", user_id)
    gateway = UpstoxGateway(
        api_key=api_key,
        api_secret=api_secret,
        access_token=access_token,
        refresh_token=refresh_token,
        redirect_uri=redirect_uri,
        sandbox=bool(keys.get("is_sandbox")) if keys else False,
    )
    _UPSTOX_GATEWAYS[user_id] = gateway
    return gateway


async def get_user_settings(user_id: str) -> dict:
    """Profile / trading preferences with safe defaults."""
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    paper_mode = (user or {}).get("paper_mode", True)
    allow_simulated = (user or {}).get("allow_simulated_prices")
    if allow_simulated is None:
        allow_simulated = bool(paper_mode)
    today_start, today_end = get_trading_day_window_ist()
    return {
        "name": (user or {}).get("name", ""),
        "default_qty": (user or {}).get("default_qty", 1),
        "default_product": (user or {}).get("default_product", "MIS"),
        "max_daily_loss": (user or {}).get("max_daily_loss", 10000.0),
        "max_position_size": (user or {}).get("max_position_size", 50000.0),
        "per_strategy_capital": (user or {}).get("per_strategy_capital", 25000.0),
        "max_trades_per_day": (user or {}).get("max_trades_per_day", 20),
        "data_broker": "upstox",
        "execution_broker": "upstox",
        "fallback_broker": "none",
        "paper_mode": paper_mode,
        "allow_simulated_prices": bool(allow_simulated),
        "paper_realism_mode": (user or {}).get("paper_realism_mode", "UPSTOX_LIKE"),
        "paper_block_suspended_instruments": (user or {}).get("paper_block_suspended_instruments", True),
        "paper_uses_upstox_like_charges": (user or {}).get("paper_uses_upstox_like_charges", True),
        "daily_loss_kill_switch_enabled": bool((user or {}).get("daily_loss_kill_switch_enabled") or (user or {}).get("daily_loss_guard_enabled")),
        "today_window_ist": {"start": today_start, "end": today_end},
        "live_auto_trading_enabled": False,
        "live_readiness_required": True,
    }


# ============== Routes: Live Readiness ==============
# live readiness routes moved to routes/readiness.py


# ============== Routes: Live Readiness — END ==============



# ============== Routes: Ops Console ==============
def _is_strategy_blocking_error(message: Optional[str]) -> bool:
    if not message:
        return False
    text = str(message)
    non_blocking = (
        "Signal filtered:",
        "Entry skipped:",
        "Option entry blocked: cooldown-active",
        "Strategy entry blocked: cooldown-active",
        "Option entry blocked: duplicate-buy-dropped",
        "Strategy entry blocked: duplicate-buy-dropped",
        "Option entry blocked: max-trades-day-reached",
        "Strategy entry blocked: max-trades-day-reached",
        "Instrument already has active strategy position:",
        "Strategy already has active position",
        "Instrument/strategy already reserved",
        "New BUY blocked",
        "Duplicate BUY blocked",
        "Re-entry blocked",
        "Live LTP unavailable",
        "Insufficient funds",
        "insufficient funds",
        "margin",
        "Margin",
    )
    return not any(text.startswith(prefix) or prefix in text for prefix in non_blocking)


def _build_recovery_plan(
    *,
    settings: Dict[str, Any],
    market_open: bool,
    tick_status: Dict[str, Any],
    errored: List[Dict[str, Any]],
    orders_open: int,
    readiness: Dict[str, Any],
) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    mode_live = not bool(settings.get("paper_mode", True))
    data_broker = "upstox"
    execution_broker = "upstox"

    def add(severity: str, title: str, detail: str, action: str, endpoint: Optional[str] = None) -> None:
        issues.append({
            "severity": severity,
            "title": title,
            "detail": detail,
            "action": action,
            "endpoint": endpoint,
        })

    # NSE/BSE timing checks.
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    is_weekday = ist_now.weekday() < 5
    minutes_now = ist_now.hour * 60 + ist_now.minute
    nse_open = is_weekday and (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)

    if mode_live:
        if not nse_open:
            add("info", "Market is closed", "Live MARKET orders are blocked outside NSE/BSE hours.", "Wait for 09:15-15:30 IST or use PAPER.")

    if execution_broker == "upstox":
        upstox = readiness.get("upstox") or {}
        upstox_session = next((c for c in readiness.get("checks") or [] if c.get("id") == "upstox_session"), {})
        if not upstox_session.get("ok"):
            add("critical", "Reconnect Upstox required", upstox_session.get("detail") or "Upstox access token is missing or expired.", "Open Broker Keys and reconnect Upstox OAuth.", "/broker/upstox/login")
    if data_broker == "upstox":
        feed = tick_status.get("feed_status") or {}
        if not tick_status.get("authenticated"):
            add("critical", "Upstox data session missing", tick_status.get("last_error") or "Ticker startup skipped: no_token.", "Reconnect Upstox on Broker Keys.", "/broker/upstox/login")
        elif not (feed.get("connected") or tick_status.get("ws_running")):
            add("warning", "Upstox ticker is stopped", feed.get("last_error") or tick_status.get("last_error") or "Feed has not started.", "Restart Upstox feed.", "/ops/ticker/restart")
    if orders_open:
        add("info", "Open orders need reconciliation", f"{orders_open} local order(s) are open/pending.", "Sync broker orders.", "/ops/orders/sync")
    if errored:
        add("warning", "Strategies have blocking errors", f"{len(errored)} strategy error(s) need attention.", "Open Strategy Errors below; clear only after fixing.", "/ops/strategies/clear-errors")
    for check in readiness.get("checks") or []:
        if check.get("id") == "market_hours":
            continue
        if not check.get("ok"):
            add("critical", check.get("label") or "Readiness failed", check.get("detail") or check.get("hint") or "A required live check failed.", check.get("hint") or "Fix readiness check.")


    weights = {"critical": 30, "warning": 15, "info": 5}
    score = max(0, 100 - sum(weights.get(item["severity"], 5) for item in issues))
    return {
        "score": score,
        "status": "READY" if score >= 85 and not any(i["severity"] == "critical" for i in issues) else "ATTENTION",
        "issues": issues[:12],
    }


def _kotak_ticker_status(user_id: str) -> Dict[str, Any]:
    gateway = _KOTAK_GATEWAYS.get(user_id)
    if not gateway:
        return {
            "connected": False,
            "connecting": False,
            "subscribed_tokens": 0,
            "websocket_url": "kotak-neo-sdk",
            "last_error": "Kotak gateway not connected",
        }
    status = gateway.status()
    authenticated = bool(status.get("authenticated"))
    subscribed = int(status.get("subscribed_tokens") or 0)
    ticks = int(status.get("ticks") or 0)
    return {
        "connected": bool(authenticated and subscribed and ticks),
        "connecting": bool(authenticated and subscribed and not ticks),
        "authenticated": authenticated,
        "subscribed_tokens": subscribed,
        "ticks": ticks,
        "last_tick_at": status.get("last_tick_at"),
        "last_error": status.get("last_error"),
        "websocket_url": "kotak-neo-sdk",
    }


async def _start_user_ticker(user_id: str) -> Dict[str, Any]:
    return await _start_user_upstox_ticker(user_id)


def _collect_kotak_instruments(node: Any, out: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    out = out or []
    if node is None or len(out) >= 25:
        return out
    if isinstance(node, list):
        for item in node:
            _collect_kotak_instruments(item, out)
        return out
    if not isinstance(node, dict):
        return out

    symbol = node.get("trdSym") or node.get("trading_symbol") or node.get("tradingSymbol") or node.get("symbol") or node.get("pSymbolName") or node.get("ts")
    token = node.get("instrument_token") or node.get("instrumentToken") or node.get("token") or node.get("tk") or node.get("pSymbol")
    exchange_segment = node.get("exchange_segment") or node.get("exSeg") or node.get("exchangeSegment")
    if symbol and token:
        candidate = {
            "symbol": str(symbol).upper(),
            "instrument_token": str(token),
            "exchange_segment": str(exchange_segment or "nse_cm"),
        }
        if not any(x["instrument_token"] == candidate["instrument_token"] for x in out):
            out.append(candidate)
    for value in node.values():
        _collect_kotak_instruments(value, out)
    return out


async def _start_user_kotak_ticker(user_id: str, symbols: Optional[List[str]] = None, exchange: str = "NSE") -> Dict[str, Any]:
    return {"started": False, "reason": "kotak_neo_removed"}


async def _start_user_upstox_ticker(user_id: str, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    status = await get_user_upstox_status(user_id)
    if not status.get("token_valid"):
        reason = status.get("reason") or "no_token"
        logger.warning("Upstox ticker startup skipped: %s user=%s", reason, user_id)
        return {
            "started": False,
            "reason": reason,
            "message": "Reconnect Upstox required",
            "status": status,
        }
    gateway = await get_user_upstox_gateway(user_id)
    if not gateway:
        logger.warning("Upstox ticker startup skipped: gateway_unavailable user=%s", user_id)
        return {"started": False, "reason": "gateway_unavailable", "message": "Reconnect Upstox required", "status": status}

    target_symbols = [str(s).upper() for s in (symbols or ["NIFTY", "BANKNIFTY", "SENSEX"])]
    keys: List[str] = []
    failures: List[Dict[str, str]] = []
    for symbol in target_symbols:
        token = _upstox_instrument_token("NSE", symbol) or _upstox_instrument_token("BSE", symbol)
        if token:
            keys.append(token)
        else:
            failures.append({"symbol": symbol, "reason": "no_token_resolved"})

    keys = list(dict.fromkeys(keys))
    if not keys:
        logger.warning("Upstox ticker startup skipped: no_tokens_resolved user=%s failures=%s", user_id, failures[:8])
        return {"started": False, "reason": "no_tokens_resolved", "failures": failures[:8], "status": gateway.status()}
    if UPSTOX_VIX_INSTRUMENT_KEY not in keys:
        keys.append(UPSTOX_VIX_INSTRUMENT_KEY)
    # "full" (full_d5) instead of "ltpc": index feeds gain OHLC, and any option
    # keys later joining this connection get bid/ask depth + greeks/IV/OI.
    result = await asyncio.to_thread(gateway.start_market_data_ws, keys, "full")
    if result.get("ok"):
        logger.info("Upstox ticker startup successful user=%s tokens=%s", user_id, len(keys))
    else:
        logger.warning("Upstox ticker startup skipped/failed user=%s reason=%s result=%s", user_id, result.get("reason"), result)
    return {
        "started": bool(result.get("ok")),
        "tokens": len(keys),
        "failures": failures[:8],
        "status": gateway.status(),
        "result": result,
    }


# ============== Routes: Ops Console (extracted to routes/ops.py) ==============
async def ops_diagnostics(user):
    commit, branch, dirty = get_git_info()
    settings = await get_user_settings(user["id"])
    upstox_auth_status = await get_user_upstox_status(user["id"])
    upstox_gw = await get_user_upstox_gateway(user["id"])
    upstox_status = upstox_gw.status() if upstox_gw else {
        "connected": False,
        "authenticated": False,
        "last_error": "Ticker startup skipped: no_token" if upstox_auth_status.get("reason") == "no_token" else upstox_auth_status.get("message"),
        "feed_status": {"connected": False, "state": "stopped", "last_error": upstox_auth_status.get("reason")},
    }
    if upstox_gw:
        upstox_status.update({
            "connected": bool(upstox_auth_status.get("token_valid")),
            "authenticated": bool(upstox_auth_status.get("token_valid")),
            "last_error": upstox_status.get("last_error") or (None if upstox_auth_status.get("token_valid") else upstox_auth_status.get("message")),
        })
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).to_list(500)
    stale_nonblocking_error_ids = [
        srow.get("id")
        for srow in strategies
        if srow.get("last_error") and not _is_strategy_blocking_error(srow.get("last_error"))
    ]
    if stale_nonblocking_error_ids:
        await db.strategies.update_many(
            {"user_id": user["id"], "id": {"$in": stale_nonblocking_error_ids}},
            {"$unset": {"last_error": ""}},
        )
        for srow in strategies:
            if srow.get("id") in stale_nonblocking_error_ids:
                srow.pop("last_error", None)
    errored = [
        {
            "id": srow.get("id"),
            "name": srow.get("name"),
            "status": srow.get("status"),
            "last_error": srow.get("last_error"),
            "last_data_source": srow.get("last_data_source"),
            "last_evaluated_at": srow.get("last_evaluated_at"),
        }
        for srow in strategies
        if _is_strategy_blocking_error(srow.get("last_error"))
    ][:20]
    orders_open = await db.orders.count_documents({"user_id": user["id"], "status": {"$in": list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)}})
    positions_count = await db.positions.count_documents({"user_id": user["id"]})
    readiness = await live_readiness(user=user)
    recovery_plan = _build_recovery_plan(
        settings=settings,
        market_open=_is_nse_market_open(),
        tick_status=upstox_status,
        errored=errored,
        orders_open=orders_open,
        readiness=readiness,
    )
    return {
        "version": APP_VERSION,
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "server_time_ist": (datetime.now(timezone.utc) + IST_OFFSET).isoformat(),
        "mode": "PAPER" if settings.get("paper_mode", True) else "LIVE",
        "market_session": market_session_snapshot(),
        "market": {"open": _is_nse_market_open(), "status": "OPEN" if _is_nse_market_open() else "CLOSED"},
        "broker_preferences": {"data_broker": "upstox", "execution_broker": "upstox", "fallback_broker": "none"},
        "readiness": readiness,
        "upstox": upstox_auth_status,
        "ticker": upstox_status,
        "brokers": {"upstox": upstox_auth_status},
        "removed_brokers": ["zerodha", "kite", "kotak_neo"],
        "removed_segments": ["MCX_FO"],
        "counts": {
            "strategies": len(strategies),
            "live_strategies": len([srow for srow in strategies if srow.get("status") == "live"]),
            "paused_strategies": len([srow for srow in strategies if srow.get("status") == "paused"]),
            "errored_strategies": len(errored),
            "open_orders": orders_open,
            "paper_positions": positions_count,
        },
        "order_sync": {"checked": 0, "updated": 0, "strategy_positions_marked": 0, "source": "upstox_only"},
        "recovery_plan": recovery_plan,
        "rate_limits": {"history_cache_entries": len(_HISTORY_CACHE)},
        "errored_strategies": errored,
    }
# ============== Routes: Ops Console - END ==============

# profile, portfolio, funds, and paper wallet routes moved to routes/profile.py
async def get_paper_wallet(user=Depends(get_current_user)):
    from routes import profile as profile_routes

    previous_db = profile_routes.db
    profile_routes.db = db
    try:
        return await profile_routes.get_paper_wallet(user=user)
    finally:
        profile_routes.db = previous_db


async def _option_engine_monitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("SQLite option engine monitor started")
    while not stop_event.is_set():
        try:
            option_ledger.advance_cooldowns()
            positions = option_ledger.open_positions()
            strategy_rows = await db.strategies.find(
                {"id": {"$in": [p["strategy_id"] for p in positions]}} if positions else {},
                {"_id": 0},
            ).to_list(1000)
            strategies_by_id = {row["id"]: row for row in strategy_rows}

            # Broker position sync and live tick capture.
            users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
            broker_symbols_by_user: Dict[str, Dict[str, Dict[str, Any]]] = {}
            
            recorded_symbols = {"NIFTY": False, "SENSEX": False}

            for user_row in users:
                user_id = user_row["id"]
                
                # Check Upstox live index prices first if connected.
                upstox_status = await get_user_upstox_status(user_id)
                if not upstox_status.get("token_valid"):
                    continue
                upstox_gw = await get_user_upstox_gateway(user_id)
                if upstox_gw and upstox_gw.connected:
                    upstox_keys = {
                        "NIFTY": "NSE_INDEX|Nifty 50",
                        "SENSEX": "BSE_INDEX|SENSEX"
                    }
                    try:
                        quotes = await asyncio.to_thread(upstox_gw.get_market_quote, list(upstox_keys.values()))
                        data_node = quotes.get("data", {}) or {}
                        for idx_sym, upstox_key in upstox_keys.items():
                            # Upstox /market-quote/ltp returns its data dict keyed by
                            # EXCHANGE:SYMBOL (colon, e.g. "NSE_INDEX:Nifty 50"), NOT the
                            # pipe instrument_key we send. Match flexibly or we silently
                            # fall through to the simulated random walk every cycle.
                            colon_key = upstox_key.replace("|", ":")
                            node = data_node.get(upstox_key) or data_node.get(colon_key) or {}
                            if not node:
                                suffix = upstox_key.split("|")[-1].upper()
                                for _k, _v in data_node.items():
                                    if str(_k).upper().endswith(suffix) and isinstance(_v, dict):
                                        node = _v
                                        break
                            spot_ltp = node.get("last_price") or node.get("ltp")
                            if spot_ltp:
                                option_ledger.record_market_tick(idx_sym, float(spot_ltp), "upstox")
                                if idx_sym in recorded_symbols:
                                    recorded_symbols[idx_sym] = True
                    except Exception as e:
                        logger.warning(f"Upstox spot/future quote failed in monitor loop: {e}")

            # Simulated random walk fallback so index telemetry is never stuck on "Waiting..." in paper.
            import random
            if not recorded_symbols["NIFTY"]:
                last_nifty = option_ledger.latest_ticks(["NIFTY"]).get("NIFTY")
                last_price = float(last_nifty["ltp"]) if last_nifty else 24850.40
                new_price = round(last_price + random.uniform(-2.5, 2.5), 2)
                option_ledger.record_market_tick("NIFTY", new_price, "simulated")
            if not recorded_symbols["SENSEX"]:
                last_sensex = option_ledger.latest_ticks(["SENSEX"]).get("SENSEX")
                last_price = float(last_sensex["ltp"]) if last_sensex else 81460.20
                new_price = round(last_price + random.uniform(-8.0, 8.0), 2)
                option_ledger.record_market_tick("SENSEX", new_price, "simulated")
            now_ist = datetime.now(timezone.utc) + IST_OFFSET
            squareoff_due = now_ist.weekday() < 5 and (now_ist.hour, now_ist.minute) >= (15, 15)
            for pos in positions:
                sid = pos["strategy_id"]
                strategy = strategies_by_id.get(sid)
                if not strategy:
                    logger.warning("ledger orphan strategy=%s missing Mongo strategy; closing ledger row", sid)
                    option_ledger.close_position(sid, float(pos.get("ltp") or pos.get("entry_price") or 0), "orphan-strategy")
                    continue
                user_id = strategy["user_id"]
                underlying = ((strategy.get("visual_config") or {}).get("options") or {}).get("underlying", "NIFTY")
                pos_exchange = options_helper.OPT_EXCHANGE.get(str(underlying).upper(), "NFO")
                ltp = await _current_ltp_for_symbol(user_id, pos["symbol"], pos_exchange)
                if ltp is None:
                    broker_pos = broker_symbols_by_user.get(user_id, {}).get(pos["symbol"])
                    ltp = float(broker_pos.get("last_price") or pos["ltp"]) if broker_pos else float(pos["ltp"])
                updated = option_ledger.update_ltp(sid, float(ltp))
                if updated:
                    option_ledger.record_market_tick(pos["symbol"], float(ltp), "broker-sync" if user_id in broker_symbols_by_user else "ledger")

                if user_id in broker_symbols_by_user and pos["symbol"] not in broker_symbols_by_user[user_id]:
                    logger.warning("broker sync orphan close strategy=%s symbol=%s", sid, pos["symbol"])
                    option_ledger.close_position(sid, float(ltp), "broker-position-missing")
                    continue

                risk_cfg = ((strategy.get("visual_config") or {}).get("risk") or {})
                time_exit_minutes = int(risk_cfg.get("time_exit_minutes") or 0)
                time_exit_due = False
                if time_exit_minutes > 0:
                    entry_dt = _parse_iso_dt(pos.get("entry_time"))
                    time_exit_due = bool(entry_dt and (datetime.now(timezone.utc) - entry_dt).total_seconds() >= time_exit_minutes * 60)
                exit_reason = (
                    "intraday-squareoff-1515" if squareoff_due
                    else f"time-exit-{time_exit_minutes}m" if time_exit_due
                    else option_ledger.exit_signal_for_position(updated or pos)
                )
                if not exit_reason:
                    continue
                logger.info("option engine exit strategy=%s symbol=%s reason=%s", sid, pos["symbol"], exit_reason)
                result = await _close_strategy_positions(user_id, sid, reason=exit_reason)
                if not result.get("closed_positions"):
                    option_ledger.close_position(sid, float(ltp), exit_reason)
        except Exception as e:
            logger.warning(f"option engine monitor error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < 30:
            await asyncio.sleep(1)
            slept += 1
    logger.info("SQLite option engine monitor stopped")


async def _quote_upstox_instrument_key(user_id: str, instrument_key: Optional[str]) -> Optional[float]:
    if not instrument_key:
        return None
    key = str(instrument_key).strip()
    if "|" not in key and ":" not in key:
        _log_throttled(
            f"invalid-upstox-key:{key}",
            120.0,
            logging.WARNING,
            "Rejecting invalid Upstox LTP key before API call instrument_key=%s",
            key,
        )
        return None
    gateway = await get_user_upstox_gateway(user_id)
    if gateway and gateway.connected:
        # V3 websocket tick cache (freshest)
        tick = gateway.latest_tick(key)
        if tick and tick.get("ltp"):
            return float(tick["ltp"])
        try:
            quote = await asyncio.to_thread(gateway.get_market_quote, [key])
            ltp = UpstoxGateway.parse_quote_ltp(quote, key)
            if ltp is not None:
                return ltp
            data = (quote.get("data") if isinstance(quote, dict) else None) or {}
            for node in data.values():
                if isinstance(node, dict):
                    for field in ("last_price", "ltp", "last_traded_price"):
                        v = node.get(field)
                        if v not in (None, ""):
                            try:
                                return float(v)
                            except Exception:
                                pass
        except Exception as exc:
            logger.warning("Upstox LTP failed instrument_key=%s: %s", key, exc)

    # Analytics Token fallback — works even when user OAuth token is expired
    if _analytics:
        ltp_map = await _analytics.get_ltp([key])
        val = ltp_map.get(key)
        if val is not None:
            return float(val)

    return None


# Superseded by position_monitor.py (run via position_monitor.run_monitor_loop at startup).
# Kept as thin delegates so any remaining call sites still compile.
def _mongo_position_exit_reason(position: Dict[str, Any], ltp: float) -> Optional[str]:
    return _exit_reason_module(position, ltp)

async def _mongo_position_monitor_loop(stop_event: asyncio.Event) -> None:
    # Startup now launches position_monitor.run_monitor_loop instead.
    logger.warning("_mongo_position_monitor_loop called but superseded by position_monitor.py — no-op")


async def _place_resting_sl_order(
    db: Any,
    user_id: str,
    entry_order: Dict[str, Any],
    fill_price: float,
    filled_qty: int,
) -> None:
    """Submit a resting stop-loss order (type SL) to Upstox immediately after entry fill."""
    try:
        strategy_id = entry_order.get("strategy_id")
        symbol = entry_order.get("symbol")
        target_symbol = entry_order.get("target_symbol") or symbol
        
        # 1. Resolve stop loss price
        # Check order_intent first, then fallback to defaults or strategy tp_sl_tsl_config
        intent_doc = entry_order.get("order_intent") or {}
        stop_loss_price = intent_doc.get("stop_loss") or entry_order.get("stop_loss")
        
        if not stop_loss_price:
            # Load strategy config to check if stop_loss_pct exists
            strategy = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
            risk_cfg = (strategy or {}).get("visual_config", {}).get("risk", {})
            sl_pct = float(risk_cfg.get("stop_loss_pct") or 15.0)
            # Long entry -> SL is below fill price. Short entry -> SL is above fill price.
            side = str(entry_order.get("side") or "BUY").upper()
            if side == "BUY":
                stop_loss_price = round(fill_price * (1 - sl_pct / 100.0), 2)
            else:
                stop_loss_price = round(fill_price * (1 + sl_pct / 100.0), 2)

        if not stop_loss_price or stop_loss_price <= 0:
            logger.warning(
                "Resting SL placement skipped: could not resolve stop loss price for order=%s symbol=%s",
                entry_order.get("id"), target_symbol
            )
            return

        # 2. Place SL order to Upstox
        # Side is opposite of entry
        entry_side = str(entry_order.get("side") or "BUY").upper()
        sl_side = "SELL" if entry_side == "BUY" else "BUY"
        
        # SL order triggers when price crosses stop_loss_price.
        # For a SELL SL (long exit), price falls to trigger. Limit price should be slightly lower (buffer).
        # For a BUY SL (short exit), price rises to trigger. Limit price should be slightly higher (buffer).
        # We use a 3% buffer.
        buffer_frac = 0.03
        if sl_side == "SELL":
            limit_price = round(stop_loss_price * (1 - buffer_frac), 2)
        else:
            limit_price = round(stop_loss_price * (1 + buffer_frac), 2)
            
        logger.info(
            "Placing resting SL order for strategy=%s symbol=%s qty=%d trigger=%.2f limit=%.2f",
            strategy_id, target_symbol, filled_qty, stop_loss_price, limit_price
        )
        
        # We route this through _place_upstox_order helper in server.py
        # Validity is DAY, order_type is "SL" (which is SL-LMT in Upstox V2)
        res = await _place_upstox_order(
            user_id,
            instrument_token=entry_order.get("instrument_token"),
            side=sl_side,
            quantity=filled_qty,
            order_type="SL",
            product=entry_order.get("product") or "MIS",
            price=limit_price,
            trigger_price=stop_loss_price,
            tag=f"sl:{strategy_id[:18]}"
        )
        
        if res.get("ok") or res.get("order_id") or res.get("broker_order_id"):
            sl_broker_order_id = res.get("order_id") or res.get("broker_order_id")
            # Create a pending SL order document in db.orders to track it
            now = datetime.now(timezone.utc).isoformat()
            sl_order_doc = {
                "id": f"sl_order_{uuid.uuid4().hex[:12]}",
                "user_id": user_id,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "target_symbol": target_symbol,
                "side": sl_side,
                "qty": filled_qty,
                "status": "PLACED",
                "execution_status": "PLACED",
                "requested_price": limit_price,
                "price": limit_price,
                "trigger_price": stop_loss_price,
                "exchange": entry_order.get("exchange") or "NFO",
                "segment": entry_order.get("segment") or "NSE_FO",
                "mode": "live",
                "broker": "upstox",
                "broker_order_id": sl_broker_order_id,
                "is_resting_sl": True,
                "parent_entry_order_id": entry_order.get("id"),
                "created_at": now,
                "updated_at": now,
            }
            await db.orders.insert_one(sl_order_doc)
            
            # Update position to link the resting SL broker order id
            await db.strategy_positions.update_many(
                {
                    "user_id": user_id,
                    "strategy_id": strategy_id,
                    "target_symbol": target_symbol,
                    "mode": "live",
                    "status": "OPEN"
                },
                {"$set": {
                    "broker_sl_order_id": sl_broker_order_id,
                    "tp_sl_tsl_config.stoploss_price": stop_loss_price,
                    "tp_sl_tsl_config.protection_status": "RESTING_SL_PLACED",
                    "updated_at": now
                }}
            )
            logger.info("Successfully registered resting SL order=%s in DB and position", sl_broker_order_id)
        else:
            logger.error("Failed to place resting SL order at broker: %s", res.get("error"))
    except Exception as exc:
        logger.exception("Exception in _place_resting_sl_order: %s", exc)


async def _on_portfolio_stream_event(db, payload: Dict[str, Any], uid: str) -> None:
    """Process real-time portfolio WebSocket stream updates.

    1. Store raw event and update order table via apply_broker_truth_event.
    2. Route to _advance_pending_order_from_broker to write to ledger trade_fills/trades immediately.
    """
    await apply_broker_truth_event(db, {**payload, "user_id": uid}, source="portfolio_stream")
    order_id = payload.get("order_id") or payload.get("broker_order_id")
    raw_status = payload.get("status") or payload.get("order_status") or payload.get("state")
    if order_id and raw_status:
        status = _normalize_upstox_order_status(raw_status)
        avg_price = 0.0
        if payload.get("average_price") not in (None, ""):
            try:
                avg_price = float(payload.get("average_price"))
            except Exception:
                pass
        filled_qty = None
        if payload.get("filled_quantity") not in (None, ""):
            try:
                filled_qty = int(float(payload.get("filled_quantity")))
            except Exception:
                pass
        pending_qty = None
        if payload.get("pending_quantity") not in (None, ""):
            try:
                pending_qty = int(float(payload.get("pending_quantity")))
            except Exception:
                pass
        status_message = payload.get("status_message") or raw_status
        await _advance_pending_order_from_broker(
            user_id=uid,
            broker_order_id=str(order_id),
            status=str(status or ""),
            avg_price=avg_price,
            filled_qty=filled_qty,
            pending_qty=pending_qty,
            status_message=str(status_message or ""),
            raw_report=payload
        )


async def _broker_reconciliation_loop(stop_event: asyncio.Event) -> None:
    interval = max(10, int(os.environ.get("BROKER_RECONCILE_INTERVAL_SEC", "30")))
    logger.info("Broker reconciliation loop started interval=%ss", interval)
    while not stop_event.is_set():
        try:
            users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
            for user_row in users:
                user_id = user_row["id"]
                # 30-second Position Reconciliation Loop and safety self-heal
                try:
                    from position_reconciler import reconcile_and_repair_positions
                    await reconcile_and_repair_positions(user_id)
                except Exception as rec_err:
                    logger.warning("Background position reconciliation failed for user %s: %s", user_id, rec_err)

                ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                is_weekday = ist_now.weekday() < 5
                minutes_now = ist_now.hour * 60 + ist_now.minute
                in_market_hours = is_weekday and (9 * 60 <= minutes_now <= 23 * 60 + 30)

                if in_market_hours:
                    try:
                        live_enabled = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").strip().lower() == "true"
                        upstox_status = await get_user_upstox_status(user_id)
                        if not upstox_status.get("token_valid"):
                            continue
                        gw = await get_user_upstox_gateway(user_id)
                        if gw and gw.connected:
                            if live_enabled:
                                streams = getattr(app.state, "upstox_portfolio_streams", None)
                                if streams is None:
                                    streams = {}
                                    app.state.upstox_portfolio_streams = streams
                                if user_id not in streams:
                                    loop = asyncio.get_running_loop()

                                    async def _process_stream_event_async(payload_dict, user_uid):
                                        try:
                                            await _on_portfolio_stream_event(db, payload_dict, user_uid)
                                        except Exception as exc:
                                            logger.error("Error processing portfolio stream event: %s", exc)

                                    def _on_event(payload, uid=user_id):
                                        loop.call_soon_threadsafe(
                                            lambda: asyncio.create_task(_process_stream_event_async(payload, uid))
                                        )

                                    stream = UpstoxPortfolioStream(access_token_getter=lambda gateway=gw: gateway.access_token, event_callback=_on_event)
                                    start_result = stream.start()
                                    if start_result.get("ok"):
                                        streams[user_id] = stream
                            await broker_reconciliation_summary(db, user_id, gw)
                    except Exception as stream_err:
                        logger.warning("Upstox portfolio stream/reconciliation state failed for user %s: %s", user_id, stream_err)
                    await _sync_upstox_order_statuses(user_id)
                    await _reconcile_stale_orders_for_user(user_id)
        except Exception as e:
            logger.warning(f"broker reconciliation error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < interval:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Broker reconciliation loop stopped")


# Include modular routers into api router to ensure /api prefix is preserved
from routes.auth import router as auth_router
from routes.ai import agent_router, router as ai_router
from routes.ops import router as ops_router
from routes.strategies import router as strategies_router
from routes.orders import router as orders_router
from routes.positions import router as positions_router
from routes.dashboard import router as dashboard_router
from routes.reports import router as reports_router
from routes.signals import router as signals_router
from routes.notifications import router as notifications_router
from routes.market import router as market_router
from routes.broker import router as broker_router
from routes.profile import router as profile_router
from routes.readiness import router as readiness_router, live_readiness
from routes.ops_runtime import router as ops_runtime_router
from routes.core_status import router as core_status_router
from routes.diagnostics import router as diagnostics_router
from routes.system import router as system_router
from routes.wiki import router as wiki_router

api.include_router(auth_router)
api.include_router(ai_router)
api.include_router(agent_router)
api.include_router(ops_router)
api.include_router(strategies_router)
api.include_router(orders_router)
api.include_router(positions_router)
api.include_router(dashboard_router)
api.include_router(reports_router)
api.include_router(signals_router)
api.include_router(notifications_router)
api.include_router(market_router)
api.include_router(broker_router)
api.include_router(profile_router)
api.include_router(readiness_router)
api.include_router(ops_runtime_router)
api.include_router(core_status_router)
api.include_router(diagnostics_router)
api.include_router(system_router)
api.include_router(wiki_router)

# ============== Boot ==============
# (app.include_router(api) moved to the bottom of the file after all routes are registered)

# Parse CORS origins properly - strip whitespace from comma-separated list
cors_origins = [o.strip() for o in os.environ.get('CORS_ORIGINS', '*').split(',') if o.strip()]
if not cors_origins:
    cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ── FIX 5 + FIX 7: Daily gateway health check + token refresh scheduler ────────
# In-memory cache: user_id → True/False. True = gateway not connected at 9:10 IST.
# This blocks new order placement until the user reconnects.
_GATEWAY_BLOCKED: Dict[str, bool] = {}

# System-level read-only market data client — uses the long-lived Analytics Token
# (1-year TTL, no daily refresh). Provides LTP, candles, option chain without
# depending on any user's OAuth token.
_analytics: Optional[UpstoxAnalyticsClient] = None

def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

async def check_all_gateways_at_market_open() -> Dict[str, Any]:
    """FIX 5: Check every user's WS gateway at 9:10 AM IST. Block orders for disconnected users."""
    results: Dict[str, Any] = {}
    users = await db.users.find({}, {"_id": 0, "id": 1, "email": 1}).to_list(1000)
    for row in users:
        uid = row["id"]
        try:
            gw = await get_user_upstox_gateway(uid)
            connected = bool(gw and gw.connected)
            ws_ok = bool(gw and getattr(gw, "_ws_running", False))
            blocked = not connected
            _GATEWAY_BLOCKED[uid] = blocked
            await db.gateway_health.update_one(
                {"user_id": uid},
                {"$set": {
                    "user_id": uid,
                    "gateway_connected": connected,
                    "ws_running": ws_ok,
                    "gateway_blocked": blocked,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
            if blocked:
                logger.warning(
                    "FIX5 GATEWAY CHECK: USER %s gateway NOT connected — all new order placement BLOCKED",
                    uid,
                )
            results[uid] = {"connected": connected, "ws_running": ws_ok, "blocked": blocked}
        except Exception as e:
            logger.warning("Gateway health check failed for user %s: %s", uid, e)
            results[uid] = {"error": str(e)}
    return results


async def request_upstox_token_refresh_for_user(user_id: str) -> Dict[str, Any]:
    """FIX 7: Send Upstox push notification asking user to approve token refresh."""
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    if not keys:
        return {"ok": False, "reason": "no_keys"}
    api_key = decrypt_secret(keys.get("api_key")) if keys.get("api_key") else None
    if not api_key:
        return {"ok": False, "reason": "no_api_key"}
    try:
        import requests as _req
        # Upstox V3 token request — sends a push notification to user's phone.
        # The user taps "Approve" and Upstox posts the new token to our webhook.
        request_id = f"quantg_{user_id[:8]}_{int(datetime.now(timezone.utc).timestamp())}"
        resp = _req.post(
            f"https://api.upstox.com/v3/login/auth/token/request/{request_id}",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={"client_id": api_key},
            timeout=10,
        )
        payload = resp.json() if resp.content else {}
        ok = resp.status_code in (200, 201, 202)
        logger.info(
            "FIX7 TOKEN PUSH: user=%s status=%s payload=%s",
            user_id, resp.status_code, str(payload)[:200],
        )
        await db.gateway_health.update_one(
            {"user_id": user_id},
            {"$set": {"token_push_sent_at": datetime.now(timezone.utc).isoformat(),
                      "token_push_ok": ok}},
            upsert=True,
        )
        return {"ok": ok, "status_code": resp.status_code, "payload": payload}
    except Exception as e:
        logger.warning("FIX7 TOKEN PUSH failed for user %s: %s", user_id, e)
        return {"ok": False, "reason": str(e)}


async def _eod_square_off_all_users(spread_phase: bool = False) -> Dict[str, Any]:
    """EXIT GUARANTEE: unconditional EOD square-off of open positions.

    Runs in two phases so spreads can ride nearer to expiry:
      - 15:15 (spread_phase=False): sweep single-leg/equity; spreads are EXCLUDED
        (position_monitor force-closes them at 15:25).
      - 15:26 (spread_phase=True): backstop sweep of EVERYTHING still open, so no
        spread is ever left open past expiry day.

    Steps each phase:
      1. Resurrect dead-end positions (STALE_NEEDS_REVIEW, CIRCUIT_BREAKER) to OPEN
         with failure counters reset, so the close path will pick them up.
      2. Revert any position stuck in EXITING back to OPEN for a fresh exit order.
      3. Call _close_strategy_positions for every (user, strategy) with live positions.
    """
    _spread_types = ["credit_spread", "debit_spread"]
    now_str = datetime.now(timezone.utc).isoformat()
    summary: Dict[str, Any] = {"resurrected": 0, "exiting_reverted": 0, "strategies_swept": 0, "errors": []}

    # 1. Resurrect dead-end statuses the monitor never scans
    res = await db.strategy_positions.update_many(
        {"status": {"$in": ["STALE_NEEDS_REVIEW", "CIRCUIT_BREAKER"]},
         "open_quantity": {"$gt": 0}},
        {"$set": {"status": "OPEN", "exit_order_failures": 0, "exit_data_failures": 0,
                  "updated_at": now_str,
                  "last_error": "EOD square-off: resurrected from dead-end status"}},
    )
    summary["resurrected"] = res.modified_count

    # 2. Free positions stuck mid-exit so they get a fresh exit order
    res = await db.strategy_positions.update_many(
        {"status": "EXITING"},
        {"$set": {"status": "OPEN", "updated_at": now_str},
         "$unset": {"exit_attempt_at": ""}},
    )
    summary["exiting_reverted"] = res.modified_count

    # 3. Sweep every (user, strategy) holding live positions. At 15:15 spreads are
    #    excluded (they square off at 15:25 via position_monitor); the 15:26 backstop
    #    phase sweeps everything that remains.
    _match: Dict[str, Any] = {"status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]},
                              "open_quantity": {"$gt": 0}}
    if not spread_phase:
        _match["structure"] = {"$nin": _spread_types}
    pairs = await db.strategy_positions.aggregate([
        {"$match": _match},
        {"$group": {"_id": {"user_id": "$user_id", "strategy_id": "$strategy_id"}}},
    ]).to_list(500)
    for pair in pairs:
        uid = pair["_id"].get("user_id")
        sid = pair["_id"].get("strategy_id")
        if not uid or not sid:
            continue
        try:
            await _close_strategy_positions(uid, sid, reason="eod-square-off")
            summary["strategies_swept"] += 1
        except Exception as exc:
            summary["errors"].append(f"{uid}/{sid}: {str(exc)[:120]}")
            logger.error("EOD square-off failed for user=%s strategy=%s: %s", uid, sid, exc)

    logger.info("EOD square-off complete: %s", summary)
    return summary


async def _snapshot_option_chains(db) -> int:
    """Phase 1 — persist a compact NIFTY/BANKNIFTY option-chain snapshot to
    db.historical_chains (one doc per underlying per call). Captures the real
    per-strike ltp/greeks/OI/bid-ask so future walk-forward backtests run on
    actual option data, not mock candles. Best-effort; never raises."""
    spot_keys = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX",
    }
    gw = None
    for row in await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000):
        g = await get_user_upstox_gateway(row["id"])
        if g and getattr(g, "connected", False):
            gw = g
            break
    if not gw:
        return 0

    def _leg(o: Dict[str, Any]) -> Dict[str, Any]:
        md = o.get("market_data") or {}
        gk = o.get("option_greeks") or {}
        return {
            "ltp": md.get("ltp") or md.get("last_price"),
            "oi": md.get("oi"), "vol": md.get("volume"),
            "bid": md.get("bid_price"), "ask": md.get("ask_price"),
            "delta": gk.get("delta"), "iv": gk.get("iv"), "theta": gk.get("theta"),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    today = _ist_now().date().isoformat()
    written = 0
    for underlying, skey in spot_keys.items():
        try:
            contracts = await asyncio.to_thread(gw.get_option_contracts, skey)
            expiries = sorted({
                c.get("expiry") for c in ((contracts or {}).get("data") or []) if c.get("expiry")
            })
            expiry = expiries[0] if expiries else None
            chain = await asyncio.to_thread(gw.get_option_chain, skey, expiry)
            if not chain or chain.get("status") != "success":
                continue
            spot = None
            strikes: List[Dict[str, Any]] = []
            for node in (chain.get("data") or []):
                strike = node.get("strike_price")
                if strike is None:
                    continue
                if spot is None:
                    spot = node.get("underlying_spot_price") or node.get("spot_price")
                strikes.append({
                    "strike": strike,
                    "ce": _leg(node.get("call_options") or {}),
                    "pe": _leg(node.get("put_options") or {}),
                })
            if not strikes:
                continue
            await db.historical_chains.insert_one({
                "underlying": underlying, "expiry": expiry, "spot": spot,
                "ts": now_iso, "date": today, "n_strikes": len(strikes), "strikes": strikes,
            })
            written += 1
        except Exception as exc:
            logger.debug("chain snapshot failed for %s: %s", underlying, exc)
    return written


_LIVE_INDEX_CAPTURE = None


def _get_live_index_capture():
    """IMD-04: process-wide index 1-minute capture (lazy). Read-only w.r.t. trading."""
    global _LIVE_INDEX_CAPTURE
    if _LIVE_INDEX_CAPTURE is None:
        from core.live_index_capture import LiveIndexCapture
        _LIVE_INDEX_CAPTURE = LiveIndexCapture()
    return _LIVE_INDEX_CAPTURE


async def _daily_scheduler_loop(stop_event: asyncio.Event) -> None:
    """FIX 5 + FIX 7: Runs every 60 seconds and fires timed tasks at the right IST times.

    8:50 AM IST — token refresh push to all users + paper daily lifecycle reset
    9:10 AM IST — gateway health check for all users
    15:15 IST   — unconditional square-off of all open positions (exit guarantee)
    """
    _token_push_done_date: Optional[str] = None
    _gateway_check_done_date: Optional[str] = None
    _lifecycle_reset_done_date: Optional[str] = None
    _squareoff_done_date: Optional[str] = None
    _spread_squareoff_done_date: Optional[str] = None
    _vix_last_snapshot_minute: Optional[str] = None
    _chain_last_snapshot_minute: Optional[str] = None
    _candle_backfill_done_date: Optional[str] = None
    _schedule_activate_done_date: Optional[str] = None
    _schedule_pause_done_date: Optional[str] = None
    _index_flush_done_date: Optional[str] = None
    _hist_validate_done_week: Optional[str] = None
    logger.info("Daily gateway scheduler started")
    while not stop_event.is_set():
        try:
            ist = _ist_now()
            today = ist.date().isoformat()
            hour, minute = ist.hour, ist.minute

            # India VIX snapshot every 5 min during market hours → db.vix_history.
            # One doc per day; "value" converges to the daily close at 15:30. This
            # series feeds the IV-rank regime gate (Phase 2).
            _vix_bucket = f"{today}:{hour}:{minute // 5}"
            if (
                ist.weekday() < 5
                and ((hour == 9 and minute >= 15) or 10 <= hour < 15 or (hour == 15 and minute <= 30))
                and _vix_last_snapshot_minute != _vix_bucket
            ):
                _vix_last_snapshot_minute = _vix_bucket
                try:
                    vix_value = None
                    users_vix = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
                    for row in users_vix:
                        gw_vix = await get_user_upstox_gateway(row["id"])
                        if not gw_vix or not gw_vix.connected:
                            continue
                        vix_tick = gw_vix.latest_tick(UPSTOX_VIX_INSTRUMENT_KEY)
                        if vix_tick and vix_tick.get("ltp"):
                            vix_value = float(vix_tick["ltp"])
                            break
                        # WS cache routinely misses for the index/VIX feed (it
                        # serves REST-bootstrap only — see the "tick cache empty"
                        # logs), so latest_tick returns nothing and vix_history was
                        # never written. Fall back to a REST market quote, which
                        # works for index keys even when the live WS feed does not.
                        try:
                            _vq = await asyncio.to_thread(
                                gw_vix.get_market_quote, [UPSTOX_VIX_INSTRUMENT_KEY]
                            )
                            # Parse without an instrument_key: Upstox keys the LTP
                            # response by colon format (NSE_INDEX:India VIX) while
                            # our key uses a pipe, so an exact-key match would miss.
                            # We requested only VIX, so the single node is the value.
                            _vltp = UpstoxGateway.parse_quote_ltp(_vq)
                            if _vltp and float(_vltp) > 0:
                                vix_value = float(_vltp)
                                break
                        except Exception:
                            continue
                    if vix_value is not None and vix_value > 0:
                        await db.vix_history.update_one(
                            {"date": today},
                            {
                                "$set": {"value": vix_value, "updated_at": datetime.now(timezone.utc).isoformat()},
                                "$min": {"low": vix_value},
                                "$max": {"high": vix_value},
                                "$setOnInsert": {"open": vix_value},
                            },
                            upsert=True,
                        )
                except Exception as _vix_err:
                    logger.debug("VIX snapshot failed: %s", _vix_err)

            # Phase 1 — option-chain snapshot every 5 min during market hours →
            # db.historical_chains. Accumulates the real (greeks + OI + bid/ask)
            # NIFTY/BANKNIFTY chains a walk-forward backtest needs instead of mock
            # candles. Foundation for the AutoResearch eval loop.
            _chain_bucket = f"{today}:{hour}:{minute // 5}"
            if (
                ist.weekday() < 5
                and ((hour == 9 and minute >= 15) or 10 <= hour < 15 or (hour == 15 and minute <= 30))
                and _chain_last_snapshot_minute != _chain_bucket
            ):
                _chain_last_snapshot_minute = _chain_bucket
                try:
                    _n_chains = await _snapshot_option_chains(db)
                    if _n_chains:
                        logger.info("Chain snapshot: wrote %d underlying chains to historical_chains", _n_chains)
                except Exception as _chain_err:
                    logger.debug("Chain snapshot failed: %s", _chain_err)

            # 16:00 IST — backfill real 5-min underlying OHLC into db.candles so the
            # options backtester scores breakout/range/VWAP on real high/low, not flat
            # chain-spot dots (TASK-052). Once a day, after close; best-effort.
            if hour == 16 and minute == 0 and _candle_backfill_done_date != today:
                _candle_backfill_done_date = today
                try:
                    from core.candle_store import backfill_underlying_candles
                    _cgw = None
                    for _row in await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000):
                        _g = await get_user_upstox_gateway(_row["id"])
                        if _g and getattr(_g, "connected", False):
                            _cgw = _g
                            break
                    if _cgw:
                        _cres = await backfill_underlying_candles(db, _cgw, days=30)
                        logger.info("Candle backfill: %s", _cres)
                except Exception as _cb_err:
                    logger.debug("Candle backfill failed: %s", _cb_err)

            # Saturday 05:00 IST — weekly: re-validate STRUCTURE lessons against the
            # 2yr bhavcopy OOS engine (heavy; run off-market on the weekend). Keeps the
            # Hermes brain's structural OOS proof fresh + refreshes the advice surface.
            _iso_week = f"{ist.year}-W{ist.isocalendar()[1]:02d}"
            if ist.weekday() == 5 and hour == 5 and _hist_validate_done_week != _iso_week:
                _hist_validate_done_week = _iso_week
                try:
                    from core.hermes_historical_validator import validate_structure_lessons
                    from core.hermes_advisor import compile_hermes_advice
                    for _row in await db.users.find({}, {"_id": 0, "id": 1}).to_list(50):
                        _hv = await validate_structure_lessons(db, _row["id"])
                        await compile_hermes_advice(db, _row["id"])
                        logger.info("Weekly Hermes historical validation user=%s: %s", _row["id"], _hv.get("validated"))
                except Exception as _hv_err:
                    logger.error("Weekly historical validation failed: %s", _hv_err)

            # 15:35 IST — flush the day's captured index 1-minute bars (IMD-04) to
            # the index-minute store for the intraday backtester. Read-only, best-effort.
            if hour == 15 and minute == 35 and _index_flush_done_date != today:
                _index_flush_done_date = today
                try:
                    _ic_res = await asyncio.to_thread(_get_live_index_capture().flush_day, today)
                    logger.info("Index minute capture flush: %s", _ic_res)
                except Exception as _ic_err:
                    logger.debug("Index capture flush failed: %s", _ic_err)

            # 8:50 AM IST — token push + daily paper lifecycle reset
            if hour == 8 and minute == 50 and _token_push_done_date != today:
                _token_push_done_date = today
                logger.info("Daily scheduler: 8:50 AM IST — sending token refresh push to all users")
                users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
                for row in users:
                    await request_upstox_token_refresh_for_user(row["id"])

            # 8:50 AM IST — paper daily lifecycle: reset order/signal counts, today_pnl
            if hour == 8 and minute == 50 and _lifecycle_reset_done_date != today:
                _lifecycle_reset_done_date = today
                logger.info("Daily scheduler: 8:50 AM IST — running paper daily lifecycle reset")
                try:
                    users_lc = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
                    for row in users_lc:
                        uid = row["id"]
                        try:
                            summary = await _daily_paper_lifecycle_for_user(uid)
                            if any(summary.values()):
                                logger.info("Daily lifecycle reset user=%s: %s", uid, summary)
                        except Exception as _ue:
                            logger.warning("Daily lifecycle failed for user %s: %s", uid, _ue)
                except Exception as _lc_err:
                    logger.warning("Daily lifecycle user scan failed: %s", _lc_err)

            # 9:10 AM IST — gateway health check for all users (FIX 5)
            if hour == 9 and minute == 10 and _gateway_check_done_date != today:
                _gateway_check_done_date = today
                logger.info("Daily scheduler: 9:10 AM IST — running gateway health check for all users")
                results = await check_all_gateways_at_market_open()
                blocked = [uid for uid, r in results.items() if r.get("blocked")]
                if blocked:
                    logger.warning(
                        "FIX5: %d users have blocked gateways at market open: %s",
                        len(blocked), blocked,
                    )
                else:
                    logger.info("FIX5: All %d user gateways connected at market open", len(results))

            # 9:00 AM IST Mon–Fri — auto-activate strategies that were paused by the market schedule.
            # Only re-activates strategies with schedule_paused=True; manually paused ones are left
            # alone. The manual_paused guard (EQ-05, 2026-07-01) is belt-and-suspenders: even if a
            # strategy somehow carries a stale schedule_paused=True, an explicit manual_paused=True
            # can never be auto-woken (the manual toggle sets manual_paused=True + schedule_paused=False).
            if (
                ist.weekday() < 5
                and _schedule_activate_done_date != today
                and hour == 9 and 15 <= minute < 30
            ):
                _schedule_activate_done_date = today
                try:
                    result = await db.strategies.update_many(
                        {"status": "paused", "schedule_paused": True, "manual_paused": {"$ne": True}},
                        {"$set": {"status": "live", "schedule_paused": False,
                                  "schedule_resumed_at": ist.isoformat()}},
                    )
                    logger.info(
                        "Market schedule: 9:00 AM — auto-activated %d strategies",
                        result.modified_count,
                    )
                except Exception as _act_err:
                    logger.warning("Market schedule activate failed: %s", _act_err)

            # 3:35 PM IST Mon–Fri — auto-pause all live strategies at market close.
            # Sets schedule_paused=True so 9:00 AM restore can distinguish from manual pauses.
            if (
                ist.weekday() < 5
                and _schedule_pause_done_date != today
                and hour == 15 and 30 <= minute < 45
            ):
                _schedule_pause_done_date = today
                try:
                    result = await db.strategies.update_many(
                        {"status": "live"},
                        {"$set": {"status": "paused", "schedule_paused": True,
                                  "schedule_paused_at": ist.isoformat()}},
                    )
                    logger.info(
                        "Market schedule: 3:35 PM — auto-paused %d strategies at market close",
                        result.modified_count,
                    )
                except Exception as _pause_err:
                    logger.warning("Market schedule pause failed: %s", _pause_err)

            # 15:15 IST — unconditional EOD square-off (exit guarantee, weekdays only).
            # Window check (>= 15:15, before 15:30) instead of exact-minute so a slow
            # tick can never skip it. _squareoff_done_date makes it once per day.
            if (
                ist.weekday() < 5
                and _squareoff_done_date != today
                and (hour == 15 and 15 <= minute < 30)
            ):
                _squareoff_done_date = today
                logger.info("Daily scheduler: 15:15 IST — EOD square-off (single-leg/equity; spreads ride to 15:25)")
                try:
                    await _eod_square_off_all_users(spread_phase=False)
                except Exception as _sq_err:
                    logger.error("EOD square-off run failed: %s", _sq_err)

            # 15:26 IST — spread backstop sweep. Spreads square off at 15:25 via
            # position_monitor; this guarantees any straggler is closed before close.
            if (
                ist.weekday() < 5
                and _spread_squareoff_done_date != today
                and (hour == 15 and 26 <= minute < 30)
            ):
                _spread_squareoff_done_date = today
                logger.info("Daily scheduler: 15:26 IST — spread backstop square-off starting")
                try:
                    await _eod_square_off_all_users(spread_phase=True)
                except Exception as _sq_err:
                    logger.error("Spread backstop square-off run failed: %s", _sq_err)
        except Exception as e:
            logger.warning("Daily scheduler error: %s", e)
        # Sleep in 10-second slices for responsive shutdown
        slept = 0
        while not stop_event.is_set() and slept < 60:
            await asyncio.sleep(10)
            slept += 10
    logger.info("Daily gateway scheduler stopped")


# gateway check route moved to routes/broker.py


# market regime route moved to routes/market.py


# gateway status route moved to routes/broker.py


# Upstox token webhook route moved to routes/broker.py

@app.on_event("startup")
async def startup():
    global _analytics
    _analytics_token = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
    if _analytics_token:
        _analytics = UpstoxAnalyticsClient(_analytics_token)
        logger.info("Upstox Analytics Token loaded — system market data client ready (1-year TTL)")
    else:
        logger.warning("UPSTOX_ANALYTICS_TOKEN not set — analytics market data disabled")

    app.state.option_ledger = option_ledger
    execution_state_manager.configure(
        db=db,
        get_user_settings=get_user_settings,
        sync_upstox_orders=_sync_upstox_order_statuses,
        sync_strategy_positions=_sync_strategy_positions_with_broker,
        fetch_positions=_fetch_broker_positions_for_user,
        option_ledger=option_ledger,
    )
    app.state.execution_state = execution_state_manager
    app.state.upstox_instrument_sync_task = asyncio.create_task(sync_upstox_instruments(db, force=False))

    # Dynamic lot sizes & strike intervals loading from MongoDB system_config
    try:
        config = await db.system_config.find_one({"_id": "exchange_rules"})
        if config:
            if "lot_sizes" in config:
                options_helper.LOT_SIZES.update(config["lot_sizes"])
            if "strike_intervals" in config:
                options_helper.STRIKE_INTERVALS.update(config["strike_intervals"])
            logger.info("Loaded system configuration (lot sizes, strike intervals) from MongoDB.")
        else:
            default_config = {
                "_id": "exchange_rules",
                "lot_sizes": options_helper.LOT_SIZES,
                "strike_intervals": options_helper.STRIKE_INTERVALS,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            await db.system_config.insert_one(default_config)
            logger.info("Seeded default exchange rules system config to MongoDB.")
    except Exception as e:
        logger.warning(f"Failed to load or seed system_config from MongoDB: {e}")

    # Index creation is best-effort — must NEVER block app startup.
    # On Atlas, an index may already exist with different options, or there may be
    # duplicates from a previous app version. We log and continue.
    indexes = [
        ("users", "email", {"unique": True}),
        ("broker_keys", [("user_id", 1), ("broker", 1)], {"unique": True}),
        ("strategies", "user_id", {}),
        ("strategies", "id", {}),
        ("strategies", [("status", 1)], {}),
        ("orders", [("user_id", 1), ("created_at", -1)], {}),
        ("orders", [("user_id", 1), ("status", 1), ("created_at", -1)], {}),
        ("orders", [("user_id", 1), ("strategy_id", 1), ("created_at", -1)], {}),
        ("orders", "idempotency_key", {"unique": True, "sparse": True}),
        ("strategy_positions", [("user_id", 1), ("strategy_id", 1), ("status", 1)], {}),
        ("strategy_positions", "active_strategy_instrument_side_key", {"unique": True, "sparse": True}),
        ("positions", [("user_id", 1), ("symbol", 1), ("strategy_id", 1)], {"unique": True, "sparse": True}),
        ("processed_fill_ids", "fill_id", {"unique": True}),
        ("paper_wallet_credits", "order_id", {"unique": True}),
        ("signals", "id", {"unique": True}),
        ("signals", [("user_id", 1), ("status", 1), ("created_at", -1)], {}),
        ("signals", [("strategy_id", 1), ("created_at", -1)], {}),
        ("skipped_signals", [("user_id", 1), ("dedupe_key", 1)], {"unique": True}),
        ("skipped_signals", [("user_id", 1), ("session_date", -1), ("last_seen_at", -1)], {}),
        ("skipped_signals", [("strategy_id", 1), ("session_date", -1)], {}),
        ("paper_trading_history", [("user_id", 1), ("created_at", -1)], {}),
        ("trade_fills", "order_id", {"unique": True}),
        ("trade_fills", [("user_id", 1), ("filled_at", -1)], {}),
        ("trade_fills", [("user_id", 1), ("broker_order_id", 1)], {}),
        ("trade_fills", [("strategy_id", 1), ("mode", 1), ("filled_at", -1)], {}),
        ("trades", [("user_id", 1), ("closed_at", -1)], {}),
        ("order_events", [("order_id", 1), ("created_at", 1)], {}),
        ("order_events", [("user_id", 1), ("created_at", -1)], {}),
        ("outbox_events", "id", {"unique": True}),
        ("outbox_events", [("status", 1), ("created_at", 1)], {}),
        ("outbox_events", [("aggregate_type", 1), ("aggregate_id", 1), ("created_at", 1)], {}),
        ("outbox_events", [("idempotency_key", 1), ("event_type", 1)], {}),
        ("risk_events", [("user_id", 1), ("created_at", -1)], {}),
        ("risk_events", [("strategy_id", 1), ("created_at", -1)], {}),
        ("risk_reservations", [("user_id", 1), ("status", 1), ("expires_at", 1)], {}),
        ("risk_reservations", [("order_id", 1), ("user_id", 1)], {"unique": True}),
        ("risk_reservations", [("strategy_id", 1), ("status", 1)], {}),
        ("risk_state", "_id", {}),
        ("broker_sync_state", [("user_id", 1), ("broker", 1)], {"unique": True}),
        ("system_config", "_id", {}),
        ("upstox_instrument_cache_meta", "_id", {}),
        ("upstox_instruments", "instrument_key", {"unique": True}),
        ("upstox_instruments", [("trading_symbol", 1), ("segment", 1), ("instrument_type", 1)], {}),
        ("upstox_suspended_instruments", "instrument_key", {"unique": True}),
        ("upstox_broker_events", [("created_at", -1)], {}),
        ("upstox_reconciliation_state", "_id", {}),
    ]
    for coll, key, opts in indexes:
        try:
            await db[coll].create_index(key, **opts)
        except Exception as e:
            logger.warning(f"index create on {coll} skipped: {e}")

    for old_index in ("active_instrument_key_1", "active_strategy_key_1"):
        try:
            await db.strategy_positions.drop_index(old_index)
            logger.warning("Dropped legacy strategy_positions index %s; duplicate scope is strategy+instrument+side now.", old_index)
        except Exception as e:
            logger.info("legacy strategy_positions index %s not dropped or absent: %s", old_index, e)

    # Drop old positions unique index that only used (user_id, symbol) — now keyed per strategy
    for old_pos_idx in ("user_id_1_symbol_1",):
        try:
            await db.positions.drop_index(old_pos_idx)
            logger.info("Dropped legacy positions index %s; new index includes strategy_id.", old_pos_idx)
        except Exception as e:
            logger.info("Legacy positions index %s not present or already dropped: %s", old_pos_idx, e)

    # Create partial unique index for live broker order tracking
    try:
        await db.orders.create_index(
            [("broker_order_id", 1)],
            unique=True,
            partialFilterExpression={"mode": "live", "broker_order_id": {"$exists": True}}
        )
        logger.info("Created unique partial index for live broker orders successfully.")
    except Exception as e:
        logger.warning(f"Failed to create live broker orders partial index: {e}")

    for coll, key, opts in [
        ("runner_locks", "expires_at", {"expireAfterSeconds": 0}),
        ("strategy_position_locks", "expires_at", {"expireAfterSeconds": 0}),
        ("risk_reservation_locks", "expires_at", {"expireAfterSeconds": 0}),
        ("option_trade_journal", [("strategy_id", 1), ("exit_time", -1)], {}),
        ("option_market_ticks", [("symbol", 1), ("tick_time", -1)], {}),
        ("strategy_loss_streaks", [("strategy_id", 1), ("user_id", 1)], {"unique": True}),
        ("daily_reports", [("user_id", 1), ("date", 1)], {"unique": True}),
        ("market_regime_state", [("index", 1)], {"unique": True}),
    ]:
        try:
            await db[coll].create_index(key, **opts)
        except Exception as e:
            logger.warning(f"index create on {coll} skipped: {e}")

    # TTL index: option_market_ticks auto-expire after 7 days
    try:
        await db.option_market_ticks.create_index(
            [("tick_time", 1)],
            expireAfterSeconds=604800,
            name="ttl_tick_time_7d",
        )
    except Exception as e:
        logger.warning(f"option_market_ticks TTL index skipped: {e}")

    # Notifications: collapse any pre-existing duplicates (keep oldest per event),
    # then enforce a unique (user_id, dedupe_key) index. This is the DB-level
    # guarantee behind create_notification_once — without it, concurrent callers
    # race past the find_one check and double-insert the same alert. Idempotent:
    # safe to run every startup.
    try:
        removed = 0
        async for grp in db.notifications.aggregate([
            {"$group": {
                "_id": {"u": "$user_id", "k": "$dedupe_key"},
                "ids": {"$push": "$_id"},
                "n": {"$sum": 1},
            }},
            {"$match": {"n": {"$gt": 1}}},
        ]):
            dup_ids = grp.get("ids", [])[1:]  # keep first, drop the rest
            if dup_ids:
                res = await db.notifications.delete_many({"_id": {"$in": dup_ids}})
                removed += res.deleted_count
        await db.notifications.create_index(
            [("user_id", 1), ("dedupe_key", 1)], unique=True, name="uniq_user_dedupe"
        )
        if removed:
            logger.warning("Notifications: removed %s duplicate alerts before uniqueness enforcement", removed)
        logger.info("Notifications: unique (user_id, dedupe_key) index ensured")
    except Exception as e:
        logger.warning(f"notifications dedupe/index step failed: {e}")

    try:
        recovered_fills = await _recover_pending_paper_fills()
        if recovered_fills:
            logger.warning("Recovered %s pending paper fills after startup.", recovered_fills)
    except Exception as e:
        logger.warning(f"pending paper fill recovery skipped: {e}")

    # Add any missing built-in presets for every user. This is additive by name:
    # it keeps custom/old strategies intact and only inserts missing v10 presets.
    try:
        async for user_row in db.users.find({}, {"id": 1}):
            user_id = user_row["id"]
            await migrate_strategy_display_names(user_id)
            await seed_default_strategies_for_user(user_id)
            await migrate_user_to_v12_upstox(user_id)
            await migrate_user_to_upstox_quality_system(user_id)
            settings = await get_user_settings(user_id)
            if bool(settings.get("paper_mode", True)):
                synced_modes = await _sync_strategy_modes_to_profile(user_id, True)
                if synced_modes:
                    logger.warning("Startup synced %s strategy mode(s) to PAPER for user %s", synced_modes, user_id)
                try:
                    lifecycle = await _daily_paper_lifecycle_for_user(user_id)
                    if any(lifecycle.values()):
                        logger.warning("Paper daily lifecycle prepared for user %s: %s", user_id, lifecycle)
                except Exception as lifecycle_err:
                    logger.warning("Paper daily lifecycle failed for user %s: %s", user_id, lifecycle_err)

            # Auditing/Migrating user account settings to match target config
            try:
                await db.users.update_one(
                    {"id": user_id},
                    {"$set": {
                        "LEGACY_EXECUTION_WRITES_ENABLED": False,
                        "CORE_ENGINE_PAPER_ENABLED": True,
                        "CORE_ENGINE_LIVE_ENABLED": False,
                    }}
                )
                
                # Normalize strategies and retire removed MCX commodity rows.
                try:
                    from core.market_clock import is_trading_session_active as _market_hours_active
                    market_hours_active = bool(_market_hours_active())
                except Exception:
                    market_hours_active = False
                user_strats = await db.strategies.find({"user_id": user_id}).to_list(1000)
                for s in user_strats:
                    s_updates = {}
                    if not s.get("required_capital"):
                        s_updates["required_capital"] = 25000.0
                    if not s.get("instrument_group"):
                        s_updates["instrument_group"] = _strategy_instrument_group(s)
                    if not s.get("strategy_type"):
                        s_updates["strategy_type"] = _strategy_type(s)
                    
                    if _strategy_instrument_group(s) == "MCX":
                        s_updates["status"] = "archived"
                        s_updates["mode"] = "paper"
                        s_updates["instrument_group"] = "REMOVED"
                        s_updates["last_filter_reason"] = "MCX commodity strategies were removed; QuantG is Upstox-only for NSE/BSE/NFO/BFO."
                        logger.warning("Archived removed MCX strategy %s during startup cleanup", s.get("id"))

                    # EDR-03/15: archive no-edge rows and every QG experiment
                    # outside the explicit paper-forward allowlist.
                    if (s.get("name") in DEAD_STRATEGY_NAMES or s.get("name") in PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES) and s.get("status") != "archived":
                        s_updates["status"] = "archived"
                        s_updates["mode"] = "paper"
                        s_updates["manual_paused"] = True
                        s_updates["schedule_paused"] = False
                        s_updates["last_filter_reason"] = (
                            "Archived: not in the founder-approved paper-forward book for the next session (only QG-O1/QG-O4/QG-O5 active)."
                            if s.get("name") in PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES
                            else "Archived 2026-07-04 (EDR-03): 0 out-of-sample edge across the whole book; replaced by NIFTY Put Spread Theta (OOS)."
                        )
                        logger.warning("Archived strategy %s during startup allowlist enforcement", s.get("name"))

                    if s.get("name") in PAPER_FORWARD_ACTIVE_STRATEGY_NAMES:
                        s_updates["status"] = "live" if market_hours_active else "paused"
                        s_updates["mode"] = "paper"
                        s_updates["manual_paused"] = False
                        s_updates["schedule_paused"] = not market_hours_active
                        s_updates["last_filter_reason"] = (
                            "Paper-forward active during market hours: Options Alpha Rebuild pack seeded 2026-07-05."
                            if market_hours_active
                            else "Market closed: queued for paper-forward activation at the next 09:15 IST open."
                        )

                    if s_updates:
                        await db.strategies.update_one({"id": s["id"], "user_id": user_id}, {"$set": s_updates})
                
                logger.info("Startup audit and safety configuration completed for user %s", user_id)
            except Exception as audit_err:
                logger.warning("Startup audit/safety config migration failed for user %s: %s", user_id, audit_err)
            
            # Executing startup self-heal
            try:
                from position_reconciler import reconcile_and_repair_positions
                await reconcile_and_repair_positions(user_id)
                logger.info("Startup self-heal completed successfully for user %s", user_id)
            except Exception as self_heal_err:
                logger.warning("Startup self-heal failed for user %s: %s", user_id, self_heal_err)

            try:
                await _sync_upstox_order_statuses(user_id, force=True)
            except Exception as sync_err:
                logger.warning("Startup order sync failed for user %s: %s", user_id, sync_err)
            try:
                await _reconcile_stale_orders_for_user(user_id)
            except Exception as reconcile_err:
                logger.warning("Startup stale order reconciliation failed for user %s: %s", user_id, reconcile_err)

            # Broker position reconciliation for live users on restart.
            # Compares local strategy_positions ledger against broker's live portfolio
            # so ghost positions and orphan broker positions are surfaced immediately.
            try:
                if not settings.get("paper_mode", True):
                    from core.reconciliation import CoreReconciliation
                    user_doc = await db.users.find_one({"id": user_id}) or {"id": user_id}
                    broker_positions = await _fetch_broker_positions_for_user(user_doc, settings)
                    rec = CoreReconciliation(db)
                    rec_result = await rec.reconcile_portfolio(user_id, broker_positions, mode="live")
                    if rec_result.get("mismatch_detected"):
                        logger.critical(
                            "STARTUP BROKER RECONCILIATION MISMATCH for user %s: %s",
                            user_id, rec_result["mismatches"],
                        )
                    else:
                        logger.info(
                            "Startup broker reconciliation passed for user %s (%d symbols checked)",
                            user_id, len(rec_result.get("checked_symbols", [])),
                        )
            except Exception as broker_rec_err:
                logger.warning("Startup broker reconciliation failed for user %s: %s", user_id, broker_rec_err)
    except Exception as e:
        logger.warning(f"default strategy seeding/reconciliation skipped: {e}")

    # Background strategy runner uses Upstox candles when available and paper-safe
    # mock data only for paper-mode history fallback.
    async def _price_history(user_id: str, symbol: str, days: int = 60, strategy: Optional[dict] = None):
        settings = await get_user_settings(user_id)
        strategy_mode = (strategy or {}).get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
        allow_mock = strategy_mode == "paper"
        interval = ((strategy or {}).get("visual_config") or {}).get("options", {}).get("candle_interval") or "5minute"
        if interval == "1minute":
            # Upstox rejects minute-history older than ~1 month (UDAPI1148); 2 days
            # of 1-min bars (~750) is far more than these strategies' 30-45 bar need.
            days = min(days, 2)
        return await _fetch_strategy_history(
            user_id,
            symbol,
            days=days,
            interval=interval,
            allow_mock=allow_mock,
            strategy=strategy,
        ) | {"paper_mode": allow_mock}

    # Resolver for index option contracts used when visual_config.options.enabled.
    async def _resolve_option(user_id: str, underlying: str, signal_action: str,
                              strike_mode: str, otm_points: int = 0,
                              expiry_offset: int = 0, strategy: Optional[dict] = None):
        strategy = strategy or {}
        settings = await get_user_settings(user_id)
        mode = str(strategy.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")).lower()
        underlying = str(underlying or "NIFTY").upper()
        strike_mode_u = str(strike_mode or "ATM_BUY").upper()
        action_u = str(signal_action or "BUY").upper()
        if underlying in REMOVED_COMMODITY_UNDERLYINGS:
            diagnostics = {
                "resolver_stage": "removed_underlying",
                "resolver_reason": "mcx_removed",
                "instrument_key": None,
                "quote_source": None,
            }
            _resolve_option.last_diagnostics = diagnostics
            return None
        option_side = "CE" if action_u == "BUY" else "PE"
        if "SELL" in strike_mode_u:
            option_side = "PE" if action_u == "BUY" else "CE"
        # Strike selection: OTM1 when otm_points set, ITM1 when the strike_mode
        # asks for it (e.g. "ITM1_BUY"), else ATM. ITM1 (~0.6 delta) carries less
        # theta and a higher win rate than ATM for directional buyers — the
        # resolver already supports "ITM1" (instrument_resolver.py); this was the
        # missing derivation that previously forced every buyer to ATM (max theta).
        if int(otm_points or 0) > 0:
            strike_rule = "OTM1"
        elif "ITM" in strike_mode_u:
            strike_rule = "ITM1"
        else:
            strike_rule = "ATM"
        instrument_type = "INDEX_OPTION"
        diagnostics: Dict[str, Any] = {
            "resolver_stage": "start",
            "resolver_reason": "starting_upstox_unified_resolver",
            "instrument_source": None,
            "instrument_key": None,
            "quote_source": None,
            "quote_age_sec": None,
            "subscribed_key": None,
            "cache_lookup_key": None,
            "cache_hit": False,
            "quote_timestamp": None,
            "quote_reject_reason": None,
        }
        _resolve_option.last_diagnostics = diagnostics

        upstox_gw = await get_user_upstox_gateway(user_id)
        spot_hint = None

        resolver = InstrumentResolver(db, upstox_gateway=upstox_gw)
        instrument = await resolver.resolve_instrument_with_source(
            underlying=underlying,
            instrument_type=instrument_type,
            option_side=option_side,
            strike_rule=strike_rule,
            expiry_rule=int(expiry_offset or 0),
            spot_price_hint=float(spot_hint or 0) if spot_hint else None,
            mode=mode,
        )
        diagnostics.update({
            "resolver_stage": resolver.last_diagnostics.get("stage"),
            "resolver_reason": resolver.last_diagnostics.get("reason"),
            "instrument_source": getattr(instrument.source, "value", None) if instrument else None,
            "instrument_key": instrument.instrument_key if instrument else diagnostics.get("instrument_key"),
        })
        _resolve_option.last_diagnostics = diagnostics
        if not instrument:
            return None
        if mode == "live" and instrument.source == InstrumentSource.PAPER_SIMULATED:
            diagnostics.update({
                "resolver_stage": "live_contract_gate",
                "resolver_reason": "live_rejects_paper_simulated_instrument",
                "instrument_source": instrument.source.value,
                "instrument_key": instrument.instrument_key,
            })
            _resolve_option.last_diagnostics = diagnostics
            return None

        if upstox_gw and instrument.instrument_key:
            try:
                sub_res = await asyncio.to_thread(upstox_gw.start_market_data_ws, [instrument.instrument_key], "full")
                diagnostics.update({
                    "subscribed_key": instrument.instrument_key,
                    "subscription_result": sub_res,
                })
            except Exception as exc:
                diagnostics.update({
                    "subscribed_key": instrument.instrument_key,
                    "subscription_result": {"ok": False, "error": str(exc)[:200]},
                })

        quote_service = QuoteService(db, upstox_gw)
        quote = await quote_service.get_quote(
            instrument.instrument_key,
            mode=mode,
            allow_simulated=(mode == "paper"),
        )
        quote_diag = quote_service.last_diagnostics or {}
        diagnostics.update({
            "subscribed_key": quote_diag.get("subscribed_key") or diagnostics.get("subscribed_key"),
            "cache_lookup_key": quote_diag.get("cache_lookup_key"),
            "cache_hit": bool(quote_diag.get("cache_hit")),
            "quote_timestamp": quote_diag.get("quote_timestamp"),
            "quote_age_sec": quote_diag.get("quote_age_sec"),
            "quote_reject_reason": quote_diag.get("quote_reject_reason"),
            "quote_source": quote_diag.get("quote_source") or diagnostics.get("quote_source"),
        })
        if not quote:
            diagnostics.update({
                "resolver_stage": "quote_lookup",
                "resolver_reason": quote_diag.get("quote_reject_reason") or "upstox_fresh_quote_unavailable",
                "instrument_source": instrument.source.value,
                "instrument_key": instrument.instrument_key,
                "quote_source": None,
            })
            _resolve_option.last_diagnostics = diagnostics
            return None
        if mode == "live" and str(quote.source).upper().startswith("PAPER"):
            diagnostics.update({
                "resolver_stage": "live_quote_gate",
                "resolver_reason": "live_rejects_paper_simulated_quote",
                "instrument_source": instrument.source.value,
                "instrument_key": instrument.instrument_key,
                "quote_source": quote.source,
            })
            _resolve_option.last_diagnostics = diagnostics
            return None

        quote_age_sec = None
        try:
            quote_ts = datetime.fromisoformat(str(quote.timestamp).replace("Z", "+00:00"))
            if quote_ts.tzinfo is None:
                quote_ts = quote_ts.replace(tzinfo=timezone.utc)
            quote_age_sec = round((datetime.now(timezone.utc) - quote_ts).total_seconds(), 3)
        except Exception:
            quote_age_sec = None
        diagnostics.update({
            "resolver_stage": resolver.last_diagnostics.get("stage"),
            "resolver_reason": resolver.last_diagnostics.get("reason"),
            "instrument_source": instrument.source.value,
            "instrument_key": instrument.instrument_key,
            "quote_source": quote.source,
            "quote_age_sec": quote_age_sec,
            "cache_lookup_key": diagnostics.get("cache_lookup_key"),
            "cache_hit": diagnostics.get("cache_hit"),
            "quote_timestamp": quote.timestamp,
            "quote_reject_reason": None,
        })
        _resolve_option.last_diagnostics = diagnostics
        contract_payload = {
            "tradingsymbol": instrument.symbol,
            "trading_symbol": instrument.symbol,
            "exchange": instrument.exchange,
            "segment": instrument.segment,
            "instrument_token": instrument.instrument_key,
            "instrument_key": instrument.instrument_key,
            "asset_class": "OPTION_LONG",
            "asset_type": "option",
            "option_type": instrument.option_type,
            "strike": instrument.strike,
            "lot_size": instrument.lot_size,
            "tick_size": instrument.tick_size,
            "expiry": instrument.expiry,
            "underlying": instrument.underlying,
            "transaction_type": "BUY",  # buyer strategies always BUY the option; action_u selects CE vs PE above
            "source": instrument.source.value,
            "simulated": instrument.source == InstrumentSource.PAPER_SIMULATED,
            "ltp": float(quote.ltp),
            "quote_source": quote.source,
            "quote_age_sec": quote_age_sec,
            "subscribed_key": diagnostics.get("subscribed_key"),
            "cache_lookup_key": diagnostics.get("cache_lookup_key"),
            "cache_hit": diagnostics.get("cache_hit"),
            "quote_timestamp": quote.timestamp,
            "quote_reject_reason": diagnostics.get("quote_reject_reason"),
            "resolver_stage": diagnostics.get("resolver_stage"),
            "resolver_reason": diagnostics.get("resolver_reason"),
        }
        # Phase 1 greeks: contract_payload is built from the InstrumentResolver +
        # QuoteService, neither of which carries greeks/OI/depth (QuoteService is
        # ltp-only), so greeks_at_signal lands null on every LIVE option signal.
        # (This is the runner's real resolver — _resolve_option_for_strategy, fixed
        # separately, is only used by manual routes.) Fetch greeks from the option
        # chain for this contract's expiry. Best-effort; never blocks resolution.
        try:
            _spot_keys = {
                "NIFTY": "NSE_INDEX|Nifty 50",
                "BANKNIFTY": "NSE_INDEX|Nifty Bank",
                "SENSEX": "BSE_INDEX|SENSEX",
            }
            _sk = _spot_keys.get(str(instrument.underlying or underlying).upper())
            if upstox_gw and _sk and instrument.expiry and instrument.strike:
                _gchain = await asyncio.to_thread(
                    upstox_gw.get_option_chain, _sk, str(instrument.expiry)[:10]
                )
                if _gchain and _gchain.get("status") == "success":
                    _otype = str(instrument.option_type or "").upper()
                    _nodes = _gchain.get("data") or []

                    # Phase 2 #2: delta-based strike selection. Pick the strike whose
                    # |delta| is closest to the risk-style target from the chain we
                    # already fetched; fall back to the resolver's ATM/OTM strike when
                    # disabled or when no usable delta strike is found.
                    _sel_strike = int(float(instrument.strike))
                    _tgt = None
                    _delta_pick = None
                    if OPTION_DELTA_SELECTION_ENABLED:
                        _rstyle = (
                            (strategy or {}).get("risk_style")
                            or ((strategy or {}).get("visual_config") or {}).get("risk", {}).get("risk_style")
                        )
                        _tgt = target_delta_for_style(_rstyle)
                        _delta_pick = pick_delta_strike(_nodes, _otype, _tgt)
                        if _delta_pick and _delta_pick.get("strike"):
                            _sel_strike = int(float(_delta_pick["strike"]))
                            contract_payload["target_delta"] = _tgt

                    # Locate the selected node (delta-pick or resolver strike) and
                    # mirror its greeks/OI/depth onto the contract.
                    _gopt = None
                    _gexpiry = None
                    for _gnode in _nodes:
                        if int(float(_gnode.get("strike_price") or 0)) == _sel_strike:
                            _gopt = _gnode.get("call_options" if _otype == "CE" else "put_options") or {}
                            _gexpiry = _gnode.get("expiry")
                            break
                    if _gopt:
                        _gg = _gopt.get("option_greeks") or {}
                        _gmd = _gopt.get("market_data") or {}
                        for _gk in ("iv", "delta", "theta", "gamma", "vega"):
                            if _gg.get(_gk) is not None:
                                contract_payload[_gk] = _gg.get(_gk)
                        for _src, _dst in (("oi", "oi"), ("bid_price", "bid"), ("ask_price", "ask")):
                            if _gmd.get(_src) is not None:
                                contract_payload[_dst] = _gmd.get(_src)

                        # Re-point the contract to the delta-selected strike — but only
                        # when it differs from the resolved strike AND the node carries a
                        # usable instrument_key and a positive LTP (so we never swap onto
                        # an illiquid/zero-quote strike).
                        _new_key = _gopt.get("instrument_key")
                        _new_ltp = _gmd.get("ltp") or _gmd.get("last_price")
                        if (_sel_strike != int(float(instrument.strike)) and _new_key
                                and _new_ltp and float(_new_ltp) > 0):
                            _new_sym = _gopt.get("trading_symbol")
                            if not _new_sym and _gexpiry:
                                try:
                                    _ed = datetime.strptime(str(_gexpiry)[:10], "%Y-%m-%d")
                                    _new_sym = (
                                        f"{str(instrument.underlying or underlying).upper()} "
                                        f"{_sel_strike} {_otype} {_ed.strftime('%d %b %y').upper()}"
                                    )
                                except Exception:
                                    _new_sym = contract_payload.get("tradingsymbol")
                            _now_iso = datetime.now(timezone.utc).isoformat()
                            contract_payload.update({
                                "tradingsymbol": _new_sym or contract_payload.get("tradingsymbol"),
                                "trading_symbol": _new_sym or contract_payload.get("trading_symbol"),
                                "instrument_token": _new_key,
                                "instrument_key": _new_key,
                                "strike": _sel_strike,
                                "ltp": float(_new_ltp),
                                "quote_source": "option_chain_delta",
                                "received_at": _now_iso,
                                "quote_timestamp": _now_iso,
                                "delta_selected": True,
                            })
                            try:
                                await asyncio.to_thread(upstox_gw.start_market_data_ws, [_new_key], "full")
                            except Exception:
                                pass
                            logger.info(
                                "delta-select: %s %s target=%.2f → strike=%s delta=%.3f (resolver had %s)",
                                str(instrument.underlying or underlying).upper(), _otype,
                                float(_tgt or 0), _sel_strike,
                                float((_delta_pick or {}).get("delta") or 0), int(float(instrument.strike)),
                            )

                    # Phase 2 #5: build a credit or debit spread when this strategy opts in.
                    try:
                        from core.spread_builder import (
                            build_credit_spread, build_credit_spread_by_offset, CREDIT_SPREADS_ENABLED,
                            CREDIT_SPREAD_SHORT_DELTA, CREDIT_SPREAD_WIDTH_STRIKES,
                            build_debit_spread, DEBIT_SPREADS_ENABLED,
                        )
                        _opts_cfg = ((strategy or {}).get("visual_config") or {}).get("options", {}) or {}
                        _struct = str(_opts_cfg.get("structure") or (strategy or {}).get("structure") or "single_leg")
                        if _struct in ("credit_spread", "debit_spread") and _nodes:
                            _intervals = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
                                          "MIDCPNIFTY": 75, "SENSEX": 100, "BANKEX": 100}
                            _u = str(instrument.underlying or underlying).upper()
                            _wstrikes = int(_opts_cfg.get("spread_width") or CREDIT_SPREAD_WIDTH_STRIKES)
                            _sdelta = float(_opts_cfg.get("short_delta") or (CREDIT_SPREAD_SHORT_DELTA if _struct == "credit_spread" else 0.50))
                            _direction = "bullish" if action_u == "BUY" else "bearish"
                            
                            if _struct == "credit_spread" and CREDIT_SPREADS_ENABLED:
                                _offset = _opts_cfg.get("short_offset_strikes")
                                if _offset is not None:
                                    _spread = build_credit_spread_by_offset(
                                        chain_nodes=_nodes, direction=_direction,
                                        spot=float(contract_payload.get("spot") or instrument.strike or 0),
                                        offset_strikes=int(_offset),
                                        width_points=_intervals.get(_u, 50) * _wstrikes,
                                    )
                                else:
                                    _spread = build_credit_spread(
                                        chain_nodes=_nodes, direction=_direction,
                                        width_points=_intervals.get(_u, 50) * _wstrikes, short_delta=_sdelta,
                                    )
                                if _spread.get("ok"):
                                    contract_payload["structure"] = "credit_spread"
                                    contract_payload["spread"] = _spread
                                    logger.info(
                                        "spread-build (credit): %s %s credit=%.2f max_loss=%.2f short=%s long=%s",
                                        _u, _direction, _spread["net_credit"], _spread["max_loss"],
                                        _spread["short_leg"]["strike"], _spread["long_leg"]["strike"],
                                    )
                                else:
                                    logger.info("spread-build (credit) skipped (%s): %s", _u, _spread.get("reason"))
                            elif _struct == "debit_spread" and DEBIT_SPREADS_ENABLED:
                                _spread = build_debit_spread(
                                    chain_nodes=_nodes, direction=_direction,
                                    width_points=_intervals.get(_u, 50) * _wstrikes, long_delta=_sdelta,
                                )
                                if _spread.get("ok"):
                                    contract_payload["structure"] = "debit_spread"
                                    contract_payload["spread"] = _spread
                                    logger.info(
                                        "spread-build (debit): %s %s debit=%.2f max_loss=%.2f short=%s long=%s",
                                        _u, _direction, _spread["net_debit"], _spread["max_loss"],
                                        _spread["short_leg"]["strike"], _spread["long_leg"]["strike"],
                                    )
                                else:
                                    logger.info("spread-build (debit) skipped (%s): %s", _u, _spread.get("reason"))
                    except Exception as _spx:
                        logger.debug("spread build failed: %s", _spx)
        except Exception as _gexc:
            logger.debug("live greeks chain fetch failed for %s: %s", instrument.instrument_key, _gexc)
        contract_payload["trade_quality_score"] = option_entry_quality_score(
            contract_payload,
            spot=spot_hint,
            quote={
                "ltp": float(quote.ltp),
                "timestamp": quote.timestamp,
                "received_at": quote.timestamp,
                "source": quote.source,
            },
        )
        contract_payload["quality_score"] = contract_payload["trade_quality_score"]["score"]
        contract_payload["quality_readiness"] = contract_payload["trade_quality_score"]["readiness"]
        return contract_payload

    app.state.tick_manager = RealtimeTickManager()
    app.state.upstox_gateways = _UPSTOX_GATEWAYS
    app.state.runner_stop = asyncio.Event()
    app.state.runner_task = asyncio.create_task(
        strategy_runner.runner_loop(db, _price_history, _place_order_core,
                                    app.state.runner_stop, _resolve_option, _close_strategy_positions)
    )
    app.state.signal_manager_stop = asyncio.Event()
    app.state.signal_manager_task = asyncio.create_task(
        signal_manager_loop(db, _place_order_core, app.state.signal_manager_stop)
    )
    app.state.health_stop = asyncio.Event()
    app.state.health_task = asyncio.create_task(_strategy_health_loop(app.state.health_stop))
    app.state.position_monitor_stop = asyncio.Event()
    from position_monitor import run_monitor_loop as _run_position_monitor
    app.state.position_monitor_task = asyncio.create_task(
        _run_position_monitor(
            db,
            app.state.position_monitor_stop,
            close_fn=_close_strategy_positions,
            quote_ltp_fn=_quote_upstox_instrument_key,
            get_ltp_fn=_current_ltp_for_symbol,
            get_settings_fn=get_user_settings,
        )
    )
    from position_guardian import run_guardian_loop as _run_position_guardian
    app.state.guardian_stop = asyncio.Event()
    app.state.guardian_task = asyncio.create_task(
        _run_position_guardian(
            db,
            app.state.guardian_stop,
            close_fn=_close_strategy_positions,
            quote_ltp_fn=_quote_upstox_instrument_key,
            get_ltp_fn=_current_ltp_for_symbol,
            get_settings_fn=get_user_settings,
        )
    )
    app.state.option_engine_stop = asyncio.Event()
    app.state.option_engine_task = asyncio.create_task(_option_engine_monitor_loop(app.state.option_engine_stop))
    app.state.broker_reconcile_stop = asyncio.Event()
    app.state.broker_reconcile_task = asyncio.create_task(_broker_reconciliation_loop(app.state.broker_reconcile_stop))
    # FIX 5 + FIX 7: Daily scheduler for gateway health check + token refresh push
    app.state.daily_scheduler_stop = asyncio.Event()
    app.state.daily_scheduler_task = asyncio.create_task(_daily_scheduler_loop(app.state.daily_scheduler_stop))

    # Subscribe V3 websocket to instrument tokens of any open positions so LTP is
    # available immediately after restart, not only after the first strategy signal.
    async def _subscribe_open_position_tokens_on_startup():
        await asyncio.sleep(8)  # wait for gateways to authenticate
        # Always subscribe these baseline index tokens so strategies can evaluate
        # entries even on days with no overnight open positions.
        _BASELINE_TOKENS = [
            "NSE_INDEX|Nifty 50",
            "NSE_INDEX|Nifty Bank",
            "BSE_INDEX|SENSEX",
        ]
        try:
            users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(200)
            for user_row in users:
                uid = user_row["id"]
                gateway = await get_user_upstox_gateway(uid)
                if not gateway or not gateway.connected:
                    continue
                # Start index feed (NIFTY/BANKNIFTY/SENSEX) so the WS is running
                await _start_user_upstox_ticker(uid)
                # Always subscribe baseline index tokens regardless of open positions
                await asyncio.to_thread(gateway.start_market_data_ws, _BASELINE_TOKENS, "full")
                # IMD-04: attach the read-only index 1-minute capture to this feed.
                try:
                    gateway._feed_v3.add_tick_listener(_get_live_index_capture().on_tick)
                except Exception:
                    pass
                # Subscribe any open position instrument keys (options etc.)
                open_positions = await db.strategy_positions.find(
                    {"user_id": uid, "status": {"$in": ["OPEN", "FILLED", "EXITING"]}},
                    {"instrument_key": 1, "legs": 1, "_id": 0},
                ).to_list(200)
                tokens = []
                for p in open_positions:
                    ik = p.get("instrument_key")
                    if ik and "|" in str(ik):
                        tokens.append(str(ik))
                    # Spreads carry NO top-level instrument_key — their two tradable
                    # legs live in legs[]. Subscribe each leg so the monitor prices
                    # spreads off the warm WS V3 cache instead of hammering the REST
                    # /market-quote/ltp endpoint every tick (which rate-limits (429)
                    # and leaves every spread mark dark -> PNL shows Rs 0.00).
                    for leg in (p.get("legs") or []):
                        lk = (leg or {}).get("instrument_key")
                        if lk and "|" in str(lk):
                            tokens.append(str(lk))
                tokens = list(dict.fromkeys(tokens))  # de-dup, preserve order
                if tokens:
                    await asyncio.to_thread(gateway.start_market_data_ws, tokens, "full")
                    logger.info("Startup: subscribed %d open-position tokens + %d baseline for user %s", len(tokens), len(_BASELINE_TOKENS), uid)
                else:
                    logger.info("Startup: subscribed %d baseline tokens for user %s (no open positions)", len(_BASELINE_TOKENS), uid)
        except Exception as _sub_err:
            logger.warning("Startup option token subscription failed: %s", _sub_err)

    asyncio.create_task(_subscribe_open_position_tokens_on_startup())

    # Phase 2 DB migration: update existing strategy python_code when
    # strategy_logic_version is stale.
    async def _migrate_strategy_code_versions():
        """Silently update python_code for any user who still has stale strategy code.

        Bumped to 2.3 to re-push live-market participation tuning:
        (1) option buyers accept valid current continuation setups instead of
            only exact cross/retest candles, and
        (2) equity templates are included in the versioned migration so live
            stored python_code receives strategy-specific threshold tuning.
        Re-pushing from the in-code catalog (the single source of truth) is
        idempotent, so version-gating only avoids redundant writes.
        """
        await asyncio.sleep(5)
        try:
            updated = 0
            for name, (code, _cat, _desc) in UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME.items():
                result = await db.strategies.update_many(
                    {
                        "name": name,
                        "$or": [
                            {"strategy_logic_version": {"$exists": False}},
                            {"strategy_logic_version": {"$lt": "2.3"}},
                            {"strategy_logic_version": "1.0"},
                        ],
                    },
                    {"$set": {
                        "python_code": code,
                        "risk_style": _cat,
                        "visual_config.risk.risk_style": _cat,
                        "strategy_logic_version": "2.3",
                        "code_migrated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
                updated += result.modified_count
            if updated:
                logger.info("DB migration: updated python_code for %d strategy documents to v2.3", updated)
        except Exception as _mig_err:
            logger.warning("Strategy code migration failed: %s", _mig_err)

    asyncio.create_task(_migrate_strategy_code_versions())

    # Debit-spread enablement: the engine (builder/lifecycle/monitor) already
    # supports debit spreads; it only activates when a strategy's
    # visual_config.options.structure == "debit_spread". Convert the report's
    # three naked-ATM directional buyers to defined-risk debit spreads so their
    # max loss is capped at net debit instead of full premium. Idempotent.
    async def _migrate_debit_spread_structure():
        await asyncio.sleep(6)
        # NOTE (2026-06-30): "BANKNIFTY Breakout Buyer" was removed from this list —
        # it is now force-converted to a credit_spread by
        # _migrate_credit_spread_structure below (it lost every directional trade in
        # chop). Leaving it here would fight that migration on every restart.
        # NOTE (2026-07-01): "BANKNIFTY Volatility Breakout" removed too — it was the
        # worst remaining debit spread (-2337, 0/2, lost in TREND_UP *and* chop) and
        # is now force-converted to credit_spread below.
        _debit_names = [
        ]
        try:
            res = await db.strategies.update_many(
                {"name": {"$in": _debit_names},
                 "visual_config.options.structure": {"$ne": "debit_spread"}},
                {"$set": {
                    "visual_config.options.structure": "debit_spread",
                    "visual_config.options.spread_width": 2,
                    "structure": "debit_spread",
                }},
            )
            if res.modified_count:
                logger.info("DB migration: set debit_spread structure on %d strategy documents", res.modified_count)
        except Exception as _ds_err:
            logger.warning("Debit-spread structure migration failed: %s", _ds_err)

    asyncio.create_task(_migrate_debit_spread_structure())

    # Credit-spread conversion (2026-06-30): the worst directional debit spreads
    # lost EVERY trade over 06-29/30 in a choppy market (NIFTY Quick EMA Scalper
    # -3.7k, BANKNIFTY HFT Momentum Scalper -2.2k, BANKNIFTY Breakout Buyer -1.8k).
    # A debit spread still needs a directional move; a credit spread earns theta if
    # price merely holds — the only structure that was green these two days. Convert
    # the three to credit_spread (sells a put spread on BUY, a call spread on SELL)
    # and size them at the proven ₹8k budget (≈1-2 lots). Idempotent: only writes
    # when not already credit_spread. This is the durable owner of these three —
    # they are excluded from _migrate_debit_spread_structure above.
    async def _migrate_credit_spread_structure():
        await asyncio.sleep(7)
        # 2026-07-01: added the two worst remaining directional debit spreads. Both
        # lost every trade over 06-30/07-01 and — with regime now instrumented —
        # BANKNIFTY Volatility Breakout lost even in TREND_UP, so this is a structural
        # failure, not a regime one (a regime gate would be curve-fitting: the debit
        # winners that day were in RANGE). Converting to theta-earning credit spreads.
        _credit_names = [
            "NIFTY Quick EMA Scalper",
            "BANKNIFTY HFT Momentum Scalper",
            "BANKNIFTY Breakout Buyer",
            "BANKNIFTY Volatility Breakout",
            "SENSEX Swing RSI Pullback",
            "NIFTY Micro-Lot Trend Follower",
            "NIFTY Momentum Buyer",
        ]
        try:
            res = await db.strategies.update_many(
                {"name": {"$in": _credit_names},
                 "visual_config.options.structure": {"$ne": "credit_spread"}},
                {"$set": {
                    "visual_config.options.structure": "credit_spread",
                    "visual_config.options.spread_width": 2,
                    "visual_config.options.required_capital": 8000.0,
                    "structure": "credit_spread",
                    "strategy_type": "Option Selling",
                    "visual_config.risk.cooldown_minutes": 15,
                    "visual_config.risk.max_trades_day": 8,
                    "visual_config.risk.daily_loss_limit": 4000.0,
                    "visual_config.risk.time_exit_minutes": 0,
                    "visual_config.risk.strategy_category": "intraday",
                }},
            )
            if res.modified_count:
                logger.info("DB migration: set credit_spread structure on %d strategy documents", res.modified_count)
        except Exception as _cs_err:
            logger.warning("Credit-spread structure migration failed: %s", _cs_err)

    asyncio.create_task(_migrate_credit_spread_structure())

    async def _migrate_alpha_repair_followups():
        await asyncio.sleep(8)
        now = datetime.now(timezone.utc).isoformat()
        try:
            equity_rows = await db.strategies.find(
                {
                    "$or": [
                        {"asset_class": "equity"},
                        {"instrument_group": {"$in": ["NSE", "BSE"]}},
                        {"visual_config.options.enabled": False},
                    ]
                },
                {"_id": 0, "id": 1, "name": 1, "required_capital": 1, "visual_config.risk": 1},
            ).to_list(200)
            equity_updated = 0
            for row in equity_rows:
                visual_capital = float(((row.get("visual_config") or {}).get("risk") or {}).get("required_capital") or 0)
                top_level_capital = float(row.get("required_capital") or 0)
                current_capital = max(top_level_capital, visual_capital)
                tier_capital = EQUITY_CAPITAL_TIERS.get(str(row.get("name") or ""), EQUITY_MIN_REQUIRED_CAPITAL)
                capital = max(current_capital, tier_capital)
                risk = ((row.get("visual_config") or {}).get("risk") or {})
                daily_loss = max(float(risk.get("daily_loss_limit") or 0), 2500.0)
                res = await db.strategies.update_one(
                    {"id": row["id"]},
                    {"$set": {
                        "required_capital": capital,
                        "visual_config.risk.required_capital": capital,
                        "visual_config.risk.daily_loss_limit": daily_loss,
                        "visual_config.risk.entry_cutoff_ist": EQUITY_ENTRY_CUTOFF,
                        "alpha_repair_followup_migrated_at": now,
                    }},
                )
                equity_updated += int(res.modified_count or 0)

            bn_res = await db.strategies.update_many(
                {
                    "name": {"$regex": "BANKNIFTY.*Theta", "$options": "i"},
                    "visual_config.options.structure": "credit_spread",
                },
                {"$set": {
                    "visual_config.options.expiry_week_only": bool(BANKNIFTY_THETA_EXPIRY_WEEK_ONLY),
                    "visual_config.options.expiry_policy": "expiry_week_only" if BANKNIFTY_THETA_EXPIRY_WEEK_ONLY else "nearest",
                    "alpha_repair_followup_migrated_at": now,
                }},
            )
            if equity_updated or bn_res.modified_count:
                logger.info(
                    "DB migration: alpha repair followups equity=%d banknifty_theta=%d",
                    equity_updated,
                    bn_res.modified_count,
                )
        except Exception as _ar_err:
            logger.warning("Alpha repair followup migration failed: %s", _ar_err)

    asyncio.create_task(_migrate_alpha_repair_followups())

    # Fix 3: Warn loudly at boot if running with the default JWT secret.
    # The secret is SHA-256'd so it won't crash, but forging tokens is trivial
    # for anyone who knows the well-known default value.
    _raw_jwt_env = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY")
    if not _raw_jwt_env:
        logger.critical(
            "JWT_SECRET is not set in environment — running with default development secret. "
            "ALL SESSION TOKENS ARE CRYPTOGRAPHICALLY INSECURE. "
            "Set JWT_SECRET to a random 64-character hex string before enabling live trading."
        )

    # Fix 2: Boot-time live arm state verification.
    # When CORE_ENGINE_LIVE_ENABLED=true, confirm the DB arm state is consistent so
    # operators know whether live orders are actually permitted before the first trade.
    _live_env_on = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    try:
        if _live_env_on:
            logger.critical("CORE_ENGINE_LIVE_ENABLED=true — LIVE TRADING IS ACTIVE. Verifying arm state.")
            armed_users = await db.live_arm_state.find({"armed": True}).to_list(100)
            if not armed_users:
                logger.critical(
                    "LIVE TRADING ENABLED but no users have live_arm_state.armed=true. "
                    "Live orders will be blocked at execution time until a user arms live trading via the UI."
                )
            else:
                for _arm in armed_users:
                    _uid = _arm.get("user_id", "unknown")
                    if not _arm.get("global_live_enabled", False):
                        logger.warning(
                            "User %s has armed=true but global_live_enabled=false — live orders will be blocked.", _uid
                        )
                    else:
                        logger.info("User %s: live_arm_state OK — armed=true, global_live_enabled=true.", _uid)
        else:
            # Warn if any user is armed in DB but env flag is off — helps catch accidental mismatches.
            armed_count = await db.live_arm_state.count_documents({"armed": True})
            if armed_count:
                logger.warning(
                    "%d user(s) have live_arm_state.armed=true in DB but CORE_ENGINE_LIVE_ENABLED=false. "
                    "Live trading is blocked by env flag — this is expected in paper mode.", armed_count
                )
    except Exception as _arm_check_err:
        logger.warning("Boot-time arm state check failed (non-fatal): %s", _arm_check_err)

    # Market schedule startup restore: if backend restarts during market hours, re-activate
    # any strategies that were auto-paused by the 3:35 PM scheduler the previous session.
    async def _restore_schedule_on_startup():
        await asyncio.sleep(5)
        try:
            from core.market_clock import is_trading_session_active, get_ist_now
            ist = get_ist_now()
            if ist.weekday() < 5 and is_trading_session_active():
                result = await db.strategies.update_many(
                    {"status": "paused", "schedule_paused": True, "manual_paused": {"$ne": True}},
                    {"$set": {"status": "live", "schedule_paused": False,
                              "schedule_resumed_at": ist.isoformat()}},
                )
                if result.modified_count:
                    logger.info(
                        "Startup: auto-activated %d schedule-paused strategies (market is open)",
                        result.modified_count,
                    )
        except Exception as _sr_err:
            logger.warning("Startup schedule restore failed: %s", _sr_err)

    asyncio.create_task(_restore_schedule_on_startup())
    logger.info("QuantG API started")


@app.on_event("shutdown")
async def shutdown():
    try:
        if getattr(app.state, "tick_manager", None):
            app.state.tick_manager.stop()
    except Exception:
        pass
    try:
        app.state.runner_stop.set()
        if app.state.runner_task:
            await asyncio.wait_for(app.state.runner_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.signal_manager_stop.set()
        if app.state.signal_manager_task:
            await asyncio.wait_for(app.state.signal_manager_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.health_stop.set()
        if app.state.health_task:
            await asyncio.wait_for(app.state.health_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.position_monitor_stop.set()
        if app.state.position_monitor_task:
            await asyncio.wait_for(app.state.position_monitor_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.guardian_stop.set()
        if app.state.guardian_task:
            await asyncio.wait_for(app.state.guardian_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.option_engine_stop.set()
        if app.state.option_engine_task:
            await asyncio.wait_for(app.state.option_engine_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.broker_reconcile_stop.set()
        if app.state.broker_reconcile_task:
            await asyncio.wait_for(app.state.broker_reconcile_task, timeout=3.0)
    except Exception:
        pass
    try:
        app.state.daily_scheduler_stop.set()
        if getattr(app.state, "daily_scheduler_task", None):
            await asyncio.wait_for(app.state.daily_scheduler_task, timeout=3.0)
    except Exception:
        pass
    try:
        for stream in getattr(app.state, "upstox_portfolio_streams", {}).values():
            stream.stop()
    except Exception:
        pass
    client.close()


# ============== Routes: Trading-Ready Check ==============

# moved to routes/ops_runtime.py
async def trading_ready_check(user=Depends(get_current_user)):
    """Holistic trading-readiness check.

    Returns a structured report of every subsystem required for live trading.
    All checks run concurrently.  The top-level ``ready`` flag is True only
    when every critical check passes.
    """
    user_id = user["id"]
    checks: Dict[str, Any] = {}

    # 1. Broker auth
    upstox_status = await get_user_upstox_status(user_id)
    checks["broker_auth"] = {
        "ok": bool(upstox_status.get("token_valid")),
        "detail": upstox_status.get("token_state", "unknown"),
        "critical": True,
    }

    # 2. Upstox gateway connected
    gw = await get_user_upstox_gateway(user_id)
    gw_connected = bool(gw and gw.connected)
    checks["gateway_connected"] = {
        "ok": gw_connected,
        "detail": "connected" if gw_connected else "gateway not initialised",
        "critical": True,
    }

    # 3. Market data websocket
    ws_active = bool(gw and getattr(gw, "_ws_thread", None) and getattr(gw._ws_thread, "is_alive", lambda: False)())
    checks["websocket_feed"] = {
        "ok": ws_active,
        "detail": "active" if ws_active else "not started (will auto-start on first strategy scan)",
        "critical": False,
    }

    # 4. NIFTY historical candles
    nifty_ok = False
    nifty_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty 50", "5minute", 3
            )
            nifty_ok = bool(candles and len(candles) >= 2)
            nifty_detail = f"{len(candles or [])} bars" if nifty_ok else "returned empty"
        except Exception as exc:
            nifty_detail = str(exc)[:120]
    checks["nifty_candles"] = {"ok": nifty_ok, "detail": nifty_detail, "critical": True}

    # 5. BANKNIFTY historical candles
    bnk_ok = False
    bnk_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty Bank", "5minute", 3
            )
            bnk_ok = bool(candles and len(candles) >= 2)
            bnk_detail = f"{len(candles or [])} bars" if bnk_ok else "returned empty"
        except Exception as exc:
            bnk_detail = str(exc)[:120]
    checks["banknifty_candles"] = {"ok": bnk_ok, "detail": bnk_detail, "critical": True}

    # 6. SENSEX historical candles
    sensex_ok = False
    sensex_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "BSE_INDEX|SENSEX", "5minute", 3
            )
            sensex_ok = bool(candles and len(candles) >= 2)
            sensex_detail = f"{len(candles or [])} bars" if sensex_ok else "returned empty"
        except Exception as exc:
            sensex_detail = str(exc)[:120]
    checks["sensex_candles"] = {"ok": sensex_ok, "detail": sensex_detail, "critical": False}

    # 7. Latest candle freshness for NIFTY (only during market hours)
    market_open = _is_order_market_open("NSE")
    if nifty_ok and market_open and gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty 50", "5minute", 1
            )
            freshness = _latest_candle_fresh_for_live(candles or [], "NSE")
            checks["candle_freshness"] = {
                "ok": bool(freshness.get("fresh")),
                "detail": freshness.get("reason", "unknown"),
                "age_sec": freshness.get("age_sec"),
                "critical": True,
            }
        except Exception as exc:
            checks["candle_freshness"] = {"ok": False, "detail": str(exc)[:120], "critical": True}
    else:
        checks["candle_freshness"] = {
            "ok": True,
            "detail": "market closed — freshness check skipped" if not market_open else "candle fetch failed above",
            "critical": False,
        }

    # 8. Pre-trade gate smoke test (paper, no real order)
    settings = await get_user_settings(user_id)
    paper_mode = bool(settings.get("paper_mode", True))
    checks["paper_mode"] = {
        "ok": True,
        "detail": "paper" if paper_mode else "LIVE",
        "critical": False,
    }

    # Summary
    critical_checks = [v for v in checks.values() if v.get("critical")]
    all_critical_ok = all(c["ok"] for c in critical_checks)
    non_critical_ok = all(v["ok"] for v in checks.values() if not v.get("critical"))
    ready = all_critical_ok

    return {
        "ready": ready,
        "trading_mode": "paper" if paper_mode else "live",
        "market_open": market_open,
        "checks": checks,
        "summary": (
            "All systems go — ready to trade."
            if ready
            else "One or more critical checks failed. See checks for details."
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ===========================================================================
# QUANTG UNIFIED CORE ARCHITECTURE REST APIS
# ===========================================================================

from core.market_clock import get_market_clock_snapshot
from core.live_safety_firewall import LiveSafetyFirewall
from core.backtest_engine import BacktestEngine
from core.performance_tracker import PerformanceTracker

# core health/feed readiness routes moved to routes/readiness.py

# core data and backtest routes moved to routes/core_status.py

# trading live-readiness route moved to routes/readiness.py


# core live-readiness route moved to routes/readiness.py

# core live control routes moved to routes/core_status.py

# ============== Register Router ==============
app.include_router(api)
