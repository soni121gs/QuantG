from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from core import (
    db,
    hash_password,
    verify_password,
    create_token,
    get_current_user,
    UserOut,
    TokenOut,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


class RegisterReq(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class LoginReq(BaseModel):
    email: EmailStr
    password: str


@router.post("/register", response_model=TokenOut)
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
    
    # Avoid circular imports by dynamically importing seed function at runtime
    import sys
    import os
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if backend_dir not in sys.path:
        sys.path.append(backend_dir)
    from server import seed_default_strategies_for_user
    await seed_default_strategies_for_user(user_doc["id"])
    
    token = create_token(user_doc["id"], email)
    return TokenOut(
        access_token=token,
        user=UserOut(
            id=user_doc["id"],
            email=email,
            name=user_doc["name"],
            role="trader",
            created_at=user_doc["created_at"],
        ),
    )


@router.post("/login", response_model=TokenOut)
async def login(req: LoginReq):
    email = req.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    token = create_token(user["id"], email)
    return TokenOut(
        access_token=token,
        user=UserOut(
            id=user["id"],
            email=email,
            name=user.get("name") or "",
            role=user.get("role") or "trader",
            created_at=user["created_at"],
        ),
    )


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return UserOut(
        id=user["id"],
        email=user["email"],
        name=user.get("name") or "",
        role=user.get("role") or "trader",
        created_at=user["created_at"],
    )
