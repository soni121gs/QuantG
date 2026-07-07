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

from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("quantg.paper_broker")

PAPER_INITIAL_BALANCE = 500_000.0   # ₹5,00,000


def _parse_ts(s: Any) -> Optional[datetime]:
    """Parse an ISO timestamp string (handles both '...Z' and '...+00:00')."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


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

        # Idempotency: the same exit order must never credit the wallet twice.
        # A unique index on paper_wallet_credits.order_id makes this atomic.
        if order_id:
            try:
                await self.db.paper_wallet_credits.insert_one({
                    "order_id": order_id,
                    "user_id": user_id,
                    "amount": amount,
                    "credited_at": datetime.now(timezone.utc).isoformat(),
                })
            except DuplicateKeyError:
                logger.warning(
                    "Paper wallet: credit for order %s already applied — duplicate skipped",
                    order_id,
                )
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

    async def block_margin(self, user_id: str, amount: float, position_id: str) -> None:
        """Reserve ``amount`` of margin for an open position (live-account behaviour).

        A credit-spread SELL entry receives premium but a real broker BLOCKS the
        spread's max loss as margin — this makes the paper wallet do the same so
        `available` funds read like a live account. Idempotent per position: a
        unique index on paper_margin_blocks.position_id prevents a double-block.
        """
        amount = max(0.0, round(float(amount), 2))
        if amount == 0.0 or not position_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self.db.paper_margin_blocks.insert_one({
                "position_id": position_id, "user_id": user_id,
                "amount": amount, "status": "BLOCKED", "blocked_at": now,
            })
        except DuplicateKeyError:
            return  # margin for this position already blocked — never double-count
        await self.db.paper_wallets.update_one(
            {"user_id": user_id},
            {"$inc": {"blocked_margin": amount}, "$set": {"updated_at": now}},
        )
        logger.info("Paper wallet BLOCK margin: user=%s amount=%.2f pos=%s", user_id, amount, position_id)

    async def release_margin(self, user_id: str, position_id: str) -> None:
        """Release the margin reserved for a position on close. Idempotent.

        The BLOCKED→RELEASED transition is an atomic compare-and-swap, so a second
        call (or a concurrent one) finds nothing to release and is a no-op — the
        same guard pattern the spread close-claim uses.
        """
        if not position_id:
            return
        now = datetime.now(timezone.utc).isoformat()
        rec = await self.db.paper_margin_blocks.find_one_and_update(
            {"position_id": position_id, "status": "BLOCKED"},
            {"$set": {"status": "RELEASED", "released_at": now}},
        )
        if not rec:
            return  # already released or never blocked
        amount = max(0.0, round(float(rec.get("amount") or 0.0), 2))
        if amount == 0.0:
            return
        await self.db.paper_wallets.update_one(
            {"user_id": user_id},
            {"$inc": {"blocked_margin": -amount}, "$set": {"updated_at": now}},
        )
        logger.info("Paper wallet RELEASE margin: user=%s amount=%.2f pos=%s", user_id, amount, position_id)

    async def reset(self, user_id: str) -> Dict[str, Any]:
        """Restore wallet to ₹5,00,000 initial balance (called by reset-paper)."""
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "user_id": user_id,
            "balance": PAPER_INITIAL_BALANCE,
            "initial_balance": PAPER_INITIAL_BALANCE,
            "total_debited": 0.0,
            "total_credited": 0.0,
            "blocked_margin": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        await self.db.paper_wallets.replace_one(
            {"user_id": user_id},
            doc,
            upsert=True,
        )
        # Clear any stale margin reservations so blocked_margin starts clean.
        await self.db.paper_margin_blocks.delete_many({"user_id": user_id})
        logger.info("Paper wallet RESET for user=%s to %.2f", user_id, PAPER_INITIAL_BALANCE)
        return doc

    async def reconcile_if_flat(self, user_id: str, *, auto_heal: bool = True,
                                drift_tol: float = 1.0) -> Dict[str, Any]:
        """Self-healing guard against phantom wallet drift.

        When the book is FLAT (no open positions), the wallet balance MUST equal
        initial_balance + cumulative realized P&L booked since the wallet epoch.
        The position ledger (strategy_positions.realized_pnl) is the single source of
        truth for paper P&L — every entry debit is matched by an exit credit and the
        net of a closed round-trip equals its realized_pnl. So when the book is flat,
        any gap between the balance and (initial + Σrealized) is phantom money: an
        over-credit (the recurring "fake profit" class — duplicate/oversized exit
        credits) or an under-credit. We snap the balance back to the ledger truth and
        log CRITICAL, so phantom drift self-corrects every day instead of silently
        compounding. No-op while the book is open (open positions legitimately hold
        debited capital / received credit). Returns a diagnostic dict.
        """
        wallet = await self.db.paper_wallets.find_one({"user_id": user_id})
        if not wallet:
            return {"ok": False, "reason": "no-wallet"}
        open_n = await self.db.strategy_positions.count_documents({
            "user_id": user_id,
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
        })
        if open_n > 0:
            return {"ok": True, "skipped": "book-not-flat", "open_positions": open_n}

        # Book is flat -> no position may still hold reserved margin. Any leftover
        # blocked_margin is a leak (a release that didn't fire); snap it to 0 and mark
        # stale reservations released so the "available" view can't drift downward.
        blocked = round(float(wallet.get("blocked_margin") or 0.0), 2)
        if abs(blocked) > drift_tol:
            logger.critical(
                "PAPER MARGIN LEAK user=%s blocked_margin=%.2f with FLAT book — releasing to 0",
                user_id, blocked,
            )
            if auto_heal:
                now2 = datetime.now(timezone.utc).isoformat()
                await self.db.paper_wallets.update_one(
                    {"user_id": user_id},
                    {"$set": {"blocked_margin": 0.0, "updated_at": now2}},
                )
                await self.db.paper_margin_blocks.update_many(
                    {"user_id": user_id, "status": "BLOCKED"},
                    {"$set": {"status": "RELEASED", "released_at": now2, "released_reason": "flat-book-heal"}},
                )

        epoch = (wallet.get("epoch_at") or wallet.get("reset_at")
                 or wallet.get("created_at") or "")
        epoch_dt = _parse_ts(epoch)
        epoch_date = str(epoch)[:10]  # coarse pre-filter to bound the scan

        realized = 0.0
        n = 0
        query = {"user_id": user_id, "status": "CLOSED"}
        if epoch_date:
            query["closed_at"] = {"$gte": epoch_date}
        cursor = self.db.strategy_positions.find(
            query, {"_id": 0, "realized_pnl": 1, "pnl": 1, "closed_at": 1},
        )
        async for p in cursor:
            # Precise epoch boundary: exclude anything closed at/before the epoch
            # (e.g. same-day-but-pre-reset closes that the coarse date filter let in).
            cdt = _parse_ts(p.get("closed_at"))
            if epoch_dt and cdt and cdt <= epoch_dt:
                continue
            realized += float(p.get("realized_pnl") or p.get("pnl") or 0.0)
            n += 1

        initial = float(wallet.get("initial_balance", PAPER_INITIAL_BALANCE))
        truth = round(initial + realized, 2)
        balance = round(float(wallet.get("balance", initial)), 2)
        drift = round(balance - truth, 2)

        if abs(drift) <= drift_tol:
            return {"ok": True, "healed": False, "drift": drift,
                    "balance": balance, "truth": truth, "closed_positions": n}

        logger.critical(
            "PAPER WALLET DRIFT user=%s balance=%.2f != ledger_truth=%.2f "
            "(initial %.0f + realized %.2f over %d closed since epoch %s) drift=%.2f — %s",
            user_id, balance, truth, initial, realized, n, epoch, drift,
            "HEALING balance to ledger truth" if auto_heal else "detect-only",
        )
        if auto_heal:
            now = datetime.now(timezone.utc).isoformat()
            await self.db.paper_wallets.update_one(
                {"user_id": user_id},
                {"$set": {"balance": truth, "updated_at": now,
                          "last_reconciled_at": now, "last_drift_corrected": drift}},
            )
        return {"ok": True, "healed": bool(auto_heal), "drift": drift,
                "balance": balance, "truth": truth, "closed_positions": n}

    def summary(self, wallet: Dict[str, Any]) -> Dict[str, Any]:
        """Return a clean summary dict for the API response."""
        balance = float(wallet.get("balance", PAPER_INITIAL_BALANCE))
        initial = float(wallet.get("initial_balance", PAPER_INITIAL_BALANCE))
        debited = float(wallet.get("total_debited", 0.0))
        credited = float(wallet.get("total_credited", 0.0))
        blocked = float(wallet.get("blocked_margin", 0.0))
        return {
            "balance": round(balance, 2),
            "initial_balance": round(initial, 2),
            "total_invested": round(debited, 2),
            "total_returned": round(credited, 2),
            "net_pnl": round(credited - debited, 2),
            # Live-account view: margin reserved for open short/spread positions and
            # the funds actually free to deploy after it.
            "blocked_margin": round(blocked, 2),
            "available_balance": round(balance - blocked, 2),
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
            "blocked_margin": 0.0,
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
