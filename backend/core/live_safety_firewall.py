"""QuantG Live Safety Firewall.

The definitive shield gating production live routes.
Blocks execution unless armed manually, global switches are active, Upstox tokens are validated,
and feed signals are fully reconciled.
"""
from typing import Dict, Any, List
from datetime import datetime, timezone
import logging

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_clock import get_segment_status

logger = logging.getLogger("quantg.live_safety_firewall")

class LiveSafetyFirewall:
    def __init__(self, db):
        self.db = db

    async def verify_readiness(
        self,
        user_id: str,
        strategy_id: str,
        symbol: str
    ) -> Dict[str, Any]:
        """Runs pre-trade pre-submission readiness inspections.
        
        Returns a dict: {"ok": bool, "reasons": List[str]}
        """
        reasons: List[str] = []

        # 1. Arm Switch Inspections
        arm_state = await self.db.live_arm_state.find_one({"user_id": user_id})
        if not arm_state:
            reasons.append("Live trading arm state is completely uninitialized.")
        else:
            if not arm_state.get("global_live_enabled"):
                reasons.append("Global live execution setting is disabled.")
            if not arm_state.get("armed"):
                reasons.append("Broker live execution pathway is not manually armed.")

        # 2. Strategy Inspection
        strategy = await self.db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not strategy:
            reasons.append("Strategy could not be located in database.")
        else:
            if strategy.get("mode") != "live":
                reasons.append("Strategy operational mode is not set to live.")
            if strategy.get("halted") or strategy.get("is_halted"):
                reasons.append("Strategy is actively marked as halted.")

        # 3. Upstox Token and Connection
        user = await self.db.users.find_one({"id": user_id})
        upstox_status = (user or {}).get("upstox_status") or {}
        if not upstox_status.get("token_valid"):
            # Try parsing from standard broker credentials
            broker_keys = await self.db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
            if not broker_keys or not broker_keys.get("access_token"):
                reasons.append("Upstox access token is invalid or expired. Re-authenticate needed.")

        # 4. Reconciliation Health Mismatch
        recon_status = await self.db.risk_state.find_one({
            "$or": [
                {"_id": f"position_reconciliation:{user_id}"},
                {"_id": "position_reconciliation", "$or": [{"user_id": user_id}, {"user_id": {"$exists": False}}]},
            ]
        })
        if recon_status and recon_status.get("mismatch_detected"):
            reasons.append("QuantG position ledger has a mismatch compared to broker book (BROKER_RECONCILIATION_REQUIRED).")

        # 5. Global Kill-Switch
        kill_switch = await self.db.risk_state.find_one({"_id": "global_kill_switch"})
        if kill_switch and kill_switch.get("active"):
            reasons.append("Global trading kill-switch is active.")

        # 6. Domain schedule verification
        try:
            domain = resolve_domain_by_underlying(symbol)
            clock = get_segment_status(domain.name)
            if not clock.get("open"):
                reasons.append(f"Market domain {domain.name.value} schedule is CLOSED.")
        except Exception as err:
            reasons.append(f"Domain resolution error: {err}")

        # 7. Sizing rules (For now: max 1 strategy live and max 1 lot live)
        if len(reasons) == 0:
            active_live_strategies = await self.db.strategies.count_documents({
                "user_id": user_id,
                "mode": "live",
                "status": "live"
            })
            if active_live_strategies > 1:
                reasons.append(f"Maximum limit exceeded: Only 1 strategy is allowed in live execution (Active: {active_live_strategies}).")

            # Check open positions count
            active_live_positions = await self.db.strategy_positions.count_documents({
                "user_id": user_id,
                "mode": "live",
                "status": "OPEN"
            })
            if active_live_positions >= 1:
                reasons.append("Maximum limit exceeded: Only 1 live position can be open concurrently.")

        return {
            "ok": len(reasons) == 0,
            "reasons": reasons
        }
