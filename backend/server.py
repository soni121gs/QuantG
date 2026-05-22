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
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, Field, EmailStr

import kite_helper
import kotak_helper
import upstox_helper
from kotak_gateway import KotakNeoGateway
import options_helper
import backtrader_runner
import strategy_runner
from option_state_ledger import OptionStateLedger
from market_protection import MarketTrendAnalyzer, FakeSignalFilter, OrderExecutionRetry
from daily_strategy_reporter import DailyStrategyReporter
from realtime_ticks import RealtimeTickManager
from safe_exec import safe_run_strategy

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
APP_VERSION = "10.0"

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
ACTIVE_STRATEGY_POSITION_STATUSES = {"RESERVED", "PENDING_OPEN", "OPEN", "EXITING"}
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

    if kite:
        token = None
        source_kind = "equity"
        if sym_upper in options_helper.INDEX_SPOT_SYMBOL:
            token = await _index_spot_token(kite, sym_upper)
            source_kind = "index-spot"
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
                daily = await _cached_safe_historical(kite, user_id, token, days, "day")
                if daily:
                    return {
                        "data": daily,
                        "source": f"zerodha-kite-day:{source_kind}:{sym_upper}",
                        "is_live": True,
                        "interval": "day",
                    }

    if allow_mock:
        sym = next((s for s in SYMBOLS if s["symbol"] == sym_upper), None)
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


@api.post("/auth/register", response_model=TokenOut)
async def register(req: RegisterReq):
    email = req.email.lower().strip()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_doc = {
        "id": str(uuid.uuid4()),
        "email": email,
        "name": req.name or email.split("@")[0],
        "password_hash": hash_password(req.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(user_doc)
    await seed_default_strategies_for_user(user_doc["id"])
    token = create_token(user_doc["id"], email)
    return TokenOut(
        access_token=token,
        user=UserOut(id=user_doc["id"], email=email, name=user_doc["name"], created_at=user_doc["created_at"]),
    )


@api.post("/auth/login", response_model=TokenOut)
async def login(req: LoginReq):
    email = req.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(user["id"], email)
    return TokenOut(
        access_token=token,
        user=UserOut(id=user["id"], email=email, name=user.get("name"), created_at=user["created_at"]),
    )


@api.get("/auth/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return UserOut(id=user["id"], email=user["email"], name=user.get("name"), created_at=user["created_at"])


# ============== Routes: AI Bot ==============
def _quantbot_reply(message: str) -> str:
    text = message.lower()
    if "rsi" in text or "macd" in text:
        return (
            "RSI measures overbought/oversold momentum, while MACD measures trend momentum.\n"
            "- RSI is bounded from 0 to 100 and is useful for mean-reversion filters.\n"
            "- MACD compares moving averages and is useful for trend confirmation.\n"
            "- Combine either signal with stops, sizing, and paper testing first."
        )
    if "bollinger" in text:
        return (
            "A simple Bollinger squeeze idea:\n"
            "1. Calculate 20-period SMA and standard deviation.\n"
            "2. Mark squeeze when band width is below its recent percentile.\n"
            "3. Buy after price closes above the upper band with momentum confirmation.\n"
            "4. Exit on a middle-band break or fixed risk stop."
        )
    if "risk" in text or "portfolio" in text:
        return (
            "Keep the system survivable: risk 0.5% to 1% per trade, cap daily loss, avoid oversized option lots, and stop trading after two or three consecutive losses. Paper trade the exact rules before switching live mode on."
        )
    if "momentum" in text or "nifty" in text:
        return (
            "NIFTY momentum template: trade only after the first 15 minutes, require price above VWAP and a rising 20 EMA, enter on pullback continuation, and exit on VWAP loss or a fixed R multiple."
        )
    if "python" in text or "code" in text or "strategy" in text:
        return (
            "Here is the structure QuantG expects:\n\n"
            "def run(data):\n"
            "    signals = []\n"
            "    closes = [row['close'] for row in data]\n"
            "    if len(closes) > 20 and closes[-1] > sum(closes[-20:]) / 20:\n"
            "        signals.append({'action': 'BUY'})\n"
            "    return signals\n\n"
            "Keep the logic deterministic and test it in paper mode before live execution."
        )
    return (
        "I can help with strategy ideas, Python strategy structure, Zerodha workflow, and risk rules. "
        "Tell me the symbol, timeframe, entry idea, exit rule, and max risk per trade."
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
You are QuantBot inside QuantG, a personal Indian algo-trading terminal.

Safety and scope:
- Give educational and implementation help only.
- Never claim profit is guaranteed.
- Never tell the user to bypass broker, exchange, or app risk controls.
- Never say you placed, modified, or cancelled an order.
- For live trading advice, emphasize paper testing, position sizing, liquidity, slippage, and daily loss limits.
- If writing QuantG strategy code, return deterministic Python with `def run(data):` and signals shaped like `{{'date': data[i]['date'], 'action': 'BUY'}}`.
- Keep answers concise and practical for NIFTY, BANKNIFTY, SENSEX, MCX crude oil, MCX natural gas, Zerodha, Kotak Neo, and QuantG workflows.

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


@api.get("/ai/chat/{session_id}")
async def get_ai_chat(session_id: str, user=Depends(get_current_user)):
    rows = await db.ai_chats.find(
        {"user_id": user["id"], "session_id": session_id},
        {"_id": 0, "user_id": 0, "session_id": 0},
    ).sort("created_at", 1).to_list(100)
    return rows


@api.get("/ai/status")
async def ai_status(user=Depends(get_current_user)):
    configured = bool(os.environ.get("GEMINI_API_KEY"))
    return {
        "provider": "google-ai-studio-rest" if configured else "local-fallback",
        "model": GEMINI_MODEL if configured else "quantg-local-rules",
        "gemini_configured": configured,
        "google_genai_sdk_available": False,
        "sdk_error": None,
        "transport": "rest",
    }


@api.post("/ai/chat")
async def ai_chat(req: ChatReq, user=Depends(get_current_user)):
    content = req.message.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message is required")

    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user["id"],
        "session_id": req.session_id,
    }
    recent_messages = await db.ai_chats.find(
        {"user_id": user["id"], "session_id": req.session_id},
        {"_id": 0, "role": 1, "content": 1},
    ).sort("created_at", -1).to_list(8)
    recent_messages = list(reversed(recent_messages))
    provider = "google-ai-studio" if os.environ.get("GEMINI_API_KEY") else "local-fallback"
    reply = await _google_ai_reply(content, recent_messages)
    if provider == "google-ai-studio" and reply == _quantbot_reply(content):
        provider = "local-fallback"
    bot_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": reply,
        "provider": provider,
        "model": GEMINI_MODEL if provider == "google-ai-studio" else "quantg-local-rules",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user["id"],
        "session_id": req.session_id,
    }
    await db.ai_chats.insert_many([user_msg, bot_msg])
    return {k: v for k, v in bot_msg.items() if k not in {"_id", "user_id", "session_id"}}


@api.get("/ai/strategy-scores")
async def ai_strategy_scores(user=Depends(get_current_user)):
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(500)
    market_rows = await watchlist(user=user)
    commodity_rows = await commodity_watchlist(user=user)
    market_by_symbol = {r["symbol"]: r for r in [*market_rows, *commodity_rows]}
    scores = [_market_score_for_strategy(row, market_by_symbol) for row in rows]
    for score in scores:
        score["user_id"] = user["id"]
        await db.strategy_ai_scores.update_one(
            {"strategy_id": score["strategy_id"], "user_id": user["id"]},
            {"$set": score},
            upsert=True,
        )
    return {
        "scores": [{k: v for k, v in score.items() if k != "user_id"} for score in scores],
        "provider": "gemini-context" if os.environ.get("GEMINI_API_KEY") else "local-market-structure",
    }


@api.get("/ai/market-analysis")
async def ai_market_analysis(user=Depends(get_current_user)):
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(50)
    market_rows = await watchlist(user=user)
    commodity_rows = await commodity_watchlist(user=user)
    scores = [_market_score_for_strategy(row, {r["symbol"]: r for r in [*market_rows, *commodity_rows]}) for row in strategies]
    prompt = (
        "Analyze this QuantG market structure snapshot for educational risk context only. "
        "Mention NIFTY/SENSEX and MCX crude oil/natural gas when relevant. "
        "Keep it concise and do not promise returns.\n\n"
        + json.dumps({
            "market": market_rows[:8],
            "commodities": commodity_rows,
            "strategy_scores": scores[:12],
        }, default=str)[:9000]
    )
    return {
        "provider": "google-ai-studio" if os.environ.get("GEMINI_API_KEY") else "local-fallback",
        "content": await _google_ai_reply(prompt, []),
        "scores": scores,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@api.get("/ai/training-context")
async def ai_training_context(user=Depends(get_current_user)):
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(200)
    recent_scores = await db.strategy_ai_scores.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("generated_at", -1).to_list(100)
    return {
        "purpose": "Context-feed payload for Gemini prompts and offline fine-tuning experiments.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": await watchlist(user=user),
        "commodities": await commodity_watchlist(user=user),
        "strategies": [_strategy_out(row).model_dump() for row in strategies],
        "recent_ai_scores": recent_scores,
    }


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

    Kotak Neo ticks are preferred when authenticated. Mock rows stay explicit so
    the UI can distinguish demo context from broker data.
    """
    settings = await get_user_settings(user["id"])
    gateway = _KOTAK_GATEWAYS.get(user["id"]) if settings.get("data_broker") == "kotak_neo" else None
    rows = []
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
        "name": "NIFTY Momentum EMA",
        "description": "Long ATM NIFTY options on strong EMA trend shifts. Good for directional momentum moves.",
        "underlying": "NIFTY",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    fast, slow = 8, 21
    signals = []
    ema_fast = []
    ema_slow = []
    for i in range(len(closes)):
        ema_fast.append(sum(closes[max(0, i-fast+1):i+1]) / min(fast, i+1))
        ema_slow.append(sum(closes[max(0, i-slow+1):i+1]) / min(slow, i+1))
        if i == 0:
            continue
        if ema_fast[i] > ema_slow[i] and ema_fast[i-1] <= ema_slow[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif ema_fast[i] < ema_slow[i] and ema_fast[i-1] >= ema_slow[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "NIFTY RSI Reversion",
        "description": "Trade NIFTY options on oversold and overbought RSI levels with trend confirmation.",
        "underlying": "NIFTY",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    period = 14
    signals = []
    for i in range(len(closes)):
        if i < period: continue
        gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-period+1, i+1))
        losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-period+1, i+1))
        avg_gain = gains / period
        avg_loss = losses / period if losses else 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        if rsi < 30:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif rsi > 70:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "NIFTY Opening Range Breakout",
        "description": "Capture opening range breakouts on NIFTY using the first 3 five-minute bars.",
        "underlying": "NIFTY",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    if len(data) < 10:
        return []
    range_high = max(d['high'] for d in data[:3])
    range_low = min(d['low'] for d in data[:3])
    signals = []
    for i in range(3, len(data)):
        if data[i]['close'] > range_high:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
            break
        if data[i]['close'] < range_low:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
            break
    return signals
""",
    },
    {
        "name": "NIFTY ATR Trend",
        "description": "Long NIFTY options when momentum expands beyond ATR-based trend thresholds.",
        "underlying": "NIFTY",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    highs = [d['high'] for d in data]
    lows = [d['low'] for d in data]
    lookback = 14
    signals = []
    for i in range(len(data)):
        if i < lookback: continue
        tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-lookback+1, i+1)]
        atr = sum(tr) / lookback
        body = closes[i] - closes[i-1]
        if body > atr * 0.4:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif body < -atr * 0.4:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "NIFTY Trend Recheck",
        "description": "Wait for pullbacks into a rising trend before taking NIFTY options exposure.",
        "underlying": "NIFTY",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    ma20 = [sum(closes[max(0, i-19):i+1]) / min(20, i+1) for i in range(len(closes))]
    signals = []
    for i in range(5, len(data)):
        if closes[i] > ma20[i] and closes[i-1] < ma20[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif closes[i] < ma20[i] and closes[i-1] > ma20[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "SENSEX Momentum EMA",
        "description": "Buy SENSEX options on crossovers that signal trend continuation.",
        "underlying": "SENSEX",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    fast, slow = 8, 21
    signals = []
    ema_fast = []
    ema_slow = []
    for i in range(len(closes)):
        ema_fast.append(sum(closes[max(0, i-fast+1):i+1]) / min(fast, i+1))
        ema_slow.append(sum(closes[max(0, i-slow+1):i+1]) / min(slow, i+1))
        if i == 0:
            continue
        if ema_fast[i] > ema_slow[i] and ema_fast[i-1] <= ema_slow[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif ema_fast[i] < ema_slow[i] and ema_fast[i-1] >= ema_slow[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "SENSEX RSI Reversion",
        "description": "Enter SENSEX option trades when RSI reaches extreme levels and momentum shifts.",
        "underlying": "SENSEX",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    period = 14
    signals = []
    for i in range(len(closes)):
        if i < period: continue
        gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-period+1, i+1))
        losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-period+1, i+1))
        avg_gain = gains / period
        avg_loss = losses / period if losses else 0.0001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        if rsi < 30:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif rsi > 70:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "SENSEX Opening Range",
        "description": "Take early SENSEX option positions on opening range breakout or breakdown.",
        "underlying": "SENSEX",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    if len(data) < 10:
        return []
    range_high = max(d['high'] for d in data[:3])
    range_low = min(d['low'] for d in data[:3])
    signals = []
    for i in range(3, len(data)):
        if data[i]['close'] > range_high:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
            break
        if data[i]['close'] < range_low:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
            break
    return signals
""",
    },
    {
        "name": "SENSEX ATR Trend",
        "description": "Jump into SENSEX options when intraday ATR momentum expands strongly.",
        "underlying": "SENSEX",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    highs = [d['high'] for d in data]
    lows = [d['low'] for d in data]
    lookback = 14
    signals = []
    for i in range(len(data)):
        if i < lookback: continue
        tr = [max(highs[j], closes[j-1]) - min(lows[j], closes[j-1]) for j in range(i-lookback+1, i+1)]
        atr = sum(tr) / lookback
        body = closes[i] - closes[i-1]
        if body > atr * 0.4:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif body < -atr * 0.4:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
    {
        "name": "SENSEX Trend Recheck",
        "description": "Wait for SENSEX pullbacks into trend support before scaling into options.",
        "underlying": "SENSEX",
        "strike_mode": "ATM_BUY",
        "otm_points": 0,
        "lots": 1,
        "python_code": """def run(data):
    closes = [d['close'] for d in data]
    ma20 = [sum(closes[max(0, i-19):i+1]) / min(20, i+1) for i in range(len(closes))]
    signals = []
    for i in range(5, len(data)):
        if closes[i] > ma20[i] and closes[i-1] < ma20[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'BUY'})
        elif closes[i] < ma20[i] and closes[i-1] > ma20[i-1]:
            signals.append({'date': data[i]['date'], 'action': 'SELL'})
    return signals
