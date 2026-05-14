from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import uuid
import asyncio
import random
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import bcrypt
import jwt
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr
from emergentintegrations.llm.chat import LlmChat, UserMessage

# Mongo
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
JWT_ALG = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24 * 7  # 7 days for trader convenience

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY")

app = FastAPI(title="QuantDesk Algo Trading API")
api = APIRouter(prefix="/api")
bearer = HTTPBearer(auto_error=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quantdesk")


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
    status: str = "draft"  # draft | live | paused


class StrategyOut(BaseModel):
    id: str
    name: str
    description: str
    kind: str
    python_code: Optional[str] = None
    visual_config: Optional[Dict[str, Any]] = None
    status: str
    created_at: str
    last_pnl: Optional[float] = None


class BacktestReq(BaseModel):
    strategy_id: Optional[str] = None
    python_code: Optional[str] = None
    symbol: str = "RELIANCE"
    days: int = 60


class OrderReq(BaseModel):
    symbol: str
    side: str  # BUY | SELL
    qty: int
    order_type: str = "MARKET"  # MARKET | LIMIT
    price: Optional[float] = None
    product: str = "MIS"


class ChatReq(BaseModel):
    session_id: str
    message: str


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
]

# Deterministic-ish live ticks using time-based jitter
def live_price(base: float, seed: int) -> Dict[str, Any]:
    t = datetime.now(timezone.utc).timestamp()
    drift = math.sin(t / 12.0 + seed) * (base * 0.004)
    noise = (random.random() - 0.5) * (base * 0.002)
    price = round(base + drift + noise, 2)
    change = round(drift + noise, 2)
    pct = round((change / base) * 100, 2)
    return {"price": price, "change": change, "pct": pct}


def historical_series(base: float, days: int = 60) -> List[Dict[str, Any]]:
    out = []
    price = base * 0.92
    for i in range(days):
        d = datetime.now(timezone.utc) - timedelta(days=days - i)
        # random walk with slight up drift
        price = price * (1 + (random.random() - 0.48) * 0.02)
        out.append({"date": d.strftime("%Y-%m-%d"), "close": round(price, 2)})
    return out


# ============== Routes: Auth ==============
@api.get("/")
async def health():
    return {"status": "ok", "service": "QuantDesk API"}


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


# ============== Routes: Broker keys ==============
@api.post("/broker/keys", response_model=BrokerKeyOut)
async def save_broker_keys(req: BrokerKeyReq, user=Depends(get_current_user)):
    # upsert per user+broker
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "broker": req.broker,
        "api_key": req.api_key,
        "api_secret": req.api_secret,
        "user_id_at_broker": req.user_id_at_broker,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": req.broker},
        {"$set": doc},
        upsert=True,
    )
    return BrokerKeyOut(
        id=doc["id"],
        broker=doc["broker"],
        api_key_masked=req.api_key[:4] + "•" * max(0, len(req.api_key) - 8) + req.api_key[-4:],
        user_id_at_broker=req.user_id_at_broker,
        created_at=doc["created_at"],
    )


