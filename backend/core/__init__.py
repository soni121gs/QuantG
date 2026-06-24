# QuantG Core Architecture Package

from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from core_legacy import (
    db,
    client,
    get_current_user as _legacy_get_current_user,
    get_user_from_token,
    encrypt_secret,
    decrypt_secret,
    hash_password,
    verify_password,
    create_token,
    bearer,
    UserOut,
    TokenOut,
    BrokerKeyReq,
    BrokerKeyOut,
    StrategyRuntimeSettingsReq,
)


async def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    user = await _legacy_get_current_user(creds)
    if not user.get("approved", True) and user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Your registration is pending approval by the owner.")
    return user