""",
    },
]

LEGACY_OPTION_STRATEGIES = DEFAULT_OPTION_STRATEGIES


TREND_CONTINUATION_CODE = """def run(data):
    if len(data) < 80:
        return []
    last = data[-1]
    today = str(last['date'])[:10]
    clock = str(last['date'])[11:16]
    if clock < '09:45' or clock > '14:45':
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    prev_high = max(highs[-11:-1])
    prev_low = min(lows[-11:-1])
    tr = [max(highs[i], closes[i-1]) - min(lows[i], closes[i-1]) for i in range(len(data)-14, len(data))]
    atr = sum(tr) / len(tr)
    body = abs(closes[-1] - closes[-2])
    if str(data[-2]['date'])[:10] != today:
        return []
    if closes[-1] > prev_high and sma20 > sma50 and closes[-1] > sma20 and body > atr * 0.35:
        return [{'date': last['date'], 'action': 'BUY'}]
    if closes[-1] < prev_low and sma20 < sma50 and closes[-1] < sma20 and body > atr * 0.35:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

OPENING_RANGE_VWAP_CODE = """def run(data):
    today_bars = [d for d in data if str(d['date'])[:10] == str(data[-1]['date'])[:10]]
    if len(today_bars) < 5:
        return []
    last = today_bars[-1]
    clock = str(last['date'])[11:16]
    if clock < '09:35' or clock > '11:15':
        return []
    opening = today_bars[:3]
    range_high = max(float(d.get('high', d['close'])) for d in opening)
    range_low = min(float(d.get('low', d['close'])) for d in opening)
    closes = [float(d['close']) for d in today_bars]
    highs = [float(d.get('high', d['close'])) for d in today_bars]
    lows = [float(d.get('low', d['close'])) for d in today_bars]
    vols = [max(1, int(d.get('volume', 1))) for d in today_bars]
    typical_value = sum(((highs[i] + lows[i] + closes[i]) / 3) * vols[i] for i in range(len(today_bars)))
    vwap = typical_value / max(1, sum(vols))
    avg_vol = sum(vols[:-1]) / max(1, len(vols) - 1)
    if closes[-1] > range_high and closes[-1] > vwap and vols[-1] > avg_vol * 1.05:
        return [{'date': last['date'], 'action': 'BUY'}]
    if closes[-1] < range_low and closes[-1] < vwap and vols[-1] > avg_vol * 1.05:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

VWAP_PULLBACK_CODE = """def run(data):
    today_bars = [d for d in data if str(d['date'])[:10] == str(data[-1]['date'])[:10]]
    if len(today_bars) < 15:
        return []
    last = today_bars[-1]
    clock = str(last['date'])[11:16]
    if clock < '10:00' or clock > '14:30':
        return []
    closes = [float(d['close']) for d in today_bars]
    highs = [float(d.get('high', d['close'])) for d in today_bars]
    lows = [float(d.get('low', d['close'])) for d in today_bars]
    vols = [max(1, int(d.get('volume', 1))) for d in today_bars]
    typical_value = sum(((highs[i] + lows[i] + closes[i]) / 3) * vols[i] for i in range(len(today_bars)))
    vwap = typical_value / max(1, sum(vols))
    ma20 = sum(closes[-20:]) / min(20, len(closes))
    ma20_prev = sum(closes[-21:-1]) / min(20, len(closes) - 1)
    prev_high = max(highs[-6:-1])
    prev_low = min(lows[-6:-1])
    if ma20 > ma20_prev and closes[-2] <= vwap and closes[-1] > vwap and closes[-1] > prev_high:
        return [{'date': last['date'], 'action': 'BUY'}]
    if ma20 < ma20_prev and closes[-2] >= vwap and closes[-1] < vwap and closes[-1] < prev_low:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

ATR_VOLUME_BREAKOUT_CODE = """def run(data):
    if len(data) < 70:
        return []
    last = data[-1]
    clock = str(last['date'])[11:16]
    if clock < '10:15' or clock > '14:40':
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    tr = [max(highs[i], closes[i-1]) - min(lows[i], closes[i-1]) for i in range(len(data)-14, len(data))]
    atr = sum(tr) / len(tr)
    recent_range = sum(highs[i] - lows[i] for i in range(len(data)-8, len(data)-2)) / 6
    prev_high = max(highs[-13:-1])
    prev_low = min(lows[-13:-1])
    avg_vol = sum(vols[-21:-1]) / 20
    if recent_range < atr * 0.85 and vols[-1] > avg_vol * 1.15 and closes[-1] > prev_high:
        return [{'date': last['date'], 'action': 'BUY'}]
    if recent_range < atr * 0.85 and vols[-1] > avg_vol * 1.15 and closes[-1] < prev_low:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

RSI_REVERSAL_CODE = """def run(data):
    if len(data) < 60:
        return []
    last = data[-1]
    clock = str(last['date'])[11:16]
    if clock < '10:00' or clock > '14:15':
        return []
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    def calc_rsi(end, period):
        gains = 0
        losses = 0
        for j in range(end - period + 1, end + 1):
            change = closes[j] - closes[j-1]
            if change > 0:
                gains += change
            else:
                losses += abs(change)
        avg_gain = gains / period
        avg_loss = losses / period if losses else 0.0001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    rsi_now = calc_rsi(len(closes) - 1, 14)
    rsi_prev = calc_rsi(len(closes) - 2, 14)
    sma50 = sum(closes[-50:]) / 50
    prev_high = max(highs[-5:-1])
    prev_low = min(lows[-5:-1])
    if rsi_prev < 32 and rsi_now > 38 and closes[-1] > prev_high and closes[-1] > sma50:
        return [{'date': last['date'], 'action': 'BUY'}]
    if rsi_prev > 68 and rsi_now < 62 and closes[-1] < prev_low and closes[-1] < sma50:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

