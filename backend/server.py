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
from pydantic import BaseModel, Field, EmailStr, validator

import upstox_helper
from brokers.upstox_gateway import UpstoxGateway, extract_order_id as extract_upstox_order_id
from brokers import upstox_gateway as upstox_gateway_utils
from brokers.upstox_portfolio_stream import UpstoxPortfolioStream
import options_helper
import backtrader_runner
import strategy_runner
from signal_manager import signal_manager_loop
from mcx_contract_resolver import MCXContractResolver, mcx_instrument_refresh_loop
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


class _LegacyKiteHelperProxy:
    def __getattr__(self, name: str):
        import importlib
        return getattr(importlib.import_module("kite_helper"), name)


kite_helper = _LegacyKiteHelperProxy()

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

KITE_HISTORICAL_MIN_INTERVAL_SEC = float(os.environ.get("KITE_HISTORICAL_MIN_INTERVAL_SEC", "0.40"))
KITE_HISTORY_CACHE_TTL_SEC = int(os.environ.get("KITE_HISTORY_CACHE_TTL_SEC", "55"))
STRATEGY_LIVE_CANDLE_MAX_AGE_SEC = int(os.environ.get("STRATEGY_LIVE_CANDLE_MAX_AGE_SEC", "1200"))
KITE_ORDER_SYNC_TTL_SEC = int(os.environ.get("KITE_ORDER_SYNC_TTL_SEC", "10"))
_RATE_LIMIT_LOCK = asyncio.Lock()
_RATE_LIMIT_LAST: Dict[str, float] = {}
_LOG_THROTTLE_LAST: Dict[str, float] = {}
_HISTORY_CACHE: Dict[str, Dict[str, Any]] = {}
_ORDER_SYNC_CACHE: Dict[str, Dict[str, Any]] = {}
_KOTAK_GATEWAYS: Dict[str, Any] = {}  # Kotak broker — deprecated; kept to prevent AttributeError on startup
_UPSTOX_GATEWAYS: Dict[str, UpstoxGateway] = {}
_UPSTOX_TOKEN_VALIDATION_CACHE: Dict[str, Dict[str, Any]] = {}
COMMODITY_UNDERLYINGS = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
COMMODITY_REQUIRED_CAPITAL = {
    "CRUDEOIL": 30000.0,
    "CRUDEOILM": 6000.0,
    "NATURALGAS": 18000.0,
    "NATGASMINI": 6000.0,
}
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
    segment: str         # 'EQUITY', 'FUTURES', 'OPTIONS', 'COMMODITY'
    exchange: str        # 'NSE', 'BSE', 'NFO', 'MCX'
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
    adaptive_exits_enabled: Optional[bool] = None
    target_r_multiple: Optional[float] = None
    broker: Optional[str] = None
    mode: Optional[str] = None


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


class KiteExchangeReq(BaseModel):
    request_token: str
    broker: str = "zerodha"


class OpsActionReq(BaseModel):
    note: Optional[str] = None


class KotakSubscribeReq(BaseModel):
    instruments: List[Dict[str, str]]


class KotakLoginReq(BaseModel):
    current_otp: Optional[str] = None


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

COMMODITY_SYMBOLS = [
    {"symbol": "CRUDEOIL", "name": "MCX Crude Oil", "base": 6550.0, "exchange": "MCX"},
    {"symbol": "CRUDEOILM", "name": "MCX Crude Oil Mini", "base": 6550.0, "exchange": "MCX"},
    {"symbol": "NATURALGAS", "name": "MCX Natural Gas", "base": 245.0, "exchange": "MCX"},
    {"symbol": "NATGASMINI", "name": "MCX Natural Gas Mini", "base": 245.0, "exchange": "MCX"},
]

