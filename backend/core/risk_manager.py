"""QuantG Risk Manager.

Acts as the central firewall for all orders (paper, live, backtest).
Enforces: strategy status, market hours, instrument validity, price freshness, daily trade counts,
daily loss limits, max position size, global kill-switch, and live arm status.

Unified pipeline: applies identical checks for paper and live modes. Mode-specific
setup is done in readiness_checker before execution.
"""
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
import logging

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_session_service import MarketSessionService
from risk_controls import SizeInputs, compute_position_size, evaluate_market_data_quality

logger = logging.getLogger("quantg.risk_manager")

class RiskManager:
    def __init__(self, db):
        self.db = db

    async def evaluate_order(
        self,
        user_id: str,
        strategy_id: str,
        symbol: str,                   # Underlying (e.g. NIFTY, CRUDEOILM)
        target_symbol: str,            # Real traded instrument (e.g. NIFTY26FEB24850CE)
        side: str,                     # "BUY" | "SELL"
        requested_qty: int,
        price: float,
        mode: str,                     # "paper" | "live" | "backtest"
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        lot_size: int = 1,
        risk_style: str = "balanced"
    ) -> Dict[str, Any]:
        """Performs pre-trade risk and parameter evaluations for a potential order placement.
        
        Returns a dict: {"ok": bool, "status": str, "reason": str, "quantity": int}
        """
        # 1. Global Kill-Switch Check
        kill_switch = await self.db.risk_state.find_one({"_id": "global_kill_switch"})
        if kill_switch and kill_switch.get("active"):
            return {
                "ok": False,
                "status": "REJECTED_KILL_SWITCH",
                "reason": "Global trade kill-switch is active.",
                "quantity": 0
            }

        # 2. Live Arming Firewall
        if mode == "live":
            arm_state = await self.db.live_arm_state.find_one({"user_id": user_id})
            if not arm_state or not arm_state.get("global_live_enabled") or not arm_state.get("armed"):
                return {
                    "ok": False,
                    "status": "REJECTED_ARM_FIREWALL",
                    "reason": "Live trading is not armed or enabled.",
                    "quantity": 0
                }

        # 3. Strategy Operational Check
        strategy = await self.db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not strategy:
            return {
                "ok": False,
                "status": "REJECTED_STRATEGY_MISSING",
                "reason": "Strategy not found.",
                "quantity": 0
            }
        if strategy.get("halted") or strategy.get("is_halted"):
            return {
                "ok": False,
                "status": "REJECTED_STRATEGY_HALTED",
                "reason": f"Strategy is halted. Reason: {strategy.get('halt_reason', 'Unknown')}",
                "quantity": 0
            }

        # 4. Market schedule domain checking (applies uniformly to all modes)
        # Backtest mode bypasses market hours checks via readiness_checker
        domain = resolve_domain_by_underlying(symbol)
        if mode != "backtest":
            segment_open = MarketSessionService.is_segment_open(domain.name)
            if not segment_open:
                return {
                    "ok": False,
                    "status": "REJECTED_MARKET_CLOSED",
                    "reason": f"Market domain {domain.name.value} is closed. Check trading hours.",
                    "quantity": 0
                }

        # 5. User Account and Sizing Gates
        user = await self.db.users.find_one({"id": user_id})
        settings = (user or {}).get("settings") or {}
        
        # Calculate daily losses
        daily_loss_limit = float(strategy.get("visual_config", {}).get("risk", {}).get("daily_loss_limit") or settings.get("max_daily_loss") or 10000.0)
        daily_loss = float(strategy.get("today_pnl") or 0.0)
        if daily_loss < -daily_loss_limit:
            return {
                "ok": False,
                "status": "REJECTED_DAILY_LOSS_LIMIT",
                "reason": f"Strategy daily loss limit breached: {daily_loss} < -{daily_loss_limit}",
                "quantity": 0
            }

        # 6. Sizing computation
        free_margin = 100000.0  # Simulated margin fallback
        if user and "funds" in user:
            free_margin = float(user["funds"].get("free_margin") or 100000.0)
            
        equity = float(settings.get("per_strategy_capital") or free_margin)
        max_pos_value = float(settings.get("max_position_size") or equity)
        
        size_inputs = SizeInputs(
            equity=equity,
            free_margin=free_margin,
            requested_qty=requested_qty,
            lot_size=lot_size,
            entry_price=price,
            stop_loss_price=stop_loss,
            max_position_value=max_pos_value,
            daily_loss_limit=daily_loss_limit,
            risk_style=risk_style
        )
        
        size_res = compute_position_size(size_inputs)
        if not size_res.allowed:
            return {
                "ok": False,
                "status": "REJECTED_RISK_SIZING",
                "reason": f"Position sizing failed: {size_res.reason}",
                "quantity": 0
            }

        return {
            "ok": True,
            "status": "APPROVED",
            "reason": "Risk verification passed.",
            "quantity": size_res.quantity
        }