DEFAULT_OPTION_STRATEGIES = [
    {
        "name": "NIFTY VWAP Trend Breakout",
        "description": "Trades only confirmed NIFTY trend breakouts with SMA, ATR, and time filters.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": TREND_CONTINUATION_CODE,
    },
    {
        "name": "NIFTY Opening Range VWAP",
        "description": "Uses current-day opening range only, confirmed by VWAP and volume.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": OPENING_RANGE_VWAP_CODE,
    },
    {
        "name": "NIFTY VWAP Pullback Continuation",
        "description": "Waits for a pullback to VWAP, then continuation through a short swing.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": VWAP_PULLBACK_CODE,
    },
    {
        "name": "NIFTY ATR Volume Expansion",
        "description": "Trades post-compression moves only when ATR and volume confirm expansion.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": ATR_VOLUME_BREAKOUT_CODE,
    },
    {
        "name": "NIFTY RSI Reversal With Trend",
        "description": "Countertrend entry only after RSI recovery and reclaim of a short swing.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": RSI_REVERSAL_CODE,
    },
    {
        "name": "SENSEX VWAP Trend Breakout",
        "description": "Trades only confirmed SENSEX trend breakouts with SMA, ATR, and time filters.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": TREND_CONTINUATION_CODE,
    },
    {
        "name": "SENSEX Opening Range VWAP",
        "description": "Uses current-day SENSEX opening range only, confirmed by VWAP and volume.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": OPENING_RANGE_VWAP_CODE,
    },
    {
        "name": "SENSEX VWAP Pullback Continuation",
        "description": "Waits for SENSEX pullback to VWAP, then continuation through a short swing.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": VWAP_PULLBACK_CODE,
    },
    {
        "name": "SENSEX ATR Volume Expansion",
        "description": "Trades post-compression SENSEX moves only when ATR and volume confirm.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": ATR_VOLUME_BREAKOUT_CODE,
    },
    {
        "name": "SENSEX RSI Reversal With Trend",
        "description": "SENSEX reversal entry only after RSI recovery and swing confirmation.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "python_code": RSI_REVERSAL_CODE,
    },
]

COMMODITY_MOMENTUM_BREAKOUT_CODE = """def run(data):
    if len(data) < 55:
        return []
    last = data[-1]
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    vols = [max(1, int(d.get('volume', 1))) for d in data]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    prev_high = max(highs[-12:-1])
    prev_low = min(lows[-12:-1])
    avg_vol = sum(vols[-21:-1]) / 20
    if closes[-1] > prev_high and sma20 > sma50 and vols[-1] > avg_vol * 1.1:
        return [{'date': last['date'], 'action': 'BUY'}]
    if closes[-1] < prev_low and sma20 < sma50 and vols[-1] > avg_vol * 1.1:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

COMMODITY_RANGE_SELLING_CODE = """def run(data):
    if len(data) < 45:
        return []
    last = data[-1]
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    recent_range = max(highs[-20:]) - min(lows[-20:])
    wider_range = max(highs[-40:]) - min(lows[-40:])
    drift = abs(closes[-1] - closes[-10]) / max(1, closes[-10])
    if wider_range and recent_range < wider_range * 0.55 and drift < 0.015:
        return [{'date': last['date'], 'action': 'SELL'}]
    return []
"""

COMMODITY_VOLATILITY_STRADDLE_CODE = """def run(data):
    if len(data) < 35:
        return []
    last = data[-1]
    closes = [float(d['close']) for d in data]
    highs = [float(d.get('high', d['close'])) for d in data]
    lows = [float(d.get('low', d['close'])) for d in data]
    tr = [max(highs[i], closes[i-1]) - min(lows[i], closes[i-1]) for i in range(len(data)-14, len(data))]
    atr = sum(tr) / len(tr)
    compression = sum(highs[i] - lows[i] for i in range(len(data)-8, len(data))) / 8
    if compression < atr * 0.75:
        return [{'date': last['date'], 'action': 'BUY'}]
    return []
"""

STANDARD_STRATEGY_CATALOG = [
    {
        "name": "NIFTY VWAP Trend Breakout",
        "description": "Directional NIFTY option buying with VWAP, SMA, ATR, and time filters.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 35000.0, "instrument_group": "NFO",
        "python_code": TREND_CONTINUATION_CODE,
    },
    {
        "name": "NIFTY Opening Range VWAP",
        "description": "Directional NIFTY option buying after opening range breakouts confirmed by VWAP and volume.",
        "underlying": "NIFTY", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 35000.0, "instrument_group": "NFO",
        "python_code": OPENING_RANGE_VWAP_CODE,
    },
    {
        "name": "SENSEX VWAP Trend Breakout",
        "description": "Directional SENSEX option buying with trend, volatility, and time-of-day filters.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 45000.0, "instrument_group": "BFO",
        "python_code": TREND_CONTINUATION_CODE,
    },
    {
        "name": "SENSEX RSI Reversal With Trend",
        "description": "SENSEX option buying only after RSI recovery aligns with swing and SMA confirmation.",
        "underlying": "SENSEX", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 45000.0, "instrument_group": "BFO",
        "python_code": RSI_REVERSAL_CODE,
    },
    {
        "name": "Crude Oil Momentum Breakout",
        "description": "MCX crude oil option buying for volume-backed trend breakouts.",
        "underlying": "CRUDEOIL", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 65000.0, "instrument_group": "MCX",
        "python_code": COMMODITY_MOMENTUM_BREAKOUT_CODE,
    },
    {
        "name": "Crude Oil Iron Condor Range",
        "description": "MCX crude oil option selling template for consolidation and range-bound volatility.",
        "underlying": "CRUDEOIL", "strike_mode": "ATM_SELL", "otm_points": 100, "lots": 1,
        "strategy_type": "Option Selling", "required_capital": 150000.0, "instrument_group": "MCX",
        "python_code": COMMODITY_RANGE_SELLING_CODE,
    },
    {
        "name": "Natural Gas Momentum Breakout",
        "description": "MCX natural gas option buying for directional expansion after compression.",
        "underlying": "NATURALGAS", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 55000.0, "instrument_group": "MCX",
        "python_code": COMMODITY_MOMENTUM_BREAKOUT_CODE,
    },
    {
        "name": "Natural Gas Volatility Straddle",
        "description": "MCX natural gas option buying template for low-volatility setups before expansion.",
        "underlying": "NATURALGAS", "strike_mode": "ATM_BUY", "otm_points": 0, "lots": 1,
        "strategy_type": "Option Buying", "required_capital": 70000.0, "instrument_group": "MCX",
        "python_code": COMMODITY_VOLATILITY_STRADDLE_CODE,
    },
]

_seed_templates_by_name = {
    template["name"]: template
    for template in [*LEGACY_OPTION_STRATEGIES, *DEFAULT_OPTION_STRATEGIES, *STANDARD_STRATEGY_CATALOG]
}
DEFAULT_OPTION_STRATEGIES = list(_seed_templates_by_name.values())

LEGACY_DEFAULT_STRATEGY_NAMES = {
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
) -> None:
    if not reservation:
        return
    now = datetime.now(timezone.utc).isoformat()
    await db.strategy_positions.update_one(
        {"id": reservation["id"], "user_id": reservation["user_id"]},
        {"$set": {
            "entry_order_id": order_id,
            "broker_order_id": broker_order_id,
            "entry_broker_order_id": broker_order_id,
            "quantity": int(quantity),
            "open_quantity": int(quantity),
            "average_buy_price": float(average_buy_price or 0),
            "status": "OPEN" if paper else "PENDING_OPEN",
            "mode": "paper" if paper else "live",
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
        "status": "OPEN",
    }, {"_id": 0})
    if not row:
        active = await db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "EXITING"]},
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
        {"id": position["id"], "user_id": position["user_id"], "status": "OPEN"},
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
        "status": "OPEN",
    }, {"_id": 0}).to_list(20)
    for pos in positions:
        sym = pos.get("trading_symbol") or pos.get("symbol")
        qty_net = int(pos.get("open_quantity") or pos.get("quantity") or 0)
        if not sym or qty_net <= 0:
            continue
        place_kwargs: Dict[str, Any] = {
            "user_id": user_id,
            "side": "SELL",
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
                "transaction_type": "SELL",
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


async def _current_ltp_for_symbol(user_id: str, symbol: str, exchange: str, allow_mock: bool = True) -> Optional[float]:
    settings = await get_user_settings(user_id)
    data_broker = settings.get("data_broker", "zerodha")
    if data_broker == "kotak_neo":
        gateway = _KOTAK_GATEWAYS.get(user_id)
        if gateway:
            tick = gateway.latest_tick_by_symbol(symbol.upper())
            if tick and tick.get("ltp"):
                return float(tick["ltp"])
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
                transaction_type=_kotak_transaction_type(side),
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
        "broker_order_id": broker_order_id,
        "raw": result.get("response"),
        "tag": execution_tag,
        "attempts": attempts,
        "trading_symbol": symbol_used,
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
    await db.strategies.update_one(
        {"id": sid, "user_id": user["id"]},
        {"$set": {"visual_config": visual_config}},
    )
    row["visual_config"] = visual_config
    _sync_option_ledger_strategy(row)
    return {"ok": True, "max_lot": 1, "risk": risk}


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
            signal_validation = _validate_trade_signal(last_sig, data)
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


def _validate_trade_signal(signal: Dict[str, Any], data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score the latest signal against trend, candle confirmation, and whipsaw risk."""
    try:
        trend = MarketTrendAnalyzer.analyze(data, lookback=min(50, max(20, len(data))))
        validation = FakeSignalFilter.validate(signal, data, trend)
        validation["threshold"] = SIGNAL_CONFIDENCE_MIN
        validation["trend"] = trend
        validation["is_valid"] = bool(validation.get("is_valid")) and float(validation.get("confidence", 0)) >= SIGNAL_CONFIDENCE_MIN
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
        "status": "COMPLETE",
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
        "status": {"$nin": ["REJECTED", "CANCELLED", "STALE", "BROKER_NOT_FOUND"]},
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


def _new_execution_tag() -> str:
    return f"QG{uuid.uuid4().hex[:14]}".upper()


def _open_order_statuses() -> set:
    return {"OPEN", "PENDING", "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED"}


def _closed_order_statuses() -> set:
    return {"COMPLETE", "CANCELLED", "REJECTED", "STALE", "BROKER_NOT_FOUND"}


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