# Mock ticks should move during market hours, then freeze. This keeps paper PnL
# from changing on nights/weekends when the user is not trading.
IST_OFFSET = timedelta(hours=5, minutes=30)
NSE_OPEN_MINUTE = 9 * 60 + 15
NSE_CLOSE_MINUTE = 15 * 60 + 30
MCX_OPEN_MINUTE = int(os.environ.get("MCX_OPEN_MINUTE", str(9 * 60)))
MCX_CLOSE_MINUTE = int(os.environ.get("MCX_CLOSE_MINUTE", str(23 * 60 + 30)))
SUPPORTED_ORDER_EXCHANGES = {"NSE", "BSE", "NFO", "BFO", "MCX", "CDS"}
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
    "MCX_FO": (MCX_OPEN_MINUTE, MCX_CLOSE_MINUTE, "MCX commodity"),
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
        for key in ("NSE_FO", "BSE_FO", "MCX_FO", "NSE_EQ", "BSE_EQ")
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
    if exch in {"MCX", "MCX_FO"}:
        return "MCX_FO"
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
    if exchange == "MCX" or segment == "MCX_FO":
        return "commodity"
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
    "SENSEX":    81460.20,  "CRUDEOIL":  6550.00,  "CRUDEOILM":  6550.00,
    "NATURALGAS":  245.00,  "NATGASMINI": 245.00,
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
        CRUDEOIL FO ~₹32
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
    allows fresh signals every 5 minutes — matching real Kite 5-min behaviour.
    """
    out = []
    price = base * 0.985
    now = datetime.now(timezone.utc)
    # snap to nearest 5-min boundary so dates align with real Kite candles
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
    sym_upper = symbol.upper()
    if sym_upper not in options_helper.INDEX_SPOT_SYMBOL:
        return None
    static_tokens = {
        # Kite's documented index tokens; avoids repeatedly downloading the
        # instruments dump during live strategy scans.
        "NIFTY": 256265,
        "BANKNIFTY": 260105,
        "SENSEX": 265,
    }
    if sym_upper in static_tokens:
        return static_tokens[sym_upper]
    spot_exch, spot_sym = options_helper.INDEX_SPOT_SYMBOL[sym_upper]
    try:
        instruments = kite.instruments(spot_exch)
        for inst in instruments:
            if inst.get("tradingsymbol", "").upper() == spot_sym.upper():
                return int(inst["instrument_token"])
    except Exception as e:
        logger.warning(f"index spot token lookup failed for {sym_upper}: {e}")
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
    market_open = _is_order_market_open("MCX" if exchange == "MCX" else "NSE")
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
    cached = _history_cache_get(user_id, token, days, interval)
    if cached is not None:
        return cached
    await _rate_limit("kite:historical", KITE_HISTORICAL_MIN_INTERVAL_SEC)
    data = kite_helper.safe_historical(kite, token, days=days, interval=interval)
    _history_cache_set(user_id, token, days, interval, data)
    return data


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
    kite, _ = await get_user_kite(user_id)
    tick_manager = getattr(app.state, "tick_manager", None)
    if kite and tick_manager:
        try:
            token_to_symbol: Dict[int, str] = {}
            for s in SYMBOLS:
                if s["symbol"] in options_helper.INDEX_SPOT_SYMBOL:
                    continue
                tok = kite_helper.instrument_token(kite, s["symbol"])
                if tok:
                    token_to_symbol[tok] = s["symbol"]
            for opt_sym, (spot_exch, spot_sym) in options_helper.INDEX_SPOT_SYMBOL.items():
                tok = kite_helper.instrument_token(kite, spot_sym, segment=spot_exch)
                if tok:
                    token_to_symbol[tok] = opt_sym
            if token_to_symbol:
                tick_manager.start_for_user(user_id, kite, token_to_symbol)
        except Exception as e:
            logger.warning(f"Realtime tick service start failed: {e}")

    settings = await get_user_settings(user_id)
    data_broker = settings.get("data_broker", "upstox")

    if data_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user_id)
        if upstox_gw and upstox_gw.connected:
            if sym_upper in {"NIFTY", "BANKNIFTY"}:
                exchange = "NSE"
            elif sym_upper == "SENSEX":
                exchange = "BSE"
            elif sym_upper in COMMODITY_UNDERLYINGS or "MCX" in sym_upper or sym_upper.endswith("FUT"):
                exchange = "MCX"
            else:
                exchange = "NSE"
            token = _upstox_instrument_token(exchange, sym_upper)
            token_candidates = [token] if token else []
            if exchange == "MCX":
                token_candidates.extend(await _search_upstox_mcx_future_keys(upstox_gw, sym_upper, limit=5))
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
                    _floored = (_ist_now.minute // 5) * 5
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
                min_required = 2 if interval != "day" else min_intraday_bars
                if live_data and len(live_data) >= min_required:
                    is_live_source = bool(tick) or bool(candle_freshness.get("fresh"))
                    return {
                        "data": live_data,
                        "source": f"upstox-v3-websocket+historical:{interval}:mcx-future:{sym_upper}" if exchange == "MCX" else f"upstox-v3-websocket+historical:{interval}:{sym_upper}",
                        "is_live": is_live_source,
                        "live_reason": "websocket tick" if tick else candle_freshness.get("reason"),
                        "last_candle_at": candle_freshness.get("last_candle_at"),
                        "latest_candle_age_sec": candle_freshness.get("age_sec"),
                        "interval": interval,
                    }
            
            # Raise explicit error instead of silently falling back to mock candles
            if not allow_mock:
                resolved_tokens = [k for k in token_candidates if k]
                raise ValueError(
                    f"Upstox V3 historical data failed for symbol '{sym_upper}' on exchange '{exchange}' ({interval}). "
                    f"Resolved tokens: {resolved_tokens}. "
                    f"Please ensure the MCX instrument master cache is seeded, or check your internet connection."
                )
        else:
            if not allow_mock:
                raise ValueError(
                    f"Upstox data broker selected but gateway is not connected or initialized for user {user_id}."
                )

    if kite:

        token = None
        source_kind = "equity"
        if sym_upper in options_helper.INDEX_SPOT_SYMBOL:
            token = await _index_spot_token(kite, sym_upper)
            source_kind = "index-spot"
        elif sym_upper in COMMODITY_UNDERLYINGS:
            active_sym = _mcx_active_future_symbol(sym_upper)
            token = kite_helper.instrument_token(kite, active_sym, segment="MCX")
            source_kind = "commodity-future"
        else:
            token = kite_helper.instrument_token(kite, sym_upper)

        if token:
            live_data = await _cached_safe_historical(kite, user_id, token, days, interval)
            tick_source = None
            if interval == "5minute" and tick_manager and tick_manager.is_running(user_id):
                if tick_manager.has_symbol(user_id, sym_upper):
                    tick_bars = tick_manager.get_candles(user_id, sym_upper, bars=max(250, min_intraday_bars + 1))
                    if tick_bars:
                        tick_source = f"tick-live"
                        if live_data:
                            live_data = _merge_tick_bars(live_data, tick_bars)
                        else:
                            live_data = tick_bars
            if not live_data and interval == "5minute" and tick_manager and tick_manager.has_symbol(user_id, sym_upper):
                tick_bars = tick_manager.get_candles(user_id, sym_upper, bars=max(250, min_intraday_bars + 1))
                if tick_bars:
                    live_data = tick_bars
                    tick_source = f"tick-live"
            enough = bool(live_data) and (interval == "day" or tick_source or len(live_data) > min_intraday_bars)
            if enough:
                source_label = f"zerodha-kite-{interval}:{source_kind}:{sym_upper}"
                if tick_source:
                    source_label = f"zerodha-kite-{interval}:{tick_source}:{source_kind}:{sym_upper}"
                return {
                    "data": live_data,
                    "source": source_label,
                    "is_live": True,
                    "interval": interval,
                }
            if interval != "day":
                # Strict timeframe validation: do NOT fall back to daily candles when requesting intraday intervals
                logger.warning(f"Intraday timeframe {interval} not available for {sym_upper}; blocking daily candle fallback for safety.")

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
        sym = next((s for s in SYMBOLS if s["symbol"] == sym_upper), None) or next((s for s in COMMODITY_SYMBOLS if s["symbol"] == sym_upper), None)
        if sym:
            if interval == "day":
                return {
                    "data": historical_series(sym["base"], days),
                    "source": f"mock-day:{sym_upper}",
                    "is_live": False,
                    "interval": "day",
                }
            return {
                "data": intraday_series(sym["base"], bars=max(250, min_intraday_bars + 1)),
                "source": f"mock-5minute:{sym_upper}",
                "is_live": False,
                "interval": "5minute",
            }

    return {"data": [], "source": "none", "is_live": False, "interval": interval}


# ============== Routes: Auth ==============
@api.get("/")
async def health():
    return {"status": "ok", "service": "QuantG API", "version": APP_VERSION}


@api.get("/health")
async def health_check():
    return await health()


@api.get("/version")
async def get_version():
    commit, branch, dirty = get_git_info()
    file_ver = get_file_version()
    uptime_seconds = (datetime.now(timezone.utc) - START_TIME).total_seconds()
    return {
        "backend_version": APP_VERSION,
        "file_version": file_ver,
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "start_time": START_TIME.isoformat(),
        "up_time_seconds": round(uptime_seconds, 2)
    }


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
        json={"contents": [{"parts": [{"text": prompt}]}]},
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



# ============== Routes: Broker keys ==============
@api.post("/broker/keys", response_model=BrokerKeyOut)
async def save_broker_keys(req: BrokerKeyReq, user=Depends(get_current_user)):
    broker = (req.broker or "upstox").strip().lower()
    if broker != "upstox":
        raise HTTPException(400, "QuantG is configured for Upstox-only execution")
    # upsert per user+broker
    existing = await db.broker_keys.find_one({"user_id": user["id"], "broker": broker})
    doc = {
        "id": (existing or {}).get("id", str(uuid.uuid4())),
        "user_id": user["id"],
        "broker": broker,
        "api_key": encrypt_secret(req.api_key),
        "api_secret": encrypt_secret(req.api_secret),
        "user_id_at_broker": req.user_id_at_broker,
        "created_at": (existing or {}).get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if broker == "upstox":
        redirect_uri = (req.redirect_uri or (existing or {}).get("redirect_uri") or os.environ.get("UPSTOX_REDIRECT_URI") or "").strip()
        if not redirect_uri:
            redirect_uri = "https://www.quantgtrade.com/api/broker/upstox/callback"
        doc["redirect_uri"] = redirect_uri
        doc["is_sandbox"] = bool(req.is_sandbox)
        _UPSTOX_GATEWAYS.pop(user["id"], None)
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": broker},
        {"$set": doc},
        upsert=True,
    )
    return BrokerKeyOut(
        id=doc["id"],
        broker=doc["broker"],
        api_key_masked=_mask_secret(req.api_key),
        user_id_at_broker=req.user_id_at_broker,
        created_at=doc["created_at"],
    )


@api.get("/broker/keys", response_model=List[BrokerKeyOut])
async def list_broker_keys(user=Depends(get_current_user)):
    rows = await db.broker_keys.find({"user_id": user["id"], "broker": "upstox"}, {"_id": 0}).to_list(50)
    out = []
    for r in rows:
        k = decrypt_secret(r.get("api_key"))
        out.append(BrokerKeyOut(
            id=r["id"], broker=r["broker"],
            api_key_masked=_mask_secret(k),
            user_id_at_broker=r.get("user_id_at_broker"),
            created_at=r["created_at"],
        ))
    return out


@api.delete("/broker/keys/{key_id}")
async def delete_broker_key(key_id: str, user=Depends(get_current_user)):
    res = await db.broker_keys.delete_one({"id": key_id, "user_id": user["id"]})
    return {"deleted": res.deleted_count}


# ============== Routes: Market ==============
@api.get("/market/watchlist")
async def watchlist(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    if settings.get("data_broker") == "kotak_neo":
        gateway = _KOTAK_GATEWAYS.get(user["id"])
        if gateway and gateway.status().get("authenticated"):
            if gateway.status().get("subscribed_tokens", 0) == 0:
                await _start_user_kotak_ticker(user["id"])
            kotak_rows = []
            live_count = 0
            for i, s in enumerate(SYMBOLS):
                tick = gateway.latest_tick_by_symbol(s["symbol"])
                if tick and tick.get("ltp"):
                    price = float(tick["ltp"])
                    live_count += 1
                    change = round(price - s["base"], 2)
                    pct = round((change / s["base"]) * 100, 2) if s["base"] else 0.0
                    kotak_rows.append({
                        "symbol": s["symbol"],
                        "name": s["name"],
                        "price": price,
                        "change": change,
                        "pct": pct,
                        "source": "kotak_neo",
                        "feed": "kotak-neo-ticker",
                        "tick_time": tick.get("received_at"),
                    })
                else:
                    lp = live_price(s["base"], i)
                    kotak_rows.append({"symbol": s["symbol"], "name": s["name"], **lp, "source": "kotak_pending", "feed": "kotak-neo-ticker"})
            if live_count:
                return kotak_rows

    if settings.get("data_broker") == "upstox":
        upstox_rows = await _upstox_watchlist_rows(user["id"])
        if upstox_rows and any(row.get("source") == "upstox" for row in upstox_rows):
            return upstox_rows

    # Try live Kite first — use ohlc() so we get last_price AND previous close
    kite, status = await get_user_kite(user["id"])
    if kite:
        tick_manager = getattr(app.state, "tick_manager", None)
        has_ticks = bool(tick_manager and tick_manager.has_live_ticks(user["id"]))
        instruments = [_nse_token(s["symbol"]) for s in SYMBOLS]
        ohlc_data = kite_helper.safe_ohlc(kite, instruments)
        if ohlc_data:
            out = []
            for s in SYMBOLS:
                key = _nse_token(s["symbol"])
                node = ohlc_data.get(key) or {}
                price = node.get("last_price") or s["base"]
                ohlc = node.get("ohlc", {}) or {}
                prev_close = ohlc.get("close") or price
                change = round(price - prev_close, 2)
                pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
                out.append({"symbol": s["symbol"], "name": s["name"],
                            "price": price, "change": change, "pct": pct,
                            "source": "real" if has_ticks else "live",
                            "feed": "kite-ticker" if has_ticks else "kite-rest"})
            return out
    # Fallback: mock
    out = []
    for i, s in enumerate(SYMBOLS):
        lp = live_price(s["base"], i)
        out.append({"symbol": s["symbol"], "name": s["name"], **lp, "source": "mock"})
    return out


@api.get("/market/commodities")
async def commodity_watchlist(user=Depends(get_current_user)):
    """MCX commodity feed for crude oil and natural gas.

    Kotak Neo or Upstox ticks are preferred when authenticated. Mock rows stay explicit so
    the UI can distinguish demo context from broker data.
    """
    settings = await get_user_settings(user["id"])
    data_broker = settings.get("data_broker", "upstox")
    rows = []

    if data_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user["id"])
        if upstox_gw and upstox_gw.connected:
            # Resolve commodity tokens
            keys = []
            futures_by_symbol: Dict[str, Dict[str, Any]] = {}
            for s in COMMODITY_SYMBOLS:
                contract = await _resolve_upstox_mcx_future_contract(s["symbol"])
                if contract and contract.get("instrument_key"):
                    futures_by_symbol[s["symbol"]] = contract
                    keys.append(contract["instrument_key"])
            if keys:
                upstox_gw.start_market_data_ws(keys, mode="ltpc")
                
            for i, s in enumerate(COMMODITY_SYMBOLS):
                contract = futures_by_symbol.get(s["symbol"]) or await _resolve_upstox_mcx_future_contract(s["symbol"])
                token = contract.get("instrument_key") if contract else None
                tick = upstox_gw.latest_tick(token) if token else None
                if tick and tick.get("ltp"):
                    price = float(tick["ltp"])
                    change = round(price - s["base"], 2)
                    pct = round((change / s["base"]) * 100, 2) if s["base"] else 0.0
                    rows.append({
                        "symbol": s["symbol"],
                        "name": s["name"],
                        "exchange": s["exchange"],
                        "price": price,
                        "change": change,
                        "pct": pct,
                        "source": "upstox",
                        "feed": tick.get("source") or "upstox-mcx",
                        "instrument_key": token,
                        "token": token,
                        "trading_symbol": contract.get("trading_symbol") if contract else None,
                        "timestamp": tick.get("timestamp") or tick.get("last_trade_time") or tick.get("received_at"),
                        "timestamp_source": tick.get("timestamp_source"),
                        "received_at": tick.get("received_at"),
                        "tick_time": tick.get("timestamp") or tick.get("last_trade_time") or tick.get("received_at"),
            "data_age_sec": _market_data_age_sec(tick.get("received_at")),
                        "bid": tick.get("bid"),
                        "ask": tick.get("ask"),
                        "market_status": "open" if _is_order_market_open("MCX") else "closed",
                        "block_reason": None if _is_order_market_open("MCX") else "MCX market is closed",
                    })
                    continue
                if not token:
                    logger.warning("MCX watchlist instrument not resolved from Upstox master symbol=%s", s["symbol"])
                lp = live_price(s["base"], i + 100)
                rows.append({
                    "symbol": s["symbol"],
                    "name": s["name"],
                    "exchange": s["exchange"],
                    **lp,
                    "source": "upstox_pending",
                    "feed": "upstox-mcx-mock",
                    "instrument_key": token,
                    "token": token,
                    "received_at": None,
                    "data_age_sec": None,
                    "market_status": "open" if _is_order_market_open("MCX") else "closed",
                    "block_reason": "instrument token unresolved" if not token else "data feed unavailable",
                })
            return rows

    # Default to Kotak Neo or mock
    gateway = _KOTAK_GATEWAYS.get(user["id"]) if settings.get("data_broker") == "kotak_neo" else None
    for i, s in enumerate(COMMODITY_SYMBOLS):
        tick = gateway.latest_tick_by_symbol(s["symbol"]) if gateway and gateway.status().get("authenticated") else None
        if tick and tick.get("ltp"):
            price = float(tick["ltp"])
            change = round(price - s["base"], 2)
            pct = round((change / s["base"]) * 100, 2) if s["base"] else 0.0
            rows.append({
                "symbol": s["symbol"],
                "name": s["name"],
                "exchange": s["exchange"],
                "price": price,
                "change": change,
                "pct": pct,
                "source": "kotak_neo",
                "feed": "kotak-neo-mcx",
                "tick_time": tick.get("received_at"),
            })
            continue
        lp = live_price(s["base"], i + 100)
        rows.append({
            "symbol": s["symbol"],
            "name": s["name"],
            "exchange": s["exchange"],
            **lp,
            "source": "mock",
            "feed": "mock-mcx",
        })
    return rows



@api.get("/market/quote/{symbol}")
async def quote(symbol: str, user=Depends(get_current_user)):
    found = next((s for s in [*SYMBOLS, *COMMODITY_SYMBOLS] if s["symbol"] == symbol.upper()), None)
    if not found:
        raise HTTPException(status_code=404, detail="Symbol not found")
    idx = [*SYMBOLS, *COMMODITY_SYMBOLS].index(found)
    if found.get("exchange") == "MCX":
        rows = await commodity_watchlist(user=user)
        item = next((r for r in rows if r["symbol"] == found["symbol"]), None)
        if item:
            return item
    kite, _ = await get_user_kite(user["id"])
    if kite:
        q = kite_helper.safe_quote(kite, [_nse_token(found["symbol"])])
        if q:
            node = list(q.values())[0]
            price = node.get("last_price", found["base"])
            ohlc = node.get("ohlc", {}) or {}
            ohlc_close = ohlc.get("close") or price
            return {
                "symbol": found["symbol"], "name": found["name"],
                "price": price,
                "change": round(price - ohlc_close, 2),
                "pct": round((price - ohlc_close) / ohlc_close * 100, 2) if ohlc_close else 0,
                "series": historical_series(price, 60),
                "source": "live",
            }
    lp = live_price(found["base"], idx)
    return {"symbol": found["symbol"], "name": found["name"], **lp,
            "series": historical_series(found["base"], 60), "source": "mock"}


# ============== Routes: Strategies ==============
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
        "cooldown_minutes": 7,
        "max_trades_day": 4,
        "daily_loss_limit": 450.0,
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
        "max_trades_day": 2,
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
        "max_trades_day": 2,
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
        "max_trades_day": 2,
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
        return "volatile_breakout" if underlying in COMMODITY_UNDERLYINGS else "breakout"
    if any(token in text for token in ("rsi", "pullback", "reversion", "swing")):
        return "pullback"
    return "momentum"


def _strategy_risk_profile(template: Dict[str, Any]) -> Dict[str, Any]:
    style = str(template.get("risk_style") or _classify_strategy_risk_style(template))
    risk = {
        **DEFAULT_STRATEGY_RISK,
        **RISK_STYLE_PRESETS.get(style, RISK_STYLE_PRESETS["momentum"]),
        **dict(template.get("risk") or {}),
    }
    risk["risk_style"] = style
    risk["adaptive_exits_enabled"] = True
    risk["trailing_sl_enabled"] = True
    return risk


def _risk_update_fields(risk: Dict[str, Any], prefix: str = "visual_config.risk") -> Dict[str, Any]:
    return {
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
        f"{prefix}.adaptive_exits_enabled": bool(risk.get("adaptive_exits_enabled", True)),
        f"{prefix}.target_r_multiple": float(risk.get("target_r_multiple") or DEFAULT_STRATEGY_RISK["target_r_multiple"]),
    }


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


DEFAULT_OPTION_STRATEGIES = [
    {
        "name": "UPSTOX NIFTY ATM Option Momentum Buyer",
        "description": "Upstox-compatible single-leg NIFTY ATM option buying strategy. Uses live NIFTY candles, resolves the exact Upstox option instrument_key, enters on momentum, and exits through the same order manager.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
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
        "name": "UPSTOX BANKNIFTY ATM Option Breakout Buyer",
        "description": "Upstox-compatible single-leg BANKNIFTY ATM option buying strategy. It avoids multi-leg selling, resolves the exact Upstox option instrument_key, and lets the order manager place one BUY/exit cycle.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 30000.0, "instrument_group": "NFO",
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
        "description": "Adaptive HFT option buying scalper for BANKNIFTY using dynamic standard deviation bands.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 15000.0, "instrument_group": "NFO",
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
        "description": "Super low-capital (INR 5,000) retail option buying scalp. Uses a fast 5-minute EMA crossover (3 EMA crossing 9 EMA) to catch quick momentum swings.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 5000.0, "instrument_group": "NFO",
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
        "name": "UPSTOX MCX Crude Mini EMA Option Buyer",
        "description": "Upstox MCX CRUDEOILM long-option scalper using fast EMA momentum on live 5-minute futures candles. One mini lot by default.",
        "underlying": "CRUDEOILM", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 6000.0, "instrument_group": "MCX",
        "python_code": CRUDEOILM_EMA_MOMENTUM_CODE,
        "market_suitability": "Fast intraday crude mini momentum",
    },
    {
        "name": "UPSTOX MCX Crude Mini RSI Option Buyer",
        "description": "Upstox MCX CRUDEOILM long-option reversal strategy for stretched RSI moves. One mini lot by default.",
        "underlying": "CRUDEOILM", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 6000.0, "instrument_group": "MCX",
        "python_code": CRUDEOILM_RSI_REVERSION_CODE,
        "market_suitability": "Mean reversion after sharp crude mini moves",
    },
    {
        "name": "UPSTOX MCX Natural Gas Breakout Option Buyer",
        "description": "Upstox MCX NATURALGAS long-option breakout strategy using volatility bands on live 5-minute futures candles.",
        "underlying": "NATURALGAS", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 18000.0, "instrument_group": "MCX",
        "python_code": NATURALGAS_HFT_MICRO_SCALPER_CODE,
        "market_suitability": "Natural gas volatility expansion",
    },
    {
        "name": "UPSTOX MCX Crude Volatility Option Buyer",
        "description": "Upstox MCX CRUDEOIL long-option breakout strategy for compression-to-expansion crude oil moves.",
        "underlying": "CRUDEOIL", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 30000.0, "instrument_group": "MCX",
        "python_code": CRUDEOIL_HFT_VOLATILITY_CODE,
        "market_suitability": "Crude oil volatility breakout",
    },
]


for _template in [*DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]:
    if str(_template.get("strategy_type") or "").lower() == "option buying":
        _template["python_code"] = RETAIL_LIVE_STATE_CODE
        _template["market_suitability"] = _template.get("market_suitability") or "Retail live momentum"


_seed_templates_by_name = {
    template["name"]: template
    for template in [*LEGACY_OPTION_STRATEGIES, *DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]
}
DEFAULT_OPTION_STRATEGIES = list(_seed_templates_by_name.values())

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
    instrument_group = str(template.get("instrument_group") or ("BFO" if underlying == "SENSEX" else "MCX" if underlying in COMMODITY_UNDERLYINGS else "NFO")).upper()
    strategy_type = template.get("strategy_type") or ("Option Selling" if str(template.get("strike_mode") or "").upper().endswith("SELL") else "Option Buying")
    required_capital = float(template.get("required_capital") or (45000.0 if underlying == "SENSEX" else COMMODITY_REQUIRED_CAPITAL.get(underlying, 35000.0)))
    is_commodity = instrument_group == "MCX" or underlying in COMMODITY_UNDERLYINGS
    options_block = {
        "enabled": True,
        "underlying": underlying,
        "strike_mode": template["strike_mode"],
        "otm_points": template["otm_points"],
        "expiry_offset": template.get("expiry_offset", 0),
        "lots": template["lots"],
        "required_capital": required_capital,
    }
    risk_profile = {**_strategy_risk_profile(template), "required_capital": required_capital}
    return {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "name": template["name"],
        "description": template["description"],
        "kind": "python",
        "python_code": template["python_code"],
        "asset_class": "commodity" if is_commodity else "options",
        "strategy_type": strategy_type,
        "required_capital": required_capital,
        "instrument_group": instrument_group,
        "broker": "upstox",
        "mode": "live",
        "market_suitability": template.get("market_suitability", "Any Market Condition"),
        "visual_config": {
            "symbol": underlying,
            "exchange": "MCX" if is_commodity else instrument_group,
            "options": options_block,
            "commodity_options": options_block if is_commodity else None,
            "risk": risk_profile,
        },
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_pnl": None,
        "evaluations": 0,
        "signals_fired": 0,
    }



def _strategy_asset_class(row: Dict[str, Any]) -> str:
    explicit = (row.get("asset_class") or "").lower()
    if explicit in ("equity", "options", "futures", "commodity"):
        return explicit
    visual_config = row.get("visual_config") or {}
    symbol = str(visual_config.get("symbol") or "").upper()
    if symbol in COMMODITY_UNDERLYINGS:
        return "commodity"
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
    if underlying in COMMODITY_UNDERLYINGS:
        return "MCX"
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
    options_config = visual_config.get("commodity_options") or visual_config.get("options") or {}
    strike_mode = str(options_config.get("strike_mode") or "").upper()
    name = str(row.get("name") or "").lower()
    if strike_mode.endswith("SELL") or any(token in name for token in ("condor", "covered call", "short straddle", "selling")):
        return "Option Selling"
    return "Option Buying"


def _strategy_required_capital(row: Dict[str, Any]) -> float:
    for value in (
        row.get("required_capital"),
        ((row.get("visual_config") or {}).get("risk") or {}).get("required_capital"),
        (((row.get("visual_config") or {}).get("options") or {}).get("required_capital")),
        (((row.get("visual_config") or {}).get("commodity_options") or {}).get("required_capital")),
    ):
        if value is not None:
            try:
                return round(float(value), 2)
            except (TypeError, ValueError):
                pass
    visual_config = row.get("visual_config") or {}
    options_config = visual_config.get("commodity_options") or visual_config.get("options") or {}
    underlying = str(options_config.get("underlying") or visual_config.get("symbol") or "").upper()
    base = {
        "NIFTY": 35000.0,
        "SENSEX": 45000.0,
        **COMMODITY_REQUIRED_CAPITAL,
    }.get(underlying, 25000.0)
    if _strategy_type(row) == "Option Selling":
        base = max(base, 125000.0)
    return base


def _strategy_out(row: Dict[str, Any]) -> StrategyOut:
    clean = dict(row)
    clean.pop("_id", None)
    clean.pop("user_id", None)
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
        res = await db.strategies.update_one(
            {"user_id": user_id, "name": template["name"]},
            {"$set": {
                "description": template["description"],
                "python_code": template["python_code"],
                "strategy_type": template.get("strategy_type", "Option Buying"),
                "required_capital": float(template.get("required_capital") or 0),
                "instrument_group": template.get("instrument_group"),
                "market_suitability": template.get("market_suitability", "Retail live momentum"),
                "visual_config.options.enabled": True,
                "visual_config.options.underlying": str(template.get("underlying") or "NIFTY").upper(),
                "visual_config.options.strike_mode": template.get("strike_mode", "ATM_BUY"),
                "visual_config.options.otm_points": int(template.get("otm_points") or 0),
                "visual_config.options.lots": int(template.get("lots") or 1),
                **_risk_update_fields(risk_profile),
                "default_strategy_version": "retail-balanced-v3",
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
    res = await db.strategies.update_many(
        {"user_id": user_id},
        {"$set": update, "$unset": unset},
    )
    return int(res.modified_count or 0)


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


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _adaptive_risk_percentages(entry: float, risk: Dict[str, Any]) -> Dict[str, float]:
    stop = float(risk["stop_loss_pct"])
    target = float(risk["take_profit_pct"])
    trigger = float(risk["trail_trigger_pct"])
    step = float(risk["trail_step_pct"])
    if not risk.get("adaptive_exits_enabled", True) or entry <= 0:
        return {"stop": stop, "target": target, "trigger": trigger, "step": step}

    style = str(risk.get("risk_style") or "balanced")
    bounds = {
        "micro_scalp": (3.5, 7.5, 1.15, 1.35),
        "momentum": (4.5, 9.5, 1.25, 1.55),
        "breakout": (5.5, 11.5, 1.35, 1.70),
        "volatile_breakout": (6.5, 13.5, 1.40, 1.85),
        "pullback": (4.5, 9.0, 1.25, 1.55),
        "balanced": (4.5, 10.0, 1.25, 1.55),
    }
    min_stop, max_stop, min_r, max_r = bounds.get(style, bounds["balanced"])
    premium_factor = 1.18 if entry < 75 else 0.88 if entry > 250 else 1.0
    stop = _clamp_float(stop * premium_factor, min_stop, max_stop)
    r_multiple = _clamp_float(float(risk.get("target_r_multiple") or min_r), min_r, max_r)
    target = _clamp_float(max(target, stop * r_multiple), stop * min_r, stop * max_r)
    trigger = _clamp_float(min(trigger, stop * 0.75), 2.5, max(3.0, stop * 0.95))
    step = _clamp_float(min(step, stop * 0.45), 1.5, max(2.0, stop * 0.65))
    return {"stop": stop, "target": target, "trigger": trigger, "step": step}


def _risk_pct(risk: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = risk.get(key)
        if value not in (None, ""):
            try:
                pct = float(value)
                return pct if pct > 1 else pct * 100.0
            except (TypeError, ValueError):
                continue
    return float(default)


def _normalize_strategy_risk(risk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(risk or {})
    stop_pct = _risk_pct(raw, "stop_loss_pct", "stoploss_pct", "stop_pct", default=DEFAULT_STRATEGY_RISK["stop_loss_pct"])
    target_pct = _risk_pct(raw, "take_profit_pct", "target_pct", "tp_pct", default=DEFAULT_STRATEGY_RISK["take_profit_pct"])
    trail_trigger_pct = _risk_pct(raw, "trail_trigger_pct", default=DEFAULT_STRATEGY_RISK["trail_trigger_pct"])
    trail_step_pct = _risk_pct(raw, "trail_step_pct", default=DEFAULT_STRATEGY_RISK["trail_step_pct"])
    risk_style = str(raw.get("risk_style") or DEFAULT_STRATEGY_RISK["risk_style"])
    target_r_multiple = float(raw.get("target_r_multiple") or DEFAULT_STRATEGY_RISK["target_r_multiple"])
    raw.update({
        "stop_loss_pct": stop_pct,
        "stoploss_pct": stop_pct,
        "take_profit_pct": target_pct,
        "target_pct": target_pct,
        "trail_trigger_pct": trail_trigger_pct,
        "trail_step_pct": trail_step_pct,
        "trailing_sl_enabled": bool(raw.get("trailing_sl_enabled", True)),
        "cooldown_minutes": int(raw.get("cooldown_minutes") or DEFAULT_STRATEGY_RISK["cooldown_minutes"]),
        "max_trades_day": int(raw.get("max_trades_day") or DEFAULT_STRATEGY_RISK["max_trades_day"]),
        "daily_loss_limit": float(raw.get("daily_loss_limit") or DEFAULT_STRATEGY_RISK["daily_loss_limit"]),
        "time_exit_minutes": int(raw.get("time_exit_minutes") or DEFAULT_STRATEGY_RISK["time_exit_minutes"]),
        "indicator_exit_enabled": bool(raw.get("indicator_exit_enabled", DEFAULT_STRATEGY_RISK["indicator_exit_enabled"])),
        "exit_mode": raw.get("exit_mode") or DEFAULT_STRATEGY_RISK["exit_mode"],
        "risk_style": risk_style,
        "adaptive_exits_enabled": bool(raw.get("adaptive_exits_enabled", DEFAULT_STRATEGY_RISK["adaptive_exits_enabled"])),
        "target_r_multiple": target_r_multiple,
    })
    return raw


def _position_risk_prices(position: Dict[str, Any], ltp: Optional[float] = None) -> Dict[str, Optional[float]]:
    entry = float(position.get("average_buy_price") or 0)
    if entry <= 0:
        return {"stop_loss": None, "take_profit": None, "trailing_sl": None}
    risk = _normalize_strategy_risk(position.get("tp_sl_tsl_config") or {})
    side = str(position.get("position_side") or "LONG").upper()
    stop_price = risk.get("stoploss_price") or risk.get("stop_loss")
    target_price = risk.get("target_price") or risk.get("take_profit")
    dynamic = _adaptive_risk_percentages(entry, risk)
    if stop_price in (None, ""):
        stop_pct = dynamic["stop"]
        stop_price = entry * (1 - stop_pct / 100) if side != "SHORT" else entry * (1 + stop_pct / 100)
    if target_price in (None, ""):
        target_pct = dynamic["target"]
        target_price = entry * (1 + target_pct / 100) if side != "SHORT" else entry * (1 - target_pct / 100)
    trailing_sl = risk.get("trailing_sl")
    if risk.get("trailing_sl_enabled") and ltp and ltp > 0:
        trigger_pct = dynamic["trigger"]
        step_pct = dynamic["step"]
        if side == "SHORT" and ltp <= entry * (1 - trigger_pct / 100):
            candidate = ltp * (1 + step_pct / 100)
            trailing_sl = min(float(trailing_sl or candidate), candidate)
        elif side != "SHORT" and ltp >= entry * (1 + trigger_pct / 100):
            candidate = ltp * (1 - step_pct / 100)
            trailing_sl = max(float(trailing_sl or 0), candidate)
    effective_stop = trailing_sl or stop_price
    return {
        "stop_loss": round(float(effective_stop), 2) if effective_stop not in (None, "") else None,
        "take_profit": round(float(target_price), 2) if target_price not in (None, "") else None,
        "trailing_sl": round(float(trailing_sl), 2) if trailing_sl not in (None, "") else None,
    }


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


def _strategy_lock_ids(user_id: str, strategy_id: str, instrument_key: str) -> List[str]:
    return [
        f"{user_id}:instrument:{instrument_key}",
        f"{user_id}:strategy:{strategy_id}",
    ]


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
    source: str,
) -> Optional[Dict[str, Any]]:
    """Create the central ownership row before sending a strategy BUY.

    Sparse unique indexes on active_instrument_key and active_strategy_key make
    the reservation atomic across runner cycles and concurrent requests.
    """
    if not strategy_id:
        return None
    existing = await db.strategy_positions.find_one({
        "user_id": user_id,
        "$or": [
            {"active_instrument_key": _active_key(user_id, instrument_key)},
            {"active_strategy_key": _active_key(user_id, strategy_id)},
        ],
        "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
    }, {"_id": 0})
    if existing:
        if existing.get("instrument_key") == instrument_key:
            raise HTTPException(
                status_code=409,
                detail=f"Instrument already has active strategy position: {existing.get('strategy_id')} {existing.get('status')}. New BUY blocked.",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Strategy already has active position {existing.get('trading_symbol')} ({existing.get('status')}). Re-entry blocked.",
        )

    row = await _strategy_row(user_id, strategy_id)
    risk = _normalize_strategy_risk(((row or {}).get("visual_config") or {}).get("risk") or {})
    now = datetime.now(timezone.utc).isoformat()
    lock_ids = _strategy_lock_ids(user_id, strategy_id, instrument_key)
    lock_docs = [
        {
            "_id": lock_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "instrument_key": instrument_key,
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
            detail=f"Instrument/strategy already reserved by another scan cycle. Duplicate BUY blocked for {trading_symbol}.",
        )

    # Resolve symbol_group
    vc = (row or {}).get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    if opt_cfg.get("enabled"):
        symbol_group = str(opt_cfg.get("underlying") or "NIFTY").upper()
    else:
        symbol_group = str(vc.get("symbol") or trading_symbol).upper()

    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "strategy_id": strategy_id,
        "instrument_key": instrument_key,
        "symbol_group": symbol_group,
        "active_instrument_key": _active_key(user_id, instrument_key),
        "active_strategy_key": _active_key(user_id, strategy_id),
        "instrument_token": instrument_token,
        "trading_symbol": trading_symbol,
        "symbol": trading_symbol,
        "exchange": exchange,
        "quantity": int(quantity),
        "open_quantity": int(quantity),
        "average_buy_price": float(entry_price or 0),
        "entry_time": now,
        "status": "RESERVED",
        "tp_sl_tsl_config": dict(risk),
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await db.strategy_positions.insert_one(doc)
    except DuplicateKeyError:
        await db.strategy_position_locks.delete_many({"_id": {"$in": lock_ids}})
        raise HTTPException(
            status_code=409,
            detail=f"Instrument/strategy already reserved by another scan cycle. Duplicate BUY blocked for {trading_symbol}.",
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
    await db.strategy_positions.update_one(
        {"id": reservation["id"], "user_id": reservation["user_id"]},
        {"$set": {
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
        }},
    )


async def _cancel_strategy_reservation(reservation: Optional[Dict[str, Any]], reason: str) -> None:
    if not reservation:
        return
    now = datetime.now(timezone.utc).isoformat()
    await db.strategy_positions.update_one(
        {"id": reservation["id"], "user_id": reservation["user_id"], "status": "RESERVED"},
        {"$set": {"status": "CANCELLED", "cancel_reason": reason, "updated_at": now},
         "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
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
            "realised_pnl": pnl,
            "exit_reason": reason,
            "closed_at": now,
            "updated_at": now,
        }, "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
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
    await db.strategy_position_locks.delete_many({
        "_id": {"$in": _strategy_lock_ids(str(user_id), str(strategy_id), str(instrument_key))}
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


async def _close_strategy_positions(user_id: str, sid: str, reason: str = "auto-exit") -> Dict[str, Any]:
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
        exit_side = "BUY" if str(pos.get("asset_class") or "").upper() == "OPTION_SHORT" or str(pos.get("position_side") or "").upper() == "SHORT" else "SELL"
        place_kwargs: Dict[str, Any] = {
            "user_id": user_id,
            "side": exit_side,
            "order_type": "MARKET",
            "product": pos.get("product"),
            "source": f"{reason}:strategy:{sid}",
        }
        if pos.get("asset_type") == "option" or str(pos.get("exchange") or "").upper() in {"NFO", "BFO", "MCX"}:
            lot_size = int(pos.get("lot_size") or 1)
            place_kwargs["symbol"] = sym
            place_kwargs["option_contract"] = {
                "tradingsymbol": sym,
                "exchange": pos.get("exchange", "NFO"),
                "instrument_token": pos.get("instrument_token"),
                "lot_size": lot_size,
                "strike": pos.get("strike"),
                "expiry": pos.get("expiry"),
                "underlying": pos.get("underlying"),
                "option_type": pos.get("option_type"),
                "transaction_type": exit_side,
            }
            place_kwargs["qty"] = max(1, math.ceil(qty_net / lot_size))
        else:
            place_kwargs["symbol"] = sym
            place_kwargs["qty"] = qty_net
            place_kwargs["exchange"] = pos.get("exchange") or "NSE"
        try:
            result = await _place_order_core(**place_kwargs)
            results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "ok", "order_id": result.get("id")})
        except Exception as e:
            results.append({"symbol": sym, "qty": qty_net, "side": exit_side, "status": "failed", "error": str(e)})
    if reason in ("risk-trigger", "feed-stale"):
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
        if gateway and gateway.connected and exchange == "MCX" and not token:
            contract = await _resolve_upstox_mcx_future_contract(symbol)
            token = contract.get("instrument_key") if contract else None
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
    all_symbols = [*SYMBOLS, *COMMODITY_SYMBOLS]
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
    gateway = await get_user_kotak_gateway(user_id)
    if not gateway:
        raise HTTPException(status_code=400, detail="Kotak Neo is not configured. Save Consumer Key and env credentials first.")
    status = gateway.status()
    if not status.get("authenticated"):
        raise HTTPException(status_code=400, detail="Kotak Neo is not connected. Open Broker Keys and click Connect Kotak.")
    execution_tag = tag or _new_execution_tag()
    attempts = 0
    result: Dict[str, Any] = {}
    symbol_used = None
    max_attempts = int(os.environ.get("KOTAK_ORDER_MAX_ATTEMPTS", "1"))
    candidates = _kotak_trading_symbol_candidates(exchange, trading_symbol)
    for candidate_idx, candidate in enumerate(candidates):
        symbol_used = candidate
        for attempt in range(1, max(1, max_attempts) + 1):
            attempts += 1
            result = await asyncio.to_thread(
                gateway.place_order,
                exchange_segment=_kotak_exchange_segment(exchange),
                product=(product or "MIS").upper(),
                price=0 if (order_type or "MARKET").upper() == "MARKET" else float(price or 0),
                quantity=int(quantity),
                trading_symbol=candidate,
                transaction_type=_kotak_transaction_type(side.upper()),
                order_type=_kotak_order_type(order_type),
                tag=execution_tag,
            )
            if result.get("ok"):
                break
            error = str(result.get("error") or "")
            if not OrderExecutionRetry.is_retryable_error(error) or attempt >= max_attempts:
                break
            retry_cfg = OrderExecutionRetry.retry_config(attempt)
            await asyncio.sleep(min(3, float(retry_cfg.get("backoff_seconds") or 1)))
        if result.get("ok"):
            break
        error = str(result.get("error") or "")
        if candidate_idx >= len(candidates) - 1 or not _kotak_symbol_error(error):
            break
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"Kotak rejected order: {result.get('error')}")
    broker_order_id = _extract_kotak_order_id(result.get("response"))
    if not broker_order_id:
        logger.warning("Kotak order response did not include order id: %s", result.get("response"))
    return {
        "ok": True,
        "broker_order_id": broker_order_id,
        "raw": result.get("response"),
        "tag": execution_tag,
        "attempts": attempts,
        "trading_symbol": symbol_used,
    }


UPSTOX_EQUITY_INSTRUMENTS = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "INFY": "NSE_EQ|INE009A01021",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "SBIN": "NSE_EQ|INE062A01020",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "ITC": "NSE_EQ|INE154A01025",
    "LT": "NSE_EQ|INE018A01030",
    "MARUTI": "NSE_EQ|INE585B01010",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}


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
        token_clean = token.split("|")[-1]
        return f"MCX_FO|{token_clean}"
        
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
        # Query sync db to resolve token from the MCX cache
        try:
            sync_db = get_sync_db()
            from datetime import datetime, timezone, timedelta
            ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
            
            # 1. Generic underlying (e.g. CRUDEOIL, NATURALGAS)
            if symbol in COMMODITY_UNDERLYINGS:
                row = sync_db.upstox_mcx_future_contracts.find_one(
                    {
                        "underlying": symbol,
                        "exchange": "MCX",
                        "instrument_type": "FUTCOM",
                        "expiry": {"$gte": ist_today},
                    },
                    sort=[("expiry", 1), ("trading_symbol", 1)]
                )
                if row and row.get("instrument_key"):
                    return f"MCX_FO|{row['instrument_key'].split('|')[-1]}"
                    
            # 2. Specific future contract symbol (e.g. CRUDEOIL26JUNFUT)
            row = sync_db.upstox_mcx_future_contracts.find_one({"trading_symbol": symbol})
            if row and row.get("instrument_key"):
                return f"MCX_FO|{row['instrument_key'].split('|')[-1]}"
                
            # 3. Option contract symbol (e.g. CRUDEOIL26JUNFUT CE/PE)
            row = sync_db.upstox_mcx_option_contracts.find_one({"trading_symbol": symbol})
            if row and row.get("instrument_key"):
                return f"MCX_FO|{row['instrument_key'].split('|')[-1]}"
        except Exception as e:
            logger.warning("MCX instrument token resolve exception: %s", e)
        
        # Remove silent fallback and log resolution failure
        logger.error("MCX instrument resolution failed: no master contract found for symbol %s in DB cache.", symbol)
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
    symbol = str(underlying or "").upper().strip()
    if not symbol:
        return []
    resolver = getattr(app.state, "mcx_contract_resolver", None) or MCXContractResolver(db)
    app.state.mcx_contract_resolver = resolver
    keys: List[str] = []
    contract = await resolver.resolve_future(underlying=symbol, expiry_offset=0, allow_refresh=True)
    if contract and contract.get("instrument_key"):
        keys.append(str(contract["instrument_key"]))
    return list(dict.fromkeys(keys))[:limit]


async def _resolve_upstox_mcx_future_contract(underlying: str, *, expiry_offset: int = 0) -> Optional[Dict[str, Any]]:
    resolver = getattr(app.state, "mcx_contract_resolver", None) or MCXContractResolver(db)
    app.state.mcx_contract_resolver = resolver
    return await resolver.resolve_future(underlying=underlying, expiry_offset=expiry_offset, allow_refresh=True)


async def _validate_upstox_mcx_instrument_key(instrument_key: Optional[str]) -> Optional[Dict[str, Any]]:
    resolver = getattr(app.state, "mcx_contract_resolver", None) or MCXContractResolver(db)
    app.state.mcx_contract_resolver = resolver
    return await resolver.validate_instrument_key(instrument_key)



async def _upstox_watchlist_rows(user_id: str) -> List[Dict[str, Any]]:
    gateway = await get_user_upstox_gateway(user_id)
    if not gateway or not gateway.connected:
        return []
    keys = [_upstox_instrument_token("NSE", s["symbol"]) for s in SYMBOLS]
    keys = [k for k in keys if k]
    if not keys:
        return []
    try:
        quote = await asyncio.to_thread(gateway.get_market_quote, keys)
    except Exception as exc:
        logger.warning("Upstox watchlist quote failed: %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for s in SYMBOLS:
        token = _upstox_instrument_token("NSE", s["symbol"])
        ltp = UpstoxGateway.parse_quote_ltp(quote, token) if token else None
        if ltp is None:
            lp = live_price(s["base"], SYMBOLS.index(s))
            out.append({**lp, "symbol": s["symbol"], "name": s["name"], "source": "upstox_pending", "feed": "upstox-rest"})
            continue
        change = round(float(ltp) - s["base"], 2)
        pct = round((change / s["base"]) * 100, 2) if s["base"] else 0.0
        out.append({
            "symbol": s["symbol"],
            "name": s["name"],
            "price": float(ltp),
            "change": change,
            "pct": pct,
            "source": "upstox",
            "feed": "upstox-rest",
        })
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
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _age_ms(value: Optional[str]) -> Optional[int]:
    parsed = _parse_iso_dt(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds() * 1000))


async def _evaluate_strategy_risk(user_id: str, sid: str) -> bool:
    risk = await _get_strategy_risk(user_id, sid)
    if not risk:
        return False
    stop_loss_pct = float(risk.get("stop_loss_pct", 0))
    take_profit_pct = float(risk.get("take_profit_pct", 0))
    orders = await _collect_strategy_orders(user_id, sid)
    if not orders:
        return False
    net_positions: Dict[str, int] = {}
    entry_prices: Dict[str, float] = {}
    for o in orders:
        symbol = o["symbol"]
        qty = int(o.get("filled_qty") or o.get("qty") or 0)
        sign = 1 if o["side"] == "BUY" else -1
        net_positions[symbol] = net_positions.get(symbol, 0) + sign * qty
        if sign > 0:
            total_cost = entry_prices.get(symbol, 0) * entry_prices.get(f"{symbol}_qty", 0) + qty * float(o.get("price", 0))
            total_qty = entry_prices.get(f"{symbol}_qty", 0) + qty
            entry_prices[symbol] = total_cost / total_qty if total_qty else float(o.get("price", 0))
            entry_prices[f"{symbol}_qty"] = total_qty
    for symbol, qty_net in net_positions.items():
        if qty_net == 0:
            continue
        current_price = None
        order = next((o for o in reversed(orders) if o["symbol"] == symbol), None)
        if order and order.get("asset_type") == "option":
            exchange = order.get("exchange") or "NFO"
            current_price = await _current_ltp_for_symbol(user_id, symbol, exchange)
            if current_price is None and order.get("underlying") and order.get("entry_spot"):
                underlying_price = await _current_ltp_for_symbol(user_id, order["underlying"], "NSE")
                if underlying_price and float(order.get("entry_spot", 0)):
                    current_price = round(float(order.get("price", 0)) * (underlying_price / float(order.get("entry_spot", 1))), 2)
        else:
            current_price = await _current_ltp_for_symbol(user_id, symbol, "NSE")
        if current_price is None:
            continue
        entry_price = entry_prices.get(symbol) or float(order.get("price", 0))
        if qty_net > 0:
            if stop_loss_pct and current_price <= entry_price * (1 - stop_loss_pct):
                return True
            if take_profit_pct and current_price >= entry_price * (1 + take_profit_pct):
                return True
        else:
            if stop_loss_pct and current_price >= entry_price * (1 + stop_loss_pct):
                return True
            if take_profit_pct and current_price <= entry_price * (1 - take_profit_pct):
                return True
    return False


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


async def _strategy_health_loop(stop_event: asyncio.Event):
    logger.info("Strategy health monitor starting")
    while not stop_event.is_set():
        try:
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
                    if await _evaluate_strategy_risk(uid, s["id"]):
                        await _close_strategy_positions(uid, s["id"], reason="risk-trigger")
                except Exception as e:
                    logger.warning(f"strategy risk evaluation failed for {s['id']}: {e}")
        except Exception as e:
            logger.warning(f"strategy health loop error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < TICK_SECONDS:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Strategy health monitor stopped")


@api.post("/strategies/{sid}/unwind")
async def unwind_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    result = await _close_strategy_positions(user["id"], sid, reason="manual-unwind")
    return {"ok": True, **result}


@api.post("/strategies", response_model=StrategyOut)
async def create_strategy(req: StrategyReq, user=Depends(get_current_user)):
    visual_config = req.visual_config or {}
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



@api.get("/strategies", response_model=List[StrategyOut])
async def list_strategies(user=Depends(get_current_user)):
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    return [_strategy_out(r) for r in rows]


@api.post("/strategies/seed-defaults")
async def seed_default_strategies(user=Depends(get_current_user)):
    inserted = await seed_default_strategies_for_user(user["id"])
    migrated = await migrate_user_to_v12_upstox(user["id"])
    return {
        "ok": True,
        "inserted": inserted,
        "migrated": migrated,
        "message": "Standardized index and MCX option presets installed. Review and backtest before enabling LIVE.",
    }


@api.post("/ops/v12/upstox-retailer/activate")
async def activate_v12_upstox_retailer(user=Depends(get_current_user)):
    inserted = await seed_default_strategies_for_user(user["id"])
    migrated = await migrate_user_to_v12_upstox(user["id"])
    await db.strategies.update_many(
        {"user_id": user["id"], "status": {"$nin": ["live", "paused"]}},
        {"$set": {"status": "live", "broker": "upstox", "mode": "live"}},
    )
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).to_list(500)
    for row in strategies:
        _sync_option_ledger_strategy(row)
    return {
        "ok": True,
        "version": APP_VERSION,
        "inserted": inserted,
        "migrated": migrated,
        "live_strategies": sum(1 for s in strategies if s.get("status") == "live"),
        "message": "QuantG v12 Upstox retailer profile is active for NSE/NFO/BSE/BFO/MCX.",
    }


@api.get("/strategies/leaderboard")
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
            "pnl": row.get("realised_pnl"),
            "source": "trade_fills",
        }
        for row in fill_summary["fills"]
        if float(row.get("realised_pnl") or 0) != 0
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
        "realised_pnl": fill_summary["realised_pnl"],
    }
    return result


@api.get("/strategies/{sid}", response_model=StrategyOut)
async def get_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    _sync_option_ledger_strategy(row)
    return _strategy_out(row)


@api.get("/strategies/{sid}/daily-report")
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
    return report


@api.put("/strategies/{sid}", response_model=StrategyOut)
async def update_strategy(sid: str, req: StrategyReq, user=Depends(get_current_user)):
    update = {k: v for k, v in req.model_dump().items() if v is not None}
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


@api.post("/strategies/{sid}/ai-modify")
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


@api.delete("/strategies/{sid}")
async def delete_strategy(sid: str, user=Depends(get_current_user)):
    res = await db.strategies.delete_one({"id": sid, "user_id": user["id"]})
    return {"deleted": res.deleted_count}


@api.post("/strategies/{sid}/toggle")
async def toggle_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    new_status = "paused" if row["status"] == "live" else "live"
    settings = await get_user_settings(user["id"])
    strategy_mode = "paper" if bool(settings.get("paper_mode", True)) else "live"
    update_fields = {
        "status": new_status,
        "broker": "upstox",
        "mode": strategy_mode,
    }
    if strategy_mode == "paper":
        update_fields.update({
            "quarantined": False,
            "halted": False,
            "is_halted": False,
            "last_filter_reason": "",
            "last_skip_reason_code": "",
            "last_error": "",
        })
    await db.strategies.update_one({"id": sid}, {"$set": update_fields})
    if new_status == "live":
        _sync_option_ledger_strategy({**row, **update_fields})
        option_ledger.set_kill_switch(False, strategy_id=sid)
    return {"status": new_status}


@api.put("/strategies/{sid}/runtime-settings")
async def update_strategy_runtime_settings(sid: str, req: StrategyRuntimeSettingsReq, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
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


@api.post("/strategies/{sid}/manual-order")
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
                detail="Could not resolve Upstox option contract. Check OAuth, MCX/NFO permission, and instrument master cache.",
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
        {"id": sid},
        {"$set": {"last_signal_at": datetime.now(timezone.utc).isoformat(),
                  "last_signal_action": f"MANUAL {action}"},
         "$inc": {"signals_fired": 1}},
    )
    return {"ok": True, "order": result}


@api.post("/strategies/{sid}/exit-all")
async def exit_strategy_positions(sid: str, user=Depends(get_current_user)):
    """Square off every stored open Position Manager row for this strategy."""
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return await _close_strategy_positions(user["id"], sid, reason="exit")


@api.post("/strategies/{sid}/test-run")
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
                        {"id": sid},
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
                         "$inc": {"signals_fired": 1, "evaluations": 1}},
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


# ============== Options preview (UI helper) ==============
@api.get("/options/preview")
async def options_preview(
    underlying: str = "NIFTY",
    strike_mode: str = "ATM_BUY",
    otm_points: int = 0,
    action: str = "BUY",
    expiry_offset: int = 0,
    user=Depends(get_current_user),
):
    """What option contract will fire RIGHT NOW for this config? Returns
    contract details for the UI so users see the exact symbol before trading."""
    if underlying.upper() not in options_helper.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Underlying must be one of {options_helper.SUPPORTED}")
    kite, status = await get_user_kite(user["id"])
    if not kite:
        # Show useful info even without Kite — return config + lot size + note
        return {
            "available": False,
            "reason": "Zerodha not connected — preview will be live after you connect on Broker Keys",
            "underlying": underlying.upper(),
            "lot_size": options_helper.LOT_SIZES.get(underlying.upper()),
            "strike_interval": options_helper.STRIKE_INTERVALS.get(underlying.upper()),
            "exchange": options_helper.OPT_EXCHANGE.get(underlying.upper()),
        }
    contract = options_helper.resolve_for_signal(
        kite,
        underlying=underlying.upper(),
        signal_action=action.upper(),
        strike_mode=strike_mode.upper(),
        otm_points=int(otm_points),
        expiry_offset_weeks=int(expiry_offset),
    )
    if not contract:
        return {
            "available": False,
            "reason": "Could not resolve a contract — markets may be closed or instruments unavailable.",
            "underlying": underlying.upper(),
        }
    return {"available": True, **contract}


@api.post("/strategies/backtest")
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
        await db.strategies.update_one({"id": req.strategy_id}, {"$set": {
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


async def _check_daily_loss_guard(user_id: str, max_loss: float, mode: str = "paper") -> None:
    """Refuse new orders if today's realised loss already exceeds max_daily_loss.
    Computed from db.orders (paper mode) — tracks realised_pnl on closing trades."""
    if not max_loss or max_loss <= 0:
        return
    start, end = get_trading_day_window_ist()
    orders = await db.orders.find({
        "user_id": user_id,
        "mode": mode,
        "created_at": {"$gte": start, "$lt": end},
        "status": {"$in": [ORDER_FILLED, ORDER_CLOSED, "COMPLETE"]},
    }, {"_id": 0}).to_list(500)
    realised = sum(float(o.get("realised_pnl") or 0) for o in orders)
    if realised <= -abs(max_loss):
        raise HTTPException(
            status_code=400,
            detail=f"Daily loss guard tripped: today's realised loss ₹{abs(realised):.0f} "
                   f"≥ max ₹{max_loss:.0f}. New orders blocked until tomorrow.",
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
    is_option = asset_type == "option" or exchange in {"NFO", "BFO", "MCX"}
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
        status = str(row.get("status") or "").lower()
        if status != "live":
            return _preflight_response(
                ok=False, reason_code="STRATEGY_DISABLED", reason=f"Strategy is not active (status={status or 'unknown'}).",
                strategy_id=strategy_id, intent=intent, option_contract=option_contract, ltp=ltp, market_session=market_session,
            )
        halted_reason = row.get("halt_reason") or row.get("last_halt_reason")
        last_error = str(row.get("last_error") or "")
        if row.get("halted") or row.get("is_halted") or "contract resolution failed" in last_error.lower():
            return _preflight_response(
                ok=False, reason_code="STRATEGY_DISABLED", reason=halted_reason or last_error or "Strategy is halted.",
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
        existing = await db.strategy_positions.find_one({
            "user_id": user_id,
            "$or": [
                {"active_instrument_key": _active_key(user_id, instrument_key)},
                {"active_strategy_key": _active_key(user_id, strategy_id)},
                {"strategy_id": strategy_id, "instrument_key": instrument_key},
            ],
            "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
        })
        if existing:
            return _preflight_response(
                ok=False,
                reason_code="CONFLICT_BLOCKED",
                reason=f"Duplicate open position blocked for {instr.tradingsymbol}.",
                strategy_id=strategy_id,
                intent=intent,
                option_contract=option_contract,
                ltp=ltp,
                market_session=market_session,
            )

    try:
        if _intent_is_entry(intent.intent):
            await _check_daily_loss_guard(user_id, settings.get("max_daily_loss", 0), mode="paper" if paper else "live")
            await _check_trade_count_guard(user_id, int(settings.get("max_trades_per_day") or 0), mode="paper" if paper else "live")
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
    if not kite or not tag:
        return None
    for _ in range(2):
        try:
            for order in kite.orders() or []:
                if str(order.get("tag") or "") == tag:
                    return order
        except Exception as e:
            logger.warning(f"kite tagged order lookup failed: {e}")
        await asyncio.sleep(0.8)
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
    execution_tag = tag or _new_execution_tag()
    attempts = 0
    last_error = None
    max_attempts = int(os.environ.get("LIVE_ORDER_MAX_ATTEMPTS", "2"))
    for attempt in range(1, max(1, max_attempts) + 1):
        attempts = attempt
        try:
            res = kite_helper.place_live_order(
                kite,
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=order_type,
                product=product,
                price=price,
                tag=execution_tag,
            )
            return {"ok": True, **res, "tag": execution_tag, "attempts": attempts, "recovered": False}
        except Exception as exc:
            last_error = str(exc)
            recovered = await _find_kite_order_by_tag(kite, execution_tag)
            if recovered:
                return {
                    "ok": True,
                    "order_id": recovered.get("order_id"),
                    "tag": execution_tag,
                    "attempts": attempts,
                    "recovered": True,
                    "broker_status": recovered.get("status"),
                    "broker_order": recovered,
                }
            retryable = OrderExecutionRetry.is_retryable_error(last_error)
            if not retryable or attempt >= max_attempts:
                break
            retry_cfg = OrderExecutionRetry.retry_config(attempt)
            await asyncio.sleep(min(3, float(retry_cfg.get("backoff_seconds") or 1)))
    return {"ok": False, "error": last_error or "unknown broker error", "tag": execution_tag, "attempts": attempts}


def _is_nse_market_open(now_utc: Optional[datetime] = None) -> bool:
    return bool(_segment_session_status("NSE_FO", now_utc).get("open"))


def _is_order_market_open(exchange: str, now_utc: Optional[datetime] = None) -> bool:
    exchange = (exchange or "NSE").upper()
    segment = (
        "MCX_FO" if exchange in {"MCX", "MCX_FO"} else
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

    # ----- equity / futures / commodity path -----
    segment = _execution_segment_for(exchange, "DIRECT", symbol_upper)
    asset_class = "DIRECT"
    if exchange in ("NFO", "BFO"):
        segment = _execution_segment_for(exchange, "FUTURES", symbol_upper)
    elif exchange == "MCX":
        segment = "MCX_FO"
    elif exchange in ("CDS",):
        segment = "NSE_FO"

    token = symbol_upper
    if execution_broker == "upstox":
        resolved = _upstox_instrument_token(exchange, symbol_upper)
        if exchange == "MCX" and not resolved:
            contract = await _resolve_upstox_mcx_future_contract(symbol_upper)
            if not contract or not contract.get("instrument_key"):
                raise HTTPException(
                    status_code=400,
                    detail=f"instrument not found: MCX {symbol_upper} future missing from Upstox instrument master.",
                )
            resolved = contract["instrument_key"]
            symbol_upper = str(contract.get("trading_symbol") or symbol_upper).upper()
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
    market_snapshot: Dict[str, Any]
) -> Dict[str, Any]:
    from core.market_domains import resolve_domain_by_underlying
    domain = resolve_domain_by_underlying(symbol)
    
    now = datetime.now(timezone.utc)
    session_date = now.date().isoformat()
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
        "mode": "paper",
        "broker": "paper",
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
    try:
        row = await db.orders.find_one_and_update(
            {"id": order_id, "user_id": user_id},
            {"$set": {
                "status": canonical,
                "legacy_status": "PENDING_BROKER",
                "execution_status": canonical,
                "broker_status": broker_status,
                "broker_order_id": broker_order_id,
                "execution_attempts": int(submit.get("attempts") or 1),
                "execution_recovered": bool(submit.get("recovered")),
                "broker_response": submit.get("raw") or submit.get("broker_order") or submit,
                "updated_at": now,
            }, "$unset": {"placement_owner": "", "placement_lock_until": ""}},
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


async def _apply_paper_fill_to_position(order_doc: Dict[str, Any], fill_price: float) -> Dict[str, Any]:
    """Apply a paper fill exactly once and write immutable fill/trade records."""
    if order_doc.get("mode") != "paper":
        raise RuntimeError("CRITICAL ERROR: Attempted to apply simulated paper fill to a LIVE order.")

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
    gross_realised = 0.0
    after_avg = float(fill_price or 0)

    if before_qty == 0 or before_qty * delta > 0:
        total_qty = abs(before_qty) + abs(delta)
        after_avg = round(((abs(before_qty) * before_avg) + (abs(delta) * fill_price)) / total_qty, 2) if total_qty else fill_price
    else:
        qty_closed = min(abs(before_qty), abs(delta))
        gross_realised = round((fill_price - before_avg) * qty_closed, 2) if before_qty > 0 else round((before_avg - fill_price) * qty_closed, 2)
        if after_qty == 0:
            after_avg = 0.0
        elif abs(delta) > abs(before_qty):
            after_avg = float(fill_price or 0)
        else:
            after_avg = before_avg

    net_realised = round(gross_realised - charges, 2) if qty_closed else 0.0

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
        "gross_realised_pnl": gross_realised,
        "gross_pnl": gross_realised,
        "realised_pnl": net_realised,
        "net_pnl": net_realised,
        "position_before_qty": before_qty,
        "position_after_qty": after_qty,
        "avg_price_before": before_avg,
        "avg_price_after": round(after_avg, 2),
        "filled_at": now,
        "mode": "paper",
    }
    logger.info(
        "Trade Fill (Accounting Ledger): user_id=%s strategy_id=%s symbol=%s side=%s qty=%d price=%.2f realised_pnl=%.2f before_qty=%d after_qty=%d",
        user_id, locked.get("strategy_id"), symbol, locked.get("side"), qty, float(fill_price or 0), net_realised, before_qty, after_qty
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
                "gross_realised_pnl": gross_realised,
                "gross_pnl": gross_realised,
                "realised_pnl": net_realised,
                "net_pnl": net_realised,
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
            "gross_realised_pnl": gross_realised,
            "gross_pnl": gross_realised,
            "realised_pnl": net_realised,
            "net_pnl": net_realised,
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
            "paper_fill_apply_lock_until": "",
            "paper_fill_apply_started_at": "",
        }},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    await _append_order_event(order_id, user_id, "PAPER_FILL_APPLIED", {
        "fill_price": float(fill_price or 0),
        "qty": qty,
        "realised_pnl": net_realised,
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
    intent_name = str(intent_doc.get("intent") or "").upper()
    side = str(order_doc.get("side") or "").upper()
    symbol = order_doc.get("symbol") or ((intent_doc.get("instrument") or {}).get("tradingsymbol"))
    brokerage = float(order_doc.get("brokerage") or 0.0)
    expected = float(order_doc.get("expected_price") or order_doc.get("requested_price") or price or 0)
    slippage = round(abs(price - expected) * qty, 2) if expected > 0 else 0.0
    now = datetime.now(timezone.utc).isoformat()

    gross_realised = 0.0
    net_realised = 0.0
    position_before_qty = None
    position_after_qty = None
    avg_price_before = None
    avg_price_after = None
    if intent_name in {"CLOSE_LONG", "CLOSE_SHORT"}:
        pos = None
        if order_doc.get("strategy_id"):
            pos = await db.strategy_positions.find_one(
                {
                    "user_id": user_id,
                    "strategy_id": order_doc.get("strategy_id"),
                    "status": {"$in": ["EXITING", "OPEN", "FILLED"]},
                },
                {"_id": 0},
            )
        if pos:
            position_before_qty = int(pos.get("open_quantity") or pos.get("quantity") or 0)
            avg_price_before = float(pos.get("average_buy_price") or pos.get("average_price") or 0)
            closed_qty = min(position_before_qty, qty) if position_before_qty > 0 else qty
            pos_side = str(pos.get("position_side") or "LONG").upper()
            gross_realised = round((avg_price_before - price) * closed_qty, 2) if pos_side == "SHORT" else round((price - avg_price_before) * closed_qty, 2)
            net_realised = round(gross_realised - brokerage, 2)
            position_after_qty = max(0, position_before_qty - closed_qty)
            avg_price_after = avg_price_before if position_after_qty else 0.0

    fill_doc = {
        "id": str(uuid.uuid4()),
        "order_id": order_id,
        "broker_order_id": order_doc.get("broker_order_id"),
        "user_id": user_id,
        "strategy_id": order_doc.get("strategy_id"),
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "fill_price": price,
        "price": price,
        "brokerage": brokerage,
        "slippage": slippage,
        "gross_realised_pnl": gross_realised,
        "realised_pnl": net_realised,
        "position_before_qty": position_before_qty,
        "position_after_qty": position_after_qty,
        "avg_price_before": avg_price_before,
        "avg_price_after": avg_price_after,
        "filled_at": now,
        "mode": "live",
        "broker": order_doc.get("broker") or "upstox",
        "raw_execution_report": raw_report or {},
    }
    try:
        await db.trade_fills.insert_one(fill_doc)
    except DuplicateKeyError:
        return await db.trade_fills.find_one({"order_id": order_id, "user_id": user_id}, {"_id": 0})

    await db.orders.update_one(
        {"id": order_id, "user_id": user_id},
        {"$set": {
            "live_fill_booked": True,
            "live_fill_id": fill_doc["id"],
            "gross_realised_pnl": gross_realised,
            "realised_pnl": net_realised,
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
        "gross_realised_pnl": gross_realised,
        "realised_pnl": net_realised,
    })
    if intent_name in {"CLOSE_LONG", "CLOSE_SHORT"} and net_realised:
        try:
            await db.trades.insert_one({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "strategy_id": order_doc.get("strategy_id"),
                "entry_symbol": symbol,
                "exit_order_id": order_id,
                "broker_order_id": order_doc.get("broker_order_id"),
                "qty": qty,
                "exit_price": price,
                "gross_realised_pnl": gross_realised,
                "realised_pnl": net_realised,
                "brokerage": brokerage,
                "slippage": slippage,
                "closed_at": now,
                "mode": "live",
            })
        except DuplicateKeyError:
            pass
    return fill_doc


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
        query["filled_at"] = window
    fills = await db.trade_fills.find(query, {"_id": 0}).to_list(10000)
    realised_fills = [row for row in fills if float(row.get("realised_pnl") or 0) != 0]
    wins = len([row for row in realised_fills if float(row.get("realised_pnl") or 0) > 0])
    losses = len([row for row in realised_fills if float(row.get("realised_pnl") or 0) < 0])
    gross_turnover = round(sum(abs(float(row.get("fill_price") or row.get("price") or 0) * int(row.get("qty") or 0)) for row in fills), 2)
    brokerage = round(sum(float(row.get("brokerage") or 0) for row in fills), 2)
    slippage = round(sum(float(row.get("slippage") or 0) for row in fills), 2)
    realised = round(sum(float(row.get("realised_pnl") or 0) for row in fills), 2)
    return {
        "fills": fills,
        "fill_count": len(fills),
        "closed_trade_count": len(realised_fills),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / max(1, wins + losses) * 100, 2),
        "realised_pnl": realised,
        "gross_turnover": gross_turnover,
        "brokerage": brokerage,
        "slippage": slippage,
        "source": "trade_fills",
    }


async def _recover_pending_paper_fills(limit: int = 500) -> int:
    """Finish paper fills that were persisted before a restart/crash."""
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


async def _place_order_core(user_id: str, symbol: str, side: str, qty: Optional[int],
                            order_type: str = "MARKET", price: Optional[float] = None,
                            product: Optional[str] = None, source: str = "manual",
                            option_contract: Optional[Dict[str, Any]] = None,
                            exchange: str = "NSE",
                            stop_loss: Optional[float] = None,
                            take_profit: Optional[float] = None,
                            idempotency_key: Optional[str] = None,
                            signal_id: Optional[str] = None) -> dict:
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

    if CORE_ENGINE_ENABLED and CORE_ENGINE_PAPER_ENABLED and paper:
        from core.risk_manager import RiskManager
        from core.order_manager import OrderManager
        from core.execution_router import ExecutionRouter
        from core.portfolio_ledger import PortfolioLedger
        from core.market_domains import resolve_domain_by_underlying

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
        if strategy_id and str(side or "").upper() == "BUY":
            existing_position = await db.strategy_positions.find_one({
                "user_id": user_id,
                "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
                "$or": [
                    {"active_instrument_key": _active_key(user_id, str(target_instrument_key))},
                    {"strategy_id": strategy_id},
                    {"instrument_key": str(target_instrument_key)},
                    {"target_symbol": target_symbol},
                ],
            })
            if existing_position:
                reason = f"Core paper entry blocked: active strategy or instrument position already exists for {target_symbol}."
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason=reason,
                    reason_code="DUPLICATE_POSITION",
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={
                        "ltp": contract_ltp,
                        "received_at": (option_contract or {}).get("received_at"),
                        "source": (option_contract or {}).get("source") or "preflight",
                    },
                )
                return _clean_order_response(skip_doc)
        
        # Check staleness in Core path of _place_order_core
        received_at = (option_contract or {}).get("received_at")
        if option_contract and market_session.get("open") and not simulated_contract and not (price and price > 0):
            if not received_at or parse_market_timestamp(received_at) is None:
                logger.warning("Core paper option order skipped: Upstox quote timestamp is missing for %s.", option_contract.get("tradingsymbol") or symbol)
                skip_doc = await _persist_core_paper_skipped_order(
                    user_id=user_id,
                    strategy_id=strategy_id or "manual",
                    symbol=symbol,
                    option_contract=option_contract,
                    side=side,
                    qty=qty or 1,
                    price=price or 0.0,
                    reason="Upstox quote timestamp is missing.",
                    reason_code="SKIPPED_QUOTE_STALE",
                    idempotency_key=idem_key,
                    signal_id=signal_id,
                    market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"}
                )
                return _clean_order_response(skip_doc)
            dt = parse_market_timestamp(received_at)
            if dt:
                age = (datetime.now(timezone.utc) - dt).total_seconds()
                if age > 60.0:
                    logger.warning("Core paper option order skipped: Upstox quote is stale for %s (age %s).", option_contract.get("tradingsymbol") or symbol, age)
                    skip_doc = await _persist_core_paper_skipped_order(
                        user_id=user_id,
                        strategy_id=strategy_id or "manual",
                        symbol=symbol,
                        option_contract=option_contract,
                        side=side,
                        qty=qty or 1,
                        price=price or 0.0,
                        reason=f"Upstox quote is stale ({age:.1f}s age > 60s).",
                        reason_code="SKIPPED_QUOTE_STALE",
                        idempotency_key=idem_key,
                        signal_id=signal_id,
                        market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"}
                    )
                    return _clean_order_response(skip_doc)

        if option_contract and market_session.get("open") and not simulated_contract and not (price and price > 0) and contract_ltp <= 0:
            logger.warning("Core paper option order skipped: fresh Upstox LTP required for %s.", option_contract.get("tradingsymbol") or symbol)
            skip_doc = await _persist_core_paper_skipped_order(
                user_id=user_id,
                strategy_id=strategy_id or "manual",
                symbol=symbol,
                option_contract=option_contract,
                side=side,
                qty=qty or 1,
                price=price or 0.0,
                reason="Fresh real Upstox option LTP is required during live market hours.",
                reason_code="PRICE_UNAVAILABLE",
                idempotency_key=idem_key,
                signal_id=signal_id,
                market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"}
            )
            return _clean_order_response(skip_doc)

        paper_ltp = price if (price and price > 0) else (contract_ltp if contract_ltp > 0 else (0.0 if market_session.get("open") else _get_paper_ltp(symbol, option_contract)))
        if paper_ltp <= 0:
            skip_doc = await _persist_core_paper_skipped_order(
                user_id=user_id,
                strategy_id=strategy_id or "manual",
                symbol=symbol,
                option_contract=option_contract,
                side=side,
                qty=qty or 1,
                price=price or 0.0,
                reason="Paper price unavailable.",
                reason_code="PRICE_UNAVAILABLE",
                idempotency_key=idem_key,
                signal_id=signal_id,
                market_snapshot={"ltp": contract_ltp, "received_at": received_at, "source": "upstox-cache"}
            )
            return _clean_order_response(skip_doc)

        risk_mgr = RiskManager(db)
        _lot_size = domain.get_lot_size(symbol)
        # qty from signal_manager is in LOTS (e.g. 1 lot), but compute_position_size
        # expects quantity in SHARES. Convert: requested_qty_shares = lots * lot_size.
        # For equity strategies lot_size=1 so this is a no-op.
        _qty_lots = qty or 1
        _qty_shares = _qty_lots * _lot_size
        risk_res = await risk_mgr.evaluate_order(
            user_id=user_id,
            strategy_id=strategy_id or "manual",
            symbol=symbol,
            target_symbol=option_contract["tradingsymbol"] if option_contract else symbol,
            side=side,
            requested_qty=_qty_shares,
            price=paper_ltp,
            mode="paper",
            stop_loss=stop_loss,
            take_profit=take_profit,
            lot_size=_lot_size
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
            mode="paper",
            stop_loss=stop_loss,
            take_profit=take_profit,
            idempotency_key=idem_key
        )
        intent_doc["execution_tag"] = _new_execution_tag(strategy_id)
        intent_doc["paper_realism"] = "UPSTOX_LIKE"

        ledger = PortfolioLedger(db)
        router = ExecutionRouter(db, ledger)
        order_res = await router.route_intent(user_id, intent_doc)
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
                    or ("NRML" if instr.exchange in {"NFO", "BFO", "MCX", "CDS"} else settings.get("default_product", "MIS"))
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
                or ("NRML" if instr.exchange in {"NFO", "BFO", "MCX", "CDS"} else settings.get("default_product", "MIS"))
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
        resolved_product_candidate = (product or ("NRML" if instr.exchange in {"NFO", "BFO", "MCX", "CDS"} else settings.get("default_product", "MIS"))).upper()
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
                source=source,
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
            "gross_realised_pnl": 0.0,
            "realised_pnl": 0.0,
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
                await _cancel_strategy_reservation(position_reservation, str(exc))
                if exposure_reservation:
                    await _close_order_exposure_reservation(order_doc["id"], user_id, status="RELEASED", reason=str(exc))
                    exposure_reservation = None
                order_doc = await _mark_order_rejected(order_doc["id"], user_id, str(exc))
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


@api.post("/orders")
async def place_order(req: OrderReq, user=Depends(get_current_user)):
    return await _place_order_core(
        user_id=user["id"], symbol=req.symbol, side=req.side, qty=req.qty,
        order_type=req.order_type, price=req.price, product=req.product, source="manual",
        exchange=req.exchange,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        idempotency_key=req.idempotency_key,
    )


@api.post("/positions/{symbol}/exit")
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


@api.post("/ops/squareoff-all")
async def squareoff_all_positions(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    positions = await list_positions(user)
    if not positions:
        return {"ok": True, "closed": [], "failed": []}

    closed, failed = [], []
    now = datetime.now(timezone.utc).isoformat()
    paper = settings.get("paper_mode", True)
    if not paper:
        gateway = await get_user_upstox_gateway(user["id"])
        if not gateway or not gateway.connected:
            raise HTTPException(status_code=400, detail="auth failed: Upstox is not connected.")

    for p in positions:
        symbol = p.get("symbol")
        qty = int(p.get("qty") or 0)
        if not symbol or qty == 0:
            continue
        side = "SELL" if qty > 0 else "BUY"
        abs_qty = abs(qty)
        try:
            if paper:
                res = await _place_order_core(
                    user_id=user["id"],
                    symbol=symbol,
                    side=side,
                    qty=abs_qty,
                    order_type="MARKET",
                    product=p.get("product") or settings.get("default_product", "MIS"),
                    source="squareoff-all",
                    exchange=p.get("exchange") or "NSE",
                    idempotency_key=f"squareoff-all:{symbol}:{now}",
                )
                closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "paper", "order_id": res.get("id")})
            else:
                exchange = p.get("exchange") or ("NFO" if str(symbol).upper().endswith(("CE", "PE")) else "NSE")
                option_contract = None
                if exchange in {"NFO", "BFO", "MCX"} or str(symbol).upper().endswith(("CE", "PE")):
                    token = str(p.get("instrument_token") or "").strip()
                    if "|" not in token:
                        failed.append({"symbol": symbol, "error": "Upstox instrument_key missing. Exit this position in Upstox, then sync broker state."})
                        continue
                    option_contract = {
                        "tradingsymbol": str(symbol).upper(),
                        "exchange": exchange,
                        "instrument_token": token,
                        "lot_size": max(1, abs_qty),
                        "transaction_type": side,
                    }
                res = await _place_order_core(
                    user_id=user["id"],
                    symbol=str(symbol).upper(),
                    side=side,
                    qty=1 if option_contract else abs_qty,
                    order_type="MARKET",
                    product=p.get("product") or settings.get("default_product", "MIS"),
                    source="squareoff",
                    exchange=exchange,
                    option_contract=option_contract,
                    idempotency_key=f"squareoff-live:{symbol}:{now}",
                )
                closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "live", "order_id": res.get("broker_order_id")})
        except Exception as e:
            failed.append({"symbol": symbol, "error": str(e)})
    return {"ok": not failed, "closed": closed, "failed": failed}


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
                    "status": "FILLED",
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
                    res = await db.strategy_positions.update_one(
                        {"id": pos["id"], "user_id": user_id},
                        {"$set": position_update},
                    )
                    changed_positions += res.modified_count
            if strategy_id and is_exit:
                exit_positions = await db.strategy_positions.find(
                    {"user_id": user_id, "exit_broker_order_id": str(broker_order_id), "status": "EXITING"},
                    {"_id": 0},
                ).to_list(20)
                for pos in exit_positions:
                    exit_price = float(avg_price or order.get("price") or pos.get("average_buy_price") or 0)
                    await _close_strategy_position_record(pos, exit_price=exit_price, reason="broker-exit-complete")
                    changed_positions += 1
        elif canonical in {ORDER_CANCELLED, ORDER_REJECTED}:
            await _close_order_exposure_reservation(order["id"], user_id, status="RELEASED", reason=f"broker-{canonical.lower()}")
            if strategy_id and is_entry:
                res = await db.strategy_positions.update_many(
                    {
                        "user_id": user_id,
                        "entry_broker_order_id": str(broker_order_id),
                        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]},
                    },
                    {
                        "$set": {"status": canonical, "legacy_status": normalized, "updated_at": now, "broker_status_message": status_message or normalized},
                        "$unset": {"active_instrument_key": "", "active_strategy_key": ""},
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
                 "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
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
                 "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
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
             "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
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
             "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
        )
        await db.strategy_position_locks.delete_many({"user_id": user_id, "strategy_id": row.get("strategy_id")})
    result = {"checked": len(items), "updated": updated, "missing_from_broker_fixed": missing_fixed}
    await _record_broker_sync_state(user_id, "upstox", result)
    return result


async def _live_broker_position_symbols(user_id: str, kite=None) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if kite:
        data = kite_helper.safe_positions(kite)
        for p in (data or {}).get("net") or []:
            symbol = p.get("tradingsymbol")
            qty = int(p.get("quantity") or 0)
            if symbol and qty != 0:
                exchange = p.get("exchange") or ("NFO" if str(symbol).endswith(("CE", "PE")) else "NSE")
                out[_instrument_key(exchange, symbol, None)] = p
                out[f"SYMBOL:{str(symbol).upper()}"] = p
    gateway = _KOTAK_GATEWAYS.get(user_id)
    if gateway and gateway.status().get("authenticated"):
        result = await asyncio.to_thread(gateway.positions)
        if result.get("ok"):
            response = result.get("response") or {}
            kotak_rows = response.get("net") if isinstance(response, dict) else _kotak_position_items(response)
            for p in kotak_rows or []:
                symbol = _kotak_first(p, ["trdSym", "trading_symbol", "tradingSymbol", "symbol"])
                qty = _kotak_first(p, ["netQty", "net_quantity", "quantity", "qty"])
                try:
                    qty_i = int(float(qty or 0))
                except Exception:
                    qty_i = 0
                if symbol and qty_i != 0:
                    exchange = _kotak_first(p, ["exSeg", "exchange_segment", "exchange"]) or "NSE"
                    out[_instrument_key(str(exchange).upper(), str(symbol).upper(), None)] = p
                    out[f"SYMBOL:{str(symbol).upper()}"] = p
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


async def _sync_strategy_positions_with_broker(user_id: str, kite=None) -> Dict[str, int]:
    gateway = _KOTAK_GATEWAYS.get(user_id)
    has_kotak = bool(gateway and gateway.status().get("authenticated"))
    upstox_gateway = await get_user_upstox_gateway(user_id)
    has_upstox = bool(upstox_gateway and upstox_gateway.connected)
    if not kite and not has_kotak and not has_upstox:
        return {"checked": 0, "broker_positions": 0, "marked_broker_not_found": 0, "reason": "no_broker_connected"}
    if not _check_and_update_sync_throttle(user_id, "positions"):
        return {"checked": 0, "broker_positions": 0, "marked_broker_not_found": 0, "throttled": True}
    broker_positions = await _live_broker_position_symbols(user_id, kite)
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
                            }, "$unset": {"active_instrument_key": "", "active_strategy_key": ""}}
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
                    }, "$unset": {"active_instrument_key": "", "active_strategy_key": ""}}
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
                }, "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
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
    positions = await _live_broker_position_symbols(user_id, kite)
    row = positions.get(_instrument_key(exchange, symbol, None)) or positions.get(f"SYMBOL:{symbol.upper()}")
    broker_qty = _broker_position_quantity(row or {})
    if broker_qty < int(qty or 0):
        raise HTTPException(
            status_code=409,
            detail=f"Broker position quantity mismatch for {symbol}: broker has {broker_qty}, QuantG wants to sell {qty}. Run Sync with Broker before retrying.",
        )


@api.get("/orders")
async def list_orders(include_stale: bool = False, user=Depends(get_current_user)):
    """Local order log + live broker orders (merged) so users see EVERY status."""
    kite, _ = await get_user_kite(user["id"])
    if kite:
        await _sync_kite_order_statuses(user["id"], kite)
        await _stale_local_open_orders(user["id"], kite)
    await _sync_kotak_order_statuses(user["id"])
    await _sync_upstox_order_statuses(user["id"])
    await _sync_strategy_positions_with_broker(user["id"], kite)
    order_query: Dict[str, Any] = {"user_id": user["id"]}
    if not include_stale:
        order_query["status"] = {"$nin": list(STALE_ORDER_STATUSES)}
        order_query["visibility"] = {"$ne": "hidden"}
    rows = await db.orders.find(order_query,
                                {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    # Add live Kite orders not already represented locally.
    if kite:
        try:
            local_broker_ids = {r.get("broker_order_id") for r in rows if r.get("broker_order_id")}
            live_orders = kite.orders() or []
            for o in live_orders:
                if o.get("order_id") not in local_broker_ids:
                    rows.insert(0, _broker_order_row(o))
        except Exception as e:
            logger.warning(f"kite orders fetch failed: {e}")
    # Sort merged orders newest-first (Kite + local) so order timeline is correct
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
        sym = next((s for s in SYMBOLS if s["symbol"] == r["symbol"]), None)
        ltp = live_price(sym["base"], SYMBOLS.index(sym))["price"] if sym else r["avg_price"]
        pnl = round((ltp - r["avg_price"]) * r["qty"], 2)
        out.append({**r, "ltp": ltp, "pnl": pnl, "mode": "paper"})
    return out


@api.get("/positions")
async def list_positions(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    return await _fetch_broker_positions_for_user(user, settings)


@api.get("/execution/snapshot")
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


@api.get("/strategy-readiness")
async def strategy_readiness(user=Depends(get_current_user)):
    rows = await _build_strategy_readiness_rows(user["id"])
    summary = {
        "ready": sum(1 for r in rows if r["status"] == "READY"),
        "warning": sum(1 for r in rows if r["status"] == "WARNING"),
        "blocked": sum(1 for r in rows if r["status"] == "BLOCKED"),
        "quarantined": sum(1 for r in rows if r["status"] == "QUARANTINED"),
    }
    return {"status": "READY" if summary["blocked"] == 0 and summary["quarantined"] == 0 else "WARNING", "summary": summary, "strategies": rows}


@api.get("/paper-readiness")
async def paper_readiness(user=Depends(get_current_user)):
    user_id = user["id"]
    settings = await get_user_settings(user_id)
    start, end = get_trading_day_window_ist()
    upstox = await get_user_upstox_status(user_id)
    strategy_rows = await _build_strategy_readiness_rows(user_id)
    active_strategy_count = sum(1 for r in strategy_rows if r.get("runtime_status") == "live" and r.get("mode") == "paper")
    quarantined_count = sum(1 for r in strategy_rows if r["status"] == "QUARANTINED")
    skipped_count = await db.signals.count_documents({"user_id": user_id, "processed_at": {"$gte": start, "$lt": end}, "status": {"$in": ["FILTERED", "REJECTED", "SKIPPED_SIGNAL", "BLOCKED"]}})
    valid_paper_order_count = await db.orders.count_documents({"user_id": user_id, "mode": "paper", "created_at": {"$gte": start, "$lt": end}, "status": {"$nin": [ORDER_SKIPPED_SIGNAL, "SKIPPED", "FAILED", "REJECTED"]}})
    recent_orders = await db.orders.find({"user_id": user_id, "mode": "paper", "created_at": {"$gte": start, "$lt": end}}, {"_id": 0}).sort("created_at", -1).limit(100).to_list(100)
    fake_or_unproven = [o for o in recent_orders if _is_fake_34_price(o.get("price")) and not _order_has_real_upstox_price_metadata(o)]
    missing_price_source = [o for o in recent_orders if str(o.get("status") or "").upper() in {ORDER_PAPER_CREATED, ORDER_PAPER_FILLED} and not o.get("price_source")]
    latest_skips = await db.signals.find({"user_id": user_id, "processed_at": {"$gte": start, "$lt": end}, "rejection_reason": {"$ne": None}}, {"_id": 0, "id": 1, "strategy_id": 1, "target_symbol": 1, "status": 1, "rejection_reason": 1, "processed_at": 1}).sort("processed_at", -1).limit(10).to_list(10)
    feed = (upstox.get("gateway") or {})
    blockers = []
    if not settings.get("paper_mode", True):
        blockers.append("paper_mode disabled")
    if fake_or_unproven:
        blockers.append("unproven 34.xx paper orders found today")
    if missing_price_source:
        blockers.append("paper orders missing price_source metadata")
    status = "BLOCKED" if blockers else ("WARNING" if quarantined_count or skipped_count else "READY")
    return {
        "status": status,
        "blockers": blockers,
        "paper_mode": bool(settings.get("paper_mode", True)),
        "live_trading_disabled": bool(settings.get("paper_mode", True)),
        "allow_simulated_prices": bool(settings.get("allow_simulated_prices")),
        "upstox": {
            "connected": bool(upstox.get("connected")),
            "token_valid": bool(upstox.get("token_valid") or upstox.get("authenticated")),
            "last_tick_at": feed.get("last_tick_at"),
            "ticks": feed.get("ticks", 0),
        },
        "feed_status": "READY" if feed.get("last_tick_at") else "WARNING",
        "active_strategy_count": active_strategy_count,
        "quarantined_strategy_count": quarantined_count,
        "skipped_signal_count": skipped_count,
        "valid_paper_order_count": valid_paper_order_count,
        "fake_suspicious_price_blocked_count": len(fake_or_unproven),
        "paper_orders_missing_price_source": len(missing_price_source),
        "latest_skipped_reasons": latest_skips,
        "latest_order_source_validation": [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "price": o.get("price"),
                "price_source": o.get("price_source"),
                "price_received_at": o.get("price_received_at"),
                "real_upstox_metadata": _order_has_real_upstox_price_metadata(o),
            }
            for o in recent_orders[:10]
        ],
    }


@api.get("/debug/position-integrity")
async def position_integrity_report(user=Depends(get_current_user)):
    user_id = user["id"]
    settings = await get_user_settings(user_id)
    
    # 1. Fetch broker/paper positions
    broker_positions = await _fetch_broker_positions_for_user(user, settings)
    broker_active = [p for p in broker_positions if int(p.get("qty") or p.get("quantity") or 0) != 0]
    
    # 2. Fetch strategy positions
    active_sp = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}
    }).to_list(1000)
    
    active_sp_symbols = {str(sp.get("symbol")).upper(): sp for sp in active_sp if sp.get("symbol")}
    active_sp_strategy_ids = {str(sp.get("strategy_id")) for sp in active_sp if sp.get("strategy_id")}
    
    # Counts
    orphan_positions = 0
    missing_sl = 0
    missing_tp = 0
    strategy_mismatches = 0
    
    # Detect orphans
    for bp in broker_active:
        symbol = str(bp.get("symbol") or "").upper()
        strategy_id = bp.get("strategy_id")
        
        is_orphan = True
        if strategy_id and str(strategy_id) in active_sp_strategy_ids:
            is_orphan = False
        elif symbol in active_sp_symbols:
            is_orphan = False
            
        if is_orphan:
            orphan_positions += 1
            
    # Detect missing SL/TP and mismatches
    for sp in active_sp:
        symbol = str(sp.get("symbol") or "").upper()
        tp_sl = sp.get("tp_sl_tsl_config") or {}
        
        has_sl = tp_sl.get("stop_loss") is not None or tp_sl.get("stoploss_price") is not None
        has_tp = tp_sl.get("take_profit") is not None or tp_sl.get("target_price") is not None
        
        if not has_sl:
            missing_sl += 1
        if not has_tp:
            missing_tp += 1
            
        # Detect mismatch
        bp_match = next((p for p in broker_active if str(p.get("symbol")).upper() == symbol), None)
        if not bp_match:
            strategy_mismatches += 1
        else:
            bp_qty = abs(int(bp_match.get("qty") or bp_match.get("quantity") or 0))
            sp_qty = int(sp.get("open_quantity") or sp.get("quantity") or 0)
            if bp_qty != sp_qty:
                strategy_mismatches += 1
                
    # Detect failed orders
    failed_orders_count = await db.orders.count_documents({
        "user_id": user_id,
        "status": "FAILED"
    })
    
    return {
        "total_positions": len(broker_active),
        "orphan_positions": orphan_positions,
        "missing_sl": missing_sl,
        "missing_tp": missing_tp,
        "strategy_mismatches": strategy_mismatches,
        "failed_orders": failed_orders_count
    }



@api.get("/portfolio/holdings")
async def list_holdings(user=Depends(get_current_user)):
    kite, _ = await get_user_kite(user["id"])
    if not kite:
        return {"holdings": [], "source": "none",
                "message": "Connect Zerodha to view long-term holdings."}
    data = kite_helper.safe_holdings(kite)
    if data is None:
        return {"holdings": [], "source": "error", "message": "Zerodha holdings fetch failed."}
    out = []
    for h in data:
        last = float(h.get("last_price") or 0)
        avg = float(h.get("average_price") or 0)
        qty = int(h.get("quantity") or 0)
        out.append({
            "symbol": h.get("tradingsymbol"),
            "qty": qty,
            "avg_price": round(avg, 2),
            "ltp": round(last, 2),
            "pnl": round((last - avg) * qty, 2),
        })
    return {"holdings": out, "source": "live"}


@api.get("/portfolio")
async def portfolio(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    mode = "paper" if settings.get("paper_mode", True) else "live"
    positions = await list_positions(user)
    open_pnl = round(sum(p["pnl"] for p in positions), 2)
    fill_summary = await _fill_ledger_summary(user["id"], mode=mode)
    charges = round(float(fill_summary.get("brokerage") or 0) + float(fill_summary.get("slippage") or 0), 2)
    gross_pnl = round(fill_summary["realised_pnl"] + charges + open_pnl, 2)
    total_pnl = round(fill_summary["realised_pnl"] + open_pnl, 2)
    deployed = round(sum(abs(p["qty"]) * p["avg_price"] for p in positions), 2)
    orders_count = await db.orders.count_documents({"user_id": user["id"]})
    strategies_count = await db.strategies.count_documents({"user_id": user["id"]})
    live_strategies = await db.strategies.count_documents({"user_id": user["id"], "status": "live"})
    paused_strategies = await db.strategies.count_documents({"user_id": user["id"], "status": "paused"})
    active_strategies = await db.strategies.count_documents({
        "user_id": user["id"],
        "status": {"$nin": ["live", "paused"]},
    })
    position_modes = sorted({p.get("mode", "unknown") for p in positions})

    # Stable paper equity curve from daily aggregated orders (simulated from PnL)
    equity = []
    base = 100000.0
    for i in range(30):
        d = datetime.now(timezone.utc) - timedelta(days=30 - i)
        wobble = math.sin((i + 1) * 0.63) * 450
        trend = i * 55
        equity.append({"date": d.strftime("%Y-%m-%d"), "equity": round(base + trend + wobble + total_pnl * (i / 30), 2)})

    return {
        "total_pnl": total_pnl,
        "gross_pnl": gross_pnl,
        "charges": charges,
        "net_pnl": total_pnl,
        "realised_pnl": fill_summary["realised_pnl"],
        "open_pnl": open_pnl,
        "pnl_type": "realised_plus_open",
        "realised_pnl_source": fill_summary["source"],
        "pnl_source": "none" if not position_modes else position_modes[0] if len(position_modes) == 1 else "mixed",
        "open_positions": len(positions),
        "deployed": deployed,
        "available": round(500000.0 - deployed, 2),
        "orders": orders_count,
        "strategies": strategies_count,
        "live_strategies": live_strategies,
        "paused_strategies": paused_strategies,
        "active_strategies": active_strategies,
        "equity_curve": equity,
    }


@api.get("/risk/dashboard")
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
    realised = fill_summary["realised_pnl"]
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
        "realised_pnl": realised,
        "realised_pnl_source": fill_summary["source"],
        "fill_count": fill_summary["fill_count"],
        "closed_trade_count": fill_summary["closed_trade_count"],
        "open_pnl": open_pnl,
        "total_pnl": round(realised + open_pnl, 2),
        "loss_remaining": round(max(0.0, loss_limit + realised), 2) if loss_limit else None,
        "trades_used": trades_used,
        "max_trades_per_day": max_trades,
        "trades_remaining": max(0, max_trades - trades_used) if max_trades else None,
        "per_strategy_capital": settings.get("per_strategy_capital"),
        "max_position_size": settings.get("max_position_size"),
        "gross_order_value": gross_order_value,
        "market_open": session["global_status"] == "OPEN",
        "market_session": session,
    }


@api.get("/trade-journal")
async def trade_journal(user=Depends(get_current_user)):
    rows = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    skipped = await db.skipped_signals.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("last_seen_at", -1).to_list(200)
    fill_summary = await _fill_ledger_summary(user["id"])
    completed = [r for r in rows if canonical_order_status(r.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
    failed_actual = [r for r in rows if str(r.get("status") or "").upper() in {"FAILED", "REJECTED"}]
    wins = fill_summary["wins"]
    losses = fill_summary["losses"]
    total_pnl = fill_summary["realised_pnl"]
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
            "realised_pnl": total_pnl,
            "realised_pnl_source": fill_summary["source"],
        },
        "orders": rows,
        "filled_trades": fill_summary["fills"],
        "failed_actual_orders": failed_actual,
        "skipped_signals": skipped,
    }


@api.get("/strategies/live-backtest-comparison")
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
        live_pnl = fill_summary["realised_pnl"]
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
                "realised_pnl": live_pnl,
                "realised_pnl_source": fill_summary["source"],
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
            kite, _ = await get_user_kite(user["id"])
            if kite:
                await _sync_kite_order_statuses(user["id"], kite)
                await _stale_local_open_orders(user["id"], kite)
            await _sync_kotak_order_statuses(user["id"])
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
    """Resolves an index or commodity option contract dynamically.
    Works for both Upstox Live and Paper trading (resolves real contract from Upstox if possible).
    """
    underlying = underlying.upper()
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
        if underlying in COMMODITY_UNDERLYINGS:
            future_contract = await _resolve_upstox_mcx_future_contract(underlying)
            if future_contract and future_contract.get("instrument_key"):
                upstox_keys[underlying] = future_contract["instrument_key"]
                logger.info(
                    "Resolved MCX spot future for option resolver underlying=%s key=%s symbol=%s",
                    underlying,
                    future_contract.get("instrument_key"),
                    future_contract.get("trading_symbol"),
                )
            else:
                logger.warning("MCX spot future not found in Upstox master underlying=%s", underlying)
        
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
            else 6500.00 if underlying in ("CRUDEOIL", "CRUDEOILM")
            else 245.00
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

    # 1. Try Live Upstox Resolution (Index / Commodity)
    if gateway_connected:
        if underlying in COMMODITY_UNDERLYINGS:
            mcx_resolver = getattr(app.state, "mcx_contract_resolver", None) or MCXContractResolver(db)
            app.state.mcx_contract_resolver = mcx_resolver
            for attempt in range(2):
                contract = await mcx_resolver.resolve(
                    underlying=underlying,
                    spot=float(spot),
                    option_type=opt_type,
                    strike_interval=int(interval),
                    otm_points=int(otm_points or 0),
                    expiry_offset=int(expiry_offset or 0),
                    allow_refresh=True,
                )
                if contract:
                    instrument_token = contract.get("instrument_token")
                    tradingsymbol = contract.get("trading_symbol")
                    if contract.get("expiry"):
                        try:
                            expiry_dt = datetime.strptime(str(contract["expiry"]), "%Y-%m-%d")
                        except Exception:
                            pass
                    lot_size = int(contract.get("lot_size") or lot_size)
                    logger.info(
                        "Resolved MCX option via master underlying=%s opt=%s strike=%s expiry=%s token=%s symbol=%s attempt=%s",
                        underlying, opt_type, strike, contract.get("expiry"), instrument_token, tradingsymbol, attempt + 1,
                    )
                    break
                if attempt == 0:
                    logger.warning(
                        "MCX master lookup failed; forcing instrument refresh and retry underlying=%s opt=%s target_strike=%s",
                        underlying, opt_type, strike,
                    )
                    await mcx_resolver.refresh(reason=f"resolve-retry:{underlying}", force=True)
        else:
            # Index Option lookup using flexible option chain / search
            try:
                spot_key = upstox_keys.get(underlying)
                if spot_key:
                    # Query option chain with None/flexible expiry first
                    chain = await asyncio.to_thread(upstox_gw.get_option_chain, spot_key, None)
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
                                    if node.get("expiry"):
                                        try:
                                            expiry_dt = datetime.strptime(str(node["expiry"]), "%Y-%m-%d")
                                        except Exception:
                                            pass
                                    break
            except Exception as e:
                logger.warning(f"Upstox option chain lookup failed: {e}")

            # Fallback search candidate loop
            if not instrument_token:
                try:
                    exch = "BSE" if underlying == "SENSEX" else "NSE"
                    segment_candidates = ("FO", "OPT", "ALL")
                    expiry_candidates = ("current_week", "next_week", "current_month", None)
                    query_roots = [underlying]
                    query_candidates = []
                    for root in query_roots:
                        query_candidates.extend([f"{root} {int(strike)}", root])
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
        "exchange": "MCX" if underlying in COMMODITY_UNDERLYINGS else ("NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO"),
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
    segment_label = "MCX_FO" if exch_label == "MCX" else "NSE_FO"

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
            if not chain_loaded and underlying not in COMMODITY_UNDERLYINGS:
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


@api.get("/upstox/data-health")
async def upstox_data_health(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    gw = await get_user_upstox_gateway(user["id"])
    gateway_status = gw.status() if gw else {"connected": False, "feed_status": {"connected": False}}
    latest_ticks = gw.latest_ticks() if gw else {}
    feed_health = feed_health_status(gateway_status, latest_ticks)
    meta = await db.upstox_instrument_sync_meta.find_one({"_id": "daily-json"}, {"_id": 0}) or {}
    suspended_count = await db.upstox_suspended_instruments.count_documents({})
    active_count = await db.upstox_instruments.count_documents({"suspended": {"$ne": True}})
    return {
        "ok": feed_health.get("readiness") == "READY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "broker": "upstox",
        "mode": "paper" if settings.get("paper_mode", True) else "live",
        "paper_realism": {
            "mode": settings.get("paper_realism_mode") or "UPSTOX_LIKE",
            "upstox_like_charges": True,
            "simulated_fallback_allowed": bool(settings.get("allow_simulated_prices")),
        },
        "live_auto_trading_enabled": False,
        "feed": feed_health,
        "gateway": gateway_status,
        "instrument_sync": meta,
        "instrument_counts": {
            "active": active_count,
            "suspended": suspended_count,
        },
        "readiness_checks": {
            "oauth_connected": bool(gateway_status.get("connected")),
            "instrument_master_synced": bool(meta.get("completed_at")),
            "feed_ready": feed_health.get("readiness") == "READY",
            "quote_fresh": not bool(feed_health.get("quote_stale")),
            "live_auto_trading_disabled_by_default": True,
        },
    }


@api.post("/upstox/instruments/sync")
async def upstox_instruments_sync(force: bool = False, user=Depends(get_current_user)):
    return await sync_upstox_instruments(db, force=force)


@api.post("/upstox/quality-system/migrate")
async def upstox_quality_system_migrate(all_users: bool = False, user=Depends(get_current_user)):
    if all_users and user.get("role") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin role required to migrate all users.")
    if all_users:
        return await migrate_all_users_to_upstox_quality_system()
    result = await migrate_user_to_upstox_quality_system(user["id"])
    return {"ok": True, "user_id": user["id"], **result}


@api.get("/upstox/option-chain")
async def upstox_option_chain(
    underlying: str = "NIFTY",
    expiry_date: Optional[str] = None,
    user=Depends(get_current_user),
):
    gw = await get_user_upstox_gateway(user["id"])
    if not gw or not gw.connected:
        raise HTTPException(status_code=400, detail="Reconnect Upstox before loading option chain.")
    underlying = underlying.upper()
    spot_keys = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX",
    }
    if underlying in COMMODITY_UNDERLYINGS:
        future = await _resolve_upstox_mcx_future_contract(underlying)
        spot_key = (future or {}).get("instrument_key")
    else:
        spot_key = spot_keys.get(underlying)
    if not spot_key:
        raise HTTPException(status_code=400, detail=f"Unsupported option-chain underlying: {underlying}")
    chain = await asyncio.to_thread(gw.get_option_chain, spot_key, expiry_date)
    rows = (chain or {}).get("data") or []
    return {
        "ok": True,
        "underlying": underlying,
        "instrument_key": spot_key,
        "expiry_date": expiry_date,
        "row_count": len(rows),
        "data": rows,
    }


@api.post("/upstox/webhook")
async def upstox_webhook(request: Request):
    payload = await request.json()
    result = await apply_broker_truth_event(db, payload, source="webhook")
    return {"ok": True, **result}


@api.get("/upstox/reconciliation")
async def upstox_reconciliation(user=Depends(get_current_user)):
    gw = await get_user_upstox_gateway(user["id"])
    return await broker_reconciliation_summary(db, user["id"], gw)


@api.post("/upstox/exit-all")
async def upstox_exit_all(
    segment: Optional[str] = None,
    tag: Optional[str] = None,
    user=Depends(get_current_user),
):
    settings = await get_user_settings(user["id"])
    if bool(settings.get("paper_mode", True)):
        raise HTTPException(status_code=400, detail="Exit All Positions is disabled in paper mode.")
    gw = await get_user_upstox_gateway(user["id"])
    if not gw or not gw.connected:
        raise HTTPException(status_code=400, detail="Reconnect Upstox before using Exit All Positions.")
    if tag:
        logger.warning("Upstox tag-based exit requested user=%s tag=%s; Upstox may exit total instrument quantity shared by multiple strategies.", user["id"], tag)
    result = await asyncio.to_thread(gw.exit_all_positions, segment=segment, tag=tag)
    await db.upstox_exit_all_events.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "segment": segment,
        "tag": tag,
        "raw": result,
        "warning": "Tag-based exit can affect total instrument quantity if multiple strategies share the same instrument.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "ok": True,
        "warning": "Tag-based exit can affect total instrument quantity if multiple strategies share the same instrument.",
        "raw": result,
    }


@api.get("/v1/dashboard/telemetry")

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
    latest_ticks = option_ledger.latest_ticks(["NIFTY", "SENSEX", "CRUDEOIL", "CRUDEOILM", "NATURALGAS"])
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


@api.post("/risk/kill-switch")
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
        logger.warning("Upstox token missing for user=%s; live trading and ticker require reconnect", user_id)
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
    })
    return status


async def get_user_upstox_gateway(user_id: str, fresh: bool = False) -> Optional[UpstoxGateway]:
    if not fresh and user_id in _UPSTOX_GATEWAYS:
        return _UPSTOX_GATEWAYS[user_id]
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
        "live_auto_trading_enabled": False,
        "live_readiness_required": True,
    }


# ============== Routes: Live Readiness ==============
@api.get("/live/readiness")
async def live_readiness(user=Depends(get_current_user)):
    """Pre-flight checks before flipping to LIVE. Returns each check + an overall ready flag."""
    checks = []
    settings = await get_user_settings(user["id"])
    data_broker = "upstox"
    execution_broker = "upstox"
    upstox_status = await get_user_upstox_status(user["id"])
    required_keys_ok = bool(upstox_status.get("keys_saved"))
    checks.append({
        "id": "broker_keys",
        "label": "Upstox credentials saved",
        "ok": required_keys_ok,
    })
    checks[-1]["hint"] = "Save Upstox credentials on Broker Keys" if not required_keys_ok else None
    required_sessions_ok = bool(upstox_status.get("connected") and upstox_status.get("token_valid"))
    checks.append({
        "id": "upstox_session",
        "label": "Active Upstox session",
        "ok": required_sessions_ok,
        "detail": f"data={data_broker}, execution={execution_broker}",
        "hint": "Reconnect Upstox required on Broker Keys" if not required_sessions_ok else None,
    })
    funds_ok = bool(upstox_status.get("connected"))
    funds_msg = "Upstox connected; live margin check not exposed yet."
    checks.append({
        "id": "funds",
        "label": "Sufficient funds in account",
        "ok": funds_ok,
        "detail": funds_msg,
        "hint": "Add funds or connect the selected execution broker" if not funds_ok else None,
    })
    settings = await get_user_settings(user["id"])
    checks.append({
        "id": "risk_limits",
        "label": "Risk limits configured",
        "ok": settings.get("max_position_size", 0) > 0 and settings.get("max_daily_loss", 0) > 0,
        "detail": f"Max position ₹{settings['max_position_size']:.0f} · Daily loss cap ₹{settings['max_daily_loss']:.0f}",
        "hint": "Configure on Profile" if (settings.get("max_position_size", 0) <= 0 or settings.get("max_daily_loss", 0) <= 0) else None,
    })

    # Find if user has MCX strategies
    strategies = await db.strategies.find({"user_id": user["id"]}).to_list(500)
    has_mcx = any(
        s.get("instrument_group") == "MCX"
        or str(s.get("symbol")).upper() in COMMODITY_UNDERLYINGS
        or "MCX" in str(s.get("symbol")).upper()
        for s in strategies
    )

    # NSE market hours: 9:15 AM – 3:30 PM IST, Mon–Fri
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    is_weekday = ist_now.weekday() < 5
    minutes_now = ist_now.hour * 60 + ist_now.minute
    nse_open = is_weekday and (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)
    checks.append({
        "id": "market_hours",
        "label": "NSE market open",
        "ok": nse_open,
        "detail": ist_now.strftime("%a %H:%M IST"),
        "hint": "Market trades 09:15 – 15:30 IST, Mon–Fri" if not nse_open else None,
    })

    mcx_open = is_weekday and (9 * 60) <= minutes_now <= (23 * 60 + 30)
    if has_mcx:
        checks.append({
            "id": "mcx_market_hours",
            "label": "MCX market open",
            "ok": mcx_open,
            "detail": ist_now.strftime("%a %H:%M IST"),
            "hint": "MCX trades 09:00 – 23:30 IST, Mon–Fri" if not mcx_open else None,
        })

    gateway_status = upstox_status.get("gateway") or {}
    feed_status = gateway_status.get("feed_status") or upstox_status.get("feed_status") or {}
    selected_tick_ok = bool(upstox_status.get("connected") and (feed_status.get("connected") or gateway_status.get("ws_running")))

    checks.append({
        "id": "tick_feed",
        "label": "Realtime selected tick feed",
        "ok": selected_tick_ok,
        "detail": (
            f"upstox feed {feed_status.get('state') or 'running'}" if data_broker == "upstox" and selected_tick_ok else
            f"ticker startup skipped: {upstox_status.get('reason') or feed_status.get('last_error') or 'not running'}"
        ),
        "hint": (
            "Reconnect Upstox on Broker Keys, then restart the Upstox feed."
            if data_broker == "upstox" and not selected_tick_ok
            else None
        ),
    })

    market_open = nse_open or mcx_open if has_mcx else nse_open
    paper_mode = bool(settings.get("paper_mode", True))
    
    overall_ready = all(c["ok"] for c in checks if c["id"] not in {"market_hours", "mcx_market_hours"})
        
    return {
        "ready": overall_ready,
        "market_open": market_open,
        "current_mode": "PAPER" if paper_mode else "LIVE",
        "checks": checks,
    }


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
    kite_status: Dict[str, Any],
    kotak_status: Dict[str, Any],
    tick_status: Dict[str, Any],
    errored: List[Dict[str, Any]],
    orders_open: int,
    readiness: Dict[str, Any],
) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    mode_live = not bool(settings.get("paper_mode", True))
    data_broker = settings.get("data_broker", "upstox")
    execution_broker = settings.get("execution_broker", "upstox")

    def add(severity: str, title: str, detail: str, action: str, endpoint: Optional[str] = None) -> None:
        issues.append({
            "severity": severity,
            "title": title,
            "detail": detail,
            "action": action,
            "endpoint": endpoint,
        })

    # MCX and NSE timing checks
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    is_weekday = ist_now.weekday() < 5
    minutes_now = ist_now.hour * 60 + ist_now.minute
    nse_open = is_weekday and (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)
    mcx_open = is_weekday and (9 * 60) <= minutes_now <= (23 * 60 + 30)
    
    # Check if they have commodity strategies
    has_mcx = any(
        s.get("instrument_group") == "MCX"
        or str(s.get("symbol")).upper() in COMMODITY_UNDERLYINGS
        for s in errored
    ) or (execution_broker == "upstox" and not nse_open and mcx_open)

    if mode_live:
        if has_mcx:
            if not nse_open and not mcx_open:
                add("info", "All markets closed", "Live MARKET orders are blocked outside trading hours.", "Wait for market open or use PAPER.")
            elif not nse_open and mcx_open:
                add("info", "NSE market is closed", "NSE orders are blocked. MCX commodities are open.", "Trade MCX commodities or use PAPER.")
        else:
            if not nse_open:
                add("info", "Market is closed", "Live MARKET orders are blocked outside NSE hours.", "Wait for 09:15-15:30 IST or use PAPER.")

    if execution_broker == "zerodha" and not kite_status.get("connected"):
        add("critical", "Execution broker disconnected", kite_status.get("reason") or "Zerodha session is not active.", "Reconnect Zerodha on Broker Keys.")
    if execution_broker == "kotak_neo" and not kotak_status.get("connected"):
        add("critical", "Kotak execution disconnected", kotak_status.get("reason") or "Kotak session is not active.", "Connect Kotak on Broker Keys.")
    if execution_broker == "upstox":
        upstox = readiness.get("upstox") or {}
        upstox_session = next((c for c in readiness.get("checks") or [] if c.get("id") == "upstox_session"), {})
        if not upstox_session.get("ok"):
            add("critical", "Reconnect Upstox required", upstox_session.get("detail") or "Upstox access token is missing or expired.", "Open Broker Keys and reconnect Upstox OAuth.", "/broker/upstox/login")
    if data_broker == "zerodha" and market_open and not tick_status.get("connected"):
        add("warning", "Realtime Kite ticker is down", tick_status.get("last_error") or "No connected Kite websocket.", "Restart ticker.", "/ops/ticker/restart")
    if data_broker == "upstox":
        feed = tick_status.get("feed_status") or {}
        if not tick_status.get("authenticated"):
            add("critical", "Upstox data session missing", tick_status.get("last_error") or "Ticker startup skipped: no_token.", "Reconnect Upstox on Broker Keys.", "/broker/upstox/login")
        elif not (feed.get("connected") or tick_status.get("ws_running")):
            add("warning", "Upstox ticker is stopped", feed.get("last_error") or tick_status.get("last_error") or "Feed has not started.", "Restart Upstox feed.", "/ops/ticker/restart")
    if data_broker == "kotak_neo":
        gateway = kotak_status.get("gateway") or {}
        if not gateway.get("authenticated"):
            add("warning", "Kotak data session is not authenticated", kotak_status.get("reason") or "Kotak gateway is not connected.", "Connect Kotak on Broker Keys.")
        elif not gateway.get("ticks"):
            add("warning", "Kotak data has no ticks yet", "Subscribe Kotak instrument tokens before using Kotak as data broker.", "Use Kotak subscribe endpoint or keep Zerodha as data broker.")
    if orders_open:
        add("info", "Open orders need reconciliation", f"{orders_open} local order(s) are open/pending.", "Sync broker orders.", "/ops/orders/sync")
    if errored:
        add("warning", "Strategies have blocking errors", f"{len(errored)} strategy error(s) need attention.", "Open Strategy Errors below; clear only after fixing.", "/ops/strategies/clear-errors")
    for check in readiness.get("checks") or []:
        if check.get("id") in {"market_hours", "mcx_market_hours"}:
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
    kite, status = await get_user_kite(user_id)
    if not kite:
        return {"started": False, "reason": status.get("reason", "not_connected"), "status": status}
    tick_manager = getattr(app.state, "tick_manager", None)
    if not tick_manager:
        return {"started": False, "reason": "tick_manager_missing", "status": status}
    token_to_symbol: Dict[int, str] = {}
    for s in SYMBOLS:
        if s["symbol"] in options_helper.INDEX_SPOT_SYMBOL:
            continue
        tok = kite_helper.instrument_token(kite, s["symbol"])
        if tok:
            token_to_symbol[tok] = s["symbol"]
    for opt_sym, (spot_exch, spot_sym) in options_helper.INDEX_SPOT_SYMBOL.items():
        tok = kite_helper.instrument_token(kite, spot_sym, segment=spot_exch)
        if tok:
            token_to_symbol[tok] = opt_sym
    if not token_to_symbol:
        return {"started": False, "reason": "no_tokens_resolved", "status": status}
    tick_manager.start_for_user(user_id, kite, token_to_symbol)
    return {"started": True, "tokens": len(token_to_symbol), "status": tick_manager.status_info(user_id)}


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

    target_symbols = [str(s).upper() for s in (symbols or ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL", "CRUDEOILM", "NATURALGAS"])]
    keys: List[str] = []
    failures: List[Dict[str, str]] = []
    for symbol in target_symbols:
        token = _upstox_instrument_token("NSE", symbol) or _upstox_instrument_token("BSE", symbol)
        if not token and symbol in COMMODITY_UNDERLYINGS:
            contract = await _resolve_upstox_mcx_future_contract(symbol)
            token = str(contract.get("instrument_key")) if contract and contract.get("instrument_key") else None
        if token:
            keys.append(token)
        else:
            failures.append({"symbol": symbol, "reason": "no_token_resolved"})

    keys = list(dict.fromkeys(keys))
    if not keys:
        logger.warning("Upstox ticker startup skipped: no_tokens_resolved user=%s failures=%s", user_id, failures[:8])
        return {"started": False, "reason": "no_tokens_resolved", "failures": failures[:8], "status": gateway.status()}
    result = await asyncio.to_thread(gateway.start_market_data_ws, keys, "ltpc")
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
    kite, kite_status = await get_user_kite(user["id"])
    kotak_status = await get_user_kotak_status(user["id"])
    order_sync = await _sync_kite_order_statuses(user["id"], kite) if kite else {"checked": 0, "updated": 0}
    stale_order_repair = await _stale_local_open_orders(user["id"], kite) if kite else {"fixed": 0, "reason": "zerodha_not_connected"}
    kotak_order_sync = await _sync_kotak_order_statuses(user["id"])
    strategy_position_sync = await _sync_strategy_positions_with_broker(user["id"], kite)
    tick_manager = getattr(app.state, "tick_manager", None)
    zerodha_tick_status = tick_manager.status_info(user["id"]) if tick_manager else {"connected": False, "last_error": "tick manager missing"}
    kotak_tick_status = _kotak_ticker_status(user["id"])
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
    
    if settings.get("data_broker") == "kotak_neo":
        tick_status = kotak_tick_status
    elif settings.get("data_broker") == "upstox":
        tick_status = upstox_status
    else:
        tick_status = zerodha_tick_status
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).to_list(500)
    stale_nonblocking_error_ids = [
        s.get("id")
        for s in strategies
        if s.get("last_error") and not _is_strategy_blocking_error(s.get("last_error"))
    ]
    if stale_nonblocking_error_ids:
        await db.strategies.update_many(
            {"user_id": user["id"], "id": {"$in": stale_nonblocking_error_ids}},
            {"$unset": {"last_error": ""}},
        )
        for s in strategies:
            if s.get("id") in stale_nonblocking_error_ids:
                s.pop("last_error", None)
    errored = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "status": s.get("status"),
            "last_error": s.get("last_error"),
            "last_data_source": s.get("last_data_source"),
            "last_evaluated_at": s.get("last_evaluated_at"),
        }
        for s in strategies
        if _is_strategy_blocking_error(s.get("last_error"))
    ][:20]
    orders_open = await db.orders.count_documents({"user_id": user["id"], "status": {"$in": list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)}})
    positions_count = await db.positions.count_documents({"user_id": user["id"]})
    readiness = await live_readiness(user=user)
    recovery_plan = _build_recovery_plan(
        settings=settings,
        market_open=_is_nse_market_open(),
        kite_status=kite_status,
        kotak_status=kotak_status,
        tick_status=tick_status,
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
        "market": {"open": _is_nse_market_open(), "status": "OPEN" if _is_nse_market_open() else "CLOSED"},
        "broker_preferences": {
            "data_broker": settings.get("data_broker"),
            "execution_broker": settings.get("execution_broker"),
            "fallback_broker": settings.get("fallback_broker"),
        },
        "readiness": readiness,
        "zerodha": kite_status,
        "kotak_neo": kotak_status,
        "upstox": upstox_auth_status,
        "ticker": tick_status,
        "zerodha_ticker": zerodha_tick_status,
        "kotak_ticker": kotak_tick_status,
        "counts": {
            "strategies": len(strategies),
            "live_strategies": len([s for s in strategies if s.get("status") == "live"]),
            "paused_strategies": len([s for s in strategies if s.get("status") == "paused"]),
            "errored_strategies": len(errored),
            "open_orders": orders_open,
            "paper_positions": positions_count,
        },
        "order_sync": {**order_sync, **stale_order_repair, "kotak_checked": kotak_order_sync.get("checked", 0), "kotak_updated": kotak_order_sync.get("updated", 0), "strategy_positions_marked": strategy_position_sync.get("marked_broker_not_found", 0)},
        "recovery_plan": recovery_plan,
        "rate_limits": {
            "kite_history_cache_entries": len(_HISTORY_CACHE),
            "kite_history_cache_ttl_sec": KITE_HISTORY_CACHE_TTL_SEC,
            "kite_historical_min_interval_sec": KITE_HISTORICAL_MIN_INTERVAL_SEC,
        },
        "errored_strategies": errored,
    }
# ============== Routes: Ops Console - END ==============

# ============== Routes: Funds ==============
@api.get("/funds")
async def funds(user=Depends(get_current_user)):
    """Return broker funds & margins when live, otherwise a paper-money snapshot."""
    settings = await get_user_settings(user["id"])
    execution_broker = settings.get("execution_broker", "upstox")
    
    if execution_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user["id"])
        if upstox_gw and upstox_gw.connected:
            try:
                margins_payload = await asyncio.to_thread(upstox_gw.get_margins)
                if margins_payload and margins_payload.get("status") == "success":
                    data = margins_payload.get("data", {})
                    equity = data.get("equity", {})
                    commodity = data.get("commodity", {}) or data.get("commodities", {}) or {}
                    avail = round(
                        float(equity.get("available_margin") or 0)
                        + float(commodity.get("available_margin") or 0),
                        2,
                    )
                    used = round(
                        float(equity.get("used_margin") or 0)
                        + float(commodity.get("used_margin") or 0),
                        2,
                    )
                    payin = round(
                        float(equity.get("payin_amount") or 0)
                        + float(commodity.get("payin_amount") or 0),
                        2,
                    )
                    opening = round(avail + used - payin, 2)
                    return {
                        "source": "live",
                        "available_cash": avail,
                        "opening_balance": opening,
                        "intraday_payin": payin,
                        "used_margin": used,
                        "m2m_realised": 0.0,
                        "m2m_unrealised": 0.0,
                        "span": round(float(equity.get("span_margin") or 0) + float(commodity.get("span_margin") or 0), 2),
                        "delivery_margin": 0.0,
                        "raw_segments": {"equity": bool(equity), "commodity": bool(commodity)},
                    }
            except Exception as e:
                logger.warning(f"Upstox margins fetch failed: {e}")

    kite, _ = await get_user_kite(user["id"])
    if kite:
        try:
            margins = kite.margins(segment="equity")
            avail = margins.get("available", {}) or {}
            util = margins.get("utilised", {}) or {}
            return {
                "source": "live",
                "available_cash": round(float(avail.get("live_balance") or avail.get("cash") or 0), 2),
                "opening_balance": round(float(avail.get("opening_balance") or 0), 2),
                "intraday_payin": round(float(avail.get("intraday_payin") or 0), 2),
                "used_margin": round(float(util.get("debits") or util.get("exposure") or 0), 2),
                "m2m_realised": round(float(util.get("m2m_realised") or 0), 2),
                "m2m_unrealised": round(float(util.get("m2m_unrealised") or 0), 2),
                "span": round(float(util.get("span") or 0), 2),
                "delivery_margin": round(float(util.get("delivery") or 0), 2),
            }
        except Exception as e:
            logger.warning(f"funds fetch failed: {e}")
    # Paper / fallback
    positions = await db.positions.find({"user_id": user["id"]}, {"_id": 0}).to_list(200)
    deployed = round(sum(abs(p["qty"]) * p["avg_price"] for p in positions), 2)
    paper_capital = 500000.0
    return {
        "source": "paper",
        "available_cash": round(paper_capital - deployed, 2),
        "opening_balance": paper_capital,
        "intraday_payin": 0.0,
        "used_margin": deployed,
        "m2m_realised": 0.0,
        "m2m_unrealised": 0.0,
        "span": 0.0,
        "delivery_margin": 0.0,
        "note": "Paper-mode estimate. Connect Upstox and switch to LIVE for real margins.",
    }


# ============== Routes: Zerodha OAuth ==============
@api.get("/zerodha/login-url")
async def zerodha_login_url(user=Depends(get_current_user)):
    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "zerodha"})
    if not keys:
        raise HTTPException(status_code=400, detail="Save your Zerodha api_key + api_secret on Broker Keys first")
    api_key = decrypt_secret(keys.get("api_key"))
    if not api_key:
        raise HTTPException(status_code=400, detail="Could not decrypt saved Zerodha API key. Save the key again.")
    return {"url": kite_helper.login_url(api_key), "api_key": _mask_secret(api_key, 6, 0)}


@api.post("/zerodha/exchange")
async def zerodha_exchange(req: KiteExchangeReq, user=Depends(get_current_user)):
    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "zerodha"})
    if not keys:
        raise HTTPException(status_code=400, detail="Save Zerodha keys first")
    api_key = decrypt_secret(keys.get("api_key"))
    api_secret = decrypt_secret(keys.get("api_secret"))
    if not api_key or not api_secret:
        raise HTTPException(status_code=400, detail="Could not decrypt saved Zerodha credentials. Save the keys again.")
    session: Dict[str, Any] = {}
    try:
        session = kite_helper.exchange_request_token(
            api_key, api_secret, req.request_token
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Exchange failed: {e}")
    expires_at = kite_helper.next_token_expiry_iso()
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": "zerodha"},
        {"$set": {
            "access_token": encrypt_secret(session.get("access_token")),
            "public_token": encrypt_secret(session.get("public_token")),
            "kite_user_id": session.get("user_id"),
            "access_token_obtained_at": datetime.now(timezone.utc).isoformat(),
            "access_token_expires_at": expires_at,
        }},
    )
    tick_manager = getattr(app.state, "tick_manager", None)
    if tick_manager:
        tick_manager.stop_for_user(user["id"])
    ticker = await _start_user_ticker(user["id"]) if tick_manager else {"started": False, "reason": "tick_manager_missing"}
    return {"connected": True, "kite_user_id": session.get("user_id"), "expires_at": expires_at, "ticker": ticker}


@api.get("/zerodha/status")
async def zerodha_status(user=Depends(get_current_user)):
    _, status = await get_user_kite(user["id"])
    return status





@api.get("/broker/upstox/config")
async def upstox_config(request: Request, user=Depends(get_current_user)):
    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "upstox"}, {"_id": 0})
    redirect_uri = _upstox_redirect_uri(request, keys)
    return {
        "redirect_uri": redirect_uri,
        "register_in_upstox_portal": True,
        "hint": "Add this exact Redirect URL in the Upstox Developer app, then save API Key + Secret here.",
    }


@api.get("/broker/upstox/login")
async def upstox_login(request: Request, user=Depends(get_current_user)):
    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "upstox"}, {"_id": 0})
    if not keys:
        raise HTTPException(status_code=400, detail="Save Upstox API key and secret first.")
    api_key = os.environ.get("UPSTOX_API_KEY") or decrypt_secret(keys.get("api_key"))
    api_secret = os.environ.get("UPSTOX_API_SECRET") or decrypt_secret(keys.get("api_secret"))
    if not api_key or not api_secret:
        raise HTTPException(
            status_code=400,
            detail="Upstox API key or secret is missing. Re-save your Upstox credentials on this page.",
        )
    redirect_uri = _upstox_redirect_uri(request, keys)
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": "upstox"},
        {"$set": {"redirect_uri": redirect_uri, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    gateway = await get_user_upstox_gateway(user["id"], fresh=True)
    if not gateway:
        raise HTTPException(status_code=400, detail="Upstox gateway could not be initialized.")
    gateway.redirect_uri = redirect_uri
    state = _secrets.token_urlsafe(24)
    await db.broker_oauth_states.insert_one({
        "state": state,
        "broker": "upstox",
        "user_id": user["id"],
        "redirect_uri": redirect_uri,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        url = gateway.build_login_url(state=state, redirect_uri=redirect_uri)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Upstox login URL could not be built: {exc}. Register redirect URI exactly as: {redirect_uri}",
        )
    return {"url": url, "redirect_uri": redirect_uri, "api_key": _mask_secret(api_key, 6, 0)}


@api.get("/broker/upstox/callback", name="upstox_callback")
async def upstox_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        raise HTTPException(status_code=400, detail=f"Upstox OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Upstox OAuth code/state.")
    state_doc = await db.broker_oauth_states.find_one({"state": state, "broker": "upstox"}, {"_id": 0})
    if not state_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired Upstox OAuth state. Start login again.")
    user_id = state_doc["user_id"]
    gateway = await get_user_upstox_gateway(user_id, fresh=True)
    if not gateway:
        raise HTTPException(status_code=400, detail="Upstox credentials are not configured for this user.")
    try:
        token_response = await asyncio.to_thread(gateway.exchange_code, code=code, redirect_uri=state_doc.get("redirect_uri"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Upstox token exchange failed: {exc}")
    access_token = token_response.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Upstox token response did not include access_token.")
    
    # Reject mock tokens if in live production mode
    settings = await get_user_settings(user_id)
    if not settings.get("paper_mode", True) and str(access_token).startswith("mock_live_upstox_token"):
        raise HTTPException(status_code=400, detail="Cannot save simulated mock tokens in live production mode.")
    
    refresh_token = token_response.get("refresh_token")
    set_fields = {
        "access_token": encrypt_secret(str(access_token)),
        "upstox_user_id": token_response.get("user_id"),
        "access_token_obtained_at": datetime.now(timezone.utc).isoformat(),
        "token_response_meta": {
            k: v for k, v in token_response.items()
            if k not in {"access_token", "refresh_token", "extended_token"}
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        set_fields["refresh_token"] = encrypt_secret(str(refresh_token))

    await db.broker_keys.update_one(
        {"user_id": user_id, "broker": "upstox"},
        {
            "$set": set_fields,
            "$setOnInsert": {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "broker": "upstox",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        upsert=True,
    )
    await db.broker_oauth_states.delete_one({"state": state, "broker": "upstox"})
    _UPSTOX_GATEWAYS.pop(user_id, None)
    _UPSTOX_TOKEN_VALIDATION_CACHE.pop(user_id, None)
    logger.info("Upstox OAuth connected and token stored for user=%s", user_id)
    base = _public_base_url(None)
    return RedirectResponse(url=f"{base}/broker-keys?upstox=connected", status_code=303)


@api.post("/broker/upstox/order/test")
async def upstox_test_order(req: UpstoxTestOrderReq, user=Depends(get_current_user)):
    side = req.side.upper()
    order_type = req.order_type.upper()
    if side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if order_type not in {"MARKET", "LIMIT", "SL", "SL-M"}:
        raise HTTPException(status_code=400, detail="order_type must be MARKET, LIMIT, SL or SL-M")
    tag = _new_execution_tag()
    preview = {
        "instrument_token": req.instrument_token,
        "quantity": req.qty,
        "side": side,
        "order_type": order_type,
        "product": UpstoxGateway.normalize_product(req.product),
        "price": req.price,
        "validity": req.validity,
        "trigger_price": req.trigger_price,
        "disclosed_quantity": req.disclosed_quantity,
        "is_amo": req.is_amo,
        "market_protection": req.market_protection,
        "tag": tag,
    }
    if not req.confirm_live_order:
        return {
            "ok": False,
            "dry_run": True,
            "message": "Set confirm_live_order=true to place this real Upstox test order.",
            "preview": preview,
        }
    res = await _place_upstox_order(
        user["id"],
        instrument_token=req.instrument_token,
        side=side,
        quantity=req.qty,
        order_type=order_type,
        product=req.product,
        price=req.price,
        tag=tag,
        validity=req.validity,
        trigger_price=req.trigger_price,
        disclosed_quantity=req.disclosed_quantity,
        is_amo=req.is_amo,
        market_protection=req.market_protection,
    )
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "symbol": req.instrument_token,
        "side": side,
        "qty": req.qty,
        "filled_qty": None,
        "pending_qty": None,
        "status_message": None,
        "realised_pnl": 0.0,
        "order_type": order_type,
        "price": float(req.price or 0),
        "brokerage": 0.0,
        "product": UpstoxGateway.normalize_product(req.product),
        "status": "OPEN",
        "mode": "live",
        "broker": "upstox",
        "broker_order_id": res.get("broker_order_id"),
        "execution_tag": tag,
        "execution_attempts": res.get("attempts"),
        "source": "manual-upstox-test",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instrument_token": req.instrument_token,
    }
    await db.orders.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("user_id", None)
    return {"ok": True, "order": doc, "broker_response": res.get("raw")}


@api.get("/broker/upstox/positions")
async def upstox_positions(user=Depends(get_current_user)):
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    return await asyncio.to_thread(gateway.get_positions)


@api.get("/broker/upstox/orders")
async def upstox_orders(user=Depends(get_current_user)):
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    return await asyncio.to_thread(gateway.get_order_book)


@api.get("/broker/upstox/quote")
async def upstox_quote(instrument_key: str, user=Depends(get_current_user)):
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    keys = [part.strip() for part in instrument_key.split(",") if part.strip()]
    return await asyncio.to_thread(gateway.get_market_quote, keys)


@api.post("/broker/upstox/market-data/start")
async def upstox_market_data_start(req: UpstoxSubscribeReq, user=Depends(get_current_user)):
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    valid_instruments: List[str] = []
    rejected: List[Dict[str, str]] = []
    for instrument_key in req.instruments:
        key = str(instrument_key or "").strip()
        if key.startswith("MCX_FO|"):
            contract = await _validate_upstox_mcx_instrument_key(key)
            if not contract:
                rejected.append({"instrument_key": key, "reason": "not_found_in_upstox_mcx_master"})
                logger.warning("Rejecting invalid MCX websocket subscription key=%s", key)
                continue
            logger.info(
                "Validated MCX websocket subscription key=%s symbol=%s type=%s",
                key,
                contract.get("trading_symbol"),
                contract.get("instrument_type"),
            )
        valid_instruments.append(key)
    if not valid_instruments:
        raise HTTPException(status_code=400, detail={"message": "No valid Upstox instrument_key supplied.", "rejected": rejected})
    result = await asyncio.to_thread(gateway.start_market_data_ws, valid_instruments, req.mode)
    result["rejected"] = rejected
    return result


@api.get("/upstox/status")
async def upstox_status(user=Depends(get_current_user)):
    return await get_user_upstox_status(user["id"])


@api.get("/broker/upstox/status")
async def broker_upstox_status(user=Depends(get_current_user)):
    return await get_user_upstox_status(user["id"])


@api.get("/brokers/status")
async def brokers_status(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    _, kite_status = await get_user_kite(user["id"])
    return {
        "version": APP_VERSION,
        "preferences": {
            "data_broker": settings.get("data_broker"),
            "execution_broker": settings.get("execution_broker"),
            "fallback_broker": settings.get("fallback_broker"),
        },
        "brokers": {
            "zerodha": kite_status,
            "kotak_neo": await get_user_kotak_status(user["id"]),
            "upstox": await get_user_upstox_status(user["id"]),
        },
    }


@api.get("/diagnostics/health")
async def diagnostics_health(user=Depends(get_current_user)):
    user_id = user["id"]
    
    # 1. Strategy Running / Not Running
    strategies = await db.strategies.find({"user_id": user_id}, {"_id": 0}).to_list(100)
    strat_diagnostics = []
    for s in strategies:
        strat_diagnostics.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "status": s.get("status"),  # "live" or "paused"
            "mode": s.get("mode"),      # "paper" or "live"
            "symbol": s.get("symbol"),
            "broker": s.get("broker", "upstox"),
        })
        
    # 2. Feed Running / Stopped & Last Quote Time
    upstox_status = await get_user_upstox_status(user_id)
    gateway = upstox_status.get("gateway") or {}
    feed_diagnostics = {
        "connected": bool(upstox_status.get("connected") or gateway.get("connected")),
        "ticks_received": gateway.get("ticks", 0),
        "last_quote_time": gateway.get("last_tick_at"),
        "last_error": gateway.get("last_error"),
    }
    
    # 3. Position Locks Status
    locks = await db.strategy_position_locks.find({"user_id": user_id}).to_list(100)
    lock_diagnostics = []
    for l in locks:
        lock_diagnostics.append({
            "strategy_id": l.get("strategy_id"),
            "trading_symbol": l.get("trading_symbol"),
            "instrument_key": l.get("instrument_key"),
            "created_at": l.get("created_at"),
            "expires_at": l.get("expires_at"),
        })
        
    # 4. Recent Rejected Orders Reasons
    rejected_orders = await db.orders.find(
        {"user_id": user_id, "status": {"$in": ["REJECTED", "CANCELLED", "FAILED"]}},
        {"_id": 0}
    ).sort("created_at", -1).limit(5).to_list(5)
    
    order_diagnostics = []
    for o in rejected_orders:
        order_diagnostics.append({
            "id": o.get("id"),
            "symbol": o.get("symbol"),
            "side": o.get("side"),
            "qty": o.get("qty"),
            "status": o.get("status"),
            "status_message": o.get("status_message"),
            "created_at": o.get("created_at"),
        })
        
    return {
        "status": "healthy",
        "strategies": strat_diagnostics,
        "feed": feed_diagnostics,
        "position_locks": lock_diagnostics,
        "recent_failed_orders": order_diagnostics,
    }


@api.get("/broker/health")
async def broker_health(user=Depends(get_current_user)):
    _, kite_status = await get_user_kite(user["id"])
    tick_manager = getattr(app.state, "tick_manager", None)
    tick_status = tick_manager.status_info(user["id"]) if tick_manager else {"connected": False}
    settings = await get_user_settings(user["id"])
    return {
        "version": APP_VERSION,
        "preferences": {
            "data_broker": settings.get("data_broker"),
            "execution_broker": settings.get("execution_broker"),
            "fallback_broker": settings.get("fallback_broker"),
        },
        "zerodha": {
            **kite_status,
            "ticker": tick_status,
            "healthy": bool(kite_status.get("connected") and tick_status.get("connected") and tick_status.get("last_tick_at")),
        },
        "kotak_neo": await get_user_kotak_status(user["id"]),
        "upstox": await get_user_upstox_status(user["id"]),
        "cache": {
            "kite_history_entries": len(_HISTORY_CACHE),
            "history_ttl_sec": KITE_HISTORY_CACHE_TTL_SEC,
        },
    }


@api.get("/market/feed-comparison")
async def market_feed_comparison(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    tick_manager = getattr(app.state, "tick_manager", None)
    zerodha_tick = tick_manager.status_info(user["id"]) if tick_manager else {"connected": False, "last_error": "tick manager missing"}
    kotak = await get_user_kotak_status(user["id"])
    kotak_gateway = kotak.get("gateway") or {}
    upstox = await get_user_upstox_status(user["id"])
    upstox_gateway = upstox.get("gateway") or {}

    zerodha_last = zerodha_tick.get("last_tick_at")
    kotak_last = kotak_gateway.get("last_tick_at")
    upstox_last = upstox_gateway.get("last_tick_at")
    
    zerodha_age = _age_ms(zerodha_last)
    kotak_age = _age_ms(kotak_last)
    upstox_age = _age_ms(upstox_last)
    
    zerodha_healthy = bool(zerodha_tick.get("connected") and zerodha_last and (zerodha_age is None or zerodha_age < 15000))
    kotak_healthy = bool(kotak_gateway.get("authenticated") and kotak_gateway.get("ticks", 0) > 0 and kotak_last and (kotak_age is None or kotak_age < 15000))
    upstox_healthy = bool(upstox.get("connected") and upstox_gateway.get("ticks", 0) > 0 and upstox_last and (upstox_age is None or upstox_age < 15000))

    candidates = []
    if zerodha_healthy:
        candidates.append(("zerodha", zerodha_age if zerodha_age is not None else 999999))
    if kotak_healthy:
        candidates.append(("kotak_neo", kotak_age if kotak_age is not None else 999999))
    if upstox_healthy:
        candidates.append(("upstox", upstox_age if upstox_age is not None else 999999))

    recommended = settings.get("data_broker", "upstox")
    reason = "Keep configured data broker until a healthier live feed is observed."
    
    if candidates:
        candidates.sort(key=lambda x: x[1])
        best_broker, _ = candidates[0]
        
        preferred = settings.get("data_broker", "upstox")
        pref_healthy = (preferred == "zerodha" and zerodha_healthy) or \
                       (preferred == "kotak_neo" and kotak_healthy) or \
                       (preferred == "upstox" and upstox_healthy)
                       
        if pref_healthy:
            recommended = preferred
            reason = f"Your configured data broker ({recommended}) is healthy and online."
        else:
            recommended = best_broker
            reason = f"{recommended.replace('_', ' ').title()} has fresh ticks and configured broker is stale/unavailable."

    return {
        "configured_data_broker": settings.get("data_broker", "upstox"),
        "recommended_data_broker": recommended,
        "reason": reason,
        "price_provider_chain": [
            "Upstox websocket LTP",
            "Upstox REST quote fallback",
            "historical candle close only for backtest/simulation profiles",
            "no price",
        ],
        "simulated_feed_allowed": bool(settings.get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true",
        "simulated_warning": "Simulated feed active - paper results are not market-valid." if bool(settings.get("allow_simulated_prices")) else None,
        "zerodha": {
            "connected": bool(zerodha_tick.get("connected")),
            "last_tick_at": zerodha_last,
            "age_ms": zerodha_age,
            "subscribed_tokens": zerodha_tick.get("subscribed_tokens"),
            "last_error": zerodha_tick.get("last_error"),
            "healthy": zerodha_healthy,
        },
        "kotak_neo": {
            "connected": bool(kotak.get("connected")),
            "authenticated": bool(kotak_gateway.get("authenticated")),
            "last_tick_at": kotak_last,
            "age_ms": kotak_age,
            "subscribed_tokens": kotak_gateway.get("subscribed_tokens", 0),
            "ticks": kotak_gateway.get("ticks", 0),
            "order_updates": kotak_gateway.get("order_updates", 0),
            "last_error": kotak_gateway.get("last_error") or kotak.get("reason"),
            "healthy": kotak_healthy,
        },
        "upstox": {
            "connected": bool(upstox.get("connected")),
            "authenticated": bool(upstox.get("authenticated")),
            "last_tick_at": upstox_last,
            "age_ms": upstox_age,
            "subscribed_tokens": upstox_gateway.get("subscribed_tokens", 0),
            "ticks": upstox_gateway.get("ticks", 0),
            "last_error": upstox_gateway.get("last_error") or upstox.get("reason"),
            "healthy": upstox_healthy,
        },
    }


@api.post("/market/auto-data-broker")
async def market_auto_data_broker(user=Depends(get_current_user)):
    comparison = await market_feed_comparison(user=user)
    broker = comparison.get("recommended_data_broker")
    if broker not in {"zerodha", "kotak_neo", "upstox"}:
        raise HTTPException(status_code=400, detail="No healthy live data broker is available yet.")
    await db.users.update_one({"id": user["id"]}, {"$set": {"data_broker": broker}})
    comparison["updated"] = True
    comparison["configured_data_broker"] = broker
    return comparison


@api.get("/market/indicators/{symbol}")
async def market_indicators(symbol: str, user=Depends(get_current_user)):
    symbol = symbol.upper()
    history = await _fetch_strategy_history(user["id"], symbol, days=60, interval="5minute")
    data = history.get("data") or []
    if len(data) < 20:
        return {
            "symbol": symbol,
            "source": history.get("source", "none"),
            "is_live": bool(history.get("is_live")),
            "available": False,
            "reason": "Not enough candles yet for indicator stack.",
        }
    trend = MarketTrendAnalyzer.analyze(data, lookback=min(80, max(20, len(data))))
    validation_context = {
        "trend": trend.get("trend"),
        "strength": trend.get("strength"),
        "rsi": trend.get("rsi"),
        "atr_pct": trend.get("atr_pct"),
        "vwap_distance_pct": trend.get("vwap_distance_pct"),
        "higher_timeframe": trend.get("higher_timeframe"),
        "volume_ratio": trend.get("volume_ratio"),
        "support": trend.get("support"),
        "resistance": trend.get("resistance"),
    }
    return {
        "symbol": symbol,
        "source": history.get("source", "unknown"),
        "is_live": bool(history.get("is_live")),
        "paper_mode": bool(history.get("paper_mode")),
        "available": True,
        "candles": len(data),
        "last_candle": data[-1],
        "indicators": validation_context,
    }


@api.get("/market/session-status")
async def market_session_status():
    from core.market_clock import get_market_clock_snapshot
    return get_market_clock_snapshot()


@api.get("/market/session")
async def market_session():
    from core.market_clock import get_market_clock_snapshot
    snapshot = get_market_clock_snapshot()
    nse = snapshot["segments"]["NSE_FO"]
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    minutes = now_ist.hour * 60 + now_ist.minute
    return {
        **snapshot,
        "market": "NSE",
        "server_time_ist": snapshot["current_ist_time"],
        "open": nse["open"],
        "status": nse["status"],
        "open_time": nse["open_time"],
        "close_time": nse["close_time"],
        "minutes_to_close": max(0, NSE_CLOSE_MINUTE - minutes) if nse["open"] else None,
    }


@api.get("/option-chain/{underlying}")
async def option_chain(underlying: str, width: int = 5, user=Depends(get_current_user)):
    underlying = underlying.upper()
    if underlying not in options_helper.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Underlying must be one of {options_helper.SUPPORTED}")
    width = max(1, min(int(width or 5), 10))
    kite, _ = await get_user_kite(user["id"])
    spot = options_helper.get_spot_ltp(kite, underlying) if kite else None
    if spot is None:
        spot = {"NIFTY": 24500.0, "BANKNIFTY": 54000.0, "SENSEX": 80500.0}.get(underlying, 100.0)
    interval = options_helper.STRIKE_INTERVALS[underlying]
    atm = options_helper.round_to_strike(float(spot), interval)
    strikes = [atm + (i * interval) for i in range(-width, width + 1)]
    exchange = options_helper.OPT_EXCHANGE[underlying]
    rows = [{"strike": s, "ce": None, "pe": None} for s in strikes]
    source = "mock"

    if kite:
        instruments = options_helper._load_instruments(kite, exchange)
        today = datetime.now(timezone.utc).date()
        expiries = sorted({
            i["expiry"] for i in instruments
            if i.get("name", "").upper() == underlying and i.get("expiry") and i["expiry"] >= today
        })
        expiry = expiries[0] if expiries else None
        if expiry:
            by_strike = {s: {"CE": None, "PE": None} for s in strikes}
            for inst in instruments:
                strike = int(inst.get("strike") or 0)
                typ = inst.get("instrument_type")
                if strike in by_strike and typ in {"CE", "PE"} and inst.get("expiry") == expiry and inst.get("name", "").upper() == underlying:
                    by_strike[strike][typ] = inst
            tokens = [
                f"{exchange}:{inst['tradingsymbol']}"
                for pair in by_strike.values()
                for inst in pair.values()
                if inst
            ]
            ltp_map = {}
            try:
                for i in range(0, len(tokens), 100):
                    ltp_map.update(kite.ltp(tokens[i:i + 100]) or {})
                source = "zerodha"
            except Exception as e:
                logger.warning(f"option chain ltp failed: {e}")
            rows = []
            for strike in strikes:
                row = {"strike": strike, "ce": None, "pe": None}
                for typ, key_name in (("CE", "ce"), ("PE", "pe")):
                    inst = by_strike[strike].get(typ)
                    if inst:
                        key = f"{exchange}:{inst['tradingsymbol']}"
                        row[key_name] = {
                            "symbol": inst["tradingsymbol"],
                            "token": inst.get("instrument_token"),
                            "ltp": (ltp_map.get(key) or {}).get("last_price"),
                            "lot_size": inst.get("lot_size"),
                        }
                rows.append(row)
            return {
                "underlying": underlying,
                "spot": round(float(spot), 2),
                "atm": atm,
                "exchange": exchange,
                "expiry": expiry.isoformat(),
                "source": source,
                "rows": rows,
            }

    return {
        "underlying": underlying,
        "spot": round(float(spot), 2),
        "atm": atm,
        "exchange": exchange,
        "expiry": None,
        "source": source,
        "rows": [
            {
                "strike": strike,
                "ce": {"symbol": f"{underlying}{strike}CE", "ltp": round(max(1, (spot - strike) + spot * 0.01), 2)},
                "pe": {"symbol": f"{underlying}{strike}PE", "ltp": round(max(1, (strike - spot) + spot * 0.01), 2)},
            }
            for strike in strikes
        ],
    }


@api.post("/zerodha/disconnect")
async def zerodha_disconnect(user=Depends(get_current_user)):
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": "zerodha"},
        {"$unset": {"access_token": "", "public_token": "", "access_token_expires_at": "",
                    "access_token_obtained_at": "", "kite_user_id": ""}},
    )
    tick_manager = getattr(app.state, "tick_manager", None)
    if tick_manager:
        tick_manager.stop_for_user(user["id"])
    return {"disconnected": True}


# ============== Routes: Profile ==============
@api.get("/profile/paper-trading-stats")
async def paper_trading_stats(user=Depends(get_current_user)):
    """Get aggregated P&L from paper trading backtests."""
    trades = await db.paper_trading_history.find(
        {"user_id": user["id"]},
        {"_id": 0}
    ).sort("created_at", -1).to_list(1000)
    
    total_pnl = round(sum(float(t.get("pnl", 0)) for t in trades), 2)
    total_trades = sum(int(t.get("trades_count", 0)) for t in trades)
    total_wins = sum(int(t.get("wins", 0)) for t in trades)
    total_losses = sum(int(t.get("losses", 0)) for t in trades)
    
    return {
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate": round(total_wins / max(1, total_wins + total_losses) * 100, 2),
        "recent_backtests": trades[:10],
    }


@api.get("/profile")
async def get_profile(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    paper_stats = await paper_trading_stats(user=user)
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        "role": user.get("role", "trader"),
        "version": APP_VERSION,
        **settings,
        "upstox": await get_user_upstox_status(user["id"]),
        "paper_trading_stats": paper_stats,
    }


@api.put("/profile")
async def update_profile(req: ProfileUpdateReq, user=Depends(get_current_user)):
    update = {k: v for k, v in req.model_dump().items() if v is not None}
    if "paper_mode" in update and update["paper_mode"] is False:
        upstox_status = await get_user_upstox_status(user["id"])
        if not upstox_status.get("token_valid"):
            raise HTTPException(
                status_code=400,
                detail="Live trading disabled: Reconnect Upstox required before switching to LIVE.",
            )
        # Find if user has MCX strategies (used to pick which session to check)
        strategies = await db.strategies.find({"user_id": user["id"]}).to_list(500)
        has_mcx = any(
            s.get("instrument_group") == "MCX"
            or str(s.get("symbol")).upper() in COMMODITY_UNDERLYINGS
            or "MCX" in str(s.get("symbol")).upper()
            for s in strategies
        )

        # Gate: live mode may only be enabled during market hours.
        # Check MCX session first if the user has MCX strategies; otherwise NSE.
        if has_mcx:
            market_open = _is_order_market_open("MCX")
        else:
            market_open = _is_nse_market_open()
        if not market_open:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Live trading can only be enabled during market hours "
                    "(NSE 09:15•13:30 IST, MCX 09:00•23:30 IST, Mon–Fri). "
                    "Switch to LIVE during an active trading session."
                ),
            )

    if "default_product" in update and update["default_product"] not in ("MIS", "CNC", "NRML"):
        raise HTTPException(status_code=400, detail="default_product must be MIS, CNC or NRML")
    if "default_qty" in update and update["default_qty"] <= 0:
        raise HTTPException(status_code=400, detail="default_qty must be > 0")
    for f in ("max_daily_loss", "max_position_size", "per_strategy_capital"):
        if f in update and update[f] < 0:
            raise HTTPException(status_code=400, detail=f"{f} cannot be negative")
    if "max_trades_per_day" in update and update["max_trades_per_day"] < 0:
        raise HTTPException(status_code=400, detail="max_trades_per_day cannot be negative")
    if "data_broker" in update and update["data_broker"] != "upstox":
        raise HTTPException(status_code=400, detail="data_broker must be upstox")
    if "execution_broker" in update and update["execution_broker"] != "upstox":
        raise HTTPException(status_code=400, detail="execution_broker must be upstox")
    if "fallback_broker" in update and update["fallback_broker"] not in {"none", "upstox"}:
        raise HTTPException(status_code=400, detail="fallback_broker must be none or upstox")
    if update:
        await db.users.update_one({"id": user["id"]}, {"$set": update})
        if "paper_mode" in update:
            await _sync_strategy_modes_to_profile(user["id"], bool(update["paper_mode"]))
    return await get_profile(user=user)


@api.post("/profile/reset-paper")
async def reset_paper_trading(user=Depends(get_current_user)):
    user_id = user["id"]
    logger.info(f"Initiating paper trading reset for user_id={user_id}")
    
    # 1. Fetch all user strategies to get their strategy IDs
    user_strategies = await db.strategies.find({"user_id": user_id}).to_list(1000)
    strategy_ids = [s["id"] for s in user_strategies]
    paper_strategy_ids = [
        s["id"] for s in user_strategies
        if str(s.get("mode") or "paper").lower() == "paper"
    ]
    paper_ledger_strategy_ids = paper_strategy_ids or strategy_ids
    
    # 2. Clear paper orders
    orders_res = await db.orders.delete_many({"user_id": user_id, "mode": "paper"})
    
    # 3. Clear paper strategy positions
    sp_res = await db.strategy_positions.delete_many({"user_id": user_id, "mode": "paper"})
    
    # 4. Clear fallback/paper positions in db.positions without touching live broker positions
    pos_res = await db.positions.delete_many({
        "user_id": user_id,
        "$or": [
            {"mode": "paper"},
            {"broker": "paper"},
            {
                "mode": {"$exists": False},
                "broker_order_id": {"$in": [None, ""]},
                "strategy_id": {"$in": strategy_ids},
            },
        ],
    })
    
    # 5. Clear paper strategy position locks/reservations for the user
    locks_res = await db.strategy_position_locks.delete_many({
        "user_id": user_id,
        "strategy_id": {"$in": strategy_ids},
    })
    
    # 6. Clear stale paper signals for the user
    sig_res = await db.signals.delete_many({
        "user_id": user_id,
        "$or": [
            {"mode": "paper"},
            {
                "mode": {"$exists": False},
                "strategy_id": {"$in": paper_strategy_ids or strategy_ids},
                "status": {"$in": ["PENDING", "FILTERED", "REJECTED", "SKIPPED", "SKIPPED_SIGNAL"]},
            },
        ],
    })
    skipped_res = await db.skipped_signals.delete_many({"user_id": user_id})
    
    # 7. Clear paper trading backtest/history stats
    history_res = await db.paper_trading_history.delete_many({"user_id": user_id})
    
    # 8. Clear closed paper trades
    trades_res = await db.trades.delete_many({"user_id": user_id, "mode": "paper"})
    fills_res = await db.trade_fills.delete_many({"user_id": user_id, "mode": "paper"})
    
    # 9. Clean up options-ledger collections for user's strategies
    open_opt_res = await db.option_open_positions.delete_many({"strategy_id": {"$in": paper_ledger_strategy_ids}})
    opt_pnl_res = await db.option_daily_pnl.delete_many({"strategy_id": {"$in": paper_ledger_strategy_ids}})
    opt_journal_res = await db.option_trade_journal.delete_many({"strategy_id": {"$in": paper_ledger_strategy_ids}})
    
    # Reset options strategy states back to IDLE
    await db.option_strategy_states.update_many(
        {"strategy_id": {"$in": paper_ledger_strategy_ids}},
        {"$set": {"state": "IDLE", "cooldown_until": None}}
    )
    
    # 10. Clean up paper risk/stat events only
    risk_events_res = await db.risk_events.delete_many({"user_id": user_id, "paper": True})

    # 11. Reset paper wallet back to ₹5,00,000
    from core.paper_broker import PaperWallet
    wallet = PaperWallet(db)
    wallet_doc = await wallet.reset(user_id)

    return {
        "ok": True,
        "detail": "Paper trading environment reset successfully. Live configurations intact.",
        "paper_wallet": {"balance": wallet_doc["balance"], "currency": "INR"},
        "purged": {
            "orders": orders_res.deleted_count,
            "strategy_positions": sp_res.deleted_count,
            "broker_positions": pos_res.deleted_count,
            "position_locks": locks_res.deleted_count,
            "signals": sig_res.deleted_count,
            "skipped_signals": skipped_res.deleted_count,
            "stats_history": history_res.deleted_count,
            "closed_trades": trades_res.deleted_count,
            "trade_fills": fills_res.deleted_count,
            "option_open_positions": open_opt_res.deleted_count,
            "option_daily_pnl": opt_pnl_res.deleted_count,
            "option_trade_journal": opt_journal_res.deleted_count,
            "risk_events": risk_events_res.deleted_count,
        }
    }


async def _recover_paper_contract_resolution_halts_for_user(user_id: str) -> Dict[str, Any]:
    """Clear only paper strategy halts caused by option contract resolution failures."""
    settings = await get_user_settings(user_id)
    mode_synced = 0
    if bool(settings.get("paper_mode", True)):
        mode_synced = await _sync_strategy_modes_to_profile(user_id, True)
    now = datetime.now(timezone.utc).isoformat()
    query = {
        "user_id": user_id,
        "mode": "paper",
        "$or": [
            {"halt_reason": "CONTRACT_RESOLUTION_FAILED"},
            {"last_error": {"$regex": "CONTRACT_RESOLUTION_FAILED|contract resolution failed|contract unresolved|option contract resolution failed", "$options": "i"}},
            {"last_filter_reason": {"$regex": "CONTRACT_RESOLUTION_FAILED|contract resolution failed|contract unresolved|option contract resolution failed", "$options": "i"}},
        ],
    }
    rows = await db.strategies.find(query, {"_id": 0, "id": 1, "name": 1, "status": 1}).to_list(500)
    res = await db.strategies.update_many(
        query,
        {
            "$set": {
                "halted": False,
                "is_halted": False,
                "last_error": "",
                "last_filter_reason": "Recovered paper contract-resolution halt; runner will rescan.",
                "paper_contract_recovered_at": now,
            },
            "$unset": {
                "halt_reason": "",
                "last_halt_reason": "",
            },
        },
    )
    return {
        "ok": True,
        "matched": res.matched_count,
        "modified": res.modified_count,
        "mode_synced": mode_synced,
        "strategies": rows,
        "recovered_at": now,
    }


@api.post("/profile/recover-paper-contract-halts")
async def recover_paper_contract_halts(user=Depends(get_current_user)):
    return await _recover_paper_contract_resolution_halts_for_user(user["id"])


@api.get("/paper-wallet")
async def get_paper_wallet(user=Depends(get_current_user)):
    """Return the user's paper trading wallet balance and P&L summary."""
    from core.paper_broker import PaperWallet
    pw = PaperWallet(db)
    wallet = await pw.get_or_initialize(user["id"])
    summary = pw.summary(wallet)
    # Attach recent paper fills for context
    recent_fills = await db.fills.find(
        {"user_id": user["id"], "mode": "paper"},
        {"_id": 0, "id": 1, "side": 1, "symbol": 1, "target_symbol": 1,
         "qty": 1, "price": 1, "trade_value": 1, "brokerage": 1, "created_at": 1}
    ).sort("created_at", -1).limit(10).to_list(10)
    return {
        "ok": True,
        "currency": "INR",
        **summary,
        "recent_fills": recent_fills,
    }