@api.get("/broker/keys", response_model=List[BrokerKeyOut])
async def list_broker_keys(user=Depends(get_current_user)):
    rows = await db.broker_keys.find({"user_id": user["id"]}, {"_id": 0}).to_list(50)
    out = []
    for r in rows:
        k = r["api_key"]
        out.append(BrokerKeyOut(
            id=r["id"], broker=r["broker"],
            api_key_masked=k[:4] + "•" * max(0, len(k) - 8) + k[-4:],
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
async def watchlist():
    out = []
    for i, s in enumerate(SYMBOLS):
        lp = live_price(s["base"], i)
        out.append({"symbol": s["symbol"], "name": s["name"], **lp})
    return out


@api.get("/market/quote/{symbol}")
async def quote(symbol: str):
    found = next((s for s in SYMBOLS if s["symbol"] == symbol.upper()), None)
    if not found:
        raise HTTPException(status_code=404, detail="Symbol not found")
    idx = SYMBOLS.index(found)
    lp = live_price(found["base"], idx)
    return {"symbol": found["symbol"], "name": found["name"], **lp,
            "series": historical_series(found["base"], 60)}


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


@api.post("/strategies", response_model=StrategyOut)
async def create_strategy(req: StrategyReq, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "name": req.name,
        "description": req.description or "",
        "kind": req.kind,
        "python_code": req.python_code or (DEFAULT_PYTHON if req.kind == "python" else None),
        "visual_config": req.visual_config,
        "status": req.status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_pnl": None,
    }
    await db.strategies.insert_one(doc)
    doc.pop("user_id", None)
    return StrategyOut(**doc)


@api.get("/strategies", response_model=List[StrategyOut])
async def list_strategies(user=Depends(get_current_user)):
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    return [StrategyOut(**r) for r in rows]


@api.get("/strategies/{sid}", response_model=StrategyOut)
async def get_strategy(sid: str, user=Depends(get_current_user)):
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyOut(**row)


@api.put("/strategies/{sid}", response_model=StrategyOut)
async def update_strategy(sid: str, req: StrategyReq, user=Depends(get_current_user)):
    update = {k: v for k, v in req.model_dump().items() if v is not None}
    await db.strategies.update_one({"id": sid, "user_id": user["id"]}, {"$set": update})
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return StrategyOut(**row)


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
    return {"status": new_status}


def _safe_run_python(code: str, data: List[dict]) -> List[dict]:
    """Sandbox-ish exec of user's Python code with run(data)->signals."""
    safe_builtins = {
        "len": len, "range": range, "sum": sum, "min": min, "max": max,
        "abs": abs, "round": round, "float": float, "int": int, "str": str,
        "list": list, "dict": dict, "enumerate": enumerate, "zip": zip, "print": print,
    }
    env: Dict[str, Any] = {"__builtins__": safe_builtins}
    exec(code, env, env)
    fn = env.get("run")
    if not callable(fn):
        raise ValueError("Strategy must define a `run(data)` function")
    return fn(data) or []


@api.post("/strategies/backtest")
async def backtest(req: BacktestReq, user=Depends(get_current_user)):
    code = req.python_code
    if not code and req.strategy_id:
        row = await db.strategies.find_one({"id": req.strategy_id, "user_id": user["id"]})
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        code = row.get("python_code")
    if not code:
        code = DEFAULT_PYTHON
    sym = next((s for s in SYMBOLS if s["symbol"] == req.symbol.upper()), SYMBOLS[0])
    data = historical_series(sym["base"], req.days)
    try:
        signals = _safe_run_python(code, data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Strategy error: {e}")

    # Simulate PnL
    cash, position, entry, trades = 100000.0, 0, 0.0, []
    equity_curve = []
    sigmap = {s["date"]: s["action"] for s in signals}
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
    final_equity = equity_curve[-1]["equity"] if equity_curve else 100000.0
    total_pnl = round(final_equity - 100000.0, 2)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2)
    if req.strategy_id:
        await db.strategies.update_one({"id": req.strategy_id}, {"$set": {"last_pnl": total_pnl}})
    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "signals": signals,
        "summary": {
            "starting_capital": 100000,
            "final_equity": final_equity,
            "total_pnl": total_pnl,
            "return_pct": round(total_pnl / 1000.0, 2),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
        },
    }


# ============== Routes: Orders & Positions ==============
@api.post("/orders")
async def place_order(req: OrderReq, user=Depends(get_current_user)):
    sym = next((s for s in SYMBOLS if s["symbol"] == req.symbol.upper()), None)
    if not sym:
        raise HTTPException(status_code=400, detail="Unknown symbol")
    lp = live_price(sym["base"], SYMBOLS.index(sym))
    fill_price = req.price if req.order_type == "LIMIT" and req.price else lp["price"]
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "symbol": req.symbol.upper(),
        "side": req.side.upper(),
        "qty": req.qty,
        "order_type": req.order_type,
        "price": fill_price,
        "product": req.product,
        "status": "COMPLETE",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.orders.insert_one(doc)
    # Update positions
    pos = await db.positions.find_one({"user_id": user["id"], "symbol": doc["symbol"]})
    delta = req.qty if req.side.upper() == "BUY" else -req.qty
    if pos:
        new_qty = pos["qty"] + delta
        if new_qty == 0:
            await db.positions.delete_one({"_id": pos["_id"]})
        else:
            # weighted avg for buys; keep avg on sells
            if req.side.upper() == "BUY":
                avg = (pos["avg_price"] * pos["qty"] + fill_price * req.qty) / (pos["qty"] + req.qty) if (pos["qty"] + req.qty) else fill_price
            else:
                avg = pos["avg_price"]
            await db.positions.update_one(
                {"_id": pos["_id"]},
                {"$set": {"qty": new_qty, "avg_price": round(avg, 2)}},
            )
    else:
        await db.positions.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "symbol": doc["symbol"],
            "qty": delta,
            "avg_price": fill_price,
            "created_at": doc["created_at"],
        })
    doc.pop("_id", None)
    doc.pop("user_id", None)
    return doc