async def _place_order_core(user_id: str, symbol: str, side: str, qty: Optional[int],
                            order_type: str = "MARKET", price: Optional[float] = None,
                            product: Optional[str] = None, source: str = "manual",
                            option_contract: Optional[Dict[str, Any]] = None,
                            exchange: str = "NSE") -> dict:
    """Shared order-placement business logic. Honours paper_mode + risk limits.
    Used by both the /orders endpoint and the background strategy runner.

    option_contract (optional): when set, the order is placed on an index option
    instead of equity. Expected keys:
      tradingsymbol, exchange (NFO|BFO), lot_size, instrument_token, strike,
      expiry, underlying, option_type (CE|PE), transaction_type (BUY|SELL).
    When option_contract is present, `symbol` is the underlying label
    (NIFTY/BANKNIFTY/SENSEX) used only for logging, and `qty` is interpreted as
    NUMBER OF LOTS (multiplied by contract.lot_size to get broker qty).
    `side` is ignored for options — the contract's transaction_type is used.
    """
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    order_type = order_type.upper()
    if order_type not in ("MARKET", "LIMIT"):
        raise HTTPException(status_code=400, detail="order_type must be MARKET or LIMIT")
    if order_type == "LIMIT" and not price:
        raise HTTPException(status_code=400, detail="LIMIT orders require a price")
    exchange = (exchange or "NSE").upper()
    if exchange not in SUPPORTED_ORDER_EXCHANGES:
        raise HTTPException(status_code=400, detail=f"exchange must be one of {sorted(SUPPORTED_ORDER_EXCHANGES)}")

    strategy_id = await _strategy_source_id(source)
    settings = await get_user_settings(user_id)
    paper = settings.get("paper_mode", True)
    execution_broker = settings.get("execution_broker", "zerodha")
    if not paper and execution_broker not in {"zerodha", "kotak_neo"}:
        raise HTTPException(
            status_code=400,
            detail=f"Live execution through {execution_broker} is not enabled yet. Switch execution broker to zerodha/kotak_neo or use PAPER.",
        )
    if not paper and order_type == "MARKET" and not _is_order_market_open(exchange):
        market_name = "MCX" if exchange == "MCX" else "NSE/BSE"
        raise HTTPException(status_code=400, detail=f"Live MARKET orders are blocked outside {market_name} market hours.")

    exit_source = side == "SELL" or any(str(source).startswith(prefix) for prefix in (
        "exit",
        "manual-exit",
        "risk-trigger",
        "feed-stale",
        "squareoff",
        "time-exit",
        "intraday-squareoff",
    ))
    if not exit_source:
        await _check_daily_loss_guard(user_id, settings.get("max_daily_loss", 0))
    if not exit_source:
        await _check_trade_count_guard(user_id, int(settings.get("max_trades_per_day") or 0))

    # ===== OPTIONS PATH =====
    if option_contract:
        # Lots × lot_size = total quantity sent to broker
        lots = int(qty or 1)
        lot_size = int(option_contract.get("lot_size") or 1)
        if strategy_id:
            cap = float(settings.get("per_strategy_capital") or settings.get("max_position_size") or 0)
            estimated_premium = float(price or option_contract.get("spot") or option_contract.get("atm_strike") or 0) * 0.02
            if cap > 0 and estimated_premium > 0:
                max_lots_by_cap = max(1, int(cap // max(1.0, estimated_premium * lot_size)))
                lots = min(lots, max_lots_by_cap)
        broker_qty = lots * lot_size
        opt_symbol = option_contract["tradingsymbol"]
        opt_exchange = option_contract.get("exchange", "NFO")
        opt_product = product or "NRML"  # options usually NRML (overnight) or MIS (intraday)
        # Transaction type comes from the contract (BUY for long, SELL for write)
        opt_side = (option_contract.get("transaction_type") or side).upper()
        ledger_opened = False
        instrument_key = _instrument_key(opt_exchange, opt_symbol, option_contract.get("instrument_token"))
        position_reservation: Optional[Dict[str, Any]] = None
        exit_position_record: Optional[Dict[str, Any]] = None
        execution_tag = _new_execution_tag()
        execution_attempts = 0
        execution_recovered = False

        # Try to fetch live LTP for the option (live mode only — paper estimates)
        fill_price = price or 0.0
        if not paper:
            kite, _ = await get_user_kite(user_id)
            if not kite:
                if execution_broker == "kotak_neo":
                    raise HTTPException(
                        status_code=400,
                        detail="Kotak execution currently still needs Zerodha connected for option LTP and contract selection. Connect Zerodha for data, then Connect Kotak for execution.",
                    )
                raise HTTPException(status_code=400, detail="Live mode is ON but Zerodha is not connected.")
            try:
                ltp_resp = kite.ltp([f"{opt_exchange}:{opt_symbol}"])
                fill_price = float(ltp_resp[f"{opt_exchange}:{opt_symbol}"]["last_price"])
            except Exception as e:
                logger.warning(f"option LTP fetch failed for {opt_symbol}: {e}")
                fill_price = price or 0.0
            if opt_side == "BUY" and fill_price <= 0:
                raise HTTPException(status_code=400, detail=f"Live option LTP unavailable for {opt_symbol}; order blocked.")
            if strategy_id and opt_side == "BUY":
                position_reservation = await _reserve_strategy_position(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    instrument_key=instrument_key,
                    trading_symbol=opt_symbol,
                    exchange=opt_exchange,
                    instrument_token=option_contract.get("instrument_token"),
                    quantity=broker_qty,
                    entry_price=float(fill_price or 0),
                    source=source,
                )
                decision = option_ledger.try_open_position(
                    strategy_id=strategy_id,
                    symbol=opt_symbol,
                    option_type=option_contract.get("option_type") or "",
                    entry_price=float(fill_price or 0),
                    quantity=lots,
                )
                if not decision.accepted:
                    await _cancel_strategy_reservation(position_reservation, decision.reason)
                    logger.info("option BUY dropped strategy=%s reason=%s", strategy_id, decision.reason)
                    raise HTTPException(status_code=409, detail=f"Option entry blocked: {decision.reason}")
                ledger_opened = True
            if strategy_id and opt_side == "SELL":
                exit_position_record = await _open_strategy_position_for_exit(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    instrument_key=instrument_key,
                )
                broker_qty = int(exit_position_record.get("open_quantity") or exit_position_record.get("quantity") or broker_qty)
                lots = max(1, math.ceil(broker_qty / lot_size))
                await _assert_broker_has_position_quantity(user_id, kite, opt_exchange, opt_symbol, broker_qty)
            if opt_side == "BUY" and fill_price and broker_qty * fill_price > settings["max_position_size"]:
                if ledger_opened and strategy_id:
                    option_ledger.release_failed_open(strategy_id)
                await _cancel_strategy_reservation(position_reservation, "max-position-size")
                raise HTTPException(
                    status_code=400,
                    detail=f"Order value INR {broker_qty * fill_price:.0f} exceeds max position size INR {settings['max_position_size']:.0f}. Adjust on Profile.",
                )
            try:
                if execution_broker == "kotak_neo":
                    res = await _place_kotak_order(
                        user_id,
                        trading_symbol=opt_symbol,
                        exchange=opt_exchange,
                        side=opt_side,
                        quantity=broker_qty,
                        order_type=order_type,
                        product=opt_product,
                        price=price,
                        tag=execution_tag,
                    )
                    broker_order_id = res.get("broker_order_id")
                    execution_attempts = int(res.get("attempts") or 1)
                else:
                    res = await _place_kite_order_with_recovery(
                        kite,
                        tradingsymbol=opt_symbol,
                        exchange=opt_exchange,
                        transaction_type=opt_side,
                        quantity=broker_qty,
                        order_type=order_type,
                        product=opt_product,
                        price=price,
                        tag=execution_tag,
                    )
                    if not res.get("ok"):
                        raise RuntimeError(res.get("error") or "Kite order failed")
                    broker_order_id = res.get("order_id")
                    execution_attempts = int(res.get("attempts") or 1)
                    execution_recovered = bool(res.get("recovered"))
            except Exception as e:
                if ledger_opened:
                    option_ledger.release_failed_open(strategy_id)
                await _cancel_strategy_reservation(position_reservation, str(e))
                raise HTTPException(status_code=400, detail=f"Broker rejected order: {e}")
        else:
            # Paper mode: estimate option premium as ~2% of spot (rough proxy)
            spot = option_contract.get("spot") or option_contract.get("atm_strike") or 100.0
            fill_price = price or round(float(spot) * 0.02, 2)
            fill_price = _simulate_paper_fill_price(fill_price, opt_side)
            if strategy_id and opt_side == "BUY":
                position_reservation = await _reserve_strategy_position(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    instrument_key=instrument_key,
                    trading_symbol=opt_symbol,
                    exchange=opt_exchange,
                    instrument_token=option_contract.get("instrument_token"),
                    quantity=broker_qty,
                    entry_price=float(fill_price or 0),
                    source=source,
                )
                decision = option_ledger.try_open_position(
                    strategy_id=strategy_id,
                    symbol=opt_symbol,
                    option_type=option_contract.get("option_type") or "",
                    entry_price=float(fill_price or 0),
                    quantity=lots,
                )
                if not decision.accepted:
                    await _cancel_strategy_reservation(position_reservation, decision.reason)
                    logger.info("option BUY dropped strategy=%s reason=%s", strategy_id, decision.reason)
                    raise HTTPException(status_code=409, detail=f"Option entry blocked: {decision.reason}")
                ledger_opened = True
            if strategy_id and opt_side == "SELL":
                exit_position_record = await _open_strategy_position_for_exit(
                    user_id=user_id,
                    strategy_id=strategy_id,
                    instrument_key=instrument_key,
                )
                broker_qty = int(exit_position_record.get("open_quantity") or exit_position_record.get("quantity") or broker_qty)
                lots = max(1, math.ceil(broker_qty / lot_size))
            broker_order_id = None

        brokerage = _simulate_paper_brokerage(fill_price, broker_qty) if paper else 0.0
        if opt_side == "BUY" and fill_price and broker_qty * fill_price > settings["max_position_size"]:
            if ledger_opened and strategy_id:
                option_ledger.release_failed_open(strategy_id)
            await _cancel_strategy_reservation(position_reservation, "max-position-size")
            raise HTTPException(
                status_code=400,
                detail=f"Order value INR {broker_qty * fill_price:.0f} exceeds max position size INR {settings['max_position_size']:.0f}. Adjust on Profile.",
            )
        doc = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "symbol": opt_symbol,
            "side": opt_side,
            "qty": broker_qty,
            "filled_qty": broker_qty if paper else None,
            "pending_qty": 0 if paper else None,
            "status_message": None,
            "realised_pnl": 0.0,
            "order_type": order_type,
            "price": fill_price,
            "brokerage": brokerage,
            "product": opt_product,
            "status": "COMPLETE" if paper else "OPEN",
            "mode": "paper" if paper else "live",
            "broker": "paper" if paper else execution_broker,
            "broker_order_id": broker_order_id,
            "execution_tag": execution_tag if not paper else None,
            "execution_attempts": execution_attempts,
            "execution_recovered": execution_recovered,
            "source": source,
            "strategy_id": strategy_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            # Option-specific metadata for reporting / UI
            "asset_type": "option",
            "exchange": opt_exchange,
            "underlying": option_contract.get("underlying"),
            "option_type": option_contract.get("option_type"),
            "strike": option_contract.get("strike"),
            "expiry": option_contract.get("expiry"),
            "instrument_token": option_contract.get("instrument_token"),
            "entry_spot": option_contract.get("spot"),
            "lots": lots,
            "lot_size": lot_size,
        }
        await db.orders.insert_one(doc)
        if strategy_id and opt_side == "BUY":
            await _activate_strategy_position(
                position_reservation,
                order_id=doc["id"],
                broker_order_id=broker_order_id,
                average_buy_price=fill_price,
                quantity=broker_qty,
                paper=paper,
            )
            if position_reservation:
                await db.strategy_positions.update_one(
                    {"id": position_reservation["id"], "user_id": user_id},
                    {"$set": {
                        "asset_type": "option",
                        "lot_size": lot_size,
                        "lots": lots,
                        "underlying": option_contract.get("underlying"),
                        "option_type": option_contract.get("option_type"),
                        "strike": option_contract.get("strike"),
                        "expiry": option_contract.get("expiry"),
                        "product": opt_product,
                    }},
                )

        # Paper positions tracking — keyed by option tradingsymbol
        if paper:
            pos = await db.positions.find_one({"user_id": user_id, "symbol": opt_symbol})
            delta = broker_qty if opt_side == "BUY" else -broker_qty
            if pos:
                new_qty = pos["qty"] + delta
                if new_qty == 0:
                    await db.positions.delete_one({"_id": pos["_id"]})
                else:
                    if opt_side == "BUY":
                        avg = (pos["avg_price"] * pos["qty"] + fill_price * broker_qty) / (pos["qty"] + broker_qty) if (pos["qty"] + broker_qty) else fill_price
                    else:
                        avg = pos["avg_price"]
                    await db.positions.update_one(
                        {"_id": pos["_id"]},
                        {"$set": {"qty": new_qty, "avg_price": round(avg, 2)}},
                    )
            else:
                await db.positions.insert_one({
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "symbol": opt_symbol,
                    "qty": delta,
                    "avg_price": fill_price,
                    "created_at": doc["created_at"],
                    "asset_type": "option",
                    "strategy_id": strategy_id,
                })
        if strategy_id and opt_side == "SELL":
            await _mark_strategy_position_exiting(
                exit_position_record,
                exit_order_id=doc["id"],
                exit_broker_order_id=broker_order_id,
            )
            if paper:
                await _close_strategy_position_record(exit_position_record, exit_price=float(fill_price or 0), reason=source)
            option_ledger.close_position(
                strategy_id=strategy_id,
                exit_price=float(fill_price or 0),
                exit_reason=source,
            )
        doc.pop("_id", None)
        doc.pop("user_id", None)
        return doc

    sym = next((s for s in SYMBOLS if s["symbol"] == symbol.upper()), None)
    # ===== KOTAK DIRECT PATH =====
    # For MCX commodities and exact Kotak F&O symbols, QuantG cannot derive
    # price history from the stock watchlist. We still allow controlled manual
    # execution through Kotak Neo using the exact exchange/trading symbol.
    if not sym:
        direct_symbol = symbol.upper().strip()
        if not direct_symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        direct_product = (product or ("NRML" if exchange in {"MCX", "NFO", "BFO", "CDS"} else settings["default_product"] or "MIS")).upper()
        instrument_key = _instrument_key(exchange, direct_symbol, None)
        position_reservation = None
        exit_position_record = None
        broker_order_id = None
        execution_tag = _new_execution_tag()
        execution_attempts = 0
        execution_recovered = False
        fill_price = float(price or 0)
        if strategy_id and side == "BUY":
            position_reservation = await _reserve_strategy_position(
                user_id=user_id,
                strategy_id=strategy_id,
                instrument_key=instrument_key,
                trading_symbol=direct_symbol,
                exchange=exchange,
                instrument_token=None,
                quantity=int(qty or 1),
                entry_price=float(fill_price or 0),
                source=source,
            )
        if strategy_id and side == "SELL":
            exit_position_record = await _open_strategy_position_for_exit(
                user_id=user_id,
                strategy_id=strategy_id,
                instrument_key=instrument_key,
            )
            qty = int(exit_position_record.get("open_quantity") or exit_position_record.get("quantity") or qty or 1)
        if side == "BUY" and fill_price and qty and qty * fill_price > settings["max_position_size"]:
            await _cancel_strategy_reservation(position_reservation, "max-position-size")
            raise HTTPException(
                status_code=400,
                detail=f"Order value INR {qty * fill_price:.0f} exceeds max position size INR {settings['max_position_size']:.0f}. Adjust on Profile.",
            )
        if not paper:
            if execution_broker != "kotak_neo":
                raise HTTPException(
                    status_code=400,
                    detail=f"{exchange} exact-symbol orders are enabled only for Kotak Neo execution in this build.",
                )
            if strategy_id and side == "SELL":
                await _assert_broker_has_position_quantity(user_id, None, exchange, direct_symbol, int(qty or 1))
            try:
                res = await _place_kotak_order(
                    user_id,
                    trading_symbol=direct_symbol,
                    exchange=exchange,
                    side=side,
                    quantity=int(qty or 1),
                    order_type=order_type,
                    product=direct_product,
                    price=price,
                    tag=execution_tag,
                )
                broker_order_id = res.get("broker_order_id")
                execution_attempts = int(res.get("attempts") or 1)
            except Exception as e:
                await _cancel_strategy_reservation(position_reservation, str(e))
                raise HTTPException(status_code=400, detail=f"Broker rejected order: {e}")
        else:
            fill_price = price if order_type == "LIMIT" and price else _simulate_paper_fill_price(fill_price or 1.0, side)

        doc = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "symbol": direct_symbol,
            "side": side,
            "qty": int(qty or 1),
            "filled_qty": int(qty or 1) if paper else None,
            "pending_qty": 0 if paper else None,
            "status_message": "Exact Kotak symbol order" if exchange == "MCX" else None,
            "realised_pnl": 0.0,
            "order_type": order_type,
            "price": fill_price,
            "brokerage": _simulate_paper_brokerage(fill_price, int(qty or 1)) if paper else 0.0,
            "product": direct_product,
            "status": "COMPLETE" if paper else "OPEN",
            "mode": "paper" if paper else "live",
            "broker": "paper" if paper else execution_broker,
            "broker_order_id": broker_order_id,
            "execution_tag": execution_tag if not paper else None,
            "execution_attempts": execution_attempts,
            "execution_recovered": execution_recovered,
            "source": source,
            "strategy_id": strategy_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "asset_type": "commodity" if exchange == "MCX" else "external",
            "exchange": exchange,
        }
        await db.orders.insert_one(doc)
        if strategy_id and side == "BUY":
            await _activate_strategy_position(
                position_reservation,
                order_id=doc["id"],
                broker_order_id=broker_order_id,
                average_buy_price=fill_price,
                quantity=int(qty or 1),
                paper=paper,
            )
            if position_reservation:
                await db.strategy_positions.update_one(
                    {"id": position_reservation["id"], "user_id": user_id},
                    {"$set": {"asset_type": doc["asset_type"], "product": direct_product}},
                )
        if strategy_id and side == "SELL":
            await _mark_strategy_position_exiting(exit_position_record, exit_order_id=doc["id"], exit_broker_order_id=broker_order_id)
            if paper:
                await _close_strategy_position_record(exit_position_record, exit_price=float(fill_price or 0), reason=source)
        if paper:
            pos = await db.positions.find_one({"user_id": user_id, "symbol": direct_symbol})
            delta = int(qty or 1) if side == "BUY" else -int(qty or 1)
            if pos:
                new_qty = pos["qty"] + delta
                if new_qty == 0:
                    await db.positions.delete_one({"_id": pos["_id"]})
                else:
                    avg = (
                        (pos["avg_price"] * pos["qty"] + fill_price * int(qty or 1)) / (pos["qty"] + int(qty or 1))
                        if side == "BUY" and (pos["qty"] + int(qty or 1))
                        else pos["avg_price"]
                    )
                    await db.positions.update_one({"_id": pos["_id"]}, {"$set": {"qty": new_qty, "avg_price": round(avg, 2)}})
            else:
                await db.positions.insert_one({
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "symbol": direct_symbol,
                    "qty": delta,
                    "avg_price": fill_price,
                    "created_at": doc["created_at"],
                    "asset_type": doc["asset_type"],
                    "exchange": exchange,
                    "strategy_id": strategy_id,
                })
        doc.pop("_id", None)
        doc.pop("user_id", None)
        return doc

    # ===== EQUITY PATH (existing behaviour) =====
    if not sym:
        raise HTTPException(status_code=400, detail="Unknown symbol")
    equity_exchange = exchange if exchange in {"NSE", "BSE"} else "NSE"
    product = product or settings["default_product"] or "MIS"

    # Risk guard: max position size
    live_ltp = await _current_ltp_for_symbol(user_id, symbol.upper(), equity_exchange, allow_mock=False) if not paper else None
    if not paper and side == "BUY" and live_ltp is None:
        raise HTTPException(status_code=400, detail=f"Live LTP unavailable for {symbol.upper()}; order blocked.")
    fill_price_hint = price or live_ltp or live_price(sym["base"], SYMBOLS.index(sym))["price"]
    if qty is None and strategy_id:
        cap = float(settings.get("per_strategy_capital") or settings.get("max_position_size") or 0)
        qty = max(1, int(cap // fill_price_hint)) if cap > 0 and fill_price_hint > 0 else settings["default_qty"]
    qty = int(qty or settings["default_qty"] or 1)
    if side == "BUY" and qty * fill_price_hint > settings["max_position_size"]:
        raise HTTPException(status_code=400,
            detail=f"Order value INR {qty * fill_price_hint:.0f} exceeds max position size INR {settings['max_position_size']:.0f}. Adjust on Profile.")

    broker_order_id = None
    ledger_opened = False
    instrument_key = _instrument_key(equity_exchange, symbol.upper(), None)
    position_reservation = None
    exit_position_record = None
    execution_tag = _new_execution_tag()
    execution_attempts = 0
    execution_recovered = False
    if strategy_id and side == "BUY":
        position_reservation = await _reserve_strategy_position(
            user_id=user_id,
            strategy_id=strategy_id,
            instrument_key=instrument_key,
            trading_symbol=symbol.upper(),
            exchange=equity_exchange,
            instrument_token=None,
            quantity=qty,
            entry_price=float(fill_price_hint or 0),
            source=source,
        )
        decision = option_ledger.try_open_position(
            strategy_id=strategy_id,
            symbol=symbol.upper(),
            option_type="EQ",
            entry_price=float(fill_price_hint or 0),
            quantity=1,
        )
        if not decision.accepted:
            await _cancel_strategy_reservation(position_reservation, decision.reason)
            logger.info("strategy BUY dropped strategy=%s reason=%s", strategy_id, decision.reason)
            raise HTTPException(status_code=409, detail=f"Strategy entry blocked: {decision.reason}")
        ledger_opened = True
    if strategy_id and side == "SELL":
        exit_position_record = await _open_strategy_position_for_exit(
            user_id=user_id,
            strategy_id=strategy_id,
            instrument_key=instrument_key,
        )
        qty = int(exit_position_record.get("open_quantity") or exit_position_record.get("quantity") or qty)
    if not paper:
        kite, _ = await get_user_kite(user_id)
        if execution_broker == "zerodha" and not kite:
            if ledger_opened:
                option_ledger.release_failed_open(strategy_id)
            raise HTTPException(status_code=400, detail="Live mode is ON but Zerodha is not connected. Connect on Broker Keys or flip to Paper.")
        if strategy_id and side == "SELL":
            await _assert_broker_has_position_quantity(user_id, kite, equity_exchange, symbol.upper(), qty)
        try:
            if execution_broker == "kotak_neo":
                res = await _place_kotak_order(
                    user_id,
                    trading_symbol=symbol.upper(),
                    exchange=equity_exchange,
                    side=side,
                    quantity=qty,
                    order_type=order_type,
                    product=product,
                    price=price,
                    tag=execution_tag,
                )
                broker_order_id = res.get("broker_order_id")
                execution_attempts = int(res.get("attempts") or 1)
            else:
                res = await _place_kite_order_with_recovery(
                    kite,
                    tradingsymbol=symbol.upper(),
                    exchange=equity_exchange,
                    transaction_type=side,
                    quantity=qty,
                    order_type=order_type,
                    product=product,
                    price=price,
                    tag=execution_tag,
                )
                if not res.get("ok"):
                    raise RuntimeError(res.get("error") or "Kite order failed")
                broker_order_id = res.get("order_id")
                execution_attempts = int(res.get("attempts") or 1)
                execution_recovered = bool(res.get("recovered"))
        except Exception as e:
            if ledger_opened:
                option_ledger.release_failed_open(strategy_id)
            await _cancel_strategy_reservation(position_reservation, str(e))
            raise HTTPException(status_code=400, detail=f"Broker rejected order: {e}")
        fill_price = fill_price_hint  # actual fill comes via Kite later
    else:
        fill_price = price if order_type == "LIMIT" and price else _simulate_paper_fill_price(fill_price_hint, side)

    brokerage = _simulate_paper_brokerage(fill_price, qty)
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "symbol": symbol.upper(),
        "side": side,
        "qty": qty,
        "filled_qty": qty if paper else None,
        "pending_qty": 0 if paper else None,
        "status_message": None,
        "realised_pnl": 0.0,
        "order_type": order_type,
        "price": fill_price,
        "brokerage": brokerage,
        "product": product,
        "status": "COMPLETE" if paper else "OPEN",
        "mode": "paper" if paper else "live",
        "broker": "paper" if paper else execution_broker,
        "broker_order_id": broker_order_id,
        "execution_tag": execution_tag if not paper else None,
        "execution_attempts": execution_attempts,
        "execution_recovered": execution_recovered,
        "source": source,
        "strategy_id": strategy_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "exchange": equity_exchange,
    }
    await db.orders.insert_one(doc)
    if strategy_id and side == "BUY":
        await _activate_strategy_position(
            position_reservation,
            order_id=doc["id"],
            broker_order_id=broker_order_id,
            average_buy_price=fill_price,
            quantity=qty,
            paper=paper,
        )
        if position_reservation:
            await db.strategy_positions.update_one(
                {"id": position_reservation["id"], "user_id": user_id},
                {"$set": {"asset_type": "equity", "product": product}},
            )

    # Update local paper positions only when paper-mode
    if paper:
        pos = await db.positions.find_one({"user_id": user_id, "symbol": doc["symbol"]})
        delta = qty if side == "BUY" else -qty
        if pos:
            new_qty = pos["qty"] + delta
            if new_qty == 0:
                await db.positions.delete_one({"_id": pos["_id"]})
            else:
                if side == "BUY":
                    avg = (pos["avg_price"] * pos["qty"] + fill_price * qty) / (pos["qty"] + qty) if (pos["qty"] + qty) else fill_price
                else:
                    avg = pos["avg_price"]
                await db.positions.update_one(
                    {"_id": pos["_id"]},
                    {"$set": {"qty": new_qty, "avg_price": round(avg, 2)}},
                )
        else:
            await db.positions.insert_one({
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "symbol": doc["symbol"],
                "qty": delta,
                "avg_price": fill_price,
                "created_at": doc["created_at"],
                "strategy_id": strategy_id,
            })
    if strategy_id and side == "SELL":
        await _mark_strategy_position_exiting(exit_position_record, exit_order_id=doc["id"], exit_broker_order_id=broker_order_id)
        if paper:
            await _close_strategy_position_record(exit_position_record, exit_price=float(fill_price or 0), reason=source)
        option_ledger.close_position(
            strategy_id=strategy_id,
            exit_price=float(fill_price or 0),
            exit_reason=source,
        )
    doc.pop("_id", None)
    doc.pop("user_id", None)
    return doc


@api.post("/orders")
async def place_order(req: OrderReq, user=Depends(get_current_user)):
    return await _place_order_core(
        user_id=user["id"], symbol=req.symbol, side=req.side, qty=req.qty,
        order_type=req.order_type, price=req.price, product=req.product, source="manual",
        exchange=req.exchange,
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
                await db.orders.insert_one({
                    "id": str(uuid.uuid4()),
                    "user_id": user["id"],
                    "symbol": symbol,
                    "side": side,
                    "qty": abs_qty,
                    "filled_qty": abs_qty,
                    "pending_qty": 0,
                    "order_type": "MARKET",
                    "price": float(p.get("ltp") or p.get("avg_price") or 0),
                    "product": p.get("product") or settings.get("default_product", "MIS"),
                    "status": "COMPLETE",
                    "mode": "paper",
                    "source": "squareoff-all",
                    "created_at": now,
                    "realised_pnl": float(p.get("pnl") or 0),
                })
                await db.positions.delete_one({"user_id": user["id"], "symbol": symbol})
                closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "paper"})
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
        "status": o.get("status"),
        "status_message": o.get("status_message"),
        "mode": "live",
        "source": "broker",
        "created_at": str(o.get("order_timestamp")) if o.get("order_timestamp") else None,
    }


