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
from pydantic import BaseModel, Field, EmailStr

import kite_helper
import kotak_helper
import upstox_helper
from kotak_gateway import KotakNeoGateway
from brokers.upstox_gateway import UpstoxGateway, extract_order_id as extract_upstox_order_id
from brokers import upstox_gateway as upstox_gateway_utils
import options_helper
import backtrader_runner
import strategy_runner
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
    ORDER_ACTIVE_STATUSES,
    ORDER_TERMINAL_STATUSES,
    LEGACY_OPEN_STATUSES,
    LEGACY_FILLED_STATUSES,
    LEGACY_TERMINAL_STATUSES,
    canonical_order_status,
    is_order_active,
)

# Cryptographically strong RNG for mock data jitter — replaces _rng.random()
_rng = _secrets.SystemRandom()

# Mongo
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY") or "quantg-development-secret"
if len(JWT_SECRET.encode()) < 32:
    JWT_SECRET = hashlib.sha256(JWT_SECRET.encode()).hexdigest()
JWT_ALG = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24 * 7  # 7 days for trader convenience
SIGNAL_CONFIDENCE_MIN = float(os.environ.get("SIGNAL_CONFIDENCE_MIN", "45"))
APP_VERSION = "11.0"

app = FastAPI(title="QuantG Algo Trading API", version=APP_VERSION)
api = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quantdesk")

OPTION_LEDGER_PATH = os.environ.get("OPTION_LEDGER_PATH") or str(ROOT_DIR / "runtime_state.sqlite3")
option_ledger = OptionStateLedger(
    OPTION_LEDGER_PATH,
    pool_size=int(os.environ.get("OPTION_LEDGER_POOL_SIZE", "4")),
)

