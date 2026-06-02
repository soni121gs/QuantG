"""QuantG Paper Broker — Virtual Trading Wallet.

Each user gets ₹5,00,000 virtual paper capital on first use.
BUY orders debit the wallet; SELL orders credit it back.
The wallet is independent of the real Upstox account.

Hard rules:
- Paper wallet NEVER touches live Upstox funds.
- If insufficient paper balance, the BUY is rejected (no over-leverage).
- Reset via /api/profile/reset-paper restores balance to ₹5,00,000.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger("quantg.paper_broker")

PAPER_INITIAL_BALANCE = 500_000.0   # ₹5,00,000


class PaperWallet:
    """Manages virtual paper trading capital per user.

    Stored in ``db.paper_wallets`` collection:
        user_id          — unique per user
        balance          — current spendable balance
        initial_balance  — always ₹5,00,000 (reset reference)
        total_debited    — cumulative BUY spend
        total_credited   — cumulative SELL receipts
        realized_pnl     — total_credited - total_debited (net)
        created_at / updated_at
    """

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def get_or_initialize(self, user_id: str) -> Dict[str, Any]:
        """Return wallet, creating one with ₹5,00,000 if absent."""
        wallet = await self.db.paper_wallets.find_one({"user_id": user_id})
        if not wallet:
            wallet = await self._create(user_id)
        return wallet

    async def get_balance(self, user_id: str) -> float:
        wallet = await self.get_or_initialize(user_id)
        return float(wallet.get("balance", PAPER_INITIAL_BALANCE))

    async def debit(self, user_id: str, amount: float, order_id: str) -> bool:
        """Deduct ``amount`` for a BUY order.

        Returns True on success, False if the paper balance is insufficient.
        """
        amount = max(0.0, round(float(amount), 2))
        if amount == 0.0:
            return True

        wallet = await self.get_or_initialize(user_id)
        current = float(wallet.get("balance", 0.0))

        if current < amount:
            logger.warning(
                "Paper wallet insufficient for user=%s: balance=%.2f < required=%.2f order=%s",
                user_id, current, amount, order_id,
            )
            return False

        now = datetime.now(timezone.utc).isoformat()
        await self.db.paper_wallets.update_one(
            {"user_id": user_id},
            {
                "$inc": {"balance": -amount, "total_debited": amount},
                "$set": {"updated_at": now},
            },
        )
        logger.info(
            "Paper wallet DEBIT: user=%s amount=%.2f order=%s new_balance=%.2f",
            user_id, amount, order_id, current - amount,
        )
        return True

    async def credit(self, user_id: str, amount: float, order_id: str) -> None:
        """Credit ``amount`` back to wallet on SELL / exit."""
        amount = max(0.0, round(float(amount), 2))
        if amount == 0.0:
            return

        now = datetime.now(timezone.utc).isoformat()
        await self.db.paper_wallets.update_one(
            {"user_id": user_id},
            {
                "$inc": {"balance": amount, "total_credited": amount},
                "$set": {"updated_at": now},
            },
        )
        new_balance = await self.get_balance(user_id)
        logger.info(
            "Paper wallet CREDIT: user=%s amount=%.2f order=%s new_balance=%.2f",
            user_id, amount, order_id, new_balance,
        )

    async def reset(self, user_id: str) -> Dict[str, Any]:
        """Restore wallet to ₹5,00,000 initial balance (called by reset-paper)."""
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "user_id": user_id,
            "balance": PAPER_INITIAL_BALANCE,
            "initial_balance": PAPER_INITIAL_BALANCE,
            "total_debited": 0.0,
            "total_credited": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        await self.db.paper_wallets.replace_one(
            {"user_id": user_id},
            doc,
            upsert=True,
        )
        logger.info("Paper wallet RESET for user=%s to %.2f", user_id, PAPER_INITIAL_BALANCE)
        return doc

    def summary(self, wallet: Dict[str, Any]) -> Dict[str, Any]:
        """Return a clean summary dict for the API response."""
        balance = float(wallet.get("balance", PAPER_INITIAL_BALANCE))
        initial = float(wallet.get("initial_balance", PAPER_INITIAL_BALANCE))
        debited = float(wallet.get("total_debited", 0.0))
        credited = float(wallet.get("total_credited", 0.0))
        return {
            "balance": round(balance, 2),
            "initial_balance": round(initial, 2),
            "total_invested": round(debited, 2),
            "total_returned": round(credited, 2),
            "net_pnl": round(credited - debited, 2),
            "utilization_pct": round((1 - balance / initial) * 100, 1) if initial > 0 else 0.0,
            "updated_at": wallet.get("updated_at"),
        }

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    async def _create(self, user_id: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "user_id": user_id,
            "balance": PAPER_INITIAL_BALANCE,
            "initial_balance": PAPER_INITIAL_BALANCE,
            "total_debited": 0.0,
            "total_credited": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self.db.paper_wallets.insert_one(doc)
        except Exception:
            # Race condition: another coroutine may have just created it
            existing = await self.db.paper_wallets.find_one({"user_id": user_id})
            if existing:
                existing.pop("_id", None)
                return existing
        doc.pop("_id", None)
        logger.info("Paper wallet CREATED for user=%s with balance=%.2f", user_id, PAPER_INITIAL_BALANCE)
        return doc