async def _sync_kite_order_statuses(user_id: str, kite) -> Dict[str, int]:
    """Mirror Kite order statuses into local rows with broker_order_id."""
    if not kite:
        return {"checked": 0, "updated": 0}
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
        set_doc = {
            "status": status,
            "filled_qty": o.get("filled_quantity"),
            "pending_qty": o.get("pending_quantity"),
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
        if status == "COMPLETE":
            pos_set = {
                "status": "OPEN",
                "average_buy_price": avg_price or set_doc.get("price") or 0,
                "updated_at": now,
            }
            filled_qty = int(o.get("filled_quantity") or 0)
            if filled_qty > 0:
                pos_set["quantity"] = filled_qty
                pos_set["open_quantity"] = filled_qty
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": broker_order_id, "status": {"$in": ["RESERVED", "PENDING_OPEN", "OPEN"]}},
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
        elif status in {"CANCELLED", "REJECTED"}:
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": broker_order_id, "status": {"$in": ["RESERVED", "PENDING_OPEN", "OPEN"]}},
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
        "status": {"$in": ["OPEN", "PENDING", "TRIGGER PENDING", "MODIFY PENDING", "VALIDATION PENDING"]},
        "broker": {"$in": ["zerodha", None]},
    }, {"_id": 0, "id": 1, "broker_order_id": 1, "created_at": 1, "strategy_id": 1}).to_list(500)
    for row in rows:
        broker_order_id = row.get("broker_order_id")
        status = broker_status.get(broker_order_id)
        if status in {"COMPLETE", "CANCELLED", "REJECTED"}:
            res = await db.orders.update_one(
                {"user_id": user_id, "id": row["id"]},
                {"$set": {"status": status, "updated_at": now.isoformat()}},
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
                    "status": stale_status,
                    "status_message": message,
                    "visibility": "hidden",
                    "updated_at": now.isoformat(),
                }},
            )
            missing_fixed += res.modified_count
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_order_id": row["id"], "status": {"$in": ["RESERVED", "PENDING_OPEN", "OPEN"]}},
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
    updates = gateway.latest_orders()
    report_count = 0
    if gateway.status().get("authenticated"):
        report = await asyncio.to_thread(gateway.order_report)
        if report.get("ok"):
            for item in _kotak_order_items(report.get("response")):
                order_id = _extract_kotak_order_id(item)
                if order_id:
                    updates[str(order_id)] = {
                        "order_id": str(order_id),
                        "status": _kotak_first(item, ["ordSt", "status", "ordStatus", "orderStatus", "order_status"]),
                        "filled_qty": _kotak_first(item, ["fldQty", "filledQty", "filled_quantity", "filledQuantity"]),
                        "pending_qty": _kotak_first(item, ["unfilledQty", "pendingQty", "pending_quantity", "remainingQty"]),
                        "average_price": _kotak_first(item, ["avgPrc", "averagePrice", "average_price", "avg_price"]),
                        "raw": item,
                    }
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
            set_doc["status"] = status
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
        if status == "COMPLETE":
            avg_price = float(set_doc.get("price") or 0)
            filled_qty = set_doc.get("filled_qty")
            pos_set = {"status": "OPEN", "updated_at": now}
            if avg_price > 0:
                pos_set["average_buy_price"] = avg_price
            if filled_qty not in (None, ""):
                pos_set["quantity"] = int(filled_qty)
                pos_set["open_quantity"] = int(filled_qty)
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": str(order_id), "status": {"$in": ["RESERVED", "PENDING_OPEN", "OPEN"]}},
                {"$set": pos_set},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": str(order_id), "status": "EXITING"},
                {"_id": 0},
            ).to_list(20)
            for pos in exit_positions:
                await _close_strategy_position_record(pos, exit_price=avg_price or float(pos.get("average_buy_price") or 0), reason="broker-exit-complete")
        elif status in {"CANCELLED", "REJECTED"}:
            await db.strategy_positions.update_many(
                {"user_id": user_id, "entry_broker_order_id": str(order_id), "status": {"$in": ["RESERVED", "PENDING_OPEN", "OPEN"]}},
                {"$set": {"status": status, "updated_at": now, "broker_status_message": row.get("status")},
                 "$unset": {"active_instrument_key": "", "active_strategy_key": ""}},
            )
            exit_positions = await db.strategy_positions.find(
                {"user_id": user_id, "exit_broker_order_id": str(order_id), "status": "EXITING"},
                {"_id": 0, "id": 1},
            ).to_list(20)
            for pos in exit_positions:
                await _reopen_strategy_position_after_exit_reject(pos["id"], user_id, status)
    return {"checked": len(updates), "updated": updated, "report_checked": report_count}


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
            for p in _kotak_position_items(result.get("response")):
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
    return out