@api.post("/profile/change-password")
async def change_password(req: ChangePasswordReq, user=Depends(get_current_user)):
    full = await db.users.find_one({"id": user["id"]})
    if not full or not verify_password(req.current_password, full["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_password(req.new_password)}})
    return {"changed": True}


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
            
            recorded_symbols = {"NIFTY": False, "SENSEX": False, "CRUDEOIL": False, "CRUDEOILM": False, "NATURALGAS": False}

            for user_row in users:
                user_id = user_row["id"]
                
                # Check Upstox live spot/future prices first if connected.
                upstox_gw = await get_user_upstox_gateway(user_id)
                if upstox_gw and upstox_gw.connected:
                    upstox_keys = {
                        "NIFTY": "NSE_INDEX|Nifty 50",
                        "SENSEX": "BSE_INDEX|SENSEX"
                    }
                    for comm_symbol in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS"):
                        contract = await _resolve_upstox_mcx_future_contract(comm_symbol)
                        if contract and contract.get("instrument_key"):
                            upstox_keys[comm_symbol] = contract["instrument_key"]
                    try:
                        quotes = await asyncio.to_thread(upstox_gw.get_market_quote, list(upstox_keys.values()))
                        data_node = quotes.get("data", {}) or {}
                        for idx_sym, upstox_key in upstox_keys.items():
                            node = data_node.get(upstox_key) or {}
                            spot_ltp = node.get("last_price") or node.get("ltp")
                            if spot_ltp:
                                option_ledger.record_market_tick(idx_sym, float(spot_ltp), "upstox")
                                if idx_sym in recorded_symbols:
                                    recorded_symbols[idx_sym] = True
                    except Exception as e:
                        logger.warning(f"Upstox spot/future quote failed in monitor loop: {e}")

                settings = await get_user_settings(user_id)
                if settings.get("data_broker") == "upstox":
                    continue
                kite, _ = await get_user_kite(user_id)
                if not kite:
                    continue
                broker_data = kite_helper.safe_positions(kite)
                net_positions = (broker_data or {}).get("net") or []
                broker_symbols_by_user[user_id] = {
                    p.get("tradingsymbol"): p for p in net_positions if p.get("tradingsymbol") and int(p.get("quantity") or 0) != 0
                }
                for idx_symbol in ("NIFTY", "SENSEX"):
                    exch, kite_symbol = options_helper.INDEX_SPOT_SYMBOL[idx_symbol]
                    key = f"{exch}:{kite_symbol}"
                    ltp_resp = kite_helper.safe_ltp(kite, [key]) or {}
                    node = ltp_resp.get(key) or {}
                    if node.get("last_price"):
                        option_ledger.record_market_tick(idx_symbol, float(node["last_price"]), "zerodha")
                        recorded_symbols[idx_symbol] = True

            # Simulated random walk fallback so index/commodity telemetry is never stuck on "Waiting..."
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
                
            for comm_symbol, base_price in [("CRUDEOIL", 6550.0), ("CRUDEOILM", 6550.0), ("NATURALGAS", 215.0)]:
                if recorded_symbols.get(comm_symbol):
                    continue
                last_comm = option_ledger.latest_ticks([comm_symbol]).get(comm_symbol)
                last_price = float(last_comm["ltp"]) if last_comm else base_price
                step_range = 1.0 if comm_symbol in {"CRUDEOIL", "CRUDEOILM"} else 0.1
                new_price = round(last_price + random.uniform(-step_range, step_range), 2)
                option_ledger.record_market_tick(comm_symbol, new_price, "simulated")

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
    if "|" not in key:
        _log_throttled(
            f"invalid-upstox-key:{key}",
            120.0,
            logging.WARNING,
            "Rejecting invalid Upstox LTP key before API call instrument_key=%s",
            key,
        )
        return None
    gateway = await get_user_upstox_gateway(user_id)
    if not gateway or not gateway.connected:
        return None
    try:
        quote = await asyncio.to_thread(gateway.get_market_quote, [key])
        return UpstoxGateway.parse_quote_ltp(quote, key)
    except Exception as exc:
        logger.warning("Upstox LTP failed instrument_key=%s: %s", key, exc)
        return None


def _mongo_position_exit_reason(position: Dict[str, Any], ltp: float) -> Optional[str]:
    entry = float(position.get("average_buy_price") or 0)
    if entry <= 0 or ltp <= 0:
        return None
    risk = _normalize_strategy_risk(position.get("tp_sl_tsl_config") or {})
    side = str(position.get("position_side") or "LONG").upper()
    prices = _position_risk_prices({**position, "tp_sl_tsl_config": risk}, ltp=ltp)
    stop_price = prices.get("stop_loss")
    target_price = prices.get("take_profit")
    if stop_price is None or target_price is None:
        return None
    stop_price = float(stop_price)
    target_price = float(target_price)
    if side == "SHORT":
        if ltp >= stop_price:
            return "trailing-sl" if prices.get("trailing_sl") else "stop-loss"
        if ltp <= target_price:
            return "take-profit"
    else:
        if ltp <= stop_price:
            return "trailing-sl" if prices.get("trailing_sl") else "stop-loss"
        if ltp >= target_price:
            return "take-profit"
    time_exit_minutes = int(risk.get("time_exit_minutes") or 0)
    if time_exit_minutes > 0:
        entry_dt = _parse_iso_dt(position.get("entry_time"))
        if entry_dt and (datetime.now(timezone.utc) - entry_dt).total_seconds() >= time_exit_minutes * 60:
            return f"time-exit-{time_exit_minutes}m"
    return None


async def _mongo_position_monitor_loop(stop_event: asyncio.Event) -> None:
    logger.info("Mongo strategy position monitor started")
    while not stop_event.is_set():
        try:
            rows = await db.strategy_positions.find(
                {"status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
                {"_id": 0},
            ).to_list(1000)
            for pos in rows:
                user_id = pos.get("user_id")
                sid = pos.get("strategy_id")
                symbol = pos.get("trading_symbol") or pos.get("symbol")
                if not user_id or not sid or not symbol:
                    continue
                ltp = await _quote_upstox_instrument_key(user_id, pos.get("instrument_token"))
                if ltp is None:
                    settings = await get_user_settings(user_id)
                    is_paper_pos = pos.get("mode") == "paper"
                    allow_simulated = bool(settings.get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
                    allow_mock_ltp = is_paper_pos and allow_simulated
                    ltp = await _current_ltp_for_symbol(
                        user_id,
                        symbol,
                        pos.get("exchange") or "NSE",
                        allow_mock=allow_mock_ltp,
                        execution_broker="upstox",
                    )
                if ltp is None:
                    await db.strategy_positions.update_one(
                        {"id": pos["id"], "user_id": user_id},
                        {"$set": {
                            "last_ltp": "LTP_UNAVAILABLE",
                            "last_error": "LTP_UNAVAILABLE: websocket disconnected or price feed offline",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }},
                    )
                    continue
                entry = float(pos.get("average_buy_price") or 0)
                qty = int(pos.get("open_quantity") or pos.get("quantity") or 0)
                side = str(pos.get("position_side") or "LONG").upper()
                pnl = round((entry - float(ltp)) * qty, 2) if side == "SHORT" else round((float(ltp) - entry) * qty, 2)
                risk_prices = _position_risk_prices(pos, ltp=float(ltp))
                risk_update = {}
                risk = _normalize_strategy_risk(pos.get("tp_sl_tsl_config") or {})
                if risk_prices.get("stop_loss") is not None:
                    risk["stoploss_price"] = risk_prices["stop_loss"]
                    risk["stop_loss"] = risk_prices["stop_loss"]
                if risk_prices.get("take_profit") is not None:
                    risk["target_price"] = risk_prices["take_profit"]
                    risk["take_profit"] = risk_prices["take_profit"]
                if risk_prices.get("trailing_sl") is not None:
                    risk["trailing_sl"] = risk_prices["trailing_sl"]
                risk_update["tp_sl_tsl_config"] = risk
                await db.strategy_positions.update_one(
                    {"id": pos["id"], "user_id": user_id},
                    {"$set": {"last_ltp": float(ltp), "unrealized_pnl": pnl, "last_tick_at": datetime.now(timezone.utc).isoformat(), "updated_at": datetime.now(timezone.utc).isoformat(), **risk_update},
                     "$unset": {"last_error": ""}},
                )
                reason = _mongo_position_exit_reason(pos, float(ltp))
                if reason:
                    logger.info("Mongo position monitor exit strategy=%s symbol=%s reason=%s", sid, symbol, reason)
                    await _close_strategy_positions(user_id, sid, reason=reason)
        except Exception as e:
            logger.warning(f"Mongo strategy position monitor error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < 30:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Mongo strategy position monitor stopped")


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
                        gw = await get_user_upstox_gateway(user_id)
                        if gw and gw.connected:
                            streams = getattr(app.state, "upstox_portfolio_streams", None)
                            if streams is None:
                                streams = {}
                                app.state.upstox_portfolio_streams = streams
                            if user_id not in streams:
                                loop = asyncio.get_running_loop()

                                def _on_event(payload, uid=user_id):
                                    loop.call_soon_threadsafe(
                                        lambda: asyncio.create_task(apply_broker_truth_event(db, {**payload, "user_id": uid}, source="portfolio_stream"))
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

api.include_router(auth_router)
api.include_router(ai_router)
api.include_router(agent_router)
api.include_router(ops_router)

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


@app.on_event("startup")
async def startup():
    app.state.option_ledger = option_ledger
    execution_state_manager.configure(
        db=db,
        get_user_settings=get_user_settings,
        get_user_kite=get_user_kite,
        sync_kite_orders=_sync_kite_order_statuses,
        sync_kotak_orders=_sync_kotak_order_statuses,
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
        ("orders", [("user_id", 1), ("created_at", -1)], {}),
        ("orders", [("user_id", 1), ("status", 1), ("created_at", -1)], {}),
        ("orders", [("user_id", 1), ("strategy_id", 1), ("created_at", -1)], {}),
        ("orders", "idempotency_key", {"unique": True, "sparse": True}),
        ("strategy_positions", [("user_id", 1), ("strategy_id", 1), ("status", 1)], {}),
        ("strategy_positions", "active_instrument_key", {"unique": True, "sparse": True}),
        ("strategy_positions", "active_strategy_key", {"unique": True, "sparse": True}),
        ("positions", [("user_id", 1), ("symbol", 1)], {"unique": True}),
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
        ("upstox_mcx_option_contracts", "cache_key", {"unique": True}),
        ("upstox_mcx_option_contracts", [("underlying", 1), ("option_type", 1), ("expiry", 1), ("strike", 1)], {}),
        ("upstox_mcx_option_contracts", "instrument_key", {}),
        ("upstox_mcx_future_contracts", "cache_key", {"unique": True}),
        ("upstox_mcx_future_contracts", [("underlying", 1), ("expiry", 1)], {}),
        ("upstox_mcx_future_contracts", "instrument_key", {}),
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
    ]:
        try:
            await db[coll].create_index(key, **opts)
        except Exception as e:
            logger.warning(f"index create on {coll} skipped: {e}")

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
            await seed_default_strategies_for_user(user_id)
            await migrate_user_to_v12_upstox(user_id)
            await migrate_user_to_upstox_quality_system(user_id)
            settings = await get_user_settings(user_id)
            if bool(settings.get("paper_mode", True)):
                synced_modes = await _sync_strategy_modes_to_profile(user_id, True)
                if synced_modes:
                    logger.warning("Startup synced %s strategy mode(s) to PAPER for user %s", synced_modes, user_id)
            
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
    except Exception as e:
        logger.warning(f"default strategy seeding/reconciliation skipped: {e}")

    # Background strategy runner — uses REAL Kite candles when user is connected,
    # falls back to MOCK 5-min intraday candles only when no broker session.
    # Mock data uses unique 5-min timestamps so signal-dedup-by-date works correctly.
    async def _price_history(user_id: str, symbol: str, days: int = 60, strategy: Optional[dict] = None):
        settings = await get_user_settings(user_id)
        strategy_mode = (strategy or {}).get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
        allow_mock = strategy_mode == "paper"
        return await _fetch_strategy_history(
            user_id,
            symbol,
            days=days,
            interval="5minute",
            allow_mock=allow_mock,
            strategy=strategy,
        ) | {"paper_mode": allow_mock}

    # Resolver for index option contracts — runner uses this when a strategy
    # has visual_config.options.enabled. Requires a live Kite session.
    async def _resolve_option(user_id: str, underlying: str, signal_action: str,
                              strike_mode: str, otm_points: int = 0,
                              expiry_offset: int = 0, strategy: Optional[dict] = None):
        strategy = strategy or {}
        settings = await get_user_settings(user_id)
        mode = str(strategy.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")).lower()
        underlying = str(underlying or "NIFTY").upper()
        strike_mode_u = str(strike_mode or "ATM_BUY").upper()
        action_u = str(signal_action or "BUY").upper()
        is_mcx = underlying in COMMODITY_UNDERLYINGS
        option_side = "CE" if action_u == "BUY" else "PE"
        if "SELL" in strike_mode_u:
            option_side = "PE" if action_u == "BUY" else "CE"
        strike_rule = "OTM1" if int(otm_points or 0) > 0 else "ATM"
        instrument_type = "MCX_OPTION" if is_mcx else "INDEX_OPTION"
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
        if is_mcx:
            future = await _resolve_upstox_mcx_future_contract(underlying)
            if future and future.get("instrument_key") and upstox_gw and upstox_gw.connected:
                try:
                    q = await asyncio.to_thread(upstox_gw.get_market_quote, [future["instrument_key"]])
                    spot_hint = UpstoxGateway.parse_quote_ltp(q, future["instrument_key"])
                    diagnostics.update({
                        "resolver_stage": "mcx_future_quote",
                        "resolver_reason": "spot_from_upstox_mcx_future",
                        "instrument_key": future.get("instrument_key"),
                        "quote_source": "UPSTOX_LIVE",
                    })
                except Exception as exc:
                    diagnostics.update({
                        "resolver_stage": "mcx_future_quote",
                        "resolver_reason": f"upstox_future_quote_failed:{exc}",
                        "instrument_key": future.get("instrument_key"),
                    })

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
                sub_res = await asyncio.to_thread(upstox_gw.start_market_data_ws, [instrument.instrument_key], "ltpc")
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
            "transaction_type": action_u,
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
    app.state.kotak_gateways = _KOTAK_GATEWAYS
    app.state.upstox_gateways = _UPSTOX_GATEWAYS
    app.state.mcx_contract_resolver = MCXContractResolver(db)
    try:
        await app.state.mcx_contract_resolver.ensure_cache(reason="startup")
    except Exception as e:
        logger.warning(f"MCX contract cache startup refresh skipped: {e}")
    app.state.mcx_refresh_stop = asyncio.Event()
    app.state.mcx_refresh_task = asyncio.create_task(
        mcx_instrument_refresh_loop(db, app.state.mcx_refresh_stop)
    )
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
    app.state.position_monitor_task = asyncio.create_task(_mongo_position_monitor_loop(app.state.position_monitor_stop))
    app.state.option_engine_stop = asyncio.Event()
    app.state.option_engine_task = asyncio.create_task(_option_engine_monitor_loop(app.state.option_engine_stop))
    app.state.broker_reconcile_stop = asyncio.Event()
    app.state.broker_reconcile_task = asyncio.create_task(_broker_reconciliation_loop(app.state.broker_reconcile_stop))
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
        for stream in getattr(app.state, "upstox_portfolio_streams", {}).values():
            stream.stop()
    except Exception:
        pass
    try:
        app.state.mcx_refresh_stop.set()
        if app.state.mcx_refresh_task:
            await asyncio.wait_for(app.state.mcx_refresh_task, timeout=3.0)
    except Exception:
        pass
    client.close()


# ============== Routes: Trading-Ready Check ==============

@api.get("/ops/trading-ready")
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

    # 6. MCX instrument master seeded
    try:
        mcx_count = await db.upstox_mcx_future_contracts.count_documents({})
        mcx_ok = mcx_count > 0
        checks["mcx_instrument_master"] = {
            "ok": mcx_ok,
            "detail": f"{mcx_count} MCX futures cached",
            "critical": False,
        }
    except Exception as exc:
        checks["mcx_instrument_master"] = {"ok": False, "detail": str(exc)[:120], "critical": False}

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

@api.get("/core/health")
async def get_core_health():
    return {
        "status": "healthy",
        "engine": "core_unified",
        "shadow_mode": os.environ.get("CORE_ENGINE_SHADOW_MODE", "true") == "true",
        "version": "1.0.0"
    }

@api.get("/core/market-status")
async def get_core_market_status():
    return get_market_clock_snapshot()

@api.get("/core/feed-status")
async def get_core_feed_status(user=Depends(get_current_user)):
    user_id = user["id"]
    status = await get_user_upstox_status(user_id)
    return {
        "connected": status.get("connected", False),
        "token_valid": status.get("token_valid", False),
        "feed_source": "upstox_websocket_v3",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@api.get("/core/strategies")
async def get_core_strategies(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.strategies.find({"user_id": user_id}).to_list(length=200)

@api.get("/core/signals")
async def get_core_signals(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.signals.find({"user_id": user_id}).sort("created_at", -1).to_list(length=200)

@api.get("/core/orders")
async def get_core_orders(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.orders.find({"user_id": user_id}).sort("created_at", -1).to_list(length=200)

@api.get("/core/positions")
async def get_core_positions(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.strategy_positions.find({"user_id": user_id}).to_list(length=200)

@api.get("/core/performance")
async def get_core_performance(user=Depends(get_current_user)):
    user_id = user["id"]
    tracker = PerformanceTracker(db)
    return await tracker.rebuild_leaderboard(user_id)

@api.get("/core/backtests")
async def get_core_backtests(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.backtest_runs.find().sort("created_at", -1).to_list(length=100)

@api.post("/core/backtests/run")
async def run_core_backtest(req: BacktestReq, user=Depends(get_current_user)):
    user_id = user["id"]
    strategy_id = req.strategy_id
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required.")
        
    strat = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")
        
    try:
        # Generate 250 test intraday candles
        raw_candles = intraday_series(100.0, 250)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed generating backtest historical bars: {e}")
        
    engine = BacktestEngine(db)
    result = await engine.run_backtest(
        strategy_id=strategy_id,
        python_code=strat.get("python_code") or "",
        candles=raw_candles,
        strategy_metadata=strat
    )
    return result

@api.get("/trading/live-readiness")
async def get_trading_live_readiness(user=Depends(get_current_user)):
    user_id = user["id"]
    checks = []
    
    # 1. Explicit live-auto enablement in environment
    live_env_enabled = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    checks.append({
        "id": "live_env_enabled",
        "label": "CORE_ENGINE_LIVE_ENABLED set to true in .env",
        "ok": live_env_enabled,
        "detail": f"CORE_ENGINE_LIVE_ENABLED={os.environ.get('CORE_ENGINE_LIVE_ENABLED')}",
        "hint": "Set CORE_ENGINE_LIVE_ENABLED=true in .env to allow live trading."
    })
    
    # 2. Live armed state in DB
    arm_state = await db.live_arm_state.find_one({"user_id": user_id})
    live_db_armed = bool(arm_state and arm_state.get("armed"))
    global_live_enabled = bool(arm_state and arm_state.get("global_live_enabled"))
    checks.append({
        "id": "live_db_armed",
        "label": "System manually armed in database",
        "ok": live_db_armed and global_live_enabled,
        "detail": f"armed={live_db_armed}, global_live_enabled={global_live_enabled}",
        "hint": "Arm system in Control Room or call /api/core/live/arm"
    })
    
    # 3. Upstox Key/Secret Configuration check
    upstox_status = await get_user_upstox_status(user_id)
    keys_saved = bool(upstox_status.get("keys_saved"))
    checks.append({
        "id": "upstox_keys",
        "label": "Upstox key/secret configured",
        "ok": keys_saved,
        "detail": f"keys_saved={keys_saved}",
        "hint": "Save Upstox API keys in Broker Keys page."
    })
    
    # 4. Active Session and Token Validity
    token_valid = bool(upstox_status.get("connected") and upstox_status.get("token_valid"))
    checks.append({
        "id": "upstox_token",
        "label": "Upstox OAuth session active",
        "ok": token_valid,
        "detail": f"connected={upstox_status.get('connected')}, token_valid={upstox_status.get('token_valid')}",
        "hint": "Log in to Upstox through Broker Keys callback."
    })
    
    # 5. Websocket Feed status and quote freshness
    gateway_status = upstox_status.get("gateway") or {}
    feed_status = gateway_status.get("feed_status") or upstox_status.get("feed_status") or {}
    feed_ok = bool(upstox_status.get("connected") and (feed_status.get("connected") or gateway_status.get("ws_running")))
    checks.append({
        "id": "feed_status",
        "label": "Upstox live price feed active",
        "ok": feed_ok,
        "detail": f"state={feed_status.get('state') or 'running'}",
        "hint": "Restart Upstox feed from Control Room."
    })

    sync_meta = await _maybe_await(db.upstox_instrument_sync_meta.find_one({"_id": "daily-json"}, {"_id": 0})) or {}
    instrument_count = await _maybe_await(db.upstox_instruments.count_documents({}))
    if not isinstance(instrument_count, (int, float)):
        instrument_count = 0
    instrument_sync_ok = bool(sync_meta.get("completed_at") and instrument_count > 0)
    checks.append({
        "id": "upstox_instrument_master",
        "label": "Daily Upstox instrument master synced",
        "ok": instrument_sync_ok,
        "detail": f"completed_at={sync_meta.get('completed_at')}, instruments={instrument_count}",
        "hint": "Run /api/upstox/instruments/sync before live order placement."
    })

    try:
        reconciliation = await broker_reconciliation_summary(db, user_id, await get_user_upstox_gateway(user_id))
    except Exception as exc:
        reconciliation = {"status": "UNKNOWN", "errors": [str(exc)[:200]], "pending_orders": []}
    recon_ok = str(reconciliation.get("status") or "").upper() in {"OK", "READY", "NO_GATEWAY"} and not reconciliation.get("errors")
    checks.append({
        "id": "broker_truth_reconciliation",
        "label": "Broker truth reconciliation clean",
        "ok": recon_ok,
        "detail": f"status={reconciliation.get('status')}, pending={len(reconciliation.get('pending_orders') or [])}",
        "hint": "Resolve pending/unknown broker orders before enabling live mode."
    })
    
    # 6. Instrument resolution readiness (MCX Contract resolver state / active resolver count)
    exchange_rules = await db.system_config.find_one({"_id": "exchange_rules"})
    rules_ok = bool(exchange_rules and exchange_rules.get("lot_sizes"))
    checks.append({
        "id": "exchange_rules",
        "label": "Instrument lot sizes and exchange rules resolution",
        "ok": rules_ok,
        "detail": f"rules_present={rules_ok}",
        "hint": "Run MCX resolver initialization or verify MongoDB system_config."
    })
    
    # 7. Funds & margins presence
    funds = await db.funds.find_one({"user_id": user_id})
    funds_ok = bool(funds and float(funds.get("available_margin") or 0) > 0)
    checks.append({
        "id": "funds_presence",
        "label": "Funds and margins balance present",
        "ok": funds_ok,
        "detail": f"margin=₹{funds.get('available_margin') or 0 if funds else 0:.2f}",
        "hint": "Fetch funds from Upstox broker API or check fund ledger."
    })
    
    # 8. Global kill switch state (db.risk_state and option ledger)
    kill_switch = await db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = bool(kill_switch and kill_switch.get("active"))
    checks.append({
        "id": "kill_switch",
        "label": "Global kill-switch status (INACTIVE is ok)",
        "ok": not kill_active,
        "detail": f"kill_switch_active={kill_active}",
        "hint": "Deactivate global kill-switch or reset risk state in DB."
    })
    
    # 9. Stale reconciliation state (active orders with status UNKNOWN_NEEDS_REVIEW)
    unknown_orders_count = await db.orders.count_documents({
        "user_id": user_id,
        "status": "UNKNOWN_NEEDS_REVIEW"
    })
    checks.append({
        "id": "stale_reconciliation",
        "label": "No active orders needing manual review",
        "ok": unknown_orders_count == 0,
        "detail": f"orders_needing_review={unknown_orders_count}",
        "hint": "Manually reconcile orders marked UNKNOWN_NEEDS_REVIEW."
    })
    
    overall_ok = all(c["ok"] for c in checks)
    return {
        "ok": overall_ok,
        "live_order_placement_ready": overall_ok,
        "live_auto_trading_enabled": False,
        "live_auto_trading_default": "disabled",
        "broker": "upstox",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@api.get("/core/live/readiness")
async def get_core_live_readiness(user=Depends(get_current_user)):
    user_id = user["id"]
    firewall = LiveSafetyFirewall(db)
    res = await firewall.verify_readiness(user_id, "manual", "NIFTY")
    return res

@api.post("/core/live/arm")
async def post_core_live_arm(user=Depends(get_current_user)):
    user_id = user["id"]
    await db.live_arm_state.update_one(
        {"user_id": user_id},
        {"$set": {"armed": True, "global_live_enabled": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "ARMED"}

@api.post("/core/live/disarm")
async def post_core_live_disarm(user=Depends(get_current_user)):
    user_id = user["id"]
    await db.live_arm_state.update_one(
        {"user_id": user_id},
        {"$set": {"armed": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "DISARMED"}

@api.post("/core/kill-switch")
async def post_core_kill_switch(user=Depends(get_current_user)):
    await db.risk_state.update_one(
        {"_id": "global_kill_switch"},
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "KILL_SWITCH_ACTIVE"}

# ============== Register Router ==============
app.include_router(api)
