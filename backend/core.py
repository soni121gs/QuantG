from __future__ import annotations

import os
import re
import uuid
import base64
import hashlib
import logging
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr

# Logger
logger = logging.getLogger("quantdesk.core")

# Mongo Setup
mongo_url = os.environ.get('MONGO_URL') or "mongodb://localhost:27017"
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME') or "quantg"]

# JWT & Authentication Constants
JWT_SECRET = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY") or "quantg-development-secret"
if len(JWT_SECRET.encode()) < 32:
    JWT_SECRET = hashlib.sha256(JWT_SECRET.encode()).hexdigest()
JWT_ALG = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24 * 7  # 7 days

bearer = HTTPBearer(auto_error=False)

# ============== Cryptography Helpers ==============
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
    val_str = str(value)
    if len(val_str) <= head + tail:
        return "****"
    return val_str[:head] + "...." + val_str[-tail:]


# ============== Authentication Helpers ==============
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


# ============== Common Pydantic Schemas ==============
class UserOut(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: str
    created_at: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class BrokerKeyReq(BaseModel):
    broker: Optional[str] = "zerodha"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    user_id_at_broker: Optional[str] = None
    mobile_number: Optional[str] = None
    mpin: Optional[str] = None
    totp_secret_key: Optional[str] = None
    redirect_uri: Optional[str] = None


class BrokerKeyOut(BaseModel):
    id: str
    broker: str
    api_key_masked: str
    user_id_at_broker: Optional[str] = None
    created_at: str


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