async def _sync_strategy_positions_with_broker(user_id: str, kite=None) -> Dict[str, int]:
    gateway = _KOTAK_GATEWAYS.get(user_id)
    has_kotak = bool(gateway and gateway.status().get("authenticated"))
    if not kite and not has_kotak:
        return {"checked": 0, "broker_positions": 0, "marked_broker_not_found": 0, "reason": "no_broker_connected"}
    broker_positions = await _live_broker_position_symbols(user_id, kite)
    rows = await db.strategy_positions.find({
        "user_id": user_id,
        "mode": "live",
        "status": {"$in": ["OPEN", "EXITING"]},
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


@api.get("/positions")
async def list_positions(user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    kite, _ = await get_user_kite(user["id"])
    # Live mode + connected: prefer real positions; cache them for fallback
    if kite and not settings.get("paper_mode", True):
        data = kite_helper.safe_positions(kite)
        if data and data.get("net") is not None:
            out = []
            for p in data["net"]:
                if not p.get("quantity"):
                    continue
                out.append({
                    "symbol": p.get("tradingsymbol"),
                    "qty": p.get("quantity"),
                    "avg_price": round(float(p.get("average_price") or 0), 2),
                    "ltp": round(float(p.get("last_price") or 0), 2),
                    "pnl": round(float(p.get("pnl") or 0), 2),
                    "product": p.get("product"),
                    "exchange": p.get("exchange") or ("NFO" if p.get("tradingsymbol", "").endswith(("CE", "PE")) else "NSE"),
                    "mode": "live",
                })
            # Cache for stale-fallback
            try:
                await db.kite_positions_cache.update_one(
                    {"user_id": user["id"]},
                    {"$set": {"user_id": user["id"], "positions": out,
                              "cached_at": datetime.now(timezone.utc).isoformat()}},
                    upsert=True,
                )
            except Exception:
                pass
            return out
        # Kite call failed — try cached snapshot
        cached = await db.kite_positions_cache.find_one({"user_id": user["id"]}, {"_id": 0})
        if cached:
            return [{**p, "stale": True, "cached_at": cached.get("cached_at")} for p in cached.get("positions", [])]
    if not settings.get("paper_mode", True) and settings.get("execution_broker") == "kotak_neo":
        gateway = _KOTAK_GATEWAYS.get(user["id"])
        if gateway and gateway.status().get("authenticated"):
            result = await asyncio.to_thread(gateway.positions)
            if result.get("ok"):
                out = []
                for p in _kotak_position_items(result.get("response")):
                    qty = _kotak_first(p, ["netQty", "net_quantity", "quantity", "qty"])
                    try:
                        qty_i = int(float(qty or 0))
                    except Exception:
                        qty_i = 0
                    if qty_i == 0:
                        continue
                    avg = float(_kotak_first(p, ["avgPrc", "averagePrice", "average_price", "avg_price"]) or 0)
                    ltp = float(_kotak_first(p, ["ltp", "last_price", "lastTradedPrice"]) or avg or 0)
                    out.append({
                        "symbol": _kotak_first(p, ["trdSym", "trading_symbol", "tradingSymbol", "symbol"]) or "-",
                        "qty": qty_i,
                        "avg_price": round(avg, 2),
                        "ltp": round(ltp, 2),
                        "pnl": round(float(_kotak_first(p, ["pnl", "unrealisedPnl", "unrealized_pnl"]) or ((ltp - avg) * qty_i)), 2),
                        "product": _kotak_first(p, ["prod", "product"]),
                        "exchange": _kotak_first(p, ["exSeg", "exchange_segment", "exchange"]) or "MCX",
                        "mode": "live",
                        "broker": "kotak_neo",
                    })
                if out:
                    return out
    # Paper / fallback
    rows = await db.positions.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(200)
    out = []
    for r in rows:
        sym = next((s for s in SYMBOLS if s["symbol"] == r["symbol"]), None)
        ltp = live_price(sym["base"], SYMBOLS.index(sym))["price"] if sym else r["avg_price"]
        pnl = round((ltp - r["avg_price"]) * r["qty"], 2)
        out.append({**r, "ltp": ltp, "pnl": pnl, "mode": "paper"})
    return out


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
    trades_used = len([o for o in orders if o.get("status") not in {"REJECTED", "CANCELLED"}])
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
        "market_open": _is_nse_market_open(),
    }


@api.get("/trade-journal")
async def trade_journal(user=Depends(get_current_user)):
    rows = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    completed = [r for r in rows if r.get("status") == "COMPLETE"]
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
        completed = [o for o in live_orders if o.get("status") == "COMPLETE"]
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
            "is_open": _is_nse_market_open(),
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
    api_key = decrypt_secret(keys.get("api_key")) if keys else None
    return upstox_helper.status_from_keys(keys, api_key)


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
    required_brokers = {b for b in (data_broker, execution_broker) if b in {"zerodha", "kotak_neo"}}
    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "zerodha"})
    kotak_status = await get_user_kotak_status(user["id"])
    zerodha_required = "zerodha" in required_brokers
    kotak_required = "kotak_neo" in required_brokers
    required_keys_ok = (not zerodha_required or bool(keys)) and (not kotak_required or bool(kotak_status.get("keys_saved")))
    checks.append({
        "id": "broker_keys",
        "label": "Required broker credentials saved",
        "ok": required_keys_ok,
        "hint": "Save on Broker Keys → Step 1" if not keys else None,
    })
    kite, status = await get_user_kite(user["id"])
    checks[-1]["hint"] = "Save required broker credentials on Broker Keys" if not required_keys_ok else None
    required_sessions_ok = (not zerodha_required or bool(status.get("connected"))) and (not kotak_required or bool(kotak_status.get("connected")))
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
    # NSE market hours: 9:15 AM – 3:30 PM IST, Mon–Fri
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    is_weekday = ist_now.weekday() < 5
    minutes_now = ist_now.hour * 60 + ist_now.minute
    market_open = is_weekday and (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)
    checks.append({
        "id": "market_hours",
        "label": "NSE market open",
        "ok": market_open,
        "detail": ist_now.strftime("%a %H:%M IST"),
        "hint": "Market trades 09:15 – 15:30 IST, Mon–Fri" if not market_open else None,
    })
    tick_manager = getattr(app.state, "tick_manager", None)
    tick_status = tick_manager.status_info(user["id"]) if tick_manager else {"connected": False}
    tick_auth_failed = bool(tick_status.get("auth_failed"))
    kotak_gateway_status = kotak_status.get("gateway") or {}
    selected_tick_ok = (
        bool((tick_status.get("connected") or tick_status.get("connecting")) and not tick_auth_failed)
        if data_broker == "zerodha"
        else bool(kotak_gateway_status.get("authenticated") and kotak_gateway_status.get("ticks", 0) > 0)
    )
    checks.append({
        "id": "tick_feed",
        "label": "Realtime selected tick feed",
        "ok": selected_tick_ok,
        "detail": (
            "auth failed; reconnect Zerodha"
            if tick_auth_failed
            else
            f"connected, last tick {tick_status.get('last_tick_at')}"
            if tick_status.get("connected")
            else "connecting"
            if tick_status.get("connecting")
            else tick_status.get("last_error") or "not connected"
        ),
        "hint": (
            "Click Connect to Zerodha on Broker Keys to create a fresh Kite session."
            if tick_auth_failed
            else "Fetch a strategy or watchlist with a live Kite session to start the websocket feed."
            if not tick_status.get("connected")
            else None
        ),
    })
    # Note: "trading mode" is intentionally NOT a check — clicking confirm in the
    # pre-flight modal IS the action that flips paper→live. Including it as a
    # check creates a circular dependency the user can never resolve.
    overall_ready = all(c["ok"] for c in checks if c["id"] not in {"market_hours", "tick_feed"})
    # Warnings (not blockers)
    return {
        "ready": overall_ready,
        "market_open": market_open,
        "current_mode": "PAPER" if settings.get("paper_mode", True) else "LIVE",
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

    if mode_live and not market_open:
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
        if check.get("id") in {"market_hours", "tick_feed"}:
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


@api.get("/ops/diagnostics")
async def ops_diagnostics(user=Depends(get_current_user)):
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
    orders_open = await db.orders.count_documents({"user_id": user["id"], "status": {"$in": ["OPEN", "PENDING"]}})
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


@api.post("/ops/ticker/restart")
async def ops_restart_ticker(req: OpsActionReq = None, user=Depends(get_current_user)):
    settings = await get_user_settings(user["id"])
    if settings.get("data_broker") == "kotak_neo":
        return await _start_user_kotak_ticker(user["id"])
    tick_manager = getattr(app.state, "tick_manager", None)
    if tick_manager:
        try:
            ticker = tick_manager._tickers.get(user["id"])
            if ticker:
                ticker.stop()
        except Exception as e:
            logger.warning(f"ticker stop from ops failed: {e}")
    return await _start_user_ticker(user["id"])


@api.post("/market/kotak-ticker/start")
async def market_kotak_ticker_start(req: OpsActionReq = None, user=Depends(get_current_user)):
    return await _start_user_kotak_ticker(user["id"])


@api.post("/ops/orders/sync")
async def ops_sync_orders(req: OpsActionReq = None, user=Depends(get_current_user)):
    kite, status = await get_user_kite(user["id"])
    _ORDER_SYNC_CACHE.pop(user["id"], None)
    sync = await _sync_kite_order_statuses(user["id"], kite) if kite else {"checked": 0, "updated": 0, "reason": status.get("reason", "zerodha_not_connected")}
    stale = await _stale_local_open_orders(user["id"], kite) if kite else {"fixed": 0, "reason": status.get("reason", "zerodha_not_connected")}
    kotak_sync = await _sync_kotak_order_statuses(user["id"])
    position_sync = await _sync_strategy_positions_with_broker(user["id"], kite)
    return {"ok": True, "sync": sync, "stale": stale, "kotak_sync": kotak_sync, "position_sync": position_sync}


@api.post("/ops/auto-recover")
async def ops_auto_recover(req: OpsActionReq = None, user=Depends(get_current_user)):
    actions: List[Dict[str, Any]] = []
    kite, kite_status = await get_user_kite(user["id"])
    if kite:
        _ORDER_SYNC_CACHE.pop(user["id"], None)
        actions.append({"name": "zerodha_order_sync", "result": await _sync_kite_order_statuses(user["id"], kite)})
        actions.append({"name": "stale_order_repair", "result": await _stale_local_open_orders(user["id"], kite)})
        actions.append({"name": "strategy_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"], kite)})
        ticker_result = await _start_user_ticker(user["id"])
        actions.append({"name": "zerodha_ticker_restart", "result": ticker_result})
    else:
        actions.append({"name": "zerodha_session", "skipped": True, "reason": kite_status.get("reason", "not_connected")})

    kotak_gateway = _KOTAK_GATEWAYS.get(user["id"])
    if kotak_gateway and kotak_gateway.status().get("authenticated"):
        actions.append({"name": "kotak_order_sync", "result": await _sync_kotak_order_statuses(user["id"])})
        actions.append({"name": "kotak_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"], kite)})
        order_feed = await asyncio.to_thread(kotak_gateway.subscribe_order_feed)
        actions.append({"name": "kotak_order_feed", "result": order_feed})
        market_feed = await _start_user_kotak_ticker(user["id"])
        actions.append({"name": "kotak_market_ticker", "result": market_feed})
    else:
        actions.append({"name": "kotak_order_feed", "skipped": True, "reason": "not_connected"})

    diagnostics = await ops_diagnostics(user=user)
    return {"ok": True, "actions": actions, "recovery_plan": diagnostics.get("recovery_plan")}