@api.get("/orders")
async def list_orders(user=Depends(get_current_user)):
    rows = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    return rows


@api.get("/positions")
async def list_positions(user=Depends(get_current_user)):
    rows = await db.positions.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(200)
    out = []
    for r in rows:
        sym = next((s for s in SYMBOLS if s["symbol"] == r["symbol"]), None)
        ltp = live_price(sym["base"], SYMBOLS.index(sym))["price"] if sym else r["avg_price"]
        pnl = round((ltp - r["avg_price"]) * r["qty"], 2)
        out.append({**r, "ltp": ltp, "pnl": pnl})
    return out


@api.get("/portfolio")
async def portfolio(user=Depends(get_current_user)):
    positions = await list_positions(user)
    total_pnl = round(sum(p["pnl"] for p in positions), 2)
    deployed = round(sum(abs(p["qty"]) * p["avg_price"] for p in positions), 2)
    orders_count = await db.orders.count_documents({"user_id": user["id"]})
    strategies_count = await db.strategies.count_documents({"user_id": user["id"]})
    live_strategies = await db.strategies.count_documents({"user_id": user["id"], "status": "live"})

    # equity curve from daily aggregated orders (simulate from PnL)
    equity = []
    base = 100000.0
    for i in range(30):
        d = datetime.now(timezone.utc) - timedelta(days=30 - i)
        base = base * (1 + (random.random() - 0.46) * 0.01)
        equity.append({"date": d.strftime("%Y-%m-%d"), "equity": round(base + total_pnl * (i / 30), 2)})

    return {
        "total_pnl": total_pnl,
        "deployed": deployed,
        "available": round(500000.0 - deployed, 2),
        "orders": orders_count,
        "strategies": strategies_count,
        "live_strategies": live_strategies,
        "equity_curve": equity,
    }


# ============== Routes: AI Bot ==============
SYSTEM_PROMPT = (
    "You are QuantBot, an expert algorithmic trading assistant inside the QuantDesk platform. "
    "Help the trader with: strategy ideas, indicator math, Python code for backtests, "
    "risk management, market context for Indian equities (NSE/BSE), and explaining concepts. "
    "Be concise, use bullet points, and when giving code, use Python with a `run(data)` function "
    "where data is a list of {date, close}. NEVER give financial advice as guaranteed; always "
    "remind the user that backtests don't guarantee future returns."
)


@api.post("/ai/chat")
async def ai_chat(req: ChatReq, user=Depends(get_current_user)):
    if not EMERGENT_LLM_KEY:
        raise HTTPException(status_code=500, detail="LLM key not configured")
    # persist user msg
    await db.ai_messages.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "session_id": req.session_id,
        "role": "user",
        "content": req.message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # load prior session for context
    prior = await db.ai_messages.find(
        {"user_id": user["id"], "session_id": req.session_id},
        {"_id": 0},
    ).sort("created_at", 1).to_list(50)

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"{user['id']}-{req.session_id}",
        system_message=SYSTEM_PROMPT,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")

    # Re-feed history except the just-inserted message (we'll send that now)
    for m in prior[:-1]:
        if m["role"] == "user":
            await chat.send_message(UserMessage(text=m["content"]))
            # NOTE: this would double cost; instead we just send the latest message
            break  # we don't actually replay; rely on lib's session

    try:
        reply = await chat.send_message(UserMessage(text=req.message))
    except Exception as e:
        logger.error(f"LLM error: {e}")
        raise HTTPException(status_code=500, detail=f"AI error: {e}")

    await db.ai_messages.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "session_id": req.session_id,
        "role": "assistant",
        "content": reply,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"role": "assistant", "content": reply}


@api.get("/ai/chat/{session_id}")
async def ai_chat_history(session_id: str, user=Depends(get_current_user)):
    rows = await db.ai_messages.find(
        {"user_id": user["id"], "session_id": session_id},
        {"_id": 0},
    ).sort("created_at", 1).to_list(200)
    return rows


# ============== Boot ==============
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.broker_keys.create_index([("user_id", 1), ("broker", 1)], unique=True)
    await db.strategies.create_index("user_id")
    await db.orders.create_index([("user_id", 1), ("created_at", -1)])
    await db.positions.create_index([("user_id", 1), ("symbol", 1)], unique=True)
    await db.ai_messages.create_index([("user_id", 1), ("session_id", 1)])
    logger.info("QuantDesk API started")


@app.on_event("shutdown")
async def shutdown():
    client.close()