KITE_HISTORICAL_MIN_INTERVAL_SEC = float(os.environ.get("KITE_HISTORICAL_MIN_INTERVAL_SEC", "0.40"))
KITE_HISTORY_CACHE_TTL_SEC = int(os.environ.get("KITE_HISTORY_CACHE_TTL_SEC", "55"))
KITE_ORDER_SYNC_TTL_SEC = int(os.environ.get("KITE_ORDER_SYNC_TTL_SEC", "10"))
_RATE_LIMIT_LOCK = asyncio.Lock()
_RATE_LIMIT_LAST: Dict[str, float] = {}
_HISTORY_CACHE: Dict[str, Dict[str, Any]] = {}
_ORDER_SYNC_CACHE: Dict[str, Dict[str, Any]] = {}
_KOTAK_GATEWAYS: Dict[str, KotakNeoGateway] = {}
_UPSTOX_GATEWAYS: Dict[str, UpstoxGateway] = {}
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
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
    broker: str = "zerodha"
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
    broker: str          # 'zerodha', 'kotak', 'upstox'
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
    {"symbol": "NATURALGAS", "name": "MCX Natural Gas", "base": 245.0, "exchange": "MCX"},
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
STALE_ORDER_STATUSES = {"STALE", "BROKER_NOT_FOUND"}


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

    Zerodha Kite is the preferred source. Mock candles are only a paper/demo
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
    data_broker = settings.get("data_broker", "zerodha")

    if data_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user_id)
        if upstox_gw and upstox_gw.connected:
            exchange = "MCX" if sym_upper in {"CRUDEOIL", "NATURALGAS"} or "MCX" in sym_upper or sym_upper.endswith("FUT") else "NSE"
            token = _upstox_instrument_token(exchange, sym_upper)
            if token:
                # Ensure we start tracking in background
                upstox_gw.start_market_data_ws([token])
                # Fetch candles
                live_data = await asyncio.to_thread(upstox_gw.get_historical_candles, token, interval, days)
                if live_data and len(live_data) > min_intraday_bars:
                    return {
                        "data": live_data,
                        "source": f"upstox-{interval}:{sym_upper}",
                        "is_live": True,
                        "interval": interval,
                    }

    if kite:

        token = None
        source_kind = "equity"
        if sym_upper in options_helper.INDEX_SPOT_SYMBOL:
            token = await _index_spot_token(kite, sym_upper)
            source_kind = "index-spot"
        elif sym_upper in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS"}:
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
    if "kotak" in text or "mcx" in text or "commodity" in text or "gas" in text or "oil" in text:
        return (
            "🛢️ **Kotak Neo & MCX Commodities Integration**\n\n"
            "- **Commodity Options & Futures:** QuantG supports MCX commodities (Crude Oil, Natural Gas) via the Kotak Neo gateway.\n"
            "- **Symbol Formatting:** Always use exact Kotak-compliant MCX symbols (e.g., `CRUDEOIL26MAYFUT` or `NATURALGAS26MAYFUT`).\n"
            "- **Risk Management:** Commodity margins are high. Double-check your capital allocation bounds in the strategy card's 'Risk & Exit Bounds' before switching live trading on."
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
        "- `Zerodha Kite` (performance guides, API rate limits, hardware thresholds)\n"
        "- `Kotak Neo MCX` (commodity option symbols, Crude Oil / Natural Gas setups)\n"
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
   - **Zerodha Kite:** Standard REST/WebSocket ticker integration. Rate-limited to ~100 calls/min. Recommended limits: max 2-3 concurrent live strategies, scanning every 30 seconds.
   - **Kotak Neo:** Standard integration supporting Equity/F&O and MCX Commodities. Commodity symbol format requires exact Kotak-compliant structures (e.g., `CRUDEOIL26MAYFUT`, `NATURALGAS26MAYFUT`).
   - **Upstox API v2 HFT Gateway:** Configured to run on high-speed order execution pipelines at `api-hft.upstox.com` in live mode when `hft=True` is provided (e.g. standard for HFT option scalp presets).
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
    broker = (req.broker or "zerodha").strip().lower()
    if broker not in {"zerodha", "kotak_neo", "upstox"}:
        raise HTTPException(400, "Unsupported broker")
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
    if broker == "kotak_neo":
        # Allow Kotak to be configured from the UI. Env vars still override
        # these values at runtime for VPS deployments.
        for key, value in {
            "mobile_number": req.mobile_number,
            "mpin": req.mpin,
            "totp_secret_key": req.totp_secret_key,
        }.items():
            if value not in (None, ""):
                doc[key] = encrypt_secret(value)
            elif existing and existing.get(key):
                doc[key] = existing.get(key)
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
    rows = await db.broker_keys.find({"user_id": user["id"]}, {"_id": 0}).to_list(50)
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
    data_broker = settings.get("data_broker", "zerodha")
    rows = []

    if data_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user["id"])
        if upstox_gw and upstox_gw.connected:
            # Resolve commodity tokens
            keys = []
            for s in COMMODITY_SYMBOLS:
                token = _upstox_instrument_token("MCX", s["symbol"])
                if token:
                    keys.append(token)
            if keys:
                upstox_gw.start_market_data_ws(keys)
                
            for i, s in enumerate(COMMODITY_SYMBOLS):
                token = _upstox_instrument_token("MCX", s["symbol"])
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
                        "feed": "upstox-mcx",
                        "tick_time": tick.get("received_at"),
                    })
                    continue
                lp = live_price(s["base"], i + 100)
                rows.append({
                    "symbol": s["symbol"],
                    "name": s["name"],
                    "exchange": s["exchange"],
                    **lp,
                    "source": "upstox_pending",
                    "feed": "upstox-mcx-mock",
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
    "stop_loss_pct": 22.0,
    "take_profit_pct": 45.0,
    "trail_trigger_pct": 25.0,
    "trail_step_pct": 10.0,
    "cooldown_minutes": 20,
    "max_trades_day": 2,
    "time_exit_minutes": 45,
    "indicator_exit_enabled": True,
    "exit_mode": "tp_sl_tsl_or_signal",
    "pause_on_issue": True,
}

DEFAULT_OPTION_STRATEGIES = [
    {
        "name": "NIFTY Intraday Theta Straddle",
        "description": "Automated delta-neutral intraday straddle writing. Sells ATM NIFTY Call & Put options at 9:20 AM to capture maximum time decay (Theta) and auto-closes at 3:15 PM.",
        "underlying": "NIFTY", "strike_mode": "ATM_SELL", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Selling", "required_capital": 150000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 20: return []
    signals = []
    for i in range(len(data)):
        dt = data[i]['date']
        try:
            time_part = dt.split(" ")[1][:5]
        except Exception:
            continue
        if time_part == "09:20":
            signals.append({'date': dt, 'action': 'SELL', 'reason': 'Straddle Entry'})
        elif time_part == "15:15":
            signals.append({'date': dt, 'action': 'BUY', 'reason': 'EOD Exit'})
    return signals
""",
        "market_suitability": "Sideways Range-bound (Theta Harvesting)",
    },
    {
        "name": "BANKNIFTY Weekly Income Strangle",
        "description": "Shorts BANKNIFTY Call & Put options 100 points out-of-the-money (OTM) at 9:20 AM. High probability weekly premium harvesting.",
        "underlying": "BANKNIFTY", "strike_mode": "ATM_SELL", "otm_points": 100, "lots": 1,
        "strategy_type": "Option Selling", "required_capital": 150000.0, "instrument_group": "NFO",
        "python_code": """def run(data):
    if len(data) < 20: return []
    signals = []
    for i in range(len(data)):
        dt = data[i]['date']
        try:
            time_part = dt.split(" ")[1][:5]
        except Exception:
            continue
        if time_part == "09:20":
            signals.append({'date': dt, 'action': 'SELL', 'reason': 'Strangle Entry'})
        elif time_part == "15:15":
            signals.append({'date': dt, 'action': 'BUY', 'reason': 'EOD Exit'})
    return signals
""",
        "market_suitability": "Sideways Range-bound (Theta Harvesting)",
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
        width = (upper - lower) / sma
        
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
            pnl = (closes[i] - entry_price) / entry_price * 100
            if closes[i] < channel_low or pnl <= -0.4 or pnl >= 1.0:
                signals.append({'date': data[i]['date'], 'action': 'SELL', 'reason': 'Crude Vol Long Exit'})
                position = "NONE"
        elif position == "SHORT":
            pnl = (entry_price - closes[i]) / entry_price * 100
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

STANDARD_STRATEGY_CATALOG = []

_seed_templates_by_name = {
    template["name"]: template
    for template in [*LEGACY_OPTION_STRATEGIES, *DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]
}
DEFAULT_OPTION_STRATEGIES = list(_seed_templates_by_name.values())

LEGACY_DEFAULT_STRATEGY_NAMES = {
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
    instrument_group = str(template.get("instrument_group") or ("BFO" if underlying == "SENSEX" else "MCX" if underlying in {"CRUDEOIL", "NATURALGAS"} else "NFO")).upper()
    strategy_type = template.get("strategy_type") or ("Option Selling" if str(template.get("strike_mode") or "").upper().endswith("SELL") else "Option Buying")
    required_capital = float(template.get("required_capital") or (45000.0 if underlying == "SENSEX" else 65000.0 if underlying == "CRUDEOIL" else 55000.0 if underlying == "NATURALGAS" else 35000.0))
    is_commodity = instrument_group == "MCX" or underlying in {"CRUDEOIL", "NATURALGAS"}
    options_block = {
        "enabled": not is_commodity,
        "underlying": underlying,
        "strike_mode": template["strike_mode"],
        "otm_points": template["otm_points"],
        "expiry_offset": template.get("expiry_offset", 0),
        "lots": template["lots"],
        "required_capital": required_capital,
    }
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
        "broker": "upstox" if ("Upstox" in template["name"] or "HFT" in template["name"]) else "zerodha",
        "mode": "paper",
        "market_suitability": template.get("market_suitability", "Any Market Condition"),
        "visual_config": {
            "symbol": underlying,
            "exchange": "MCX" if is_commodity else instrument_group,
            "options": options_block,
            "commodity_options": options_block if is_commodity else None,
            "risk": {**dict(DEFAULT_STRATEGY_RISK), "required_capital": required_capital},
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
    if symbol in {"CRUDEOIL", "NATURALGAS"}:
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
    if underlying in {"CRUDEOIL", "NATURALGAS"}:
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
        "CRUDEOIL": 65000.0,
        "NATURALGAS": 55000.0,
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
    try:
        # Dynamically delete any legacy strategy templates for this user first!
        delete_res = await db.strategies.delete_many({
            "user_id": user_id,
            "name": {"$in": list(LEGACY_DEFAULT_STRATEGY_NAMES)}
        })
        if delete_res.deleted_count > 0:
            logger.info(f"Wiped {delete_res.deleted_count} legacy strategies for user {user_id}")
    except Exception as e:
        logger.warning(f"Error wiping legacy strategies for user {user_id}: {e}")

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


async def _strategy_source_id(source: Optional[str]) -> Optional[str]:
    if not source:
        return None
    m = re.search(r"strategy:([0-9a-fA-F\-]+)", source)
    return m.group(1) if m else None


def _ledger_pct(value: Any, default: float) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return default
    return pct / 100.0 if pct > 1 else pct


def _sync_option_ledger_strategy(row: Dict[str, Any]) -> None:
    visual_config = row.get("visual_config") or {}
    risk = visual_config.get("risk") or {}
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
    risk = ((row or {}).get("visual_config") or {}).get("risk") or {}
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

    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "strategy_id": strategy_id,
        "instrument_key": instrument_key,
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
    risk_patch: Dict[str, Any] = dict(reservation.get("tp_sl_tsl_config") or {})
    if stop_loss is not None:
        risk_patch["stoploss_price"] = float(stop_loss)
        risk_patch["stop_loss"] = float(stop_loss)
    if take_profit is not None:
        risk_patch["target_price"] = float(take_profit)
        risk_patch["take_profit"] = float(take_profit)
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
        "status": {"$in": ["OPEN", "FILLED"]},
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
        if pos.get("asset_type") == "option" or str(pos.get("exchange") or "").upper() in {"NFO", "BFO"}:
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
            results.append({"symbol": sym, "qty": qty_net, "side": "SELL", "status": "ok", "order_id": result.get("id")})
        except Exception as e:
            results.append({"symbol": sym, "qty": qty_net, "side": "SELL", "status": "failed", "error": str(e)})
    if reason in ("risk-trigger", "feed-stale"):
        await db.strategies.update_one({"id": sid, "user_id": user_id}, {"$set": {
            "status": "paused",
            "last_error": f"Auto-paused after {reason} due to risk or data issue.",
        }})
    return {"closed_positions": results, "open_positions_found": len(positions)}


async def _current_ltp_for_symbol(user_id: str, symbol: str, exchange: str, allow_mock: bool = True, execution_broker: Optional[str] = None) -> Optional[float]:
    settings = await get_user_settings(user_id)
    data_broker = execution_broker or settings.get("data_broker", "zerodha")
    if data_broker == "kotak_neo":
        gateway = _KOTAK_GATEWAYS.get(user_id)
        if gateway:
            tick = gateway.latest_tick_by_symbol(symbol.upper())
            if tick and tick.get("ltp"):
                return float(tick["ltp"])
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
    kite, _ = await get_user_kite(user_id)
    if kite:
        try:
            key = f"{exchange}:{symbol}"
            ltp_resp = kite.ltp([key])
            if ltp_resp and key in ltp_resp:
                return float(ltp_resp[key]["last_price"])
        except Exception:
            pass
    if data_broker != "kotak_neo" and settings.get("fallback_broker") == "kotak_neo":
        gateway = _KOTAK_GATEWAYS.get(user_id)
        if gateway:
            tick = gateway.latest_tick_by_symbol(symbol.upper())
            if tick and tick.get("ltp"):
                return float(tick["ltp"])
    if data_broker != "upstox" and settings.get("fallback_broker") == "upstox":
        gateway = await get_user_upstox_gateway(user_id)
        token = _upstox_instrument_token(exchange, symbol)
        if gateway and gateway.connected and token:
            try:
                quote = await asyncio.to_thread(gateway.get_market_quote, [token])
                ltp = UpstoxGateway.parse_quote_ltp(quote, token)
                if ltp is not None:
                    return ltp
            except Exception:
                pass
    if not allow_mock:
        return None
    sym = next((s for s in SYMBOLS if s["symbol"] == symbol.upper()), None)
    return live_price(sym["base"], SYMBOLS.index(sym))["price"] if sym else None


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


def _upstox_instrument_token(exchange: str, trading_symbol: str, instrument_token: Any = None) -> Optional[str]:
    token = str(instrument_token or "").strip()
    if "|" in token:
        return token
    symbol = str(trading_symbol or "").upper().strip()
    if "|" in symbol:
        return symbol
    exch = (exchange or "NSE").upper()
    if exch in {"NSE", "BSE"}:
        return UPSTOX_EQUITY_INSTRUMENTS.get(symbol)
    if exch in {"NFO", "BFO", "NSE_FO"}:
        return f"NSE_FO|{symbol}"
    if exch == "MCX":
        if len(symbol) > 10 and symbol.endswith("FUT"):
            return f"MCX_FO|{symbol}"
        active_sym = _mcx_active_future_symbol(symbol)
        return f"MCX_FO|{active_sym}"
    if "|" in symbol and "_" in symbol.split("|", 1)[0]:
        return symbol
    return None



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
            if not OrderExecutionRetry.is_retryable_error(last_error) or attempt >= max_attempts:
                raise HTTPException(status_code=400, detail=f"Upstox rejected order: {last_error}")
            retry_cfg = OrderExecutionRetry.retry_config(attempt)
            await asyncio.sleep(min(3, float(retry_cfg.get("backoff_seconds") or 1)))
    broker_order_id = extract_upstox_order_id(result)
    if not broker_order_id:
        logger.warning("Upstox order response did not include order id: %s", result)
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
                    tick_manager = getattr(app.state, "tick_manager", None)
                    tick_status = tick_manager.status_info(uid) if tick_manager else {"connected": False, "last_tick_at": None}
                    last_tick = tick_status.get("last_tick_at")
                    stale = True
                    if last_tick:
                        try:
                            last_dt = datetime.fromisoformat(last_tick)
                            stale = (datetime.now(timezone.utc) - last_dt).total_seconds() > 120
                        except Exception:
                            stale = True
                    if not tick_status.get("connected") or stale:
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
    return {
        "ok": True,
        "inserted": inserted,
        "message": "Standardized index and MCX option presets installed. Review and backtest before enabling LIVE.",
    }


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
    await db.strategies.update_one({"id": sid}, {"$set": {"status": new_status}})
    if new_status == "live":
        _sync_option_ledger_strategy(row)
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
    }
    for key, value in mapping.items():
        if value is not None:
            risk[key] = value
    risk["max_lot"] = 1
    visual_config["risk"] = risk
    
    update_fields = {"visual_config": visual_config}
    if req.broker is not None:
        update_fields["broker"] = req.broker.strip().lower()
        row["broker"] = req.broker.strip().lower()
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
    # Options mode: resolve contract & place option order
    if opt_cfg.get("enabled"):
        kite, _ = await get_user_kite(user["id"])
        if not kite:
            raise HTTPException(status_code=400, detail="Options trading requires a connected Zerodha session.")
        contract = options_helper.resolve_for_signal(
            kite,
            underlying=opt_cfg.get("underlying", "NIFTY"),
            signal_action=action,
            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
            otm_points=int(opt_cfg.get("otm_points") or 0),
            expiry_offset_weeks=int(opt_cfg.get("expiry_offset") or 0),
        )
        if not contract:
            raise HTTPException(status_code=400, detail="Could not resolve option contract. Markets may be closed or instruments unavailable.")
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

    # Fetch candles using the same path as the background runner.
    history = await _fetch_strategy_history(user["id"], symbol, days=60, interval="5minute")
    data: List[dict] = history["data"]
    source_label = history["source"]
    kite, _ = await get_user_kite(user["id"])

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
                settings = await get_user_settings(user["id"])
                if not history.get("is_live", False) and not settings.get("paper_mode", True):
                    raise HTTPException(
                        status_code=400,
                        detail="Live execution blocked: candle source is mock. Connect Kite or switch to paper mode.",
                    )
                try:
                    if options_mode:
                        if not kite:
                            raise HTTPException(status_code=400, detail="Options test-run requires a connected Zerodha session.")
                        option_contract_used = options_helper.resolve_for_signal(
                            kite,
                            underlying=symbol,
                            signal_action=action,
                            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
                            otm_points=int(opt_cfg.get("otm_points") or 0),
                            expiry_offset_weeks=int(opt_cfg.get("expiry_offset") or 0),
                        )
                        if not option_contract_used:
                            raise HTTPException(status_code=400, detail="Could not resolve option contract - markets may be closed or instruments unavailable.")
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
        validation["is_valid"] = bool(validation.get("is_valid")) and float(validation.get("confidence", 0)) >= threshold
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
    if not data:
        raise HTTPException(status_code=400, detail=f"No price data for {target_symbol}")
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
async def _check_daily_loss_guard(user_id: str, max_loss: float) -> None:
    """Refuse new orders if today's realised loss already exceeds max_daily_loss.
    Computed from db.orders (paper mode) — tracks realised_pnl on closing trades."""
    if not max_loss or max_loss <= 0:
        return
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    orders = await db.orders.find({
        "user_id": user_id,
        "created_at": {"$gte": today_start},
        "status": {"$in": [ORDER_FILLED, ORDER_CLOSED, "COMPLETE"]},
    }, {"_id": 0}).to_list(500)
    realised = sum(float(o.get("realised_pnl") or 0) for o in orders)
    if realised <= -abs(max_loss):
        raise HTTPException(
            status_code=400,
            detail=f"Daily loss guard tripped: today's realised loss ₹{abs(realised):.0f} "
                   f"≥ max ₹{max_loss:.0f}. New orders blocked until tomorrow.",
        )


async def _check_trade_count_guard(user_id: str, max_trades: int) -> None:
    if not max_trades or max_trades <= 0:
        return
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    trades = await db.orders.count_documents({
        "user_id": user_id,
        "created_at": {"$gte": today_start},
        "status": {"$nin": [ORDER_REJECTED, ORDER_CANCELLED, "REJECTED", "CANCELLED", "STALE", "BROKER_NOT_FOUND"]},
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


def _new_execution_tag(strategy_id: Optional[str] = None) -> str:
    strategy_part = re.sub(r"[^A-Za-z0-9]", "", strategy_id or "MANUAL")[:8] or "MANUAL"
    trade_part = uuid.uuid4().hex[:8]
    return f"QG_{strategy_part}_{trade_part}".upper()


def _open_order_statuses() -> set:
    return set(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)


def _closed_order_statuses() -> set:
    return set(ORDER_TERMINAL_STATUSES | LEGACY_TERMINAL_STATUSES)


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
    now_utc = now_utc or datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    minutes = ist_now.hour * 60 + ist_now.minute
    return ist_now.weekday() < 5 and NSE_OPEN_MINUTE <= minutes <= NSE_CLOSE_MINUTE


def _is_order_market_open(exchange: str, now_utc: Optional[datetime] = None) -> bool:
    exchange = (exchange or "NSE").upper()
    if exchange == "MCX":
        now_utc = now_utc or datetime.now(timezone.utc)
        ist_now = now_utc + IST_OFFSET
        minutes = ist_now.hour * 60 + ist_now.minute
        return ist_now.weekday() < 5 and MCX_OPEN_MINUTE <= minutes <= MCX_CLOSE_MINUTE
    return _is_nse_market_open(now_utc)


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
    mapping = {
        "zerodha": "zerodha",
        "kotak_neo": "kotak_neo",
        "kotak": "kotak_neo",
        "upstox": "upstox",
    }
    return mapping.get((broker_raw or "").lower(), broker_raw or "zerodha")


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
    execution_broker = settings.get("execution_broker", "zerodha")
    exchange = (exchange or "NSE").upper()
    symbol_upper = symbol.upper().strip()

    # ----- option contract path -----
    if option_contract and option_contract.get("tradingsymbol"):
        tsym = str(option_contract["tradingsymbol"]).upper().strip()
        seg = "OPTIONS"
        opt_exchange = (option_contract.get("exchange") or exchange or "NFO").upper()
        token = str(option_contract.get("instrument_token") or tsym)
        asset_class = "OPTION_SHORT" if side == "SELL" else "OPTION_LONG"

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

        # Determine intent
        strategy_id = await _strategy_source_id(source)
        intent_str = _infer_intent(side, strategy_id, user_id, instr, asset_class)

        intent = OrderIntent(
            instrument=instr,
            quantity=final_qty,
            intent=intent_str,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return {"intent": intent, "lot_size": lot_size, "lots": lots}

    # ----- equity / futures / commodity path -----
    segment = "EQUITY"
    asset_class = "DIRECT"
    if exchange in ("NFO", "BFO"):
        segment = "FUTURES"
    elif exchange == "MCX":
        segment = "COMMODITY"
    elif exchange in ("CDS",):
        segment = "FUTURES"

    token = symbol_upper
    if execution_broker == "upstox":
        resolved = _upstox_instrument_token(exchange, symbol_upper)
        if resolved:
            token = resolved

    final_qty = int(qty or settings.get("default_qty", 1))

    instr = InstrumentRef(
        broker=execution_broker,
        segment=segment,
        exchange=exchange,
        tradingsymbol=symbol_upper,
        instrument_token=token,
        asset_class=asset_class,
    )

    strategy_id = await _strategy_source_id(source)
    intent_str = _infer_intent(side, strategy_id, user_id, instr, asset_class)

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
    execution_broker: str = "zerodha",
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
    """Route a live order to the broker adapter identified by
    ``intent.instrument.broker`` and return the broker response dict."""
    instr = intent.instrument
    broker = (instr.broker or "zerodha").lower()
    side = _intent_side(intent.intent)
    qty = int(intent.quantity)

    if broker in ("kotak_neo", "kotak"):
        return await _place_kotak_order(
            user_id,
            trading_symbol=instr.tradingsymbol,
            exchange=instr.exchange,
            side=side,
            quantity=qty,
            order_type=order_type,
            product=product,
            price=price,
            tag=tag,
        )

    if broker == "upstox":
        upstox_token = instr.instrument_token
        if not upstox_token or "|" not in upstox_token:
            resolved = _upstox_instrument_token(instr.exchange, instr.tradingsymbol, instr.instrument_token)
            if resolved:
                upstox_token = resolved
        if not upstox_token:
            raise RuntimeError(f"Cannot resolve Upstox instrument token for {instr.exchange}:{instr.tradingsymbol}")
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

    # Default: Zerodha / Kite
    kite, _ = await get_user_kite(user_id)
    if not kite:
        raise RuntimeError("Zerodha Kite is not connected. Please complete the login flow first.")
    return await _place_kite_order_with_recovery(
        kite,
        tradingsymbol=instr.tradingsymbol,
        exchange=instr.exchange,
        transaction_type=side,
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
    return out


def _scoped_idempotency_key(user_id: str, raw_key: Optional[str]) -> str:
    raw = str(raw_key or "").strip()
    if not raw:
        raw = f"auto:{uuid.uuid4().hex}"
    digest = hashlib.sha256(f"{user_id}:{raw}".encode("utf-8")).hexdigest()
    return f"idem:{digest}"


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
    except Exception as exc:
        logger.warning("order event append failed order=%s event=%s: %s", order_id, event_type, exc)


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


async def _mark_order_rejected(order_id: str, user_id: str, message: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    row = await db.orders.find_one_and_update(
        {"id": order_id, "user_id": user_id},
        {"$set": {
            "status": ORDER_REJECTED,
            "legacy_status": "REJECTED",
            "execution_status": ORDER_REJECTED,
            "status_message": message,
            "error_message": message,
            "updated_at": now,
        }, "$unset": {"placement_owner": "", "placement_lock_until": ""}},
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    await _append_order_event(order_id, user_id, "ORDER_REJECTED", {"message": message})
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


async def _apply_paper_fill_to_position(order_doc: Dict[str, Any], fill_price: float) -> Dict[str, Any]:
    """Apply a paper fill exactly once and write immutable fill/trade records."""
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
    brokerage = float(locked.get("brokerage") or _simulate_paper_brokerage(fill_price, qty))
    expected = float(locked.get("expected_price") or locked.get("requested_price") or fill_price or 0)
    slippage = round(abs(float(fill_price or 0) - expected) * qty, 2) if expected > 0 else 0.0

    pos = await db.positions.find_one({"user_id": user_id, "symbol": symbol})
    before_qty = int((pos or {}).get("qty") or 0)
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

    net_realised = round(gross_realised - brokerage, 2) if qty_closed else 0.0

    if after_qty == 0:
        await db.positions.delete_one({"user_id": user_id, "symbol": symbol})
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
        "slippage": slippage,
        "gross_realised_pnl": gross_realised,
        "realised_pnl": net_realised,
        "position_before_qty": before_qty,
        "position_after_qty": after_qty,
        "avg_price_before": before_avg,
        "avg_price_after": round(after_avg, 2),
        "filled_at": now,
        "mode": "paper",
    }
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
                "realised_pnl": net_realised,
                "brokerage": brokerage,
                "slippage": slippage,
                "closed_at": now,
                "mode": "paper",
            })
        except DuplicateKeyError:
            pass

    updated = await db.orders.find_one_and_update(
        {"id": order_id, "user_id": user_id},
        {"$set": {
            "status": ORDER_FILLED,
            "legacy_status": "COMPLETE",
            "execution_status": ORDER_FILLED,
            "filled_qty": qty,
            "pending_qty": 0,
            "price": float(fill_price or 0),
            "gross_realised_pnl": gross_realised,
            "realised_pnl": net_realised,
            "brokerage": brokerage,
            "slippage": slippage,
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


async def _recover_pending_paper_fills(limit: int = 500) -> int:
    """Finish paper fills that were persisted before a restart/crash."""
    rows = await db.orders.find(
        {
            "mode": "paper",
            "paper_fill_applied": {"$ne": True},
            "status": {"$in": [ORDER_NEW, ORDER_FILLED]},
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
                            idempotency_key: Optional[str] = None) -> dict:
    side = (side or "").upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    order_type = (order_type or "MARKET").upper()
    if order_type not in ("MARKET", "LIMIT"):
        raise HTTPException(status_code=400, detail="order_type must be MARKET or LIMIT")
    if order_type == "LIMIT" and price is None:
        raise HTTPException(status_code=400, detail="LIMIT orders require a price")

    settings = dict(await get_user_settings(user_id))
    strategy_id = await _strategy_source_id(source)
    if strategy_id:
        strat = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if strat:
            if strat.get("mode") in ("paper", "live"):
                settings["paper_mode"] = strat.get("mode") == "paper"
            if strat.get("broker"):
                strat_broker = strat.get("broker")
                if settings.get("execution_broker") == "upstox" and strat_broker == "zerodha":
                    settings["execution_broker"] = "upstox"
                else:
                    settings["execution_broker"] = strat_broker

    paper = bool(settings.get("paper_mode", True))
    execution_broker = settings.get("execution_broker", "zerodha")
    if not paper and execution_broker not in {"zerodha", "kotak_neo", "kotak", "upstox"}:
        raise HTTPException(status_code=400, detail=f"Live execution through {execution_broker} is not enabled yet.")

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
    intent: OrderIntent = resolution["intent"]
    instr = intent.instrument
    if instr.exchange not in SUPPORTED_ORDER_EXCHANGES:
        raise HTTPException(status_code=400, detail=f"exchange must be one of {sorted(SUPPORTED_ORDER_EXCHANGES)}")
    if not paper and order_type == "MARKET" and not _is_order_market_open(instr.exchange):
        market_name = "MCX" if instr.exchange == "MCX" else "NSE/BSE"
        raise HTTPException(status_code=400, detail=f"Live MARKET orders are blocked outside {market_name} market hours.")

    strategy_id = await _strategy_source_id(source)
    fill_price_hint = await _resolve_order_fill_hint(user_id, intent, price, paper, option_contract, execution_broker=execution_broker)
    if _intent_is_entry(intent.intent):
        await _check_daily_loss_guard(user_id, settings.get("max_daily_loss", 0))
        await _check_trade_count_guard(user_id, int(settings.get("max_trades_per_day") or 0))
        if intent.intent in ("OPEN_LONG", "OPEN_SHORT") and fill_price_hint <= 0 and not paper:
            raise HTTPException(status_code=400, detail=f"Live LTP unavailable for {instr.tradingsymbol}; order blocked.")
        if fill_price_hint > 0 and intent.quantity * fill_price_hint > settings["max_position_size"] and intent.intent in ("OPEN_LONG", "OPEN_SHORT"):
            raise HTTPException(
                status_code=400,
                detail=f"Order value INR {intent.quantity * fill_price_hint:.0f} exceeds max position size INR {settings['max_position_size']:.0f}. Adjust on Profile.",
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
    position_reservation = None
    exit_position_record = None
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
        if not paper:
            kite, _ = await get_user_kite(user_id) if execution_broker == "zerodha" else (None, None)
            await _assert_broker_has_position_quantity(user_id, kite, instr.exchange, instr.tradingsymbol, int(intent.quantity))

    resolved_product = (product or ("NRML" if instr.exchange in {"NFO", "BFO", "MCX", "CDS"} else settings.get("default_product", "MIS"))).upper()
    execution_tag = _new_execution_tag(strategy_id)
    broker_order_id = None
    fill_price = price if price is not None else fill_price_hint
    if paper:
        fill_price = price if order_type == "LIMIT" and price is not None else _simulate_paper_fill_price(fill_price_hint or 1.0, _intent_side(intent.intent))

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
        "status_message": "Paper fill pending local accounting" if paper else "Order intent persisted before broker submission",
        "gross_realised_pnl": 0.0,
        "realised_pnl": 0.0,
        "order_type": order_type,
        "requested_price": float(price or 0),
        "expected_price": float(fill_price_hint or price or 0),
        "price": float(fill_price or 0),
        "brokerage": _simulate_paper_brokerage(float(fill_price or 0), int(intent.quantity)) if paper else 0.0,
        "slippage": 0.0,
        "product": resolved_product,
        "status": ORDER_NEW,
        "legacy_status": "PENDING_LOCAL" if paper else "PENDING_BROKER",
        "mode": "paper" if paper else "live",
        "broker": "paper" if paper else _runtime_broker_name(instr.broker),
        "execution_tag": execution_tag if not paper else None,
        "execution_attempts": 0,
        "execution_recovered": False,
        "source": source,
        "strategy_id": strategy_id,
        "created_at": now,
        "updated_at": now,
        "exchange": instr.exchange,
        "asset_type": "option" if instr.segment == "OPTIONS" else ("commodity" if instr.segment == "COMMODITY" else "equity"),
        "order_intent": intent.model_dump(),
        "instrument": instr.model_dump(),
        "segment": instr.segment,
        "stop_loss": intent.stop_loss,
        "take_profit": intent.take_profit,
        "execution_status": ORDER_NEW,
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
        })
    order_doc, inserted = await _insert_order_intent(order_doc)
    execution_tag = order_doc.get("execution_tag") or execution_tag
    if not inserted:
        if order_doc.get("mode") == "paper" and not order_doc.get("paper_fill_applied"):
            order_doc = await _apply_paper_fill_to_position(order_doc, float(order_doc.get("price") or fill_price or 0))
        await _cancel_strategy_reservation(position_reservation, "duplicate-idempotency-key")
        return _clean_order_response(order_doc)

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
            if strategy_id:
                option_ledger.release_failed_open(strategy_id)
            order_doc = await _mark_order_rejected(order_doc["id"], user_id, str(exc))
            raise HTTPException(
                status_code=400,
                detail=f"Broker rejected order: {exc}",
                headers={"X-Order-Id": order_doc.get("id", "")},
            )

    if strategy_id and _intent_is_entry(intent.intent):
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
                    "asset_type": order_doc["asset_type"],
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
        if paper and strategy_id:
            ledger_side = "SHORT" if instr.asset_class == "OPTION_SHORT" or intent.intent == "OPEN_SHORT" else "LONG"
            decision = option_ledger.try_open_position(
                strategy_id=strategy_id,
                symbol=instr.tradingsymbol,
                option_type=order_doc.get("option_type") or ("EQ" if instr.segment == "EQUITY" else instr.segment),
                entry_price=float(fill_price or 0),
                quantity=max(1, int(intent.quantity)),
                position_side=ledger_side,
                broker_confirmed=True,
            )
            if not decision.accepted:
                await _cancel_strategy_reservation(position_reservation, decision.reason)
                order_doc = await _mark_order_rejected(order_doc["id"], user_id, f"Strategy entry blocked: {decision.reason}")
                raise HTTPException(status_code=409, detail=f"Strategy entry blocked: {decision.reason}")

    if strategy_id and _intent_is_exit(intent.intent):
        await _mark_strategy_position_exiting(exit_position_record, exit_order_id=order_doc["id"], exit_broker_order_id=broker_order_id)
        if paper:
            await _close_strategy_position_record(exit_position_record, exit_price=float(fill_price or 0), reason=source)
            option_ledger.close_position(strategy_id=strategy_id, exit_price=float(fill_price or 0), exit_reason=source)

    if paper:
        order_doc = await _apply_paper_fill_to_position(order_doc, float(fill_price or 0))

    return _clean_order_response(order_doc)


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
    return await _place_order_core(
        user_id=user["id"], symbol=symbol, side=side, qty=qty,
        order_type="MARKET", product=target.get("product"), source="manual-exit",
        exchange=target.get("exchange") or "NSE",
    )


@api.post("/ops/squareoff-all")
async def squareoff_all_positions(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    positions = await list_positions(user)
    if not positions:
        return {"ok": True, "closed": [], "failed": []}

    closed, failed = [], []
    now = datetime.now(timezone.utc).isoformat()
    kite, kite_status = await get_user_kite(user["id"])
    paper = settings.get("paper_mode", True)
    execution_broker = settings.get("execution_broker", "zerodha")
    if not paper and execution_broker == "zerodha" and not kite:
        raise HTTPException(status_code=400, detail=kite_status.get("reason", "broker_not_connected"))

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
                if any(s["symbol"] == str(symbol).upper() for s in SYMBOLS):
                    res = await _place_order_core(
                        user_id=user["id"],
                        symbol=str(symbol).upper(),
                        side=side,
                        qty=abs_qty,
                        order_type="MARKET",
                        product=p.get("product") or settings.get("default_product", "MIS"),
                        source="squareoff",
                    )
                    closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "live", "order_id": res.get("broker_order_id")})
                elif execution_broker == "kotak_neo":
                    res = await _place_kotak_order(
                        user["id"],
                        trading_symbol=symbol,
                        exchange=p.get("exchange") or "NSE",
                        side=side,
                        quantity=abs_qty,
                        order_type="MARKET",
                        product=p.get("product") or settings.get("default_product", "MIS"),
                        tag=_new_execution_tag(),
                    )
                    closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "live", "order_id": res.get("broker_order_id")})
                else:
                    res = await _place_kite_order_with_recovery(
                        kite,
                        tradingsymbol=symbol,
                        exchange=p.get("exchange") or "NSE",
                        transaction_type=side,
                        quantity=abs_qty,
                        order_type="MARKET",
                        product=p.get("product") or settings.get("default_product", "MIS"),
                        tag=_new_execution_tag(),
                    )
                    if not res.get("ok"):
                        raise RuntimeError(res.get("error") or "Square-off order failed")
                    closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "live", "order_id": res.get("order_id")})
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
    status_message: Optional[str] = None,
) -> Dict[str, int]:
    if not broker_order_id or not status:
        return {"orders": 0, "positions": 0}
    normalized = str(status).upper()
    canonical = canonical_order_status(normalized, filled_qty=filled_qty)
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
        if canonical == ORDER_FILLED:
            changed_orders += 1
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
                res = await db.strategy_positions.update_many(
                    {
                        "user_id": user_id,
                        "entry_broker_order_id": str(broker_order_id),
                        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]},
                    },
                    {"$set": pos_set},
                )
                changed_positions += res.modified_count
                ledger_side = "SHORT" if instrument.get("asset_class") == "OPTION_SHORT" or intent_name == "OPEN_SHORT" else "LONG"
                decision = option_ledger.confirm_position_fill(
                    strategy_id=strategy_id,
                    entry_price=float(avg_price or order.get("price") or 0),
                    quantity=max(1, int(filled_qty or order.get("filled_qty") or order.get("qty") or 1)),
                    position_side=ledger_side,
                    create_if_missing=True,
                )
                if not decision.accepted and decision.reason not in {"already-filled", "duplicate-buy-dropped"}:
                    logger.warning("ledger fill sync rejected strategy=%s reason=%s", strategy_id, decision.reason)
            if strategy_id and is_exit:
                exit_positions = await db.strategy_positions.find(
                    {"user_id": user_id, "exit_broker_order_id": str(broker_order_id), "status": "EXITING"},
                    {"_id": 0},
                ).to_list(20)
                for pos in exit_positions:
                    exit_price = float(avg_price or order.get("price") or pos.get("average_buy_price") or 0)
                    await _close_strategy_position_record(pos, exit_price=exit_price, reason="broker-exit-complete")
                    option_ledger.close_position(strategy_id=pos.get("strategy_id"), exit_price=exit_price, exit_reason="broker-exit-complete")
                    changed_positions += 1
        elif canonical in {ORDER_CANCELLED, ORDER_REJECTED}:
            changed_orders += 1
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
                option_ledger.release_failed_open(strategy_id)
                await db.strategy_position_locks.delete_many({"user_id": user_id, "strategy_id": strategy_id})
            if strategy_id and is_exit:
                exit_positions = await db.strategy_positions.find(
                    {"user_id": user_id, "exit_broker_order_id": str(broker_order_id), "status": "EXITING"},
                    {"_id": 0, "id": 1, "user_id": 1},
                ).to_list(20)
                for pos in exit_positions:
                    await _reopen_strategy_position_after_exit_reject(pos["id"], user_id, status_message or normalized)
                    changed_positions += 1
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
            status_message=o.get("status_message"),
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
    gateway = _KOTAK_GATEWAYS.get(user_id)
    if not gateway:
        return {"checked": 0, "updated": 0}
    if not _check_and_update_sync_throttle(user_id, "kotak"):
        return {"checked": 0, "updated": 0, "throttled": True}
    updates = gateway.latest_orders()
    report_count = 0
    if gateway.status().get("authenticated"):
        report = await asyncio.to_thread(gateway.order_report)
        if report.get("ok"):
            for item in _kotak_order_items(report.get("response")):
                normalized = KotakNeoGateway.normalize_order_report_item(item)
                order_id = normalized.get("order_id") or _extract_kotak_order_id(item)
                if order_id:
                    updates[str(order_id)] = normalized
            report_count = len(updates)
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for order_id, row in updates.items():
        status = _normalize_kotak_order_status(row.get("status"))
        set_doc = {
            "updated_at": now,
            "status_message": status,
        }
        if status:
            set_doc["status"] = canonical_order_status(status, filled_qty=row.get("filled_qty"), pending_qty=row.get("pending_qty"))
            set_doc["legacy_status"] = status
            set_doc["broker_status"] = status
        if row.get("filled_qty") not in (None, ""):
            try:
                set_doc["filled_qty"] = int(float(row.get("filled_qty")))
            except Exception:
                set_doc["filled_qty"] = row.get("filled_qty")
        if row.get("pending_qty") not in (None, ""):
            try:
                set_doc["pending_qty"] = int(float(row.get("pending_qty")))
            except Exception:
                set_doc["pending_qty"] = row.get("pending_qty")
        if row.get("average_price") not in (None, ""):
            try:
                avg = float(row.get("average_price"))
                if avg > 0:
                    set_doc["price"] = avg
            except Exception:
                pass
        res = await db.orders.update_many(
            {"user_id": user_id, "broker": "kotak_neo", "broker_order_id": str(order_id)},
            {"$set": set_doc},
        )
        updated += res.modified_count
        await _advance_pending_order_from_broker(
            user_id=user_id,
            broker_order_id=str(order_id),
            status=str(status or ""),
            avg_price=float(set_doc.get("price") or 0),
            filled_qty=int(set_doc.get("filled_qty") or 0) if set_doc.get("filled_qty") not in (None, "") else None,
            status_message=str(row.get("status") or status or ""),
        )
        if set_doc.get("status") == ORDER_FILLED:
            avg_price = float(set_doc.get("price") or 0)
            filled_qty = set_doc.get("filled_qty")
            pos_set = {"status": "FILLED", "updated_at": now}
            if avg_price > 0:
                pos_set["average_buy_price"] = avg_price
            if filled_qty not in (None, ""):
                pos_set["quantity"] = int(filled_qty)
                pos_set["open_quantity"] = int(filled_qty)
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": str(order_id), "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": pos_set},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": str(order_id), "status": "EXITING"},
                {"_id": 0},
            ).to_list(20)
            for pos in exit_positions:
                await _close_strategy_position_record(pos, exit_price=avg_price or float(pos.get("average_buy_price") or 0), reason="broker-exit-complete")
        elif set_doc.get("status") in {ORDER_CANCELLED, ORDER_REJECTED}:
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": str(order_id), "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": {"status": set_doc.get("status"), "legacy_status": status, "updated_at": now, "broker_status_message": row.get("status")},
                 "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": str(order_id), "status": "EXITING"},
                {"_id": 0, "id": 1},
            ).to_list(20)
            for pos in exit_positions:
                await _reopen_strategy_position_after_exit_reject(pos["id"], user_id, status)
    result = {"checked": len(updates), "updated": updated, "report_checked": report_count}
    await _record_broker_sync_state(user_id, "kotak_neo", result)
    return result


async def _sync_upstox_order_statuses(user_id: str) -> Dict[str, int]:
    gateway = await get_user_upstox_gateway(user_id)
    if not gateway or not gateway.connected:
        return {"checked": 0, "updated": 0, "reason": "upstox_not_connected"}
    if not _check_and_update_sync_throttle(user_id, "upstox"):
        return {"checked": 0, "updated": 0, "throttled": True}
    try:
        report = await asyncio.to_thread(gateway.get_order_book)
    except Exception as exc:
        logger.warning("Upstox order book fetch failed: %s", exc)
        return {"checked": 0, "updated": 0, "reason": str(exc)}
    items = report.get("orders") or upstox_gateway_utils.order_items(report)
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        order_id = extract_upstox_order_id(item)
        if not order_id:
            continue
        status = _normalize_upstox_order_status(_upstox_first(item, ["status", "order_status"]))
        set_doc = {
            "updated_at": now,
            "status_message": _upstox_first(item, ["status_message", "status_message_raw"]) or status,
        }
        if status:
            set_doc["status"] = canonical_order_status(status)
            set_doc["legacy_status"] = status
            set_doc["broker_status"] = status
        for source_key, target_key in (
            ("filled_quantity", "filled_qty"),
            ("pending_quantity", "pending_qty"),
            ("average_price", "price"),
        ):
            value = item.get(source_key)
            if value not in (None, ""):
                try:
                    set_doc[target_key] = float(value) if target_key == "price" else int(float(value))
                except Exception:
                    set_doc[target_key] = value
        res = await db.orders.update_many(
            {"user_id": user_id, "broker": "upstox", "broker_order_id": str(order_id)},
            {"$set": set_doc},
        )
        updated += res.modified_count
        await _advance_pending_order_from_broker(
            user_id=user_id,
            broker_order_id=str(order_id),
            status=str(status or ""),
            avg_price=float(set_doc.get("price") or 0),
            filled_qty=int(set_doc.get("filled_qty") or 0) if set_doc.get("filled_qty") not in (None, "") else None,
            status_message=str(set_doc.get("status_message") or status or ""),
        )
        if set_doc.get("status") == ORDER_FILLED:
            avg_price = float(set_doc.get("price") or 0)
            filled_qty = set_doc.get("filled_qty")
            pos_set = {"status": "FILLED", "updated_at": now}
            if avg_price > 0:
                pos_set["average_buy_price"] = avg_price
            if filled_qty not in (None, ""):
                pos_set["quantity"] = int(filled_qty)
                pos_set["open_quantity"] = int(filled_qty)
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": str(order_id), "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": pos_set},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": str(order_id), "status": "EXITING"},
                {"_id": 0},
            ).to_list(20)
            for pos in exit_positions:
                await _close_strategy_position_record(pos, exit_price=avg_price or float(pos.get("average_buy_price") or 0), reason="broker-exit-complete")
        elif set_doc.get("status") in {ORDER_CANCELLED, ORDER_REJECTED}:
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": str(order_id), "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": {"status": set_doc.get("status"), "legacy_status": status, "updated_at": now, "broker_status_message": set_doc.get("status_message")},
                 "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": str(order_id), "status": "EXITING"},
                {"_id": 0, "id": 1},
            ).to_list(20)
            for pos in exit_positions:
                await _reopen_strategy_position_after_exit_reject(pos["id"], user_id, status)
    result = {"checked": len(items), "updated": updated}
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
        "status": {"$in": ["OPEN", "FILLED", "EXITING"]},
    }, {"_id": 0}).to_list(500)
    marked = 0
    now = datetime.now(timezone.utc).isoformat()
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
    execution_broker = settings.get("execution_broker") or "zerodha"
    kite, _ = await get_user_kite(user_id)

    if not paper and execution_broker == "kotak_neo":
        gateway = _KOTAK_GATEWAYS.get(user_id)
        if gateway and gateway.status().get("authenticated"):
            result = await asyncio.to_thread(gateway.positions)
            if result.get("ok"):
                out = []
                response = result.get("response") or {}
                rows = response.get("net") if isinstance(response, dict) else _kotak_position_items(response)
                for p in rows or []:
                    if int(p.get("quantity") or 0) == 0:
                        continue
                    out.append(_api_position_row(p, broker="kotak_neo"))
                if out:
                    return out
    if not paper and execution_broker == "upstox":
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
    if kite and not paper:
        data = kite_helper.safe_positions(kite)
        if data and data.get("net") is not None:
            out = []
            for p in data["net"]:
                if not p.get("quantity"):
                    continue
                out.append(_api_position_row(p, broker="zerodha"))
            try:
                await db.kite_positions_cache.update_one(
                    {"user_id": user_id},
                    {"$set": {"user_id": user_id, "positions": out,
                              "cached_at": datetime.now(timezone.utc).isoformat()}},
                    upsert=True,
                )
            except Exception:
                pass
            return out
        cached = await db.kite_positions_cache.find_one({"user_id": user_id}, {"_id": 0})
        if cached:
            return [{**p, "stale": True, "cached_at": cached.get("cached_at")} for p in cached.get("positions", [])]
    rows = await db.positions.find({"user_id": user_id}, {"_id": 0, "user_id": 0}).to_list(200)
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
    return await execution_state_manager.build_snapshot(user, sync=sync)


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
    positions = await list_positions(user)
    total_pnl = round(sum(p["pnl"] for p in positions), 2)
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
        "pnl_type": "open_unrealized",
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
    day = datetime.now(timezone.utc).date().isoformat()
    orders = await db.orders.find(
        {"user_id": user["id"], "created_at": {"$gte": day}},
        {"_id": 0},
    ).to_list(1000)
    positions = await list_positions(user)
    realised = round(sum(float(o.get("realised_pnl") or 0) for o in orders), 2)
    open_pnl = round(sum(float(p.get("pnl") or 0) for p in positions), 2)
    loss_limit = float(settings.get("max_daily_loss") or 0)
    gross_order_value = round(sum(abs(float(o.get("price") or 0) * int(o.get("qty") or 0)) for o in orders), 2)
    trades_used = len([o for o in orders if canonical_order_status(o.get("status")) not in {ORDER_REJECTED, ORDER_CANCELLED}])
    max_trades = int(settings.get("max_trades_per_day") or 0)
    return {
        "date": day,
        "mode": "PAPER" if settings.get("paper_mode", True) else "LIVE",
        "daily_loss_limit": loss_limit,
        "realised_pnl": realised,
        "open_pnl": open_pnl,
        "total_pnl": round(realised + open_pnl, 2),
        "loss_remaining": round(max(0.0, loss_limit + realised), 2) if loss_limit else None,
        "trades_used": trades_used,
        "max_trades_per_day": max_trades,
        "trades_remaining": max(0, max_trades - trades_used) if max_trades else None,
        "per_strategy_capital": settings.get("per_strategy_capital"),
        "max_position_size": settings.get("max_position_size"),
        "gross_order_value": gross_order_value,
        "market_open": _is_nse_market_open() or _is_order_market_open("MCX"),
    }


@api.get("/trade-journal")
async def trade_journal(user=Depends(get_current_user)):
    rows = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    completed = [r for r in rows if canonical_order_status(r.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
    wins = len([r for r in completed if float(r.get("realised_pnl") or 0) > 0])
    losses = len([r for r in completed if float(r.get("realised_pnl") or 0) < 0])
    total_pnl = round(sum(float(r.get("realised_pnl") or 0) for r in completed), 2)
    return {
        "summary": {
            "orders": len(rows),
            "completed": len(completed),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(1, wins + losses) * 100, 2),
            "realised_pnl": total_pnl,
        },
        "orders": rows,
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
        completed = [o for o in live_orders if canonical_order_status(o.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
        live_pnl = round(sum(float(o.get("realised_pnl") or 0) for o in completed), 2)
        live_wins = len([o for o in completed if float(o.get("realised_pnl") or 0) > 0])
        live_losses = len([o for o in completed if float(o.get("realised_pnl") or 0) < 0])
        live_win_rate = round(live_wins / max(1, live_wins + live_losses) * 100, 2)
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
                "realised_pnl": live_pnl,
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
    """Resolves an index option contract dynamically based on the strategy's broker & mode.
    Bypasses Zerodha dependency for Upstox / Paper trading.
    """
    underlying = underlying.upper()
    settings = await get_user_settings(user_id)
    strategy_mode = strategy_row.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
    is_paper = strategy_mode == "paper"
    execution_broker = strategy_row.get("broker") or settings.get("execution_broker", "zerodha")
    if settings.get("execution_broker") == "upstox" and execution_broker == "zerodha":
        execution_broker = "upstox" 

    # For paper trading, generate simulated/mock options contract to remove Zerodha session requirement
    if is_paper:
        latest_ticks = option_ledger.latest_ticks([underlying])
        tick = latest_ticks.get(underlying)
        spot = float(tick["ltp"]) if tick else (24850.40 if underlying == "NIFTY" else 81460.20)
        
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
        
        lot_size = options_helper.LOT_SIZES.get(underlying, 50)
        
        # Formulate a standard mock tradingsymbol
        expiry_dt = datetime.now() + timedelta(days=(7 - datetime.now().weekday() + 3) % 7)
        expiry_str = expiry_dt.strftime("%y%m%d")
        tradingsymbol = f"{underlying}{expiry_str}{strike}{opt_type}"
        
        return {
            "tradingsymbol": tradingsymbol,
            "exchange": "NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO",
            "instrument_token": 999999 + int(strike),
            "lot_size": lot_size,
            "strike": strike,
            "expiry": expiry_dt.date().isoformat(),
            "underlying": underlying,
            "option_type": opt_type,
            "spot": spot,
            "atm_strike": atm,
            "transaction_type": "BUY" if "BUY" in strike_mode else "SELL",
        }

    # Live resolution
    if execution_broker == "zerodha":
        kite, _ = await get_user_kite(user_id)
        if not kite:
            return None
        return await asyncio.to_thread(
            options_helper.resolve_for_signal,
            kite,
            underlying=underlying,
            signal_action=signal_action,
            strike_mode=strike_mode,
            otm_points=otm_points,
            expiry_offset_weeks=expiry_offset,
        )
        
    elif execution_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user_id)
        if not upstox_gw or not upstox_gw.connected:
            return None
            
        # Fetch spot LTP from Upstox
        upstox_keys = {
            "NIFTY": "NSE_INDEX|Nifty 50",
            "BANKNIFTY": "NSE_INDEX|Nifty Bank",
            "SENSEX": "BSE_INDEX|SENSEX"
        }
        if underlying in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS"}:
            active_sym = _mcx_active_future_symbol(underlying)
            upstox_keys[underlying] = f"MCX_FO|{active_sym}"
            
        spot = None
        if underlying in upstox_keys:
            try:
                res = await asyncio.to_thread(upstox_gw.get_market_quote, [upstox_keys[underlying]])
                node = res.get("data", {}).get(upstox_keys[underlying]) or {}
                spot_ltp = node.get("last_price") or node.get("ltp")
                if spot_ltp:
                    spot = float(spot_ltp)
            except Exception as e:
                logger.warning(f"Failed to fetch Upstox spot price: {e}")
                
        if spot is None:
            if not is_paper:
                logger.warning(f"Live Upstox spot price unavailable for {underlying}; options resolution blocked.")
                return None
            spot = 24850.40 if underlying == "NIFTY" else (81460.20 if underlying == "SENSEX" else (6500.00 if underlying in ("CRUDEOIL", "CRUDEOILM") else 245.00))
            
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
        try:
            spot_key = upstox_keys.get(underlying)
            if spot_key:
                chain = await asyncio.to_thread(upstox_gw.get_option_chain, spot_key, expiry_dt.date().isoformat())
                if chain and chain.get("status") == "success":
                    data = chain.get("data", []) or []
                    for node in data:
                        opt_node = node.get("call_options" if opt_type == "CE" else "put_options") or {}
                        node_strike = float(node.get("strike_price") or opt_node.get("strike_price") or 0)
                        if opt_node and int(node_strike) == int(strike):
                            instrument_token = opt_node.get("instrument_key")
                            tradingsymbol = opt_node.get("trading_symbol")
                            break
        except Exception as e:
            logger.warning(f"Upstox option chain lookup failed: {e}")
            
        if not instrument_token:
            if not is_paper:
                logger.warning(f"Live Upstox option contract strike {strike} not found in chain; guessing blocked.")
                return None
            if underlying in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS"}:
                # MCX monthly option symbol format: CRUDEOILM26JUN6500CE
                ist_now = datetime.now(timezone.utc) + IST_OFFSET
                month_offset = 1 if ist_now.day > 18 else 0
                target_date = ist_now
                if month_offset > 0:
                    target_date = ist_now + timedelta(days=15)
                    if target_date.month == ist_now.month:
                        target_date = ist_now + timedelta(days=32)
                yy = target_date.strftime("%y")
                mmm = target_date.strftime("%b").upper()
                tradingsymbol = f"{underlying.upper()}{yy}{mmm}{strike}{opt_type}"
                instrument_token = f"MCX_FO|{tradingsymbol}"
                expiry_dt = target_date
            else:
                expiry_str = expiry_dt.strftime("%y%m%d")
                tradingsymbol = f"{underlying}{expiry_str}{strike}{opt_type}"
                instrument_token = f"NSE_FO|{tradingsymbol}"
            
        return {
            "tradingsymbol": tradingsymbol or f"{underlying}{expiry_dt.strftime('%y%m%d')}{strike}{opt_type}",
            "exchange": "MCX" if underlying in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS"} else ("NFO" if underlying in ("NIFTY", "BANKNIFTY") else "BFO"),
            "instrument_token": instrument_token,
            "upstox_instrument_token": instrument_token,
            "lot_size": options_helper.LOT_SIZES.get(underlying, 50),
            "strike": strike,
            "expiry": expiry_dt.date().isoformat(),
            "underlying": underlying,
            "option_type": opt_type,
            "spot": spot,
            "atm_strike": atm,
            "transaction_type": "BUY" if "BUY" in strike_mode else "SELL",
        }
    return None


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
        strategies_page_data.append({
            "strategy_id": row["id"],
            "name": row.get("name"),
            "status": row.get("status"),
            "asset_class": row.get("asset_class", "equity"),
            "state": ledger_row.get("state", "IDLE"),
            "cooldown_until": ledger_row.get("cooldown_until"),
            "required_capital": ledger_row.get("required_capital", 0),
            "max_lots": 1,
            "target_pct": round(float(ledger_row.get("target_pct") or 0) * 100, 2),
            "stoploss_pct": round(float(ledger_row.get("stoploss_pct") or 0) * 100, 2),
            "trailing_sl_enabled": ledger_row.get("trailing_sl_enabled"),
            "trail_trigger_pct": round(float(ledger_row.get("trail_trigger_pct") or 0) * 100, 2),
            "trail_step_pct": round(float(ledger_row.get("trail_step_pct") or 0) * 100, 2),
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
                "last_error": row.get("last_error"),
            },
        })
    latest_ticks = option_ledger.latest_ticks(["NIFTY", "SENSEX"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ledger": "sqlite",
        "market_status": {
            "is_open": _is_nse_market_open() or _is_order_market_open("MCX"),
            "nifty": latest_ticks.get("NIFTY"),
            "sensex": latest_ticks.get("SENSEX"),
            "last_tick_time": max([t["tick_time"] for t in latest_ticks.values()], default=None),
            "data_source": next((t["data_source"] for t in latest_ticks.values() if t.get("data_source")), None),
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
    """Return (kite_instance, status_dict). kite is None if not connected/expired."""
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "zerodha"})
    access_token = decrypt_secret(keys.get("access_token")) if keys else None
    api_key = decrypt_secret(keys.get("api_key")) if keys else None
    if not keys or not access_token:
        return None, {"connected": False, "reason": "no_token"}
    if not kite_helper.is_token_valid(keys.get("access_token_expires_at")):
        return None, {"connected": False, "reason": "expired", "kite_user_id": keys.get("kite_user_id")}
    if not api_key:
        return None, {"connected": False, "reason": "credential_decrypt_failed"}
    kite = kite_helper.make_kite(api_key, access_token)
    return kite, {"connected": True, "kite_user_id": keys.get("kite_user_id"),
                  "expires_at": keys["access_token_expires_at"]}


async def get_user_kotak_status(user_id: str) -> Dict[str, Any]:
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "kotak_neo"})
    consumer_key = decrypt_secret(keys.get("api_key")) if keys else None
    consumer_secret = decrypt_secret(keys.get("api_secret")) if keys else None
    status = kotak_helper.status_from_keys(keys, consumer_key)
    gateway = _KOTAK_GATEWAYS.get(user_id)
    if gateway:
        gw_status = gateway.status()
        gateway_error = gw_status.get("last_error")
        status.update({
            "connected": bool(gw_status.get("authenticated")),
            "authenticated": bool(gw_status.get("authenticated")),
            "state": gw_status.get("state"),
            "gateway": gw_status,
            "reason": None if gw_status.get("authenticated") else (gateway_error or status.get("reason")),
        })
    mobile_number = os.environ.get("KOTAK_MOBILE_NUMBER") or (decrypt_secret(keys.get("mobile_number")) if keys else None)
    password = os.environ.get("KOTAK_PASSWORD") or os.environ.get("KOTAK_MPIN") or (decrypt_secret(keys.get("mpin")) if keys else None)
    totp_secret_key = os.environ.get("KOTAK_TOTP_SECRET_KEY") or (decrypt_secret(keys.get("totp_secret_key")) if keys else None)
    required_values = {
        "KOTAK_CONSUMER_SECRET": os.environ.get("KOTAK_CONSUMER_SECRET") or consumer_secret,
        "KOTAK_MOBILE_NUMBER": mobile_number,
        "KOTAK_UCC": os.environ.get("KOTAK_UCC") or (keys or {}).get("user_id_at_broker"),
        "KOTAK_PASSWORD_OR_MPIN": password,
        "KOTAK_TOTP_SECRET_KEY": totp_secret_key,
    }
    missing_env = [k for k, value in required_values.items() if not value]
    status["env_ready"] = not missing_env
    status["missing_env"] = missing_env
    status["consumer_secret_saved"] = bool(consumer_secret or os.environ.get("KOTAK_CONSUMER_SECRET"))
    status["credentials_source"] = {
        "consumer_secret": "env" if os.environ.get("KOTAK_CONSUMER_SECRET") else ("saved" if consumer_secret else "missing"),
        "mobile_number": "env" if os.environ.get("KOTAK_MOBILE_NUMBER") else ("saved" if mobile_number else "missing"),
        "ucc": "env" if os.environ.get("KOTAK_UCC") else ("saved" if (keys or {}).get("user_id_at_broker") else "missing"),
        "password_or_mpin": "env" if (os.environ.get("KOTAK_PASSWORD") or os.environ.get("KOTAK_MPIN")) else ("saved" if password else "missing"),
        "totp_secret_key": "env" if os.environ.get("KOTAK_TOTP_SECRET_KEY") else ("saved" if totp_secret_key else "missing"),
    }
    return status


async def get_user_kotak_gateway(user_id: str, fresh: bool = False) -> Optional[KotakNeoGateway]:
    cached = _KOTAK_GATEWAYS.get(user_id)
    if not fresh and cached:
        cached_status = cached.status()
        if cached_status.get("initialized") or cached_status.get("state") in {"CLIENT_CREATED", "LOGIN_DONE", "TWO_FA_DONE", "READY"}:
            return cached
        logger.info("Dropping uninitialized Kotak gateway from cache for user=%s state=%s", user_id, cached_status.get("state"))
        _KOTAK_GATEWAYS.pop(user_id, None)
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "kotak_neo"})
    consumer_key = decrypt_secret(keys.get("api_key")) if keys else None
    consumer_secret = decrypt_secret(keys.get("api_secret")) if keys else None
    if not consumer_key:
        return None
    config = {
        "consumer_key": consumer_key,
        "consumer_secret": os.environ.get("KOTAK_CONSUMER_SECRET") or consumer_secret,
        "neo_fin_key": os.environ.get("KOTAK_NEO_FIN_KEY"),
        "mobile_number": os.environ.get("KOTAK_MOBILE_NUMBER") or decrypt_secret(keys.get("mobile_number")),
        "username": os.environ.get("KOTAK_USERNAME") or os.environ.get("KOTAK_UCC") or (keys or {}).get("user_id_at_broker"),
        "ucc": os.environ.get("KOTAK_UCC") or (keys or {}).get("user_id_at_broker"),
        "password": os.environ.get("KOTAK_PASSWORD") or os.environ.get("KOTAK_MPIN") or decrypt_secret(keys.get("mpin")),
        "mpin": os.environ.get("KOTAK_MPIN") or os.environ.get("KOTAK_PASSWORD") or decrypt_secret(keys.get("mpin")),
        "totp_secret_key": os.environ.get("KOTAK_TOTP_SECRET_KEY") or decrypt_secret(keys.get("totp_secret_key")),
    }
    gateway = KotakNeoGateway(config=config)
    _KOTAK_GATEWAYS[user_id] = gateway
    return gateway


async def get_user_upstox_status(user_id: str) -> Dict[str, Any]:
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    api_key = os.environ.get("UPSTOX_API_KEY") or (decrypt_secret(keys.get("api_key")) if keys else None)
    api_secret = os.environ.get("UPSTOX_API_SECRET") or (decrypt_secret(keys.get("api_secret")) if keys else None)
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN") or (decrypt_secret(keys.get("access_token")) if keys else None)
    redirect_uri = _upstox_redirect_uri(None, keys)
    status = upstox_helper.status_from_keys(keys, api_key)
    gateway = _UPSTOX_GATEWAYS.get(user_id)
    gateway_status = gateway.status() if gateway else {}
    missing = [
        name
        for name, value in {
            "UPSTOX_API_KEY": api_key,
            "UPSTOX_API_SECRET": api_secret,
            "UPSTOX_REDIRECT_URI": redirect_uri,
        }.items()
        if not value
    ]
    status.update({
        "connected": bool(access_token),
        "authenticated": bool(access_token),
        "keys_saved": bool(keys or api_key),
        "api_secret_saved": bool(api_secret),
        "redirect_uri_ready": bool(redirect_uri),
        "access_token_saved": bool(access_token),
        "env_ready": not missing,
        "missing_env": missing,
        "reason": None if access_token else ("no_token" if api_key else "no_keys"),
        "gateway": gateway_status or None,
        "is_sandbox": bool(keys.get("is_sandbox")) if keys else False,
    })
    return status


async def get_user_upstox_gateway(user_id: str, fresh: bool = False) -> Optional[UpstoxGateway]:
    if not fresh and user_id in _UPSTOX_GATEWAYS:
        return _UPSTOX_GATEWAYS[user_id]
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    api_key = os.environ.get("UPSTOX_API_KEY") or (decrypt_secret(keys.get("api_key")) if keys else None)
    api_secret = os.environ.get("UPSTOX_API_SECRET") or (decrypt_secret(keys.get("api_secret")) if keys else None)
    access_token = os.environ.get("UPSTOX_ACCESS_TOKEN") or (decrypt_secret(keys.get("access_token")) if keys else None)
    redirect_uri = _upstox_redirect_uri(None, keys)
    if not api_key and not access_token:
        return None
    gateway = UpstoxGateway(
        api_key=api_key,
        api_secret=api_secret,
        access_token=access_token,
        redirect_uri=redirect_uri,
        sandbox=bool(keys.get("is_sandbox")) if keys else False,
    )
    _UPSTOX_GATEWAYS[user_id] = gateway
    return gateway


async def get_user_settings(user_id: str) -> dict:
    """Profile / trading preferences with safe defaults."""
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    return {
        "name": (user or {}).get("name", ""),
        "default_qty": (user or {}).get("default_qty", 1),
        "default_product": (user or {}).get("default_product", "MIS"),
        "max_daily_loss": (user or {}).get("max_daily_loss", 10000.0),
        "max_position_size": (user or {}).get("max_position_size", 50000.0),
        "per_strategy_capital": (user or {}).get("per_strategy_capital", 25000.0),
        "max_trades_per_day": (user or {}).get("max_trades_per_day", 20),
        "data_broker": (user or {}).get("data_broker", "zerodha"),
        "execution_broker": (user or {}).get("execution_broker", "zerodha"),
        "fallback_broker": (user or {}).get("fallback_broker", "none"),
        "paper_mode": (user or {}).get("paper_mode", True),
    }


# ============== Routes: Live Readiness ==============
@api.get("/live/readiness")
async def live_readiness(user=Depends(get_current_user)):
    """Pre-flight checks before flipping to LIVE. Returns each check + an overall ready flag."""
    checks = []
    settings = await get_user_settings(user["id"])
    data_broker = settings.get("data_broker", "zerodha")
    execution_broker = settings.get("execution_broker", "zerodha")
    required_brokers = {b for b in (data_broker, execution_broker) if b in {"zerodha", "kotak_neo", "upstox"}}
    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "zerodha"})
    kotak_status = await get_user_kotak_status(user["id"])
    upstox_status = await get_user_upstox_status(user["id"])
    zerodha_required = "zerodha" in required_brokers
    kotak_required = "kotak_neo" in required_brokers
    upstox_required = "upstox" in required_brokers
    required_keys_ok = (
        (not zerodha_required or bool(keys))
        and (not kotak_required or bool(kotak_status.get("keys_saved")))
        and (not upstox_required or bool(upstox_status.get("keys_saved")))
    )
    checks.append({
        "id": "broker_keys",
        "label": "Required broker credentials saved",
        "ok": required_keys_ok,
    })
    kite, status = await get_user_kite(user["id"])
    checks[-1]["hint"] = "Save required broker credentials on Broker Keys" if not required_keys_ok else None
    required_sessions_ok = (
        (not zerodha_required or bool(status.get("connected")))
        and (not kotak_required or bool(kotak_status.get("connected")))
        and (not upstox_required or bool(upstox_status.get("connected")))
    )
    checks.append({
        "id": "kite_session",
        "label": "Active selected broker session",
        "ok": required_sessions_ok,
        "detail": f"data={data_broker}, execution={execution_broker}",
        "hint": "Connect the selected data/execution broker on Broker Keys" if not required_sessions_ok else None,
    })
    funds_ok = False
    funds_msg = None
    if execution_broker == "kotak_neo":
        funds_ok = bool(kotak_status.get("connected"))
        funds_msg = "Kotak connected; live margin check not exposed yet."
    elif execution_broker == "upstox":
        funds_ok = bool(upstox_status.get("connected"))
        funds_msg = "Upstox connected; live margin check not exposed yet."
    elif kite:
        try:
            margins = kite.margins(segment="equity")
            avail = float((margins.get("available", {}) or {}).get("live_balance") or 0)
            funds_ok = avail > 100
            funds_msg = f"₹{avail:.2f} available"
        except Exception as e:
            funds_msg = f"Margins call failed: {e}"
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
        or str(s.get("symbol")).upper() in {"CRUDEOIL", "NATURALGAS"}
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

    tick_manager = getattr(app.state, "tick_manager", None)
    tick_status = tick_manager.status_info(user["id"]) if tick_manager else {"connected": False}
    tick_auth_failed = bool(tick_status.get("auth_failed"))
    kotak_gateway_status = kotak_status.get("gateway") or {}

    if data_broker == "zerodha":
        selected_tick_ok = bool((tick_status.get("connected") or tick_status.get("connecting")) and not tick_auth_failed)
    elif data_broker == "upstox":
        selected_tick_ok = bool(upstox_status.get("connected"))
    else:  # kotak_neo
        selected_tick_ok = bool(kotak_gateway_status.get("authenticated") and kotak_gateway_status.get("ticks", 0) > 0)

    checks.append({
        "id": "tick_feed",
        "label": "Realtime selected tick feed",
        "ok": selected_tick_ok,
        "detail": (
            f"upstox connected" if data_broker == "upstox" and selected_tick_ok else
            "auth failed; reconnect Zerodha" if data_broker == "zerodha" and tick_auth_failed else
            f"connected, last tick {tick_status.get('last_tick_at')}" if data_broker == "zerodha" and tick_status.get("connected") else
            "connecting" if data_broker == "zerodha" and tick_status.get("connecting") else
            f"kotak connected, ticks {kotak_gateway_status.get('ticks', 0)}" if data_broker == "kotak_neo" and selected_tick_ok else
            "not connected"
        ),
        "hint": (
            "Connect Upstox on Broker Keys to start the market feed."
            if data_broker == "upstox" and not selected_tick_ok
            else "Click Connect to Zerodha on Broker Keys to create a fresh Kite session."
            if data_broker == "zerodha" and tick_auth_failed
            else "Fetch a strategy or watchlist with a live Kite session to start the websocket feed."
            if data_broker == "zerodha" and not tick_status.get("connected")
            else "Connect Kotak on Broker Keys and subscribe tokens."
            if data_broker == "kotak_neo" and not selected_tick_ok
            else None
        ),
    })

    market_open = nse_open or mcx_open if has_mcx else nse_open
    paper_mode = bool(settings.get("paper_mode", True))
    
    overall_ready = all(c["ok"] for c in checks if c["id"] not in {"market_hours", "mcx_market_hours", "tick_feed"})
    if not paper_mode and not market_open:
        overall_ready = False
        
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
    data_broker = settings.get("data_broker", "zerodha")
    execution_broker = settings.get("execution_broker", "zerodha")

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
        or str(s.get("symbol")).upper() in {"CRUDEOIL", "NATURALGAS"}
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
    if data_broker == "zerodha" and market_open and not tick_status.get("connected"):
        add("warning", "Realtime Kite ticker is down", tick_status.get("last_error") or "No connected Kite websocket.", "Restart ticker.", "/ops/ticker/restart")
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
        if check.get("id") in {"market_hours", "mcx_market_hours", "tick_feed"}:
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
    gateway = await get_user_kotak_gateway(user_id)
    if not gateway:
        return {"started": False, "reason": "kotak_not_configured"}
    status = gateway.status()
    if not status.get("authenticated"):
        return {"started": False, "reason": "kotak_not_authenticated", "status": status}

    target_symbols = [s.upper() for s in (symbols or [s["symbol"] for s in SYMBOLS])]
    segment = _kotak_exchange_segment(exchange)
    tokens: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    for symbol in target_symbols:
        try:
            result = await asyncio.to_thread(gateway.search_scrip, exchange_segment=segment, symbol=symbol)
            if not result.get("ok"):
                failures.append({"symbol": symbol, "reason": result.get("error", "search_failed")})
                continue
            candidates = _collect_kotak_instruments(result.get("response"))
            exact = next((c for c in candidates if c.get("symbol") == symbol), None)
            chosen = exact or (candidates[0] if candidates else None)
            if chosen:
                chosen["symbol"] = symbol
                chosen["exchange_segment"] = chosen.get("exchange_segment") or segment
                tokens.append(chosen)
            else:
                failures.append({"symbol": symbol, "reason": "no_token_found"})
        except Exception as exc:
            failures.append({"symbol": symbol, "reason": str(exc)})

    if not tokens:
        return {"started": False, "reason": "no_kotak_tokens_resolved", "failures": failures[:8], "status": gateway.status()}
    result = await asyncio.to_thread(gateway.subscribe_symbols, tokens)
    return {
        "started": bool(result.get("ok")),
        "tokens": len(result.get("tokens") or tokens),
        "failures": failures[:8],
        "status": gateway.status(),
        "result": result,
    }


# ============== Routes: Ops Console (extracted to routes/ops.py) ==============
async def ops_diagnostics(user):
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
    tick_status = kotak_tick_status if settings.get("data_broker") == "kotak_neo" else zerodha_tick_status
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
        "upstox": await get_user_upstox_status(user["id"]),
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
    execution_broker = settings.get("execution_broker", "zerodha")
    
    if execution_broker == "upstox":
        upstox_gw = await get_user_upstox_gateway(user["id"])
        if upstox_gw and upstox_gw.connected:
            try:
                margins_payload = await asyncio.to_thread(upstox_gw.get_margins)
                if margins_payload and margins_payload.get("status") == "success":
                    data = margins_payload.get("data", {})
                    equity = data.get("equity", {})
                    avail = round(float(equity.get("available_margin") or 0), 2)
                    used = round(float(equity.get("used_margin") or 0), 2)
                    payin = round(float(equity.get("payin_amount") or 0), 2)
                    opening = round(avail + used - payin, 2)
                    return {
                        "source": "live",
                        "available_cash": avail,
                        "opening_balance": opening,
                        "intraday_payin": payin,
                        "used_margin": used,
                        "m2m_realised": 0.0,
                        "m2m_unrealised": 0.0,
                        "span": round(float(equity.get("span_margin") or 0), 2),
                        "delivery_margin": 0.0,
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
        "note": "Paper-mode estimate. Connect Zerodha and switch to LIVE for real margins.",
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


@api.get("/kotak/status")
async def kotak_status(user=Depends(get_current_user)):
    return await get_user_kotak_status(user["id"])


@api.get("/broker/kotak/status")
async def broker_kotak_status(user=Depends(get_current_user)):
    status = await get_user_kotak_status(user["id"])
    gateway = status.get("gateway") or {}
    return {
        "authenticated": bool(status.get("authenticated") or status.get("connected")),
        "state": status.get("state") or gateway.get("state") or "DISCONNECTED",
        "connected": bool(status.get("connected")),
        "ready": bool(gateway.get("ready")),
        "reason": status.get("reason"),
        "last_error": gateway.get("last_error"),
        "status": status,
    }


@api.get("/kotak/diagnostics")
async def kotak_diagnostics(user=Depends(get_current_user)):
    status = await get_user_kotak_status(user["id"])
    gateway = _KOTAK_GATEWAYS.get(user["id"])
    order_sync = await _sync_kotak_order_statuses(user["id"]) if gateway else {"checked": 0, "updated": 0}
    return {
        "ok": bool(status.get("connected")),
        "status": status,
        "order_sync": order_sync,
        "supported_segments": ["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"],
        "commodity_note": "For MCX, use the exact Kotak trading symbol from scrip search, for example a current GOLD/CRUDEOIL futures symbol.",
        "gateway_diagnostics": gateway.diagnostics() if gateway else None,
    }


@api.post("/kotak/login")
async def kotak_login(req: KotakLoginReq = None, user=Depends(get_current_user)):
    req = req or KotakLoginReq()
    existing = _KOTAK_GATEWAYS.get(user["id"])
    existing_status = existing.status() if existing else {}
    gateway = await get_user_kotak_gateway(
        user["id"],
        fresh=bool(existing_status and not existing_status.get("initialized")),
    )
    if not gateway:
        raise HTTPException(status_code=400, detail="Save Kotak Neo Consumer Key, Consumer Secret, mobile, MPIN/password, and TOTP secret first.")
    status = gateway.status()
    if status.get("state") in {"READY", "FAILED"}:
        result = await asyncio.to_thread(gateway.reconnect, req.current_otp)
    else:
        result = await asyncio.to_thread(gateway.authenticate, req.current_otp)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak login failed"))
    return {"ok": True, "status": gateway.status()}


@api.post("/kotak/reconnect")
async def kotak_reconnect(req: KotakLoginReq = None, user=Depends(get_current_user)):
    req = req or KotakLoginReq()
    gateway = await get_user_kotak_gateway(user["id"])
    if not gateway:
        raise HTTPException(status_code=400, detail="Kotak Neo is not configured.")
    result = await asyncio.to_thread(gateway.reconnect, req.current_otp)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak reconnect failed"))
    return {"ok": True, "status": gateway.status()}


@api.post("/kotak/error/clear")
async def kotak_clear_error(user=Depends(get_current_user)):
    gateway = _KOTAK_GATEWAYS.get(user["id"])
    if gateway:
        return await asyncio.to_thread(gateway.clear_error)
    return {"ok": True, "status": await get_user_kotak_status(user["id"])}


@api.post("/kotak/repair")
async def kotak_repair(user=Depends(get_current_user)):
    gateway = _KOTAK_GATEWAYS.pop(user["id"], None)
    if gateway:
        try:
            await asyncio.to_thread(gateway.logout)
        except Exception as exc:
            logger.warning("Kotak repair logout skipped: %s", exc)
    new_gateway = await get_user_kotak_gateway(user["id"], fresh=True)
    return {
        "ok": True,
        "message": "Kotak gateway reset. Enter a fresh OTP and click Connect Kotak.",
        "status": new_gateway.status() if new_gateway else await get_user_kotak_status(user["id"]),
    }


@api.post("/kotak/logout")
async def kotak_logout(user=Depends(get_current_user)):
    gateway = _KOTAK_GATEWAYS.pop(user["id"], None)
    if not gateway:
        return {"ok": True, "message": "no_active_kotak_gateway"}
    return await asyncio.to_thread(gateway.logout)


@api.post("/kotak/subscribe")
async def kotak_subscribe(req: KotakSubscribeReq, user=Depends(get_current_user)):
    gateway = await get_user_kotak_gateway(user["id"])
    if not gateway:
        raise HTTPException(status_code=400, detail="Kotak Neo is not configured.")
    result = await asyncio.to_thread(gateway.subscribe_symbols, req.instruments)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak subscribe failed"))
    return {"ok": True, "status": gateway.status(), "tokens": result.get("tokens", [])}


@api.get("/kotak/search-scrip")
async def kotak_search_scrip(
    exchange: str = "MCX",
    symbol: str = "",
    expiry: Optional[str] = None,
    option_type: Optional[str] = None,
    strike_price: Optional[str] = None,
    user=Depends(get_current_user),
):
    gateway = await get_user_kotak_gateway(user["id"])
    if not gateway:
        raise HTTPException(status_code=400, detail="Kotak Neo is not configured.")
    segment = _kotak_exchange_segment(exchange)
    result = await asyncio.to_thread(
        gateway.search_scrip,
        exchange_segment=segment,
        symbol=symbol,
        expiry=expiry,
        option_type=option_type,
        strike_price=strike_price,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak scrip search failed"))
    return {"ok": True, "exchange_segment": segment, "response": result.get("response")}


@api.post("/kotak/order-feed/start")
async def kotak_start_order_feed(user=Depends(get_current_user)):
    gateway = await get_user_kotak_gateway(user["id"])
    if not gateway:
        raise HTTPException(status_code=400, detail="Kotak Neo is not configured.")
    result = await asyncio.to_thread(gateway.subscribe_order_feed)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak order feed failed"))
    return {"ok": True, "status": gateway.status()}


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
    await db.broker_keys.update_one(
        {"user_id": user_id, "broker": "upstox"},
        {
            "$set": {
                "access_token": encrypt_secret(str(access_token)),
                "upstox_user_id": token_response.get("user_id"),
                "access_token_obtained_at": datetime.now(timezone.utc).isoformat(),
                "token_response_meta": {
                    k: v for k, v in token_response.items()
                    if k not in {"access_token", "extended_token"}
                },
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
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
    return await asyncio.to_thread(gateway.start_market_data_ws, req.instruments)


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

    recommended = settings.get("data_broker", "zerodha")
    reason = "Keep configured data broker until a healthier live feed is observed."
    
    if candidates:
        candidates.sort(key=lambda x: x[1])
        best_broker, _ = candidates[0]
        
        preferred = settings.get("data_broker", "zerodha")
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
        "configured_data_broker": settings.get("data_broker", "zerodha"),
        "recommended_data_broker": recommended,
        "reason": reason,
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


@api.get("/market/session")
async def market_session():
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc + IST_OFFSET
    minutes = now_ist.hour * 60 + now_ist.minute
    open_now = _is_nse_market_open(now_utc)
    return {
        "market": "NSE",
        "timezone": "Asia/Kolkata",
        "server_time_ist": now_ist.isoformat(),
        "open": open_now,
        "status": "OPEN" if open_now else "CLOSED",
        "open_time": "09:15",
        "close_time": "15:30",
        "minutes_to_close": max(0, NSE_CLOSE_MINUTE - minutes) if open_now else None,
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
    _, kite_status = await get_user_kite(user["id"])
    paper_stats = await paper_trading_stats(user=user)
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        "role": user.get("role", "trader"),
        "version": APP_VERSION,
        **settings,
        "zerodha": kite_status,
        "kotak_neo": await get_user_kotak_status(user["id"]),
        "upstox": await get_user_upstox_status(user["id"]),
        "paper_trading_stats": paper_stats,
    }


@api.put("/profile")
async def update_profile(req: ProfileUpdateReq, user=Depends(get_current_user)):
    update = {k: v for k, v in req.model_dump().items() if v is not None}
    if "paper_mode" in update and update["paper_mode"] is False:
        # Find if user has MCX strategies
        strategies = await db.strategies.find({"user_id": user["id"]}).to_list(500)
        has_mcx = any(
            s.get("instrument_group") == "MCX"
            or str(s.get("symbol")).upper() in {"CRUDEOIL", "NATURALGAS"}
            or "MCX" in str(s.get("symbol")).upper()
            for s in strategies
        )
        
        # Check market hours
        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        is_weekday = ist_now.weekday() < 5
        minutes_now = ist_now.hour * 60 + ist_now.minute
        nse_open = is_weekday and (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)
        mcx_open = is_weekday and (9 * 60) <= minutes_now <= (23 * 60 + 30)
        
        market_open = nse_open or (mcx_open if has_mcx else False)
        if not market_open:
            market_name = "NSE or MCX" if has_mcx else "NSE"
            raise HTTPException(
                status_code=400, 
                detail=f"Live mode activation is blocked because the {market_name} market is currently closed."
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
    broker_values = {"zerodha", "kotak_neo", "upstox", "none"}
    for f in ("data_broker", "execution_broker", "fallback_broker"):
        if f in update and update[f] not in broker_values:
            raise HTTPException(status_code=400, detail=f"{f} must be one of {sorted(broker_values)}")
    if update:
        await db.users.update_one({"id": user["id"]}, {"$set": update})
    return await get_profile(user=user)


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
            
            recorded_nifty = False
            recorded_sensex = False

            for user_row in users:
                user_id = user_row["id"]
                
                # Check Upstox live spot prices first if connected
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
                            node = data_node.get(upstox_key) or {}
                            spot_ltp = node.get("last_price") or node.get("ltp")
                            if spot_ltp:
                                option_ledger.record_market_tick(idx_sym, float(spot_ltp), "upstox")
                                if idx_sym == "NIFTY":
                                    recorded_nifty = True
                                elif idx_sym == "SENSEX":
                                    recorded_sensex = True
                    except Exception as e:
                        logger.warning(f"Upstox index spot quote failed in monitor loop: {e}")

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
                        if idx_symbol == "NIFTY":
                            recorded_nifty = True
                        elif idx_symbol == "SENSEX":
                            recorded_sensex = True

            # Simulated random walk fallback so index/commodity telemetry is never stuck on "Waiting..."
            import random
            if not recorded_nifty:
                last_nifty = option_ledger.latest_ticks(["NIFTY"]).get("NIFTY")
                last_price = float(last_nifty["ltp"]) if last_nifty else 24850.40
                new_price = round(last_price + random.uniform(-2.5, 2.5), 2)
                option_ledger.record_market_tick("NIFTY", new_price, "simulated")
            if not recorded_sensex:
                last_sensex = option_ledger.latest_ticks(["SENSEX"]).get("SENSEX")
                last_price = float(last_sensex["ltp"]) if last_sensex else 81460.20
                new_price = round(last_price + random.uniform(-8.0, 8.0), 2)
                option_ledger.record_market_tick("SENSEX", new_price, "simulated")
                
            for comm_symbol, base_price in [("CRUDEOIL", 6550.0), ("NATURALGAS", 215.0)]:
                last_comm = option_ledger.latest_ticks([comm_symbol]).get(comm_symbol)
                last_price = float(last_comm["ltp"]) if last_comm else base_price
                step_range = 1.0 if comm_symbol == "CRUDEOIL" else 0.1
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


async def _broker_reconciliation_loop(stop_event: asyncio.Event) -> None:
    interval = max(10, int(os.environ.get("BROKER_RECONCILE_INTERVAL_SEC", "30")))
    logger.info("Broker reconciliation loop started interval=%ss", interval)
    while not stop_event.is_set():
        try:
            users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
            for user_row in users:
                user_id = user_row["id"]
                kite, _ = await get_user_kite(user_id)
                if kite:
                    await _sync_kite_order_statuses(user_id, kite)
                    tick_manager = getattr(app.state, "tick_manager", None)
                    if tick_manager:
                        tick_status = tick_manager.status_info(user_id)
                        last_tick = tick_status.get("last_tick_at")
                        stale = True
                        if last_tick:
                            try:
                                last_dt = datetime.fromisoformat(last_tick)
                                stale = (datetime.now(timezone.utc) - last_dt).total_seconds() > 180
                            except Exception:
                                stale = True
                        if not tick_status.get("auth_failed") and not tick_status.get("connecting") and (
                            not tick_status.get("connected") or stale
                        ):
                            await _start_user_ticker(user_id)
                await _sync_upstox_order_statuses(user_id)
        except Exception as e:
            logger.warning(f"broker reconciliation error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < interval:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Broker reconciliation loop stopped")


# Include modular routers into api router to ensure /api prefix is preserved
from routes.auth import router as auth_router
from routes.ai import router as ai_router
from routes.ops import router as ops_router

api.include_router(auth_router)
api.include_router(ai_router)
api.include_router(ops_router)

# ============== Boot ==============
app.include_router(api)

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
    option_ledger.initialize()
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
        ("paper_trading_history", [("user_id", 1), ("created_at", -1)], {}),
        ("trade_fills", "order_id", {"unique": True}),
        ("trade_fills", [("user_id", 1), ("filled_at", -1)], {}),
        ("trades", [("user_id", 1), ("closed_at", -1)], {}),
        ("order_events", [("order_id", 1), ("created_at", 1)], {}),
        ("order_events", [("user_id", 1), ("created_at", -1)], {}),
        ("broker_sync_state", [("user_id", 1), ("broker", 1)], {"unique": True}),
        ("system_config", "_id", {}),
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
            await seed_default_strategies_for_user(user_row["id"])
    except Exception as e:
        logger.warning(f"default strategy seeding skipped: {e}")

    try:
        async for strategy_row in db.strategies.find({}, {"_id": 0}):
            _sync_option_ledger_strategy(strategy_row)
    except Exception as e:
        logger.warning(f"option ledger strategy sync skipped: {e}")

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
        return await _resolve_option_for_strategy(
            user_id,
            strategy or {},
            underlying,
            signal_action,
            strike_mode,
            otm_points,
            expiry_offset
        )

    app.state.tick_manager = RealtimeTickManager()
    app.state.kotak_gateways = _KOTAK_GATEWAYS
    app.state.upstox_gateways = _UPSTOX_GATEWAYS
    app.state.runner_stop = asyncio.Event()
    app.state.runner_task = asyncio.create_task(
        strategy_runner.runner_loop(db, _price_history, _place_order_core,
                                    app.state.runner_stop, _resolve_option)
    )
    app.state.health_stop = asyncio.Event()
    app.state.health_task = asyncio.create_task(_strategy_health_loop(app.state.health_stop))
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
        app.state.health_stop.set()
        if app.state.health_task:
            await asyncio.wait_for(app.state.health_task, timeout=3.0)
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
        option_ledger.close()
    except Exception:
        pass
    client.close()