@api.post("/ops/emergency-stop")
async def ops_emergency_stop(req: OpsActionReq = None, user=Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "id": 1}).to_list(500)
    for row in strategies:
        option_ledger.set_kill_switch(True, strategy_id=row["id"])
    await db.users.update_one({"id": user["id"]}, {"$set": {"paper_mode": True, "ops_last_emergency_stop_at": now}})
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": f"Emergency stop at {now}: switched to PAPER and paused automation."}},
    )
    return {"ok": True, "paper_mode": True, "paused_strategies": res.modified_count, "disabled_strategies": len(strategies), "at": now}


@api.post("/ops/strategies/pause-all")
async def ops_pause_all(req: OpsActionReq = None, user=Depends(get_current_user)):
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": None}},
    )
    return {"ok": True, "paused_strategies": res.modified_count}


@api.post("/ops/strategies/enable-all")
async def ops_enable_all_strategies(req: OpsActionReq = None, user=Depends(get_current_user)):
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).to_list(500)
    for row in rows:
        _sync_option_ledger_strategy(row)
        option_ledger.set_kill_switch(False, strategy_id=row["id"])
    res = await db.strategies.update_many(
        {"user_id": user["id"]},
        {"$set": {"status": "live"}, "$unset": {"last_error": "", "last_signal_validation": ""}},
    )
    return {"ok": True, "enabled_strategies": res.modified_count, "ledger_enabled": len(rows)}


@api.post("/ops/strategies/clear-errors")
async def ops_clear_strategy_errors(req: OpsActionReq = None, user=Depends(get_current_user)):
    res = await db.strategies.update_many(
        {"user_id": user["id"]},
        {"$unset": {"last_error": "", "last_signal_validation": ""}},
    )
    return {"ok": True, "updated_strategies": res.modified_count}


# ============== Routes: Ops Console - END ==============

# ============== Routes: Funds ==============
@api.get("/funds")
async def funds(user=Depends(get_current_user)):
    """Return broker funds & margins when live, otherwise a paper-money snapshot."""
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
    }


@api.post("/kotak/login")
async def kotak_login(user=Depends(get_current_user)):
    existing = _KOTAK_GATEWAYS.get(user["id"])
    existing_status = existing.status() if existing else {}
    gateway = await get_user_kotak_gateway(
        user["id"],
        fresh=bool(existing_status and not existing_status.get("initialized")),
    )
    if not gateway:
        raise HTTPException(status_code=400, detail="Save Kotak Neo Consumer Key, Consumer Secret, mobile, MPIN/password, and TOTP secret first.")
    status = gateway.status()
    result = await asyncio.to_thread(gateway.reconnect if status.get("state") in {"READY", "FAILED"} else gateway.authenticate)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak login failed"))
    return {"ok": True, "status": gateway.status()}


@api.post("/kotak/reconnect")
async def kotak_reconnect(user=Depends(get_current_user)):
    gateway = await get_user_kotak_gateway(user["id"])
    if not gateway:
        raise HTTPException(status_code=400, detail="Kotak Neo is not configured.")
    result = await asyncio.to_thread(gateway.reconnect)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Kotak reconnect failed"))
    return {"ok": True, "status": gateway.status()}


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


@api.get("/upstox/status")
async def upstox_status(user=Depends(get_current_user)):
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

    zerodha_last = zerodha_tick.get("last_tick_at")
    kotak_last = kotak_gateway.get("last_tick_at")
    zerodha_age = _age_ms(zerodha_last)
    kotak_age = _age_ms(kotak_last)
    zerodha_healthy = bool(zerodha_tick.get("connected") and zerodha_last and (zerodha_age is None or zerodha_age < 15000))
    kotak_healthy = bool(kotak_gateway.get("authenticated") and kotak_gateway.get("ticks", 0) > 0 and kotak_last and (kotak_age is None or kotak_age < 15000))

    recommended = settings.get("data_broker", "zerodha")
    reason = "Keep configured data broker until a healthier live feed is observed."
    if zerodha_healthy and kotak_healthy:
        if kotak_age is not None and zerodha_age is not None and kotak_age + 250 < zerodha_age:
            recommended, reason = "kotak_neo", "Kotak ticks are fresher right now."
        else:
            recommended, reason = "zerodha", "Zerodha ticks are as fresh or fresher right now."
    elif kotak_healthy:
        recommended, reason = "kotak_neo", "Kotak has fresh ticks and Zerodha is stale/unavailable."
    elif zerodha_healthy:
        recommended, reason = "zerodha", "Zerodha has fresh ticks and Kotak is stale/unavailable."

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
    }


@api.post("/market/auto-data-broker")
async def market_auto_data_broker(user=Depends(get_current_user)):
    comparison = await market_feed_comparison(user=user)
    broker = comparison.get("recommended_data_broker")
    if broker not in {"zerodha", "kotak_neo"}:
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
            for user_row in users:
                user_id = user_row["id"]
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
        except Exception as e:
            logger.warning(f"broker reconciliation error: {e}")
        slept = 0
        while not stop_event.is_set() and slept < interval:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Broker reconciliation loop stopped")


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
    # Index creation is best-effort — must NEVER block app startup.
    # On Atlas, an index may already exist with different options, or there may be
    # duplicates from a previous app version. We log and continue.
    indexes = [
        ("users", "email", {"unique": True}),
        ("broker_keys", [("user_id", 1), ("broker", 1)], {"unique": True}),
        ("strategies", "user_id", {}),
        ("orders", [("user_id", 1), ("created_at", -1)], {}),
        ("strategy_positions", [("user_id", 1), ("strategy_id", 1), ("status", 1)], {}),
        ("strategy_positions", "active_instrument_key", {"unique": True, "sparse": True}),
        ("strategy_positions", "active_strategy_key", {"unique": True, "sparse": True}),
        ("positions", [("user_id", 1), ("symbol", 1)], {"unique": True}),
        ("paper_trading_history", [("user_id", 1), ("created_at", -1)], {}),
    ]
    for coll, key, opts in indexes:
        try:
            await db[coll].create_index(key, **opts)
        except Exception as e:
            logger.warning(f"index create on {coll} skipped: {e}")

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
    async def _price_history(user_id: str, symbol: str, days: int = 60):
        settings = await get_user_settings(user_id)
        return await _fetch_strategy_history(
            user_id,
            symbol,
            days=days,
            interval="5minute",
            allow_mock=bool(settings.get("paper_mode", True)),
        ) | {"paper_mode": bool(settings.get("paper_mode", True))}

    # Resolver for index option contracts — runner uses this when a strategy
    # has visual_config.options.enabled. Requires a live Kite session.
    async def _resolve_option(user_id: str, underlying: str, signal_action: str,
                              strike_mode: str, otm_points: int = 0,
                              expiry_offset: int = 0):
        kite, _ = await get_user_kite(user_id)
        if not kite:
            return None
        return options_helper.resolve_for_signal(
            kite, underlying=underlying, signal_action=signal_action,
            strike_mode=strike_mode, otm_points=otm_points,
            expiry_offset_weeks=expiry_offset,
        )

    app.state.tick_manager = RealtimeTickManager()
    app.state.kotak_gateways = _KOTAK_GATEWAYS
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
